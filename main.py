import os
import asyncio
import logging
import json
from typing import Optional, Dict

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("anonchat")

TOKEN = os.environ.get("TELEGRAM_TOKEN")
REDIS_URL = os.environ.get("REDIS_URL")  # может быть None

# ---------- Хранилище профилей (Redis -> fallback in-memory) ----------
class ProfileStore:
    def __init__(self):
        self._mem: Dict[int, dict] = {}
        self._redis = None

    async def init(self):
        if REDIS_URL:
            try:
                import redis.asyncio as redis
                self._redis = await redis.from_url(REDIS_URL, decode_responses=True)
                # sanity ping
                await self._redis.ping()
                log.info("Redis connected OK")
                return
            except Exception as e:
                log.error(f"Redis connect failed: {e}. Fallback to in-memory.")
                self._redis = None
        else:
            log.warning("REDIS_URL not set. Using in-memory store.")

    async def get(self, uid: int) -> dict:
        if self._redis:
            raw = await self._redis.get(f"profile:{uid}")
            if not raw:
                return {"gender": None, "age_range": None}
            try:
                return json.loads(raw)
            except Exception:
                return {"gender": None, "age_range": None}
        # memory
        return self._mem.get(uid, {"gender": None, "age_range": None})

    async def set(self, uid: int, data: dict):
        if self._redis:
            await self._redis.set(f"profile:{uid}", json.dumps(data))
        else:
            self._mem[uid] = data

    async def reset(self, uid: int):
        if self._redis:
            await self._redis.delete(f"profile:{uid}")
        self._mem.pop(uid, None)

store = ProfileStore()

# ---------- UI ----------
def gender_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[  # одна строка, две кнопки
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

# ---------- Flow ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = await store.get(uid)

    if not p["gender"]:
        await update.message.reply_text("Привет! Выбери свой пол:", reply_markup=gender_kb())
        return
    if not p["age_range"]:
        await update.message.reply_text("Укажи возраст:", reply_markup=age_kb())
        return

    await update.message.reply_text("Профиль уже заполнен ✅\n" + profile_str(p))

async def on_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    _, gender = q.data.split(":", 1)
    p = await store.get(uid)
    p["gender"] = gender
    await store.set(uid, p)
    await q.edit_message_text("Пол сохранён ✅\nТеперь выбери возраст:", reply_markup=age_kb())

async def on_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    _, age_range = q.data.split(":", 1)
    p = await store.get(uid)
    p["age_range"] = age_range
    await store.set(uid, p)
    await q.edit_message_text("Возраст сохранён ✅\n\nТвой профиль:\n" + profile_str(p) + "\n\nГотово. Что делаем дальше? ✍️")

# ---------- Utils ----------
async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = await store.get(uid)
    await update.message.reply_text("Твой профиль:\n" + profile_str(p))

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await store.reset(uid)
    await update.message.reply_text("Профиль очищен. Напиши /start.")

# ---------- Bootstrap ----------
async def main():
    if not TOKEN:
        # Пишем явную причину в лог — это частая ошибка
        raise RuntimeError("TELEGRAM_TOKEN is not set in environment variables")

    log.info(f"Boot… TOKEN={'set' if TOKEN else 'missing'}, REDIS_URL={'set' if REDIS_URL else 'missing'}")

    await store.init()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("reset_profile", cmd_reset))
    app.add_handler(CallbackQueryHandler(on_gender, pattern=r"^gender:(M|F)$"))
    app.add_handler(CallbackQueryHandler(on_age, pattern=r"^age:(12-20|21-30|31-40)$"))

    log.info("Bot started (polling)")
    await app.run_polling(close_loop=False)

if __name__ == "__main__":
    import asyncio

    try:
        asyncio.get_event_loop().run_until_complete(main())
    except KeyboardInterrupt:
        pass