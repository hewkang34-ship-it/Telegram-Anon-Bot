import os
import asyncio
import logging
import json
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)
import redis.asyncio as redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("anonchat")

# ========= ENV =========
TOKEN = os.environ.get("TELEGRAM_TOKEN")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# ========= Redis client & keys =========
r: Optional[redis.Redis] = None
PROFILE_KEY = "profile:{uid}"  # JSON: {"gender": "M/F", "age_range": "12-20|21-30|31-40"}

# ========= Keyboards =========
def gender_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("М 🙋🏻‍♂️", callback_data="gender:M"),
            InlineKeyboardButton("Ж 🙋🏻‍♀️", callback_data="gender:F"),
        ]
    ])

def age_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("12–20", callback_data="age:12-20"),
            InlineKeyboardButton("21–30", callback_data="age:21-30"),
            InlineKeyboardButton("31–40", callback_data="age:31-40"),
        ]
    ])

# ========= Profile helpers =========
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

def profile_str(p: dict) -> str:
    g_map = {"M": "Мужской", "F": "Женский"}
    g = g_map.get(p.get("gender"), "—")
    a = p.get("age_range") or "—"
    return f"Пол: {g}\nВозраст: {a}"

def is_profile_complete(p: dict) -> bool:
    return bool(p.get("gender") and p.get("age_range"))

# ========= Flow =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = await get_profile(uid)

    if not p["gender"]:
        await update.message.reply_text(
            "Привет! Выбери свой пол:", reply_markup=gender_kb()
        )
        return

    if not p["age_range"]:
        await update.message.reply_text(
            "Укажи возраст:", reply_markup=age_kb()
        )
        return

    await update.message.reply_text(
        "Профиль уже заполнен ✅\n" + profile_str(p)
    )

async def on_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    _, gender = query.data.split(":", 1)
    p = await get_profile(uid)
    p["gender"] = gender
    await save_profile(uid, p)

    # Сразу спрашиваем возраст
    await query.edit_message_text(
        "Пол сохранён ✅\nТеперь выбери возраст:", reply_markup=age_kb()
    )

async def on_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    _, age_range = query.data.split(":", 1)
    p = await get_profile(uid)
    p["age_range"] = age_range
    await save_profile(uid, p)

    await query.edit_message_text(
        "Возраст сохранён ✅\n\nТвой профиль:\n" + profile_str(p) +
        "\n\nГотово. Пиши, что делаем дальше ✍️"
    )

# ========= Utility commands =========
async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = await get_profile(uid)
    await update.message.reply_text("Твой профиль:\n" + profile_str(p))

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await reset_profile(uid)
    await update.message.reply_text("Профиль очищен. Напиши /start, чтобы заполнить заново.")

# ========= App bootstrap =========
async def main():
    global r
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан в переменных окружения")

    r = await redis.from_url(REDIS_URL, decode_responses=True)

    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("reset_profile", cmd_reset))

    # Кнопки
    app.add_handler(CallbackQueryHandler(on_gender, pattern=r"^gender:(M|F)$"))
    app.add_handler(CallbackQueryHandler(on_age, pattern=r"^age:(12-20|21-30|31-40)$"))

    log.info("Bot started")
    await app.run_polling(close_loop=False)

if __name__ == "__main__":
    asyncio.run(main())