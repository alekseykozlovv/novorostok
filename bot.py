import os
import asyncio
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MINI_APP_URL = os.getenv("MINI_APP_URL")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        f"Нова поможет разобраться: кто ты, что тебе интересно и куда двигаться.\n"
        f"Займёт 5 минут. Без правильных ответов. Без давления.",
        reply_markup=keyboard
    )

async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    print("✅ Бот НовоРосток запущен!")
    async with app:
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())