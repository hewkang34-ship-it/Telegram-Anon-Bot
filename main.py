import logging
import os
import asyncio
from typing import Optional
import json
from datetime import datetime, timedelta

from telegram import (
    Update, Message,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ChatMemberHandler, ContextTypes, filters
)

# Redis (async)
from redis.asyncio import Redis

# ========= ENV =========
TOKEN = os.environ.get("TELEGRAM_TOKEN")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0"))  # id канала для проверки подписки (-100xxxxxxxxxx)

# ========= Redis helpers / Keys =========
redis: Optional[Redis] = None

QUEUE_KEY = "q:global"                 # очередь ожидающих
PAIR_KEY  = "pair:{uid}"               # ключ пары для юзера
STAT_MATCH = "stat:matches"
STAT_MSG   = "stat:messages"
PROFILE_KEY = "profile:{uid}"          # JSON: {"gender":"M/F", "age_group":"18-24/25-34/35-44/45+", "vip_until": 0}

# ===== Helpers для получения ID чата/канала =====
async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    text = f"Тип: {chat.type}\nID: {chat.id}"
    if getattr(chat, "username", None):
        text += f"\n@{chat.username}"
    await update.effective_message.reply_text(text)

async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    logging.info("CHANNEL_POST -> id=%s title=%s", chat.id, getattr(chat, "title", None))

async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    logging.info("MY_CHAT_MEMBER -> id=%s title=%s", chat.id, getattr(chat, "title", None))

# ===== Профили =====
async def load_profile(uid: int) -> dict:
    raw = await redis.get(PROFILE_KEY.format(uid=uid))
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}

async def save_profile(uid: int, data: dict):
    await redis.set(PROFILE_KEY.format(uid=uid), json.dumps(data), ex=30*24*3600)

def is_profile_complete(p: dict) -> bool:
    return bool(p.get("gender") and p.get("age_group"))

async def get_peer(uid: int) -> Optional[int]:
    pid = await redis.get(PAIR_KEY.format(uid=uid))
    return int(pid) if pid else None

async def set_pair(a: int, b: int):
    pipe = redis.pipeline()
    pipe.set(PAIR_KEY.format(uid=a), b, ex=24 * 3600)
    pipe.set(PAIR_KEY.format(uid=b), a, ex=24 * 3600)
    pipe.incr(STAT_MATCH)
    await pipe.execute()

# ===== Стартовый экран (inline-кнопки) =====
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

# ===== Команды/handlers верхнего уровня =====
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

# ===== VIP =====
def _now() -> int:
    return int(datetime.utcnow().timestamp())

async def is_vip(uid: int) -> bool:
    p = await load_profile(uid)
    return int(p.get("vip_until", 0)) > _now()

async def grant_vip(uid: int, hours: int):
    p = await load_profile(uid)
    p["vip_until"] = _now() + hours * 3600
    await save_profile(uid, p)

async def ensure_vip_or_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True — VIP уже есть. Иначе показываем оффер и возвращаем False."""
    uid = update.effective_user.id
    if await is_vip(uid):
        return True

    btns = [
        [InlineKeyboardButton("📣 Подписаться на канал", url=f"https://t.me/{context.bot.username}")],  # TODO: замени URL на свой канал
        [InlineKeyboardButton("✅ Я подписался — проверить", callback_data="vip:check")],
        [
            InlineKeyboardButton("💳 0.99$ / 1 мес.", callback_data="vip:buy:1m"),
            InlineKeyboardButton("💳 7.99$ / 3 мес.", callback_data="vip:buy:3m"),
            InlineKeyboardButton("💳 4.99$ / 7 мес.", callback_data="vip:buy:7m"),
        ],
    ]
    await update.effective_chat.send_message(
        "🔒 Поиск по полу доступен только VIP-пользователям.\n"
        "Получить VIP на 3 часа — за подписку на канал и проверку.\n"
        "Или купи подписку:",
        reply_markup=InlineKeyboardMarkup(btns),
    )
    return False

async def vip_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data == "vip:check":
        if CHANNEL_ID == 0:
            await q.edit_message_text("Не настроен CHANNEL_ID. Укажи переменную окружения CHANNEL_ID.")
            return
        try:
            member = await context.bot.get_chat_member(CHANNEL_ID, uid)
            if member.status in ("creator", "administrator", "member", "restricted"):
                await grant_vip(uid, 3)  # 3 часа
                await q.edit_message_text("✅ Подписка подтверждена! VIP на 3 часа активирован.")
            else:
                await q.edit_message_text("Похоже, ты не подписан. Подпишись и нажми «Проверить».")
        except Exception:
            await q.edit_message_text("Не удалось проверить подписку. Убедись, что бот админ в канале.")
        return

    if data.startswith("vip:buy:"):
        await q.edit_message_text("💳 Оплата скоро будет подключена. Пока доступна только бесплатная VIP за подписку на канал.")
        return

# ===== Поиск =====
async def clear_pair(uid: int):
    peer = await get_peer(uid)
    pipe = redis.pipeline()
    pipe.delete(PAIR_KEY.format(uid=uid))
    if peer:
        pipe.delete(PAIR_KEY.format(uid=peer))
    await pipe.execute()
    return peer

async def enqueue(uid: int):
    if await get_peer(uid):
        return False
    try:
        await redis.lrem(QUEUE_KEY, 0, str(uid))
    except Exception:
        pass
    await redis.rpush(QUEUE_KEY, uid)
    return True

async def try_match(uid: int) -> Optional[int]:
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

async def find_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE, by_button: bool = False):
    uid = update.effective_user.id

    peer = await get_peer(uid)
    if peer:
        await update.effective_chat.send_message("Ты уже в диалоге. Напиши /next для нового собеседника или /stop чтобы завершить.")
        return

    await enqueue(uid)
    await update.effective_chat.send_message("🔍 Ищу собеседника…")

    peer = await try_match(uid)
    if peer:
        await update.effective_chat.send_message("✅ Собеседник найден. Можешь писать!")
        await ctx.bot.send_message(peer, "✅ Собеседник найден. Можешь писать!")

async def next_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    old_peer = await clear_pair(uid)
    if old_peer:
        await ctx.bot.send_message(old_peer, "🔁 Собеседник вышел. Ищу тебе нового…")
        await enqueue(old_peer)
        new_for_old = await try_match(old_peer)
        if new_for_old:
            await ctx.bot.send_message(old_peer, "✅ Новый собеседник найден. Пиши!")
            await ctx.bot.send_message(new_for_old, "✅ Собеседник найден. Пиши!")

    await enqueue(uid)
    await update.effective_chat.send_message("🔁 Ищу нового собеседника…")
    new_peer = await try_match(uid)
    if new_peer:
        await update.effective_chat.send_message("✅ Новый собеседник найден. Пиши!")
        await ctx.bot.send_message(new_peer, "✅ Собеседник найден. Пиши!")

async def stop_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    peer = await clear_pair(uid)
    await update.effective_chat.send_message("⛔ Диалог завершён. Нажми /find, чтобы искать заново.")
    if peer:
        await ctx.bot.send_message(peer, "⛔ Собеседник завершил диалог. Нажми /find, чтобы искать заново.")

# Кнопки “Поиск Ж/М” (если решишь вернуть ReplyKeyboard меню)
async def search_female(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_vip_or_offer(update, context):
        return
    await find_cmd(update, context)

async def search_male(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_vip_or_offer(update, context):
        return
    await find_cmd(update, context)

# Пересылка всех типов сообщений анонимно собеседнику
async def relay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
        await update.effective_chat.send_message("⚠️ Отправка контактов отключена ради анонимности.")
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
    await update.effective_chat.send_message(f"📈 Матчей: {int(m or 0)}\n✉️ Сообщений: {int(s or 0)}")

# ===== MAIN =====
def main():
    if not TOKEN:
        raise SystemExit("Нет TELEGRAM_TOKEN в переменных окружения")

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

    # Обработка VIP-кнопок
    app.add_handler(CallbackQueryHandler(vip_cb, pattern="^vip:"))

    # Обработка выбора пола (если будет ReplyKeyboard меню)
    app.add_handler(MessageHandler(filters.Regex(r"^🙋‍♀️ Поиск Ж$"), search_female))
    app.add_handler(MessageHandler(filters.Regex(r"^🙋‍♂️ Поиск М$"), search_male))

    # Общий callback для inline-кнопок
    app.add_handler(CallbackQueryHandler(cb_query))

    # Пересылка любых пользовательских сообщений
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, relay))

    # ===== Наши хендлеры для получения ID =====
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, on_channel_post))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # Запуск — БЕЗ await и БЕЗ asyncio.run
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()