import os
import asyncio
from typing import Optional

from telegram import (
    Update, Message,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# Redis (async)
from redis.asyncio import Redis

# ========= ENV =========
TOKEN = os.environ.get("TELEGRAM_TOKEN")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

# ========= Redis helpers / Keys =========
redis: Optional[Redis] = None

QUEUE_KEY = "q:global"                 # очередь ожидающих
PAIR_KEY  = "pair:{uid}"               # ключ пары для юзера
STAT_MATCH = "stat:matches"
STAT_MSG   = "stat:messages"


async def get_peer(uid: int) -> Optional[int]:
    pid = await redis.get(PAIR_KEY.format(uid=uid))
    return int(pid) if pid else None


async def set_pair(a: int, b: int):
    pipe = redis.pipeline()
    pipe.set(PAIR_KEY.format(uid=a), b, ex=24 * 3600)
    pipe.set(PAIR_KEY.format(uid=b), a, ex=24 * 3600)
    pipe.incr(STAT_MATCH)
    await pipe.execute()


async def clear_pair(uid: int):
    peer = await get_peer(uid)
    pipe = redis.pipeline()
    pipe.delete(PAIR_KEY.format(uid=uid))
    if peer:
        pipe.delete(PAIR_KEY.format(uid=peer))
    await pipe.execute()
    return peer


async def enqueue(uid: int):
    # если уже в паре — не ставим
    if await get_peer(uid):
        return False
    # удалим возможные дубли
    try:
        await redis.lrem(QUEUE_KEY, 0, str(uid))
    except Exception:
        pass
    await redis.rpush(QUEUE_KEY, uid)
    return True


async def try_match(uid: int) -> Optional[int]:
    # ищем другого человека из очереди
    while True:
        other = await redis.lpop(QUEUE_KEY)
        if other is None:
            return None
        other = int(other)
        if other == uid:
            continue
        if not await get_peer(other):
            await set_pair(uid, other)
            return other


# ========= UI =========
WELCOME_CARD = (
    "<b>Анонимный чат в Telegram</b>\n"
    "Общайся анонимно со случайными собеседниками.\n\n"
    "🔍 Нажми <code>/find</code> чтобы начать поиск.\n"
    "➡️ <code>/next</code> — новый собеседник\n"
    "⛔ <code>/stop</code> — закончить диалог\n"
)

async def send_start_card(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Начать поиск", callback_data="find")],
        [
            InlineKeyboardButton("➡️ Следующий", callback_data="next"),
            InlineKeyboardButton("⛔ Стоп", callback_data="stop"),
        ],
    ])
    await update.effective_chat.send_message(
        WELCOME_CARD, parse_mode=ParseMode.HTML, reply_markup=kb
    )


# ========= Handlers =========
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_start_card(update, ctx)


async def cb_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    if q.data == "find":
        await find_cmd(update, ctx, by_button=True)
    elif q.data == "next":
        await next_cmd(update, ctx)
    elif q.data == "stop":
        await stop_cmd(update, ctx)


async def find_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE, by_button: bool = False):
    uid = update.effective_user.id

    # уже в диалоге?
    peer = await get_peer(uid)
    if peer:
        await update.effective_chat.send_message(
            "Ты уже в диалоге. Напиши /next для нового собеседника или /stop чтобы завершить."
        )
        return

    await enqueue(uid)
    await update.effective_chat.send_message("🔍 Ищу собеседника…")

    # пробуем моментально сматчить
    peer = await try_match(uid)
    if peer:
        await update.effective_chat.send_message("✅ Собеседник найден. Можешь писать!")
        await ctx.bot.send_message(peer, "✅ Собеседник найден. Можешь писать!")


async def next_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # освобождаем текущего собеседника, если есть
    old_peer = await clear_pair(uid)
    if old_peer:
        await ctx.bot.send_message(old_peer, "🔁 Собеседник вышел. Ищу тебе нового…")
        await enqueue(old_peer)
        new_for_old = await try_match(old_peer)
        if new_for_old:
            await ctx.bot.send_message(old_peer, "✅ Новый собеседник найден. Пиши!")
            await ctx.bot.send_message(new_for_old, "✅ Собеседник найден. Пиши!")

    # ищем нового для текущего пользователя
    await enqueue(uid)
    await update.effective_chat.send_message("🔁 Ищу нового собеседника…")
    new_peer = await try_match(uid)
    if new_peer:
        await update.effective_chat.send_message("✅ Новый собеседник найден. Пиши!")
        await ctx.bot.send_message(new_peer, "✅ Собеседник найден. Пиши!")


async def stop_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    peer = await clear_pair(uid)
    await update.effective_chat.send_message(
        "⛔ Диалог завершён. Нажми /find, чтобы искать заново."
    )
    if peer:
        await ctx.bot.send_message(
            peer, "⛔ Собеседник завершил диалог. Нажми /find, чтобы искать заново."
        )


async def relay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Пересылка всех типов сообщений анонимно собеседнику."""
    uid = update.effective_user.id
    peer = await get_peer(uid)
    if not peer:
        return

    msg: Message = update.effective_message
    await redis.incr(STAT_MSG)

    if msg.text:
        await ctx.bot.send_message(peer, msg.text)
    elif msg.photo:
        await ctx.bot.send_photo(peer, msg.photo[-1].file_id, caption=msg.caption or None)
    elif msg.video:
        await ctx.bot.send_video(peer, msg.video.file_id, caption=msg.caption or None)
    elif msg.voice:
        await ctx.bot.send_voice(peer, msg.voice.file_id, caption=msg.caption or None)
    elif msg.audio:
        await ctx.bot.send_audio(peer, msg.audio.file_id, caption=msg.caption or None)
    elif msg.document:
        await ctx.bot.send_document(peer, msg.document.file_id, caption=msg.caption or None)
    elif msg.sticker:
        await ctx.bot.send_sticker(peer, msg.sticker.file_id)
    elif msg.animation:
        await ctx.bot.send_animation(peer, msg.animation.file_id, caption=msg.caption or None)
    elif msg.video_note:
        await ctx.bot.send_video_note(peer, msg.video_note.file_id)
    elif msg.location:
        await ctx.bot.send_location(peer, msg.location.latitude, msg.location.longitude)
    elif msg.contact:
        await update.effective_chat.send_message(
            "⚠️ Отправка контактов отключена ради анонимности."
        )
    else:
        await update.effective_chat.send_message("Тип сообщения пока не поддержан.")


async def rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📜 Правила:\n"
        "— Не раскрывай персональные данные\n"
        "— Запрещён спам, оскорбления и нелегальный контент\n"
        "— Для нового собеседника: /next, чтобы завершить: /stop"
    )
    await update.effective_chat.send_message(text)


async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m, s = await redis.get(STAT_MATCH), await redis.get(STAT_MSG)
    await update.effective_chat.send_message(
        f"📈 Матчей: {int(m or 0)}\n✉️ Сообщений: {int(s or 0)}"
    )


# ========= Main =========
async def main():
    if not TOKEN:
        raise SystemExit("Нет TELEGRAM_TOKEN в переменных окружения!")
    global redis
    redis = Redis.from_url(REDIS_URL, decode_responses=True)

    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("find", find_cmd))
    app.add_handler(CommandHandler("next", next_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("stats", stats))

    # Кнопки
    app.add_handler(CallbackQueryHandler(cb_query))

    # Пересылка любых пользовательских сообщений
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, relay))

    # Запуск (PTB v20)
    await app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    asyncio.run(main())