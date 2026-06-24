import os
import sys
import json
import asyncio
import sqlite3
import re
import requests
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from openai import OpenAI
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import Forbidden

# ─── ПРОВЕРКА ПЕРЕМЕННЫХ ───────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
MINI_APP_URL = os.getenv("MINI_APP_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not BOT_TOKEN:
    print("ОШИБКА: BOT_TOKEN не установлен")
    sys.exit(1)

if not OPENAI_API_KEY:
    print("ОШИБКА: OPENAI_API_KEY не установлен")
    sys.exit(1)

if not MINI_APP_URL:
    print("ОШИБКА: MINI_APP_URL не установлен")
    sys.exit(1)

openai_client = OpenAI(api_key=OPENAI_API_KEY)

# ─── АНАЛИТИКА → GOOGLE SHEETS ────────────────────────────
SHEETS_WEBHOOK = "https://script.google.com/macros/s/AKfycbxutqYubF1d5bJ10awlMNSrQzkqQHa97uP2RXTduj1-ptTUHiLyLFKPovxu8Z7PdwoMRQ/exec"

def log_event(user_id: int, username: str, event: str, details: str = ""):
    try:
        payload = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "user_id": str(user_id),
            "username": username or "",
            "event": event,
            "details": details
        }
        requests.post(SHEETS_WEBHOOK, json=payload, timeout=5)
        print(f"📊 Лог: {event} | {user_id} | {username}")
    except Exception as e:
        print(f"⚠️ Ошибка логирования (не критично): {e}")

# ─── БАЗА ДАННЫХ ───────────────────────────────────────────
def init_db():
    conn = sqlite3.connect("novorostok.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            age TEXT,
            answers TEXT,
            report TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def save_session(user_id, name, age, answers, report):
    conn = sqlite3.connect("novorostok.db")
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO sessions (user_id, name, age, answers, report)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, name, age,
          json.dumps(answers, ensure_ascii=False),
          json.dumps(report, ensure_ascii=False)))
    conn.commit()
    conn.close()

def get_session(user_id):
    conn = sqlite3.connect("novorostok.db")
    c = conn.cursor()
    c.execute("SELECT name, report FROM sessions WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row

# ─── БЕЗОПАСНЫЙ ПАРСИНГ JSON ───────────────────────────────
def safe_parse_json(text: str) -> dict:
    text = re.sub(r'```json\s*|\s*```', '', text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    print(f"⚠️ Не удалось распарсить JSON. Текст: {text[:200]}")
    return {
        "profile_title": "Уникальная личность",
        "riasec_code": "—",
        "riasec_explanation": "Анализ временно недоступен. Попробуй ещё раз.",
        "soft_skills_top3": ["Любознательность", "Настойчивость", "Креативность"],
        "directions": [
            {"icon": "🌱", "name": "Анализ недоступен", "match": "Попробуй пройти интервью снова", "match_pct": 0}
        ],
        "steps": ["Попробуй /start снова — иногда AI нужно чуть больше времени 🌱"],
        "nova_message": "Ты молодец, что дошёл до конца! Попробуй ещё раз — я готова.",
        "parent_summary": "Анализ временно недоступен. Попробуйте снова."
    }

# ─── ЭКРАНИРОВАНИЕ HTML ────────────────────────────────────
def esc(text: str) -> str:
    """Экранирует спецсимволы HTML для безопасной вставки в parse_mode=HTML"""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))

# ─── ПРОМПТ ────────────────────────────────────────────────
NOVA_SYSTEM_PROMPT = """
Ты — Нова, AI-наставник подростков проекта НовоРосток.
Речь: тёплая, уважительная, как умный старший друг. Без пафоса и без давления.

КРИТИЧЕСКИ ВАЖНО:
- Во всех полях кроме parent_summary обращайся ТОЛЬКО на "ты" и во 2-м лице единственного числа.
  НЕЛЬЗЯ: "Алексей любит", "он интересуется", "у него есть"
  НУЖНО: "ты любишь", "ты интересуешься", "у тебя есть"
- Каждое предложение заканчивается точкой.
- В поле riasec_explanation: РОВНО 3 отдельных предложения. Каждое предложение — отдельная строка. Разделяй их символом | (вертикальная черта). Пример: "Ты исследуешь мир через вопросы и логику.|Тебе важно понять как всё устроено.|Ты умеешь находить творческие решения там где другие останавливаются."
- В поле parent_summary: обращайся к родителю на "Вы". Разделяй части символом | следующим образом: описание сильных сторон|Рекомендуем: 1) первое действие|2) второе действие
- В поле nova_message: РОВНО 2 предложения. Разделяй символом |.
- В поле steps: ровно 5 пунктов. Каждый пункт начинается с временного маркера: "На этой неделе:", "В этом месяце:", "Через 3 месяца:", "Через 6 месяцев:", "Через год:".

МЕТОДОЛОГИЯ АНАЛИЗА:

1. RIASEC-код (2-3 буквы из R,I,A,S,E,C):
R = Realistic — любит делать руками, чинить, строить
I = Investigative — исследует, задаёт вопросы "почему", любит науку
A = Artistic — творчество, создание, нестандартное мышление
S = Social — помогает людям, любит общение, командная работа
E = Enterprising — лидер, организатор, предприниматель
C = Conventional — порядок, системы, чёткие правила, точность

2. Soft Skills (топ-3 из ответов):
Коммуникация, Эмпатия, Самоорганизация, Обучаемость, Креативность,
Лидерство, Командная работа, Стрессоустойчивость, Аналитика, Саморефлексия

3. Подбор профессий:
Формула: 0.4 × RIASEC + 0.4 × SoftSkills + 0.2 × потенциал
Учитывай возраст подростка при составлении плана.

ФОРМАТ — строго JSON, без markdown, без пояснений вне JSON:
{
  "profile_title": "Технарь-Исследователь",
  "riasec_code": "IRC",
  "riasec_explanation": "Ты исследуешь мир через вопросы и логику.|Тебе важно понять как всё устроено.|Ты умеешь находить творческие решения там где другие останавливаются.",
  "soft_skills_top3": ["Аналитика", "Обучаемость", "Саморефлексия"],
  "directions": [
    {"icon": "💻", "name": "IT и разработка", "match": "Ты хорошо справляешься с задачами где нужно думать и создавать.", "match_pct": 87},
    {"icon": "🔬", "name": "Наука и инженерия", "match": "Твоя любовь к вопросу почему отлично подходит для исследований.", "match_pct": 74},
    {"icon": "🎨", "name": "Дизайн и творчество", "match": "Ты умеешь видеть нестандартные решения там где другие не замечают.", "match_pct": 61}
  ],
  "steps": [
    "На этой неделе: одно конкретное действие которое ты можешь сделать сегодня.",
    "В этом месяце: конкретный курс или ресурс для старта.",
    "Через 3 месяца: мини-проект который ты можешь сделать.",
    "Через 6 месяцев: измеримая цель.",
    "Через год: результат который ты увидишь."
  ],
  "nova_message": "Первое личное предложение на ты.|Второе предложение — тёплое и честное.",
  "parent_summary": "Ваш ребёнок обладает сильными сторонами.|Рекомендуем: 1) конкретное действие для вас как родителя.|2) конкретный ресурс или активность."
}
"""

def generate_report_sync(name: str, age: str, answers: dict) -> dict:
    answers_text = f"Имя: {name}\nВозраст: {age} лет\n\n"
    for key, val in answers.items():
        answers_text += f"{key}: {val}\n"

    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": NOVA_SYSTEM_PROMPT},
            {"role": "user", "content": answers_text}
        ],
        response_format={"type": "json_object"},
        max_tokens=1200
    )
    raw = response.choices[0].message.content
    return safe_parse_json(raw)

# ─── ФОРМАТИРОВАНИЕ — HTML (надёжнее MarkdownV2) ──────────

def format_teen_report(report: dict, name: str) -> str:
    """Отчёт для подростка. parse_mode=HTML"""
    skills = report.get("soft_skills_top3", [])
    directions = report.get("directions", [])
    steps = report.get("steps", [])

    # Заголовок
    lines = [
        f"🌱 <b>{esc(name)} — твой профиль готов</b>",
        "",
        f"<b>Ты — {esc(report.get('profile_title', ''))}</b>",
        f"🔑 Твой тип: <b>{esc(report.get('riasec_code', ''))}</b>",
    ]

    # Описание личности — разбиваем по разделителю |
    explanation = report.get("riasec_explanation", "")
    for sentence in explanation.split("|"):
        s = sentence.strip()
        if s:
            lines.append(esc(s))
    lines.append("")

    # Сильные стороны
    if skills:
        lines.append("💪 <b>Твои сильные стороны:</b>")
        for skill in skills:
            lines.append(f"• {esc(skill)}")
        lines.append("")

    # Направления
    if directions:
        lines.append("🎯 <b>Направления для тебя:</b>")
        for item in directions:
            lines.append(
                f"{esc(item.get('icon',''))} <b>{esc(item.get('name',''))}</b> — {esc(str(item.get('match_pct', 0)))}%"
            )
            match_text = item.get("match", "").strip()
            if match_text:
                lines.append(f"<i>{esc(match_text)}</i>")
        lines.append("")

    # План
    if steps:
        lines.append("🗺 <b>Твой план:</b>")
        for step in steps:
            lines.append(f"• {esc(step)}")
        lines.append("")

    # Личное послание Новы — разбиваем по |
    nova_msg = report.get("nova_message", "")
    if nova_msg:
        parts = [s.strip() for s in nova_msg.split("|") if s.strip()]
        if parts:
            lines.append(f"💬 {esc(parts[0])}")
            for part in parts[1:]:
                lines.append(esc(part))

    return "\n".join(lines)


def format_parent_report(report: dict, name: str) -> str:
    """Отчёт для родителей. parse_mode=HTML"""
    skills = report.get("soft_skills_top3", [])
    directions = report.get("directions", [])

    riasec_map = {
        "R": "Практик", "I": "Исследователь", "A": "Творец",
        "S": "Помощник", "E": "Лидер", "C": "Организатор"
    }
    code = report.get("riasec_code", "")
    decoded = ", ".join([f"{c} — {riasec_map.get(c, c)}" for c in code])

    lines = [
        f"👨‍👩‍👧 <b>Отчёт для родителей — {esc(name)}</b>",
        "",
        f"📋 <b>Профиль:</b> {esc(report.get('profile_title', ''))}",
        "",
        f"🔑 <b>Тип личности:</b> {esc(code)}",
        f"<i>({esc(decoded)})</i>",
        "",
    ]

    # Описание — разбиваем по |
    explanation = report.get("riasec_explanation", "")
    for sentence in explanation.split("|"):
        s = sentence.strip()
        if s:
            lines.append(esc(s))
    lines.append("")

    # Сильные стороны
    if skills:
        lines.append("💪 <b>Сильные стороны:</b>")
        for skill in skills:
            lines.append(f"• {esc(skill)}")
        lines.append("")

    # Направления (топ-2)
    if directions:
        lines.append("🎯 <b>Подходящие направления:</b>")
        for item in directions[:2]:
            lines.append(
                f"• {esc(item.get('name', ''))} — {esc(str(item.get('match_pct', '')))}%"
            )
        lines.append("")

    # Резюме для родителя — разбиваем по |
    parent_summary = report.get("parent_summary", "")
    if parent_summary:
        parts = [p.strip() for p in parent_summary.split("|") if p.strip()]
        if parts:
            lines.append("👀 <b>О вашем ребёнке:</b>")
            lines.append(esc(parts[0]))
            lines.append("")
            if len(parts) > 1:
                lines.append("✅ <b>Рекомендуем:</b>")
                for part in parts[1:]:
                    # Убираем дубль "Рекомендуем:" если GPT его вставил
                    clean = re.sub(r'^Рекомендуем:\s*', '', part.strip())
                    lines.append(esc(clean))
            lines.append("")

    # Финал
    lines += [
        "📌 Оставьте отзыв на сайте:",
        "novorostok.ru",
        "И получите расширенный отчёт первым 🎁",
    ]

    return "\n".join(lines)


# ─── ПОЛУЧЕНИЕ ДАННЫХ ИЗ WEBAPP ────────────────────────────
async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data_str = update.effective_message.web_app_data.data
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name or str(user_id)
    print(f"📥 Данные от {user_id}: {data_str[:80]}...")

    try:
        payload = json.loads(data_str)
        name = payload.get("name", "друг")
        age = payload.get("age", "")
        answers = payload.get("answers", {})

        answered_count = len([v for v in answers.values() if v])
        total_questions = len(answers)

        await update.message.reply_text("⏳ Нова анализирует ответы... ~20 секунд 🌱")

        report = await asyncio.to_thread(generate_report_sync, name, age, answers)
        save_session(user_id, name, age, answers, report)

        await asyncio.to_thread(
            log_event, user_id, username,
            "completed",
            f"Имя: {name}, Возраст: {age}, Вопросов: {answered_count}/{total_questions}, Профиль: {report.get('profile_title','')}"
        )

        teen_msg = format_teen_report(report, name)
        await update.message.reply_text(teen_msg, parse_mode="HTML")

        await update.message.reply_text(
            "💬 Это базовый профиль — бесплатно и навсегда твой.\n\n"
            "🎁 Хочешь расширенный отчёт с планом на 5 лет, картой навыков и разделом для родителей?\n"
            "Оставь отзыв на сайте 👉 novorostok.ru — и я пришлю его тебе первым!\n\n"
            "📩 Версия для родителей: /report"
        )
        print(f"✅ Отчёт отправлен {user_id}")

    except Forbidden:
        print(f"⚠️ Пользователь {user_id} заблокировал бота")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await asyncio.to_thread(
            log_event, user_id, username,
            "dropped",
            f"Ошибка: {str(e)[:100]}"
        )
        await update.message.reply_text(
            "Нова думает чуть дольше обычного 🌱\n"
            "Попробуй /start снова через минуту."
        )

# ─── КОМАНДЫ ───────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.first_name or "друг"
    username = user.username or user.first_name or str(user.id)

    await asyncio.to_thread(
        log_event, user.id, username,
        "started",
        f"Имя в TG: {name}"
    )

    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton(
            text="🌱 Начать с Новой",
            web_app=WebAppInfo(url=MINI_APP_URL)
        )]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await update.message.reply_text(
        f"Привет, {name}! 👋\n\n"
        f"Я НовоРосток — здесь тебя ждёт Нова, твой личный AI-наставник.\n\n"
        f"Займёт 10 минут. Без правильных ответов. Без давления.",
        reply_markup=keyboard
    )

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        row = get_session(update.effective_user.id)
        if row:
            name, report_json = row
            report = json.loads(report_json)
            msg = format_parent_report(report, name)
            await update.message.reply_text(msg, parse_mode="HTML")
        else:
            await update.message.reply_text("Сначала пройди интервью: /start 🌱")
    except Forbidden:
        print(f"⚠️ Пользователь заблокировал бота")
    except Exception as e:
        print(f"❌ Ошибка в /report: {e}")
        await update.message.reply_text("Попробуй /start снова 🌱")

# ─── ЗАПУСК ────────────────────────────────────────────────
async def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))

    print("✅ Бот НовоРосток запущен!")
    print(f"🔗 Mini App URL: {MINI_APP_URL}")
    print(f"🤖 OpenAI key: {'OK' if OPENAI_API_KEY else 'НЕТ КЛЮЧА!'}")

    async with app:
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
