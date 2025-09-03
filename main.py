import os
import asyncio
from collections import deque
from typing import Dict, Optional

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)

TOKEN = os.getenv("TELEGRAM_TOKEN")

# Очередь ожидания и активные пары
waiting_queue = deque()            # deque[chat_id]
pairs: Dict[int, int] = {}         # chat_id -> partner_chat_id

# ---------- Вспомогалки ----------

async def safe_send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str):
    try:
        await context.bot.sendMessage(chat_id, text, parse_mode=ParseMode.HTML)
    except Exception:
        pass

def partner_of(chat_id: int) -> Optional[int]:
    return pairs.get(chat_id)

def unlink(chat_id: int):
    partner = pairs.pop(chat_id, None)
    if partner is not None:
        pairs.pop(partner, None)

async def match_if_possible(context: ContextTypes.DEFAULT_TYPE):
    # Собираем по двое из очереди
    while len(waiting_queue) >= 2:
        a = waiting_queue.popleft()
        b = waiting_queue.popleft()
        pairs[a] = b
        pairs[b] = a
        await safe_send(context, a,
                        "🎯 <b>Собеседник найден</b>\n\n"
                        "Общие интересы: <i>Флирт</i>\n\n"
                        "<b>/next</b> — искать нового\n"
                        "<b>/stop</b> — закончить диалог\n")
        await safe_send(context, b,
                        "🎯 <b>Собеседник найден</b>\n\n"
                        "Общие интересы: <i>Флирт</i>\n\n"
                        "<b>/next</b> — искать нового\n"
                        "<b>/stop</b> — закончить диалог\n")

# ---------- Команды ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # Красивое приветствие (как «экран»)
    welcome = (
        "🔒 <b>Анонимный чат в Telegram</b>\n"
        "Общайся анонимно со случайными собеседниками.\n\n"
        "🔎 <i>Ищем собеседника с общими интересами…</i>"
    )
    await update.message.reply_text(
        welcome,
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove()
    )

    # Если уже в паре — разрываем
    if partner_of(chat_id):
        unlink(chat_id)

    # Если уже в очереди — не дублируем
    if chat_id not in waiting_queue:
        waiting_queue.append(chat_id)

    await match_if_possible(context)

async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    partner = partner_of(chat_id)

    if partner:
        # Уведомляем обоих и разрываем
        await safe_send(context, partner, "⚠️ Собеседник вышел и ищет нового (/next).")
        unlink(chat_id)

    await update.message.reply_text("🔎 Ищем нового собеседника…")
    if chat_id not in waiting_queue:
        waiting_queue.append(chat_id)
    await match_if_possible(context)

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    partner = partner_of(chat_id)
    if partner:
        await safe_send(context, partner, "⛔ Диалог завершён собеседником.")
        unlink(chat_id)
    await update.message.reply_text("✔️ Диалог завершён. Напиши /start чтобы начать заново.")

async def interests_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔧 Изменение интересов скоро добавим. Пока используем дефолтные 😉"
    )

# ---------- Роутинг обычных сообщений ----------

async def relay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    partner = partner_of(chat_id)
    if not partner:
        # Не в паре — подскажем команду
        await update.message.reply_text("Пока ты не в диалоге. Напиши /start чтобы найти собеседника.")
        return

    # Пересылаем текст собеседнику
    if update.message.text:
        await safe_send(context, partner, update.message.text)

    # Пересылаем стикеры/голоса/фото по-простому (опционально)
    if update.message.sticker:
        await context.bot.send_sticker(partner, update.message.sticker.file_id)
    if update.message.voice:
        await context.bot.send_voice(partner, update.message.voice.file_id)
    if update.message.photo:
        await context.bot.send_photo(partner, update.message.photo[-1].file_id)
    if update.message.animation:
        await context.bot.send_animation(partner, update.message.animation.file_id)

# ---------- Запуск ----------

async def on_startup(app):
    print("Bot started")

def main():
    if not TOKEN:
        raise SystemExit("Нет TELEGRAM_TOKEN в переменных окружения!")

    application = ApplicationBuilder().token(TOKEN).post_init(on_startup).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("next", next_cmd))
    application.add_handler(CommandHandler("stop", stop_cmd))
    application.add_handler(CommandHandler("interests", interests_cmd))

    # Все прочие сообщения перекидываем собеседнику
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, relay))

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()