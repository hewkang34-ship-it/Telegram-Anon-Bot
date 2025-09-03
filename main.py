import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.getenv("TELEGRAM_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я запущен ✅")

async def find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Поиск собеседника… (заглушка)")

async def next_partner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Следующий собеседник… (заглушка)")

async def stop_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Чат остановлен. Напиши /start, чтобы начать заново.")

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Правила: будь вежлив, не спамь, уважай собеседника.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Статистика (заглушка): 0 пользователей онлайн.")

def main():
    if not TOKEN:
        raise SystemExit("Нет TELEGRAM_TOKEN в переменных окружения!")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("find", find))
    app.add_handler(CommandHandler("next", next_partner))
    app.add_handler(CommandHandler("stop", stop_chat))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("stats", stats))

    app.run_polling()

if __name__ == "__main__":
    main()