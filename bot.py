import os
import json
import asyncio
import sqlite3
import anthropic
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MINI_APP_URL = os.getenv("MINI_APP_URL")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ─── БАЗА ДАННЫХ ───────────────────────────────────────────
def init_db():
    conn = sqlite3.connect("novorostok.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            answers TEXT,
            report TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def save_session(user_id, name, answers, report):
    conn = sqlite3.connect("novorostok.db")
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO sessions (user_id, name, answers, report)
        VALUES (?, ?, ?, ?)
    """, (user_id, name,
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

# ─── ПРОМПТ ────────────────────────────────────────────────
NOVA_SYSTEM_PROMPT = """
Ты — Нова, AI-наставник подростков проекта НовоРосток.
Речь: тёплая, уважительная, как умный старший друг. Без пафоса.

Определи RIASEC-код (2-3 буквы):
R=руками/практик, I=исследователь, A=творчество, S=помощь людям, E=лидер, C=порядок

Формула подбора: 0.4 x RIASEC + 0.4 x SoftSkills + 0.2 x потенциал

ФОРМАТ — строго JSON, без markdown, без лишнего текста:
{
  "profile_title": "Технарь-Исследователь",
  "riasec_code": "IRC",
  "riasec_explanation": "2 предложения простым языком",
  "directions": [
    {"icon": "💻", "name": "IT и разработка", "match": "почему подходит", "match_pct": 87},
    {"icon": "🔬", "name": "Наука и инженерия", "match": "почему подходит", "match_pct": 74},
    {"icon": "🎨", "name": "Дизайн и творчество", "match": "почему подходит", "match_pct": 61}
  ],
  "steps": [
    "На этой неделе: конкретное действие",
    "В этом месяце: конкретный курс",
    "Через 3 месяца: мини-проект",
    "Через 6 месяцев: цель",
    "Через год: результат"
  ],
  "nova_message": "Личное послание подростку, 2-3 предложения.",
  "parent_summary": "Для родителя: сильные стороны и 2 рекомендации."
}
"""

def generate_report_sync(name: str, answers: dict) -> dict:
    answers_text = f"Имя: {name}\n\n"
    for key, val in answers.items():
        answers_text += f"{key}: {val}\n"

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        system=NOVA_SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": answers_text}
        ]
    )
    text = response.content[0].text
    clean = text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)

# ─── ФОРМАТИРОВАНИЕ ────────────────────────────────────────
def format_teen_report(report: dict, name: str) -> str:
    d = report.get("directions", [])
    steps = report.get("steps", [])
    msg = f"🌱 *{name} — твой профиль готов!*\n\n"
    msg += f"*Ты — {report.get('profile_title', '')}*\n"
    msg += f"Код: {report.get('riasec_code', '')} — {report.get('riasec_explanation', '')}\n\n"
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
    msg = f"👨‍👩‍👧 *Отчёт для родителей — {name}*\n\n"
    msg += f"*Профиль:* {report.get('profile_title', '')}\n"
    msg += f"*RIASEC:* {report.get('riasec_code', '')} — {report.get('riasec_explanation', '')}\n\n"
    msg += "*Направления:*\n"
    for item in d[:2]:
        msg += f"• {item.get('name', '')} ({item.get('match_pct', '')}%)\n"
    parent = report.get("parent_summary", "")
    if parent:
        msg += f"\n{parent}"
    msg += "\n\n📌 Полный отчёт — на сайте НовоРосток."
    return msg

# ─── ДЕБАГ: ловим ВСЕ входящие ────────────────────────────
async def debug_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"🔍 ВХОДЯЩЕЕ СООБЩЕНИЕ: {update.message}")
    if update.message and update.message.web_app_data:
        print(f"✅ WEB_APP_DATA: {update.message.web_app_data.data}")

# ─── ПОЛУЧЕНИЕ ДАННЫХ ИЗ WEBAPP ────────────────────────────
async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data_str = update.effective_message.web_app_data.data
    user_id = update.effective_user.id
    print(f"📥 Данные от {user_id}: {data_str[:80]}...")

    try:
        payload = json.loads(data_str)
        name = payload.get("name", "друг")
        answers = payload.get("answers", {})

        await update.message.reply_text("⏳ Нова анализирует ответы... ~20 секунд 🌱")

        report = await asyncio.to_thread(generate_report_sync, name, answers)
        save_session(user_id, name, answers, report)

        teen_msg = format_teen_report(report, name)
        await update.message.reply_text(teen_msg, parse_mode="Markdown")
        await update.message.reply_text("📩 Напиши /report — пришлю версию для родителей.")

    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await update.message.reply_text("Что-то пошло не так. Попробуй /start снова.")

# ─── КОМАНДЫ ───────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.first_name or "друг"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            text="🌱 Начать с Новой",
            web_app=WebAppInfo(url=MINI_APP_URL)
        )
    ]])
    await update.message.reply_text(
        f"Привет, {name}! 👋\n\n"
        f"Я НовоРосток — здесь тебя ждёт Нова, твой личный AI-наставник.\n\n"
        f"Займёт 5 минут. Без правильных ответов. Без давления.",
        reply_markup=keyboard
    )

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = get_session(update.effective_user.id)
    if row:
        name, report_json = row
        report = json.loads(report_json)
        msg = format_parent_report(report, name)
        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text("Сначала пройди интервью: /start 🌱")

# ─── ЗАПУСК ────────────────────────────────────────────────
async def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers — порядок важен
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    app.add_handler(MessageHandler(filters.ALL, debug_all))  # дебаг — ловим всё

    print("✅ Бот НовоРосток запущен!")
    print(f"🔗 Mini App URL: {MINI_APP_URL}")
    print(f"🤖 Anthropic key: {'OK' if ANTHROPIC_API_KEY else 'НЕТ КЛЮЧА!'}")

    async with app:
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
