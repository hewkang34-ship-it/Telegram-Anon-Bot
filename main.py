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

# Картинки (URL). Если пусто — шлём только текст.
DIAMOND_IMG_URL = os.environ.get("DIAMOND_IMG_URL", "")
HALO_IMG_URL    = os.environ.get("HALO_IMG_URL", "")

# ===== Redis клиент =====
r: Optional[redis.Redis] = None

PROFILE_KEY = "profile:{uid}"   # JSON
PAIR_KEY    = "pair:{uid}"      # int
QUEUE_KEY   = "q:global"        # list

# ====== VIP (Telegram Stars) ======
VIP_KEY        = "vip_until:{uid}"  # int (unix time)
CURRENCY_XTR   = "XTR"              # Telegram Stars
PROVIDER_TOKEN = ""                 # для Stars — пустая строка

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

def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Поиск любого собеседника", callback_data="search:any")],
        [
            InlineKeyboardButton("🙋‍♀️ Поиск Ж", callback_data="search:f"),
            InlineKeyboardButton("🙋‍♂️ Поиск М", callback_data="search:m"),
        ],
    ])

def end_dialog_text() -> str:
    return "Собеседник закончил с вами связь😔\nНапишите /search чтобы найти следующего."

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
    await update.message.reply_text(menu_text("Отлично!"), reply_markup=main_kb())

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
    await q.message.reply_text(menu_text("Отлично!"), reply_markup=main_kb())

# ===== Команды =====
async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = await get_profile(uid)
    await update.message.reply_text("Твой профиль:\n" + profile_str(p), reply_markup=main_kb())

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
    text = "Собеседник найден! Можете общаться анонимно. ✍️\n\n/next — искать нового собеседника\n/stop — закончить диалог"
    await context.bot.send_message(chat_id=a, text=text)
    await context.bot.send_message(chat_id=b, text=text)

async def do_search(chat_id: int, uid: int, context: ContextTypes.DEFAULT_TYPE):
    p = await get_profile(uid)
    if not (p.get("gender") and p.get("age_range")):
        await context.bot.send_message(chat_id, "Сначала заполни профиль: /start")
        return

    if await get_peer(uid):
        await context.bot.send_message(chat_id, "Ты уже в диалоге.\n/next — новый собеседник\n/stop — закончить диалог")
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
        await context.bot.send_message(chat_id, "Ищу собеседника… ⏳\n/stop — отменить поиск", reply_markup=main_kb())

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_search(update.effective_chat.id, update.effective_user.id, context)

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    peer = await get_peer(uid)
    await remove_from_queue(uid)
    if peer:
        await clear_pair(uid)
        try:
            await context.bot.send_message(chat_id=peer, text=end_dialog_text(), reply_markup=main_kb())
        except Exception as e:
            log.error(f"notify peer fail: {e}")
        await update.message.reply_text(end_dialog_text(), reply_markup=main_kb())
    else:
        await update.message.reply_text("Поиск остановлен.", reply_markup=main_kb())

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

# ===== VIP UI =====
def vip_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💎 VIP на 3 мес — {VIP_PLANS['3m']['amount']}⭐",  callback_data="vip_buy:3m")],
        [InlineKeyboardButton(f"💎 VIP на 6 мес — {VIP_PLANS['6m']['amount']}⭐",  callback_data="vip_buy:6m")],
        [InlineKeyboardButton(f"💎 VIP на 12 мес — {VIP_PLANS['12m']['amount']}⭐", callback_data="vip_buy:12m")],
    ])

async def cmd_vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    until = await get_vip_until(uid)
    from time import time as now
    active = until > int(now())
    left = max(0, until - int(now()))
    days = left // 86400
    hours = (left % 86400) // 3600
    status = "активен ✅" if active else "не активен ❌"

    caption = (
        f"VIP-статус: {status}\n"
        f"Осталось: {days}d {hours}h\n\n"
        "Если вы хотите выделиться и стать самым ярким собеседником в боте, предлагаем стать VIP-пользователем на 3–12 месяцев.\n\n"
        "⏳ VIP-пользователи получают премиум-аккаунт в боте на 3, 6 или 12 месяцев\n"
        "🧭 У VIP-пользователей есть приоритет в поиске: вы быстрее остальных будете находить собеседников по выбранному полу\n\n"
        "Стоимость VIP-статуса:\n"
        f"• 3 месяца — {VIP_PLANS['3m']['amount']} Telegram Stars\n"
        f"• 6 месяцев — {VIP_PLANS['6m']['amount']} Telegram Stars\n"
        f"• 12 месяцев — {VIP_PLANS['12m']['amount']} Telegram Stars\n"
        "Перейдите к оплате кнопкой ниже:"
    )
    if DIAMOND_IMG_URL:
        await update.message.reply_photo(DIAMOND_IMG_URL, caption=caption, reply_markup=vip_menu_kb())
    else:
        await update.message.reply_text(caption, reply_markup=vip_menu_kb())

async def cb_vip_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        _, plan_key = q.data.split(":", 1)
    except Exception:
        await q.edit_message_text("План не найден, попробуйте ещё раз.")
        return

    plan = VIP_PLANS.get(plan_key)
    if not plan:
        await q.edit_message_text("План не найден, попробуйте ещё раз.")
        return

    title = plan["title"]
    description = "VIP доступ для Анонимного Чата (приоритет в поиске)."
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
    await extend_vip(uid, months)

    text = f"Спасибо за приобретение VIP-статуса! Теперь вам доступен поиск по полу на {months} месяц(ев)."
    if HALO_IMG_URL:
        await update.message.reply_photo(HALO_IMG_URL, caption=text, reply_markup=main_kb())
    else:
        await update.message.reply_text(text, reply_markup=main_kb())

# ===== Кнопки поиска/ворота VIP =====
async def show_vip_gate(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Поиск по полу доступен только VIP-пользователям 💎.\n\n"
        "Для приобретения VIP-статуса напишите /vip"
    )
    if DIAMOND_IMG_URL:
        await context.bot.send_photo(chat_id, DIAMOND_IMG_URL, caption=text, reply_markup=main_kb())
    else:
        await context.bot.send_message(chat_id, text, reply_markup=main_kb())

async def on_search_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    _, mode = q.data.split(":", 1)  # any | f | m
    chat_id = q.message.chat_id

    if mode == "any":
        await do_search(chat_id, uid, context)
        return

    # f/m — только для VIP; если VIP — пока что запускаем обычный поиск
    if not await is_vip(uid):
        await show_vip_gate(chat_id, context)
        return
    await do_search(chat_id, uid, context)

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
    app.add_handler(CommandHandler("vip", cmd_vip))

    # анкета
    app.add_handler(CallbackQueryHandler(on_gender, pattern=r"^gender:(M|F)$"))
    app.add_handler(CallbackQueryHandler(on_age, pattern=r"^age:(12-20|21-30|31-40)$"))

    # кнопки поиска
    app.add_handler(CallbackQueryHandler(on_search_buttons, pattern=r"^search:(any|f|m)$"))

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