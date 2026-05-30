import os
import sys
import json
import asyncio
import sqlite3
import re

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
    """Парсит JSON от GPT даже если он обрёзан или в markdown"""
    # Убираем markdown блоки
    text = re.sub(r'```json\s*|\s*```', '', text).strip()
    
    # Пробуем прямой парсинг
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    
    # Ищем JSON внутри текста
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    
    # Возвращаем заглушку — бот не упадёт
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

# ─── ПРОМПТ ────────────────────────────────────────────────
NOVA_SYSTEM_PROMPT = """
Ты — Нова, AI-наставник подростков проекта НовоРосток.
Речь: тёплая, уважительная, как умный старший друг. Без пафоса и без давления.

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
  "riasec_explanation": "2 предложения простым языком для подростка",
  "soft_skills_top3": ["Аналитика", "Обучаемость", "Саморефлексия"],
  "directions": [
    {"icon": "💻", "name": "IT и разработка", "match": "почему подходит — 1 предложение", "match_pct": 87},
    {"icon": "🔬", "name": "Наука и инженерия", "match": "почему подходит", "match_pct": 74},
    {"icon": "🎨", "name": "Дизайн и творчество", "match": "почему подходит", "match_pct": 61}
  ],
  "steps": [
    "На этой неделе: конкретное действие",
    "В этом месяце: конкретный курс или ресурс",
    "Через 3 месяца: мини-проект",
    "Через 6 месяцев: измеримая цель",
    "Через год: результат"
  ],
  "nova_message": "Личное послание подростку, 2-3 предложения. Честно и тепло.",
  "parent_summary": "Для родителя: сильные стороны ребёнка и 2 конкретные рекомендации."
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

# ─── ФОРМАТИРОВАНИЕ ────────────────────────────────────────
def format_teen_report(report: dict, name: str) -> str:
    d = report.get("directions", [])
    steps = report.get("steps", [])
    skills = report.get("soft_skills_top3", [])

    msg = f"🌱 *{name} — твой профиль готов\!*\n\n"
    msg += f"*Ты — {report.get('profile_title', '')}*\n"
    msg += f"Код: {report.get('riasec_code', '')} — {report.get('riasec_explanation', '')}\n\n"

    if skills:
        msg += f"💪 *Твои сильные стороны:* {', '.join(skills)}\n\n"

    msg += "🎯 *Направления для тебя:*\n"
    for item in d:
        msg += f"{item.get('icon', '')} *{item.get('name', '')}* — {item.get('match_pct', '')}%\n"
        msg += f"{item.get('match', '')}\n"

    msg += "\n🗺 *Твой план:*\n"
    for step in steps:
        msg += f"• {step}\n"

    nova_msg = report.get("nova_message", "")
    if nova_msg:
        msg += f"\n💬 {nova_msg}"

    return msg

def format_parent_report(report: dict, name: str) -> str:
    d = report.get("directions", [])
    skills = report.get("soft_skills_top3", [])
    msg = f"👨‍👩‍👧 *Отчёт для родителей — {name}*\n\n"
    msg += f"*Профиль:* {report.get('profile_title', '')}\n"
    msg += f"*RIASEC:* {report.get('riasec_code', '')} — {report.get('riasec_explanation', '')}\n\n"
    if skills:
        msg += f"*Сильные стороны:* {', '.join(skills)}\n\n"
    msg += "*Направления:*\n"
    for item in d[:2]:
        msg += f"• {item.get('name', '')} ({item.get('match_pct', '')}%)\n"
    parent = report.get("parent_summary", "")
    if parent:
        msg += f"\n{parent}"
    msg += "\n\n📌 Полный отчёт — на сайте НовоРосток\."
    return msg

# ─── ПОЛУЧЕНИЕ ДАННЫХ ИЗ WEBAPP ────────────────────────────
async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data_str = update.effective_message.web_app_data.data
    user_id = update.effective_user.id
    print(f"📥 Данные от {user_id}: {data_str[:80]}...")

    try:
        payload = json.loads(data_str)
        name = payload.get("name", "друг")
        age = payload.get("age", "")
        answers = payload.get("answers", {})

        await update.message.reply_text("⏳ Нова анализирует ответы... ~20 секунд 🌱")

        report = await asyncio.to_thread(generate_report_sync, name, age, answers)
        save_session(user_id, name, age, answers, report)

        teen_msg = format_teen_report(report, name)
        
        try:
            await update.message.reply_text(teen_msg, parse_mode="MarkdownV2")
        except Exception as fmt_err:
            print(f"⚠️ Ошибка форматирования, отправляю без markdown: {fmt_err}")
            # Убираем markdown и отправляем чистый текст
            clean_msg = re.sub(r'[*_`\[\]()~>#+=|{}.!\\]', '', teen_msg)
            await update.message.reply_text(clean_msg)

        await update.message.reply_text("📩 Напиши /report — пришлю версию для родителей.")
        print(f"✅ Отчёт отправлен {user_id}")

    except Forbidden:
        print(f"⚠️ Пользователь {user_id} заблокировал бота — пропускаем")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await update.message.reply_text(
            "Нова думает чуть дольше обычного 🌱\n"
            "Попробуй /start снова через минуту."
        )

# ─── КОМАНДЫ ───────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.first_name or "друг"
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
            try:
                await update.message.reply_text(msg, parse_mode="MarkdownV2")
            except Exception:
                clean_msg = re.sub(r'[*_`\[\]()~>#+=|{}.!\\]', '', msg)
                await update.message.reply_text(clean_msg)
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
