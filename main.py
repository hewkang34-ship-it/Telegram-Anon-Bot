import os
from telegram.ext import Updater, CommandHandler

TOKEN = os.getenv("TELEGRAM_TOKEN")

def start(update, context):
    update.message.reply_text("Привет! Я запущен ✅")

def find(update, context):
    update.message.reply_text("Поиск собеседника… (заглушка)")

def next_partner(update, context):
    update.message.reply_text("Следующий собеседник… (заглушка)")

def stop_chat(update, context):
    update.message.reply_text("Чат остановлен. Напиши /start, чтобы начать снова.")

def rules(update, context):
    update.message.reply_text("Правила: будь вежлив, без спама и нарушений закона.")

def stats(update, context):
    update.message.reply_text("Статистика (заглушка): онлайн 0, в поиске 0.")

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Нет TELEGRAM_TOKEN в переменных окружения")

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("find", find))
    dp.add_handler(CommandHandler("next", next_partner))
    dp.add_handler(CommandHandler("stop", stop_chat))
    dp.add_handler(CommandHandler("rules", rules))
    dp.add_handler(CommandHandler("stats", stats))

    updater.start_polling()
    updater.idle()
