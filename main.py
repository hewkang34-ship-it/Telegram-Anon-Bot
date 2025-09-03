# main.py
import os
import asyncio
import time
from typing import Optional

from telegram import (
    Update, Message,
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup,
    ReplyKeyboardRemove
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, CallbackQueryHandler,
    filters
)

# Redis (async)
from redis.asyncio import Redis

# ====== ENV ======
TOKEN = os.environ.get("TELEGRAM_TOKEN")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
CHANNEL_ID = os.environ.get("CHANNEL_ID")  # для VIP-проверки подписки

# ====== Redis keys ======
redis: Optional[Redis] = None

K_Q_GLOBAL = "q:global"                      # общая очередь
K_Q_GIRLS  = "q:gender:f"
K_Q_BOYS   = "q:gender:m"

K_PAIR  = "pair:{uid}"                       # текущая пара
K_USER  = "user:{uid}"                       # профиль пользователя (hash)

K_STAT_MATCH = "stat:matches"
K_STAT_MSG   = "stat:messages"

# ====== онбординг ======
AGE_GROUPS = ["12-20", "21-30", "31-40"]
GENDERS = {"m": "Мужской", "f": "Женский"}

# ====== утилиты Redis ======
async def get_peer(uid: int) -> Optional[int]:
    pid = await redis.get(K_PAIR.format(uid=uid))
    return int(pid) if pid else None

async def set_pair(a: int, b: int):
    pipe = redis.pipeline()
    pipe.set(K_PAIR.format(uid=a), b, ex=24 * 3600)
    pipe.set(K_PAIR.format(uid=b), a, ex=24 * 3600)
    pipe.incr(K_STAT_MATCH)
    await pipe.execute()

async def clear_pair(uid: int):
    peer = await get_peer(uid)
    pipe = redis.pipeline()
    pipe.delete(K_PAIR.format(uid=uid))
    if peer:
        pipe.delete(K_PAIR.format(uid=peer))
    await pipe.execute()
    return peer

async def in_any_queue_remove(uid: int):
    # удаляем из всех очередей возможные дубликаты
    try:
        await redis.lrem(K_Q_GLOBAL, 0, str(uid))
        await redis.lrem(K_Q_GIRLS, 0, str(uid))
        await redis.lrem(K_Q_BOYS, 0, str(uid))
    except Exception:
        pass

async def enqueue(uid: int, queue_key: str):
    # не добавляем, если уже в паре
    if await get_peer(uid):
        return False
    await in_any_queue_remove(uid)
    await redis.rpush(queue_key, uid)
    return True

async def try_match_from(queue_key: str, uid: int) -> Optional[int]:
    # пробуем взять того, кто уже ожидает
    while True:
        other = await redis.lpop(queue_key)
        if other is None:
            return None
        other = int(other)
        if other == uid:
            continue
        if not await get_peer(other):
            await set_pair(uid, other)
            return other

# профиль и VIP
async def get_profile(uid: int) -> dict:
    data = await redis.hgetall(K_USER.format(uid=uid))
    # redis возвращает всё как str
    return data or {}

async def set_profile(uid: int, **fields):
    key = K_USER.format(uid=uid)
    await redis.hset(key, mapping={k: str(v) for k, v in fields.items()})

async def is_vip(uid: int) -> bool:
    prof = await get_profile(uid)
    until = float(prof.get("vip_until", "0"))
    return until > time.time()

async def grant_vip_hours(uid: int, hours: float):
    until = time.time() + hours * 3600
    await set_profile(uid, vip_until=until)

# ====== тексты/кнопки ======
WELCOME_CARD = (
    "<b>Анонимный чат</b>\n"
    "Общайся анонимно со случайными собеседниками.\n\n"
    "Пожалуйста, укажи свой пол и возраст — это поможет улучшить подбор."
)

def kb_gender():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👩 Женский", callback_data="gender:f"),
            InlineKeyboardButton("👨 Мужской", callback_data="gender:m"),
        ]
    ])

def kb_age():
    rows = []
    row = []
    for i, g in enumerate(AGE_GROUPS, start=1):
        row.append(InlineKeyboardButton(g, callback_data=f"age:{g}"))
        if i % 3 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def kb_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Поиск любого собеседника", callback_data="menu:find_any")],
        [
            InlineKeyboardButton("👩 Поиск Ж", callback_data="menu:find_f"),
            InlineKeyboardButton("👨 Поиск М", callback_data="menu:find_m"),
        ],
    ])

def kb_vip():
    buttons = [
        [InlineKeyboardButton("✅ Проверить подписку на канал (3 ч VIP)", callback_data="vip:check_sub")],
        [
            InlineKeyboardButton("💳 0,99$ / 1 мес.", callback_data="vip:buy_1"),
            InlineKeyboardButton("💳 4,99$ / 7 мес.", callback_data="vip:buy_7"),
            InlineKeyboardButton("💳 7,99$ / 3 мес.", callback_data="vip:buy_3"),
        ],
    ]
    # если указан канал — добавим URL для удобства
    if CHANNEL_ID and isinstance(CHANNEL_ID, str) and CHANNEL_ID.startswith("@"):
        buttons.insert(0, [InlineKeyboardButton("🔔 Перейти в канал", url=f"https://t.me/{CHANNEL_ID.lstrip('@')}")])
    return InlineKeyboardMarkup(buttons)

# ====== HELPER: показать главное меню или продолжить онбординг ======
async def ensure_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    prof = await get_profile(uid)
    gender = prof.get("gender")
    age = prof.get("age")

    if not gender:
        await update.effective_chat.send_message(
            WELCOME_CARD, parse_mode=ParseMode.HTML, reply_markup=kb_gender()
        )
        return False
    if not age:
        await update.effective_chat.send_message("Выбери возраст:", reply_markup=kb_age())
        return False

    await update.effective_chat.send_message(
        "Готово! Выбери режим поиска:", reply_markup=kb_main_menu()
    )
    return True

# ====== HANDLERS ======
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ensure_profile(update, ctx)

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    uid = q.from_user.id
    await q.answer()

    data = q.data or ""

    # Онбординг: пол
    if data.startswith("gender:"):
        g = data.split(":", 1)[1]
        if g not in ("m", "f"):
            return
        await set_profile(uid, gender=g)
        await q.edit_message_text(f"Пол: {GENDERS[g]}")
        await q.message.chat.send_message("Теперь выбери возраст:", reply_markup=kb_age())
        return

    # Онбординг: возраст
    if data.startswith("age:"):
        a = data.split(":", 1)[1]
        if a not in AGE_GROUPS:
            return
        await set_profile(uid, age=a)
        await q.edit_message_text(f"Возраст: {a}")
        await q.message.chat.send_message("Отлично! Выбери режим поиска:", reply_markup=kb_main_menu())
        return

    # Главное меню
    if data == "menu:find_any":
        await find_any(update, ctx)
        return

    if data in ("menu:find_f", "menu:find_m"):
        # Поиск по полу только для VIP
        if not await is_vip(uid):
            await q.message.chat.send_message(
                "Поиск по полу доступен только VIP-пользователям.\n"
                "• Подпишись на канал и получи VIP на 3 часа\n"
                "• Или оформи платную подписку",
                reply_markup=kb_vip()
            )
            return
        # VIP есть → ставим в соответствующую очередь
        target = "f" if data.endswith("_f") else "m"
        await find_by_gender(update, ctx, target)
        return

    # VIP: проверка подписки
    if data == "vip:check_sub":
        if not CHANNEL_ID:
            await q.message.chat.send_message("Канал для проверки не настроен (CHANNEL_ID). Обратись к админу.")
            return
        try:
            member = await ctx.bot.get_chat_member(CHANNEL_ID, uid)
            if member.status in ("member", "administrator", "creator"):
                await grant_vip_hours(uid, 3)
                await q.message.chat.send_message("✅ Подписка найдена. VIP активирован на 3 часа!")
                await q.message.chat.send_message("Вернуться к меню:", reply_markup=kb_main_menu())
            else:
                await q.message.chat.send_message("❌ Я не вижу подписку на канал.")
        except Exception:
            await q.message.chat.send_message("Не удалось проверить подписку. Убедись, что канал публичный и бот — админ.")
        return

    # VIP: опции оплаты — здесь заглушки
    if data.startswith("vip:buy_"):
        plan = data.split("_", 1)[1]
        await q.message.chat.send_message(
            "Оплата пока не подключена.\n"
            "Могу активировать временный VIP (демо) на 3 часа.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Активировать демо VIP 3 часа", callback_data="vip:demo")]
            ])
        )
        return

    if data == "vip:demo":
        await grant_vip_hours(uid, 3)
        await q.message.chat.send_message("🎁 Демо-VIP активирован на 3 часа.")
        await q.message.chat.send_message("Вернуться к меню:", reply_markup=kb_main_menu())
        return

# Команды (опционально — дублируют меню)
async def find_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await find_any(update, ctx)

async def next_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    old_peer = await clear_pair(uid)
    if old_peer:
        await ctx.bot.send_message(old_peer, "🔁 Собеседник вышел. Ищу нового…")
        await enqueue(old_peer, K_Q_GLOBAL)
        # пробуем сматчить освободившегося
        new_peer_for_old = await try_match_from(K_Q_GLOBAL, old_peer)
        if new_peer_for_old:
            await ctx.bot.send_message(old_peer, "✅ Новый собеседник найден. Пиши!")
            await ctx.bot.send_message(new_peer_for_old, "✅ Собеседник найден. Пиши!")

    await enqueue(uid, K_Q_GLOBAL)
    await update.effective_chat.send_message("🔁 Ищу нового собеседника…")
    new_peer = await try_match_from(K_Q_GLOBAL, uid)
    if new_peer:
        await update.effective_chat.send_message("✅ Новый собеседник найден. Пиши!")
        await ctx.bot.send_message(new_peer, "✅ Собеседник найден. Пиши!")

async def stop_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    peer = await clear_pair(uid)
    await update.effective_chat.send_message("⛔ Диалог завершён. Нажми /find, чтобы искать заново.", reply_markup=kb_main_menu())
    if peer:
        await ctx.bot.send_message(peer, "⛔ Собеседник завершил диалог. Нажми /find, чтобы искать заново.")

async def rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📜 Правила:\n"
        "— Не раскрывай персональные данные\n"
        "— Запрещён спам, оскорбления и нелегальный контент\n"
        "— /next — новый собеседник, /stop — завершить диалог"
    )
    await update.effective_chat.send_message(text)

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = await redis.get(K_STAT_MATCH)
    s = await redis.get(K_STAT_MSG)
    await update.effective_chat.send_message(
        f"📈 Матчей: {int(m or 0)}\n✉️ Сообщений: {int(s or 0)}"
    )

# Реальный поиск
async def find_any(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    prof = await get_profile(uid)
    if not (prof.get("gender") and prof.get("age")):
        await ensure_profile(update, ctx)
        return

    if await get_peer(uid):
        await update.effective_chat.send_message("Ты уже в диалоге. /next — новый, /stop — завершить.")
        return

    await enqueue(uid, K_Q_GLOBAL)
    await update.effective_chat.send_message("🔍 Ищу собеседника…")
    peer = await try_match_from(K_Q_GLOBAL, uid)
    if peer:
        await update.effective_chat.send_message("✅ Собеседник найден. Пиши!")
        await ctx.bot.send_message(peer, "✅ Собеседник найден. Пиши!")

async def find_by_gender(update: Update, ctx: ContextTypes.DEFAULT_TYPE, target_gender: str):
    uid = update.effective_user.id
    my_prof = await get_profile(uid)
    if not (my_prof.get("gender") and my_prof.get("age")):
        await ensure_profile(update, ctx)
        return

    if await get_peer(uid):
        await update.effective_chat.send_message("Ты уже в диалоге. /next — новый, /stop — завершить.")
        return

    queue_key = K_Q_GIRLS if target_gender == "f" else K_Q_BOYS
    await enqueue(uid, queue_key)
    await update.effective_chat.send_message(
        f"🔍 Ищу собеседника по полу: { 'Ж' if target_gender=='f' else 'М' }…"
    )

    # мгновенный матч из нужной очереди
    peer = await try_match_from(queue_key, uid)
    if peer:
        await update.effective_chat.send_message("✅ Собеседник найден. Пиши!")
        await ctx.bot.send_message(peer, "✅ Собеседник найден. Пиши!")

# Пересылка сообщений
async def relay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    peer = await get_peer(uid)
    if not peer:
        return
    msg: Message = update.effective_message
    await redis.incr(K_STAT_MSG)

    if msg.text:
        await ctx.bot.send_message(peer, msg.text)
    elif msg.photo:
        await ctx.bot.send_photo(peer, photo=msg.photo[-1].file_id, caption=msg.caption or None)
    elif msg.video:
        await ctx.bot.send_video(peer, video=msg.video.file_id, caption=msg.caption or None)
    elif msg.voice:
        await ctx.bot.send_voice(peer, voice=msg.voice.file_id, caption=msg.caption or None)
    elif msg.audio:
        await ctx.bot.send_audio(peer, audio=msg.audio.file_id, caption=msg.caption or None)
    elif msg.document:
        await ctx.bot.send_document(peer, document=msg.document.file_id, caption=msg.caption or None)
    elif msg.sticker:
        await ctx.bot.send_sticker(peer, msg.sticker.file_id)
    elif msg.animation:
        await ctx.bot.send_animation(peer, animation=msg.animation.file_id, caption=msg.caption or None)
    elif msg.video_note:
        await ctx.bot.send_video_note(peer, video_note=msg.video_note.file_id)
    elif msg.location:
        await ctx.bot.send_location(peer, latitude=msg.location.latitude, longitude=msg.location.longitude)
    else:
        await update.effective_chat.send_message("Этот тип сообщения пока не поддержан.")

# ====== MAIN ======
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
    app.add_handler(CallbackQueryHandler(cb))

    # Пересылка любых пользовательских сообщений
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, # Запуск (PTB v20)
    await app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    asyncio.run(main())  