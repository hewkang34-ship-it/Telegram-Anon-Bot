import os
import json
import logging
import re
import time
from typing import Optional

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice,
    ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters, PreCheckoutQueryHandler
)

import redis.asyncio as redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("anonchat")

TOKEN = os.environ.get("TELEGRAM_TOKEN")
REDIS_URL = os.environ.get("REDIS_URL")  # redis://default:<pass>@<host>:<port>/0

DIAMOND_IMG_URL = os.environ.get("DIAMOND_IMG_URL", "")
HALO_IMG_URL    = os.environ.get("HALO_IMG_URL", "")

# ===== Redis =====
r: Optional[redis.Redis] = None
PROFILE_KEY = "profile:{uid}"
PAIR_KEY    = "pair:{uid}"
QUEUE_KEY   = "q:global"
VIP_KEY     = "vip_until:{uid}"
PREMIUM_KEY = "premium_until:{uid}"
RATES_KEY   = "rates:{uid}"
REPORTS_KEY = "reports:{uid}"
USERS_SET   = "users"  # множество уникальных пользователей

# ===== Payments (Telegram Stars) =====
CURRENCY_XTR   = "XTR"
PROVIDER_TOKEN = ""  # Stars → пустая строка

# VIP (12 месяцев)
VIP_PLANS = {
    "12m": {"months": 12, "amount": 5000, "title": "💎 VIP • 12 months"},
}
# PREMIUM (тексты включают ₽/$ чисто для отображения)
PREMIUM_PLANS = {
    "7d":  {"days": 7,    "amount": 150,  "title": "Премиум • 7 дней",  "price_line": "7 дней за 150 ⭐ / 199₽ / $1.99"},
    "1m":  {"months": 1,  "amount": 250,  "title": "Премиум • 1 месяц", "price_line": "1 месяц за 250 ⭐ / 399₽ / $3.99"},
    "12m": {"months": 12, "amount": 1000, "title": "Премиум • 1 год",   "price_line": "1 год за 1000 ⭐ / 1999₽ / $19.99"},
}

# ===== Reply-клавиатура (кнопка «квадратики») =====
BTN_ANY = "🚀 Поиск любого собеседника"
BTN_F   = "🙋‍♀️ Поиск Ж"
BTN_M   = "🙋‍♂️ Поиск М"

def reply_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[BTN_ANY],[BTN_F, BTN_M]],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Выберите тип поиска…",
        selective=True,
    )

def hide_reply_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()

def end_dialog_text() -> str:
    return (
        "Собеседник закончил с вами связь 😔\n"
        "Напишите /search чтобы найти следующего\n\n"
        "https://t.me/AnonymousWrldChatBot"
    )

# ===== Анкета UI =====
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

def settings_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👨‍🦱 Пол 👩‍🦱", callback_data="settings:gender")],
        [InlineKeyboardButton("📅 Возраст", callback_data="settings:age")],
    ])

def profile_str(p: dict) -> str:
    g_map = {"M": "Мужской", "F": "Женский"}
    g = g_map.get(p.get("gender"), "—")
    a = p.get("age_range") or "—"
    return f"Пол: {g}\nВозраст: {a}"

# ===== Users counter =====
async def add_user(uid: int):
    try:
        await r.sadd(USERS_SET, uid)
    except Exception as e:
        log.warning(f"SADD users failed: {e}")

async def users_count() -> int:
    try:
        return await r.scard(USERS_SET)
    except Exception:
        return 0

def menu_text_base(users_total: int, prefix: str = "") -> str:
    top = f"Сейчас в чате {users_total} пользователей 👥"
    body = (
        "/search — поиск собеседника\n"
        "/next — закончить текущий диалог и сразу же искать нового собеседника\n"
        "/stop — закончить разговор с собеседником"
    )
    return (prefix + "\n" if prefix else "") + top + "\n\n" + body

# ===== Helpers: VIP / PREMIUM =====
def vip_k(uid: int) -> str: return VIP_KEY.format(uid=uid)
def premium_k(uid: int) -> str: return PREMIUM_KEY.format(uid=uid)

async def _get_until(key: str) -> int:
    val = await r.get(key)
    return int(val) if val else 0

async def get_vip_until(uid: int) -> int:
    return await _get_until(vip_k(uid))

async def get_premium_until(uid: int) -> int:
    return await _get_until(premium_k(uid))

async def is_vip(uid: int) -> bool:
    return (await get_vip_until(uid)) > int(time.time())

async def is_premium(uid: int) -> bool:
    return (await get_premium_until(uid)) > int(time.time())

async def has_gender_rights(uid: int) -> bool:
    # Право искать по полу — у Премиума ИЛИ у VIP
    return (await is_premium(uid)) or (await is_vip(uid))

def _add_months_or_days(base_ts: int, months: int = 0, days: int = 0) -> int:
    # months -> 30 дней условно
    return base_ts + int(days*24*3600 + months*30*24*3600)

async def extend_vip(uid: int, months: int) -> int:
    now_i = int(time.time())
    base = max(now_i, await get_vip_until(uid))
    new_until = _add_months_or_days(base, months=months)
    await r.set(vip_k(uid), new_until)
    return new_until

async def extend_premium(uid: int, months: int = 0, days: int = 0) -> int:
    now_i = int(time.time())
    base = max(now_i, await get_premium_until(uid))
    new_until = _add_months_or_days(base, months=months, days=days)
    await r.set(premium_k(uid), new_until)
    return new_until

# ===== Profiles/Pairs/Queue =====
async def get_profile(uid: int) -> dict:
    raw = await r.get(PROFILE_KEY.format(uid=uid))
    if not raw:
        return {"gender": None, "age_range": None}
    try:
        return json.loads(raw)
    except Exception:
        return {"gender": None, "age_range": None}

async def save_profile(uid: int, data: dict): await r.set(PROFILE_KEY.format(uid=uid), json.dumps(data))
async def reset_profile(uid: int): await r.delete(PROFILE_KEY.format(uid=uid))

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

async def push_queue(uid: int):
    members = await r.lrange(QUEUE_KEY, 0, -1)
    if str(uid) not in members:
        await r.rpush(QUEUE_KEY, uid)
async def pop_queue() -> Optional[int]:
    val = await r.lpop(QUEUE_KEY)
    return int(val) if val else None
async def remove_from_queue(uid: int): await r.lrem(QUEUE_KEY, 0, uid)

# ===== START / анкета =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await add_user(uid)
    p = await get_profile(uid)
    if not p["gender"]:
        await update.message.reply_text("Привет! Выбери свой пол:", reply_markup=gender_kb());  return
    if not p["age_range"]:
        await update.message.reply_text("Укажи возраст:", reply_markup=age_kb());  return

    total = await users_count()
    await update.message.reply_text("Профиль уже заполнен ✅\n" + profile_str(p))
    await update.message.reply_text(menu_text_base(total, "Отлично!"), reply_markup=reply_menu_kb())

# callbacks анкеты
async def on_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query;  await q.answer()
    uid = q.from_user.id;  _, gender = q.data.split(":", 1)
    p = await get_profile(uid);  p["gender"] = gender;  await save_profile(uid, p)
    await q.edit_message_text("Пол сохранён ✅\nТеперь выбери возраст:", reply_markup=age_kb())

async def on_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query;  await q.answer()
    uid = q.from_user.id;  _, age_range = q.data.split(":", 1)
    p = await get_profile(uid);  p["age_range"] = age_range;  await save_profile(uid, p)
    total = await users_count()
    await q.edit_message_text("Возраст сохранён ✅\n\nТвой профиль:\n" + profile_str(p))
    await q.message.reply_text(menu_text_base(total, "Отлично!"), reply_markup=reply_menu_kb())

# ===== Matching =====
async def try_match(uid: int) -> Optional[int]:
    while True:
        other = await pop_queue()
        if other is None: return None
        if other == uid:
            await push_queue(other)
            return None
        if await get_peer(other):  # уже занят
            continue
        return other

async def try_match_priority(uid: int) -> Optional[int]:
    # Для прем/вип — приоритетный просмотр очереди
    members = await r.lrange(QUEUE_KEY, 0, -1)
    for raw in members:
        other = int(raw)
        if other == uid: continue
        if await get_peer(other): continue
        await r.lrem(QUEUE_KEY, 1, other)
        return other
    return None

async def announce_pair(context: ContextTypes.DEFAULT_TYPE, a: int, b: int):
    text = "Собеседник найден! Можете общаться анонимно. ✍️\n\n/next — искать нового собеседника\n/stop — закончить диалог"
    await context.bot.send_message(a, text, reply_markup=hide_reply_kb())
    await context.bot.send_message(b, text, reply_markup=hide_reply_kb())

async def do_search(chat_id: int, uid: int, context: ContextTypes.DEFAULT_TYPE):
    p = await get_profile(uid)
    if not (p.get("gender") and p.get("age_range")):
        await context.bot.send_message(chat_id, "Сначала заполни профиль: /start");  return
    if await get_peer(uid):
        await context.bot.send_message(chat_id, "Ты уже в диалоге.\n/next — новый собеседник\n/stop — закончить диалог");  return

    peer = None
    if await has_gender_rights(uid):
        peer = await try_match_priority(uid)
    if not peer:
        peer = await try_match(uid)

    if peer:
        await set_pair(uid, peer);  await announce_pair(context, uid, peer)
    else:
        await push_queue(uid)
        await context.bot.send_message(chat_id, "Ищу собеседника… ⏳\n/stop — отменить поиск", reply_markup=hide_reply_kb())

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await add_user(update.effective_user.id)
    await do_search(update.effective_chat.id, update.effective_user.id, context)

# ===== Оценка и жалобы =====
RATE_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("👍🏻", callback_data="rate:+"), InlineKeyboardButton("👎🏻", callback_data="rate:-")],
    [InlineKeyboardButton("⚠️Пожаловаться", callback_data="report:open")],
])
REPORT_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("📰 Реклама", callback_data="report:Реклама")],
    [InlineKeyboardButton("💰 Продажа", callback_data="report:Продажа")],
    [InlineKeyboardButton("⛔ Разжигание розни", callback_data="report:Разжигание розни")],
    [InlineKeyboardButton("🔞 Детская порнография", callback_data="report:Детская порнография")],
    [InlineKeyboardButton("🪙 Попрошайничество", callback_data="report:Попрошайничество")],
    [InlineKeyboardButton("😡 Оскорбление", callback_data="report:Оскорбление")],
    [InlineKeyboardButton("👊 Насилие", callback_data="report:Насилие")],
    [InlineKeyboardButton("📣 Пропаганда суицида", callback_data="report:Пропаганда суицида")],
    [InlineKeyboardButton("❌ Пошлый собеседник", callback_data="report:Пошлый собеседник")],
    [InlineKeyboardButton("← Назад", callback_data="report:back")],
])

async def send_rate_prompt(context: ContextTypes.DEFAULT_TYPE, uid: int):
    txt = "Если хотите, оставьте мнение о вашем собеседнике. Это поможет находить вам подходящих собеседников"
    await context.bot.send_message(uid, txt, reply_markup=RATE_MENU)

async def on_rate_or_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query;  await q.answer()
    uid = q.from_user.id
    data = q.data
    peer = await get_peer(uid)
    peer_id = peer if peer else 0

    if data == "report:open":
        await q.edit_message_reply_markup(REPORT_MENU);  return
    if data == "report:back":
        await q.edit_message_reply_markup(RATE_MENU);  return

    if data.startswith("rate:"):
        val = 1 if data.endswith("+") else -1
        rec = {"peer": peer_id, "v": val, "ts": int(time.time())}
        await r.rpush(RATES_KEY.format(uid=uid), json.dumps(rec))
        await q.edit_message_text("Спасибо! Оценка сохранена ✅");  return

    if data.startswith("report:"):
        reason = data.split(":",1)[1]
        rec = {"peer": peer_id, "reason": reason, "ts": int(time.time())}
        await r.rpush(REPORTS_KEY.format(uid=uid), json.dumps(rec))
        await q.edit_message_text("Спасибо! Жалоба отправлена ⚠️");  return

# ===== STOP / NEXT =====
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    peer = await get_peer(uid)
    await remove_from_queue(uid)

    if peer:
        await clear_pair(uid)
        try:
            await context.bot.send_message(peer, end_dialog_text(), reply_markup=reply_menu_kb())
            await send_rate_prompt(context, peer)
        except Exception as e:
            log.error(f"notify peer fail: {e}")

        await update.message.reply_text(end_dialog_text(), reply_markup=reply_menu_kb())
        await send_rate_prompt(context, uid)
    else:
        await update.message.reply_text("Поиск остановлен.", reply_markup=reply_menu_kb())

async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_stop(update, context)
    await cmd_search(update, context)

# ===== Релей сообщений =====
async def relay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg: return
    uid = update.effective_user.id
    await add_user(uid)
    peer = await get_peer(uid)
    if not peer: return
    try: await msg.copy(chat_id=peer)
    except Exception as e: log.error(f"relay error: {e}")

# ===== VIP / PREMIUM UI и оплата =====
def vip_menu_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💎 VIP за {VIP_PLANS['12m']['amount']}⭐ / 6999₽ / $69", callback_data="vip_buy:12m")]
    ])

def premium_menu_inline() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(PREMIUM_PLANS["7d"]["price_line"],  callback_data="premium_buy:7d")],
        [InlineKeyboardButton(PREMIUM_PLANS["1m"]["price_line"],  callback_data="premium_buy:1m")],
        [InlineKeyboardButton(PREMIUM_PLANS["12m"]["price_line"], callback_data="premium_buy:12m")],
        [InlineKeyboardButton("💎 Стать VIP", callback_data="open_vip")],
    ]
    return InlineKeyboardMarkup(rows)

def fmt_until(ts: int) -> str:
    if not ts: return "—"
    import datetime as dt
    d = dt.datetime.fromtimestamp(ts)
    return d.strftime("%d.%m.%Y")

async def send_vip_page(chat_id: int, uid: int, context: ContextTypes.DEFAULT_TYPE):
    until = await get_vip_until(uid)
    active = until > int(time.time())
    left = max(0, until - int(time.time()))
    days = left // 86400; hours = (left % 86400) // 3600
    status = "не активен ❌" if not active else f"активен до {fmt_until(until)} ✅"

    caption = (
        f"VIP-статус: {status}\n"
        f"Осталось: {days}d {hours}h\n\n"
        "Если вы хотите выделиться и стать самым ярким собеседником в боте, предлагаем стать VIP-пользователем на 12 месяцев\n\n"
        "⏳ VIP-пользователи получают премиум-аккаунт в боте на 12 месяцев\n"
        "🧭 У VIP-пользователей есть приоритет в поиске: вы быстрее остальных будете находить собеседников по выбранным интересам и полу\n"
        "💎 Каждый собеседник увидит ваш VIP-статус и проявит больший интерес\n\n"
        f"Стоимость VIP-статуса на 12 месяцев — {VIP_PLANS['12m']['amount']} Telegram Stars / 6999₽ / $69\n"
        "Переходите к оплате по кнопке ниже:"
    )
    if DIAMOND_IMG_URL:
        await context.bot.send_photo(chat_id, DIAMOND_IMG_URL, caption=caption, reply_markup=vip_menu_inline())
    else:
        await context.bot.send_message(chat_id, caption, reply_markup=vip_menu_inline())

async def cmd_vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_vip_page(update.effective_chat.id, update.effective_user.id, context)

async def cb_open_vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    # Убираем клавиатуру под сообщением, чтобы не путало
    try:
        await q.edit_message_reply_markup(None)
    except Exception:
        pass
    await send_vip_page(q.message.chat_id, q.from_user.id, context)

async def cb_vip_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query;  await q.answer()
    try: _, plan_key = q.data.split(":", 1)
    except Exception: await q.edit_message_text("План не найден, попробуйте ещё раз."); return
    plan = VIP_PLANS.get(plan_key)
    if not plan: await q.edit_message_text("План не найден, попробуйте ещё раз."); return

    title = plan["title"];  description = "VIP доступ для Анонимного Чата (приоритет в поиске)."
    payload = f"vip_{plan_key}"
    prices = [LabeledPrice(label=title, amount=plan["amount"])]

    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title=title, description=description, payload=payload,
        provider_token=PROVIDER_TOKEN, currency=CURRENCY_XTR, prices=prices,
        start_parameter="vip", need_name=False, need_phone_number=False,
        need_email=False, need_shipping_address=False, is_flexible=False,
    )

# ===== Премиум =====
def premium_text_no(active: bool) -> str:
    return (
        "Премиум пользователи могут искать собеседников по полу. Приобретая премиум, вы получаете следующие преимущества:\n\n"
        "📌 Поиск по полу\n"
        "👩 Премиум пользователи могут искать только девушек или только парней\n\n"
        "📌 Безлимитное общение\n"
        "♾️ Премиум пользователи не могут получить ограничение на количество чатов в день\n\n"
        "📌 Отсутствие рекламы\n"
        "📲 Мы не показываем рекламу премиум пользователям\n\n"
        "📌 Поддержка чата\n"
        "🎁 Чем больше вы нас поддерживаете, тем больше новых функций мы внедрим, будем лучше модерировать чат и покупать больше рекламы для увеличения количества собеседников\n\n"
        "💎 Если вы хотите стать не просто премиум-пользователем, а уникальным VIP-собеседником, переходите за подробностями по команде /vip\n\n"
        "Чтобы " + ("продлить" if active else "купить") + " премиум, выберите продолжительность и удобный способ оплаты ниже:"
    )

async def cmd_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    prem_until = await get_premium_until(uid)
    active = prem_until > int(time.time())
    if not active:
        text = premium_text_no(False)
    else:
        text = (
            f"❗ У вас есть активный премиум до {fmt_until(prem_until)}\n\n" +
            premium_text_no(True)
        )
    await update.message.reply_text(text, reply_markup=premium_menu_inline())

async def cb_premium_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query;  await q.answer()
    try: _, plan_key = q.data.split(":", 1)
    except Exception: await q.edit_message_text("План не найден, попробуйте ещё раз."); return
    plan = PREMIUM_PLANS.get(plan_key)
    if not plan: await q.edit_message_text("План не найден, попробуйте ещё раз."); return

    title = plan["title"];  description = "Премиум доступ для Анонимного Чата (поиск по полу, безлимит, нет рекламы)."
    payload = f"premium_{plan_key}"
    prices = [LabeledPrice(label=title, amount=plan["amount"])]

    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title=title, description=description, payload=payload,
        provider_token=PROVIDER_TOKEN, currency=CURRENCY_XTR, prices=prices,
        start_parameter="premium", need_name=False, need_phone_number=False,
        need_email=False, need_shipping_address=False, is_flexible=False,
    )

# ===== Общий pre-checkout / успешная оплата =====
async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sp = update.message.successful_payment
    payload = sp.invoice_payload if sp else ""
    uid = update.effective_user.id

    if payload.startswith("vip_"):
        plan_key = payload.split("_",1)[1]
        months = VIP_PLANS.get(plan_key, VIP_PLANS["12m"])["months"]
        await extend_vip(uid, months)
        text = f"Спасибо за приобретение VIP-статуса! Активен до {fmt_until(await get_vip_until(uid))}."
    elif payload.startswith("premium_"):
        plan_key = payload.split("_",1)[1]
        plan = PREMIUM_PLANS.get(plan_key, PREMIUM_PLANS["1m"])
        months = plan.get("months", 0)
        days = plan.get("days", 0)
        await extend_premium(uid, months=months, days=days)
        text = f"Спасибо! Премиум активен до {fmt_until(await get_premium_until(uid))}. Теперь доступен поиск по полу."
    else:
        text = "Оплата получена."

    if HALO_IMG_URL: await update.message.reply_photo(HALO_IMG_URL, caption=text, reply_markup=reply_menu_kb())
    else: await update.message.reply_text(text, reply_markup=reply_menu_kb())

# ===== /link — отправить ссылку на себя собеседнику =====
async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    peer = await get_peer(uid)
    if not peer:
        await update.message.reply_text("Вы не в диалоге. Сначала начните чат: /search")
        return
    user = update.effective_user
    if user.username:
        url = f"https://t.me/{user.username}"
    else:
        url = f"tg://user?id={uid}"

    # Собеседнику — кликабельное слово «Ссылка»
    peer_text = f"👋 Собеседник отправил свой профиль: [Ссылка]({url})"
    await context.bot.send_message(peer, peer_text, parse_mode="Markdown")
    # Отправителю — подтверждение
    await update.message.reply_text("Ссылка на ваш аккаунт отправлена собеседнику.")

# ===== Доп. команды =====
async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Правила поведения в чате:\nanonchat.io/rules")

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выберите настройки, которые вы бы хотели изменить:", reply_markup=settings_menu_kb())

async def on_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    _, what = q.data.split(":", 1)
    if what == "gender":
        await q.edit_message_text("Выберите пол:", reply_markup=gender_kb())
    elif what == "age":
        await q.edit_message_text("Выберите возраст:", reply_markup=age_kb())

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rates = await r.llen(RATES_KEY.format(uid=uid))
    reports = await r.llen(REPORTS_KEY.format(uid=uid))
    total = await users_count()
    await update.message.reply_text(f"📊 Статистика: ваши оценки — {rates}, жалобы — {reports}.\n👥 Всего пользователей: {total}")

async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 ID вашего аккаунта: <code>{update.effective_user.id}</code>", parse_mode="HTML")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Это бот для анонимного общения в Телеграме\n\n"
        "Бот умеет пересылать сообщения, фото, видео, гифки, стикеры, аудиосообщения и видеосообщения.\n\n"
        "/search — поиск собеседника\n"
        "/next — закончить текущий диалог и сразу же искать нового собеседника\n"
        "/stop — закончить разговор с собеседником\n"
        "/pay — поиск по полу, оформление и управление премиумом\n"
        "/vip — стать VIP-пользователем\n"
        "/link — отправить собеседнику ссылку на вас в Телеграме\n"
        "/settings — изменить свой пол и другие настройки\n"
        "/rules — ознакомиться с правилами"
    )
    await update.message.reply_text(text)

async def cmd_paysupport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Помощь по оплате: если возникла проблема с покупкой Премиума/VIP — напишите нам @Support (или ответьте на это сообщение).")

# ===== Reply-кнопки обработчики (нужны ДО main) =====
async def show_premium_gate(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    text = "Поиск по полу доступен только Премиум/💎VIP пользователям.\n\nОформить — /pay\nПодробности VIP — /vip"
    if DIAMOND_IMG_URL: await context.bot.send_photo(chat_id, DIAMOND_IMG_URL, caption=text)
    else: await context.bot.send_message(chat_id, text)

async def on_btn_any(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_search(update.effective_chat.id, update.effective_user.id, context)

async def on_btn_f(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_gender_rights(update.effective_user.id):
        await show_premium_gate(update.effective_chat.id, context);  return
    await do_search(update.effective_chat.id, update.effective_user.id, context)

async def on_btn_m(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_gender_rights(update.effective_user.id):
        await show_premium_gate(update.effective_chat.id, context);  return
    await do_search(update.effective_chat.id, update.effective_user.id, context)

# ===== post_init / меню команд =====
async def post_init(app: Application):
    await app.bot.delete_webhook(drop_pending_updates=True)
    if not REDIS_URL:
        raise RuntimeError("REDIS_URL is not set")

    global r
    r = await redis.from_url(REDIS_URL, decode_responses=True)
    await r.ping()
    log.info("Redis connected OK")

    try:
        commands = [
            BotCommand("start", "🔄 Начать"),
            BotCommand("search", "🔎 Поиск собеседника"),
            BotCommand("next", "🆕 Закончить диалог и искать нового собеседника"),
            BotCommand("stop", "⛔ Закончить диалог с собеседником"),
            BotCommand("help", "🆘 Помощь по боту"),
            BotCommand("pay", "👩‍❤️‍👨 Поиск по полу / Премиум"),
            BotCommand("paysupport", "💰 Помощь по оплате"),
            BotCommand("vip", "💎 Стать VIP-пользователем"),
            BotCommand("link", "🔗 Отправить ссылку на ваш Telegram собеседнику"),
            BotCommand("settings", "⚙️ Настройки пола, возраста и тех. настройки"),
            BotCommand("rules", "📋 Правила общения в чате"),
            BotCommand("stats", "📊 Статистика и число пользователей"),
            BotCommand("myid", "🆔 Отобразить ID вашего аккаунта"),
        ]
        await app.bot.set_my_commands(commands)
    except Exception as e:
        log.warning(f"set_my_commands failed: {e}")

# ===== Application =====
def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is not set")
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("pay", cmd_pay))
    app.add_handler(CommandHandler("paysupport", cmd_paysupport))
    app.add_handler(CommandHandler("vip", cmd_vip))
    app.add_handler(CommandHandler("link", cmd_link))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("settings", cmd_settings))

    # Инлайн коллбэки
    app.add_handler(CallbackQueryHandler(on_gender, pattern=r"^gender:(M|F)$"))
    app.add_handler(CallbackQueryHandler(on_age, pattern=r"^age:(12-20|21-30|31-40)$"))
    app.add_handler(CallbackQueryHandler(on_settings, pattern=r"^settings:(gender|age)$"))
    app.add_handler(CallbackQueryHandler(cb_open_vip, pattern=r"^open_vip$"))

    # Покупки
    app.add_handler(CallbackQueryHandler(cb_vip_buy, pattern=r"^vip_buy:(12m)$"))
    app.add_handler(CallbackQueryHandler(cb_premium_buy, pattern=r"^premium_buy:(7d|1m|12m)$"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    # Оценки/жалобы
    app.add_handler(CallbackQueryHandler(on_rate_or_report, pattern=r"^(rate:\+|rate:-|report:.*)$"))

    # Кнопки Reply (текстовые)
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_ANY)}$"), on_btn_any))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_F)}$"),   on_btn_f))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_M)}$"),   on_btn_m))

    # Релеинг всего остального
    ignore = (
        filters.COMMAND
        | filters.Regex(f"^{re.escape(BTN_ANY)}$")
        | filters.Regex(f"^{re.escape(BTN_F)}$")
        | filters.Regex(f"^{re.escape(BTN_M)}$")
    )
    app.add_handler(MessageHandler(~ignore, relay))

    log.info("Bot starting (run_polling)…")
    app.run_polling()

if __name__ == "__main__":
    main()