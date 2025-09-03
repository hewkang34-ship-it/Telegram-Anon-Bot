import os
import json
import logging
from typing import Optional

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

import redis.asyncio as redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("anonchat")

TOKEN = os.environ.get("TELEGRAM_TOKEN")
REDIS_URL = os.environ.get("REDIS_URL")  # например: redis://default:<pass>@<host>:<port>/0

# ===== Redis клиент =====
r: Optional[redis.Redis] = None

PROFILE_KEY = "profile:{uid}"   # JSON
PAIR_KEY    = "pair:{uid}"      # int
QUEUE_KEY   = "q:global"        # list

# ===== UI =====
def gender_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("М 🙋🏻‍♂️", callback_data="gender:M"),
        InlineKeyboardButton("Ж 🙋🏻‍♀️", callback_data="gender:F"),
    ]])

def age_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("12–20", callback_data="age:12-20"),
        InlineKeyboardButton("21–30", callback_data="age:21-30"),
        InlineKeyboardButton("31–40", callback_data="age:31-40"),
    ]])

def profile_str(p: dict) -> str:
    g_map = {"M": "Мужской", "F": "Женский"}
    g = g_map.get(p.get("gender"), "—")
    a = p.get("age_range") or "—"
    return f"Пол: {g}\nВозраст: {a}"

def menu_text(prefix: str = "") -> str:
    return (
        (prefix + "\n" if prefix else "")
        + "Теперь можно приступить к поиску собеседника!\n\n"
        "/search — поиск собеседника\n"
        "/next — следующий собеседник\n"
        "/stop — остановить диалог"
    )

# ===== Профили =====
async def get_profile(uid: int) -> dict:
    raw = await r.get(PROFILE_KEY.format(uid=uid))
    if not raw:
        return {"gender": None, "age_range": None}
    try:
        return json.loads(raw)
    except Exception:
        return {"gender": None, "age_range": None}

async def save_profile(uid: int, data: dict):
    await r.set(PROFILE_KEY.format(uid=uid), json.dumps(data))

async def reset_profile(uid: int):
    await r.delete(PROFILE_KEY.format(uid=uid))

# ===== Пары =====
async def get_peer(uid: int) -> Optional[int]:
    val = await r.get(PAIR_KEY.format(uid=uid))
    return int(val) if val else None

async def set_pair(a: int, b: int):
    pipe = r.pipeline()
    pipe.set(PAIR_KEY.format(uid=a), b)
    pipe.set(PAIR_KEY.format(uid=b), a)
    await pipe.execute()

async def clear_pair(uid: int):
    peer = await get_peer(uid)
    pipe = r.pipeline()
    pipe.delete(PAIR_KEY.format(uid=uid))
    if peer:
        pipe.delete(PAIR_KEY.format(uid=peer))
    await pipe.execute()

# ===== Очередь =====
async def push_queue(uid: int):
    # не добавляем дубликаты
    members = await r.lrange(QUEUE_KEY, 0, -1)
    if str(uid) not in members:
        await r.rpush(QUEUE_KEY, uid)

async def pop_queue() -> Optional[int]:
    val = await r.lpop(QUEUE_KEY)
    return int(val) if val else None

async def remove_from_queue(uid: int):
    await r.lrem(QUEUE_KEY, 0, uid)

# ===== Анкета =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = await get_profile(uid)
    if not p["gender"]:
        await update.message.reply_text("Привет! Выбери свой пол:", reply_markup=gender_kb())
        return
    if not p["age_range"]:
        await update.message.reply_text("Укажи возраст:", reply_markup=age_kb())
        return
    await update.message.reply_text("Профиль уже заполнен ✅\n" + profile_str(p))
    await update.message.reply_text(menu_text("Отлично!"))

async def on_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    _, gender = q.data.split(":", 1)
    p = await get_profile(uid)
    p["gender"] = gender
    await save_profile(uid, p)
    await q.edit_message_text("Пол сохранён ✅\nТеперь выбери возраст:", reply_markup=age_kb())

async def on_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    _, age_range = q.data.split(":", 1)
    p = await get_profile(uid)
    p["age_range"] = age_range
    await save_profile(uid, p)
    await q.edit_message_text("Возраст сохранён ✅\n\nТвой профиль:\n" + profile_str(p))
    await q.message.reply_text(menu_text("Отлично!"))

# ===== Команды =====
async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = await get_profile(uid)
    await update.message.reply_text("Твой профиль:\n" + profile_str(p))

async def cmd_reset_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await reset_profile(uid)
    await clear_pair(uid)
    await remove_from_queue(uid)
    await update.message.reply_text("Профиль очищен. Напиши /start.")

# матчинг
async def try_match(uid: int) -> Optional[int]:
    while True:
        other = await pop_queue()
        if other is None:
            return None
        if other == uid:
            await push_queue(other)
            return None
        # уже занят? ищем следующего
        if await get_peer(other):
            continue
        return other

async def announce_pair(context: ContextTypes.DEFAULT_TYPE, a: int, b: int):
    text = "Собеседник найден 👤\n\n/next — искать нового собеседника\n/stop — закончить диалог"
    await context.bot.send_message(chat_id=a, text=text)
    await context.bot.send_message(chat_id=b, text=text)

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = await get_profile(uid)
    if not (p.get("gender") and p.get("age_range")):
        await update.message.reply_text("Сначала заполни профиль: /start")
        return

    if await get_peer(uid):
        await update.message.reply_text("Ты уже в диалоге.\n/next — новый собеседник\n/stop — закончить диалог")
        return

    peer = await try_match(uid)
    if peer:
        await set_pair(uid, peer)
        await announce_pair(context, uid, peer)
    else:
        await push_queue(uid)
        await update.message.reply_text("Ищу собеседника… ⏳\n/stop — отменить поиск")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    peer = await get_peer(uid)
    await remove_from_queue(uid)
    if peer:
        await clear_pair(uid)
        try:
            await context.bot.send_message(chat_id=peer, text="Собеседник завершил диалог. /search — найти нового")
        except Exception as e:
            log.error(f"notify peer fail: {e}")
        await update.message.reply_text("Диалог завершён. /search — найти нового")
    else:
        await update.message.reply_text("Поиск остановлен.")

async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_stop(update, context)
    await cmd_search(update, context)

# ===== Релеинг сообщений =====
async def relay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    uid = update.effective_user.id
    peer = await get_peer(uid)
    if not peer:
        return
    try:
        await msg.copy(chat_id=peer)
    except Exception as e:
        log.error(f"relay error: {e}")

# ===== post_init и запуск =====
async def post_init(app: Application):
    # гасим вебхук и инициализируем Redis
    await app.bot.delete_webhook(drop_pending_updates=True)

    if not REDIS_URL:
        raise RuntimeError("REDIS_URL is not set (persistent storage required).")

    global r
    r = await redis.from_url(REDIS_URL, decode_responses=True)
    await r.ping()
    log.info("Redis connected OK")

def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is not set")

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("reset_profile", cmd_reset_profile))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("next", cmd_next))

    # анкета
    app.add_handler(CallbackQueryHandler(on_gender, pattern=r"^gender:(M|F)$"))
    app.add_handler(CallbackQueryHandler(on_age, pattern=r"^age:(12-20|21-30|31-40)$"))

    # релеим всё, кроме команд
    app.add_handler(MessageHandler(~filters.COMMAND, relay))

    log.info("Bot starting (run_polling)…")
    app.run_polling()

if __name__ == "__main__":
    main()