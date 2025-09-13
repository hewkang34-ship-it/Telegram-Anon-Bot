import os
import json
import logging
from typing import Optional

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters, PreCheckoutQueryHandler
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

# ====== VIP (Telegram Stars) ======
VIP_KEY     = "vip_until:{uid}"  # int (unix time)
CURRENCY_XTR = "XTR"             # Telegram Stars
PROVIDER_TOKEN = ""              # для Stars — пустая строка

VIP_PLANS = {
    "3m":  {"months": 3,  "amount": 1299, "title": "VIP • 3 months"},
    "6m":  {"months": 6,  "amount": 2399, "title": "VIP • 6 months"},
    "12m": {"months": 12, "amount": 4499, "title": "VIP • 12 months"},
}

def vip_k(uid: int) -> str:
    return VIP_KEY.format(uid=uid)

async def get_vip_until(uid: int) -> int:
    val = await r.get(vip_k(uid))
    return int(val) if val else 0

async def is_vip(uid: int) -> bool:
    from time import time as now
    return (await get_vip_until(uid)) > int(now())

async def extend_vip(uid: int, months: int) -> int:
    """Продлеваем VIP: добавляем ~30 дней за месяц."""
    from time import time as now
    now_i = int(now())
    current = await get_vip_until(uid)
    base = max(now_i, current)
    new_until = base + int(30 * 24 * 3600 * months)
    await r.set(vip_k(uid), new_until)
    return new_until

def vip_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ 3 months — 1299 Stars", callback_data="vip_buy:3m")],
        [InlineKeyboardButton("⭐ 6 months — 2399 Stars", callback_data="vip_buy:6m")],
        [InlineKeyboardButton("⭐ 12 months — 4499 Stars", callback_data="vip_buy:12m")],
    ])

async def cmd_vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    until = await get_vip_until(uid)
    from time import time as now
    active = until > int(now())
    left = max(0, until - int(now()))
    days = left // 86400
    hours = (left % 86400) // 3600
    status = "✅ Active" if active else "❌ Not active"
    txt = (
        f"<b>VIP Subscription</b>\n"
        f"Status: {status}\n"
        f"Time left: {days}d {hours}h\n\n"
        "VIP даёт приоритет в поиске (и позже — расширенные фильтры)."
    )
    await update.message.reply_html(txt, reply_markup=vip_menu_kb())

async def cb_vip_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        _, plan_key = q.data.split(":", 1)
    except Exception:
        await q.edit_message_text("Plan not found, try again.")
        return

    plan = VIP_PLANS.get(plan_key)
    if not plan:
        await q.edit_message_text("Plan not found, try again.")
        return

    title = plan["title"]
    description = "VIP access for Anonymous Chat (priority in search)."
    payload = f"vip_{plan_key}"
    prices = [LabeledPrice(label=title, amount=plan["amount"])]

    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title=title,
        description=description,
        payload=payload,
        provider_token=PROVIDER_TOKEN,     # Stars → пусто
        currency=CURRENCY_XTR,             # Stars
        prices=prices,
        start_parameter="vip",
        need_name=False,
        need_phone_number=False,
        need_email=False,
        need_shipping_address=False,
        is_flexible=False,
    )

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Для Stars — просто подтверждаем
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sp = update.message.successful_payment
    payload = sp.invoice_payload or ""
    plan_key = "3m"
    if payload.startswith("vip_"):
        plan_key = payload.split("_", 1)[1] or "3m"
    months = VIP_PLANS.get(plan_key, VIP_PLANS["3m"])["months"]

    uid = update.effective_user.id
    new_until = await extend_vip(uid, months)
    from datetime import datetime
    until_s = datetime.fromtimestamp(new_until).strftime("%Y-%m-%d %H:%M")

    await update.message.reply_html(
        f"🎉 <b>VIP activated!</b>\nValid until: <code>{until_s}</code>\nСпасибо за поддержку 🙏"
    )

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
        "/stop — остановить диалог\n"
        "/vip — VIP и оплата Stars"
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

# ===== Матчинг =====
async def try_match(uid: int) -> Optional[int]:
    """Обычный (для невипов): достаём из головы очереди первого свободного."""
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

async def try_match_vip(uid: int) -> Optional[int]:
    """VIP-поиск: пробегаем всю очередь и берём первого свободного, удаляя его."""
    members = await r.lrange(QUEUE_KEY, 0, -1)
    for raw in members:
        other = int(raw)
        if other == uid:
            continue
        if await get_peer(other):
            continue
        # удалить конкретного other из очереди
        await r.lrem(QUEUE_KEY, 1, other)
        return other
    return None

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

    # VIP-приоритет: сначала пробуем vip-поиск
    peer = None
    if await is_vip(uid):
        peer = await try_match_vip(uid)
    if not peer:
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
    app.add_handler(CommandHandler("vip", cmd_vip))  # <— добавили

    # анкета
    app.add_handler(CallbackQueryHandler(on_gender, pattern=r"^gender:(M|F)$"))
    app.add_handler(CallbackQueryHandler(on_age, pattern=r"^age:(12-20|21-30|31-40)$"))

    # VIP покупки (кнопки и платежи)
    app.add_handler(CallbackQueryHandler(cb_vip_buy, pattern=r"^vip_buy:(3m|6m|12m)$"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    # релеим всё, кроме команд
    app.add_handler(MessageHandler(~filters.COMMAND, relay))

    log.info("Bot starting (run_polling)…")
    app.run_polling()

if __name__ == "__main__":
    main()