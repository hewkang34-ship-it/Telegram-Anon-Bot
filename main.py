import os
import json
import logging
from typing import Dict, Optional

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("anonchat")

TOKEN = os.environ.get("TELEGRAM_TOKEN")
REDIS_URL = os.environ.get("REDIS_URL")  # опционально

# ===== Хранилище (Redis -> in-memory) =====
class Store:
    def __init__(self):
        self._mem_profiles: Dict[int, dict] = {}
        self._mem_pairs: Dict[int, int] = {}
        self._mem_queue: list[int] = []
        self._redis = None

    async def init(self):
        if not REDIS_URL:
            log.warning("REDIS_URL not set. Using in-memory store.")
            return
        try:
            import redis.asyncio as redis
            self._redis = await redis.from_url(REDIS_URL, decode_responses=True)
            await self._redis.ping()
            log.info("Redis connected OK")
        except Exception as e:
            log.error(f"Redis connect failed: {e}. Fallback to in-memory.")
            self._redis = None

    # ---- Профили ----
    async def get_profile(self, uid: int) -> dict:
        if self._redis:
            raw = await self._redis.get(f"profile:{uid}")
            if not raw:
                return {"gender": None, "age_range": None}
            try:
                return json.loads(raw)
            except Exception:
                return {"gender": None, "age_range": None}
        return self._mem_profiles.get(uid, {"gender": None, "age_range": None})

    async def save_profile(self, uid: int, data: dict):
        if self._redis:
            await self._redis.set(f"profile:{uid}", json.dumps(data))
        else:
            self._mem_profiles[uid] = data

    async def reset_profile(self, uid: int):
        if self._redis:
            await self._redis.delete(f"profile:{uid}")
        self._mem_profiles.pop(uid, None)

    # ---- Пары ----
    async def get_peer(self, uid: int) -> Optional[int]:
        if self._redis:
            val = await self._redis.get(f"pair:{uid}")
            return int(val) if val else None
        return self._mem_pairs.get(uid)

    async def set_pair(self, a: int, b: int):
        if self._redis:
            await self._redis.set(f"pair:{a}", b)
            await self._redis.set(f"pair:{b}", a)
        else:
            self._mem_pairs[a] = b
            self._mem_pairs[b] = a

    async def clear_pair(self, uid: int):
        peer = await self.get_peer(uid)
        if self._redis:
            await self._redis.delete(f"pair:{uid}")
            if peer:
                await self._redis.delete(f"pair:{peer}")
        else:
            self._mem_pairs.pop(uid, None)
            if peer:
                self._mem_pairs.pop(peer, None)

    # ---- Очередь ----
    async def push_queue(self, uid: int):
        if self._redis:
            # не добавляем дубликаты
            members = await self._redis.lrange("q:global", 0, -1)
            if str(uid) not in members:
                await self._redis.rpush("q:global", uid)
            return
        if uid not in self._mem_queue:
            self._mem_queue.append(uid)

    async def pop_queue(self) -> Optional[int]:
        if self._redis:
            val = await self._redis.lpop("q:global")
            return int(val) if val else None
        if self._mem_queue:
            return self._mem_queue.pop(0)
        return None

    async def remove_from_queue(self, uid: int):
        if self._redis:
            await self._redis.lrem("q:global", 0, uid)
        else:
            if uid in self._mem_queue:
                self._mem_queue.remove(uid)

store = Store()

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

# ===== Handlers =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = await store.get_profile(uid)
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
    p = await store.get_profile(uid)
    p["gender"] = gender
    await store.save_profile(uid, p)
    await q.edit_message_text("Пол сохранён ✅\nТеперь выбери возраст:", reply_markup=age_kb())

async def on_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    _, age_range = q.data.split(":", 1)
    p = await store.get_profile(uid)
    p["age_range"] = age_range
    await store.save_profile(uid, p)
    await q.edit_message_text(
        "Возраст сохранён ✅\n\nТвой профиль:\n" + profile_str(p)
    )
    await q.message.reply_text(menu_text("Отлично!"))

# --- Команды: профиль/сброс ---
async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = await store.get_profile(uid)
    await update.message.reply_text("Твой профиль:\n" + profile_str(p))

async def cmd_reset_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await store.reset_profile(uid)
    await store.clear_pair(uid)
    await store.remove_from_queue(uid)
    await update.message.reply_text("Профиль очищен. Напиши /start.")

# --- Матчинг ---
async def try_match(uid: int) -> Optional[int]:
    # простая глобальная очередь; фильтры по полу/возрасту добавим позже (VIP)
    while True:
        other = await store.pop_queue()
        if other is None:
            return None
        if other == uid:
            # не матчим с самим собой; вернём его назад в очередь (хвост)
            await store.push_queue(other)
            return None
        # если другой уже с кем-то, ищем дальше
        if await store.get_peer(other):
            continue
        return other

async def announce_pair(context: ContextTypes.DEFAULT_TYPE, a: int, b: int):
    text = "Собеседник найден 👤\n\n/next — искать нового собеседника\n/stop — закончить диалог"
    try:
        await context.bot.send_message(chat_id=a, text=text)
        await context.bot.send_message(chat_id=b, text=text)
    except Exception as e:
        log.error(f"announce_pair error: {e}")

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    # профиль должен быть заполнен
    p = await store.get_profile(uid)
    if not (p.get("gender") and p.get("age_range")):
        await update.message.reply_text("Сначала заполни профиль: /start")
        return
    # если уже в диалоге — просто подсказываем команды
    if await store.get_peer(uid):
        await update.message.reply_text("Ты уже в диалоге.\n/next — новый собеседник\n/stop — закончить диалог")
        return

    # моментальный матч
    peer = await try_match(uid)
    if peer:
        await store.set_pair(uid, peer)
        await announce_pair(context, uid, peer)
        return

    # иначе — в очередь
    await store.push_queue(uid)
    await update.message.reply_text("Ищу собеседника… ⏳\n/stop — отменить поиск")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    peer = await store.get_peer(uid)
    await store.remove_from_queue(uid)
    if peer:
        await store.clear_pair(uid)
        try:
            await context.bot.send_message(chat_id=peer, text="Собеседник завершил диалог. /search — найти нового")
        except Exception as e:
            log.error(f"notify peer fail: {e}")
        await update.message.reply_text("Диалог завершён. /search — найти нового")
    else:
        await update.message.reply_text("Поиск остановлен.")

async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # next = stop + search
    await cmd_stop(update, context)
    # вызовем поиск сразу после stop
    await cmd_search(update, context)

# --- Релеинг всех сообщений между собеседниками ---
async def relay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    uid = update.effective_user.id
    peer = await store.get_peer(uid)
    if not peer:
        # не в диалоге — подскажем команды
        return
    try:
        await update.message.copy(chat_id=peer)
    except Exception as e:
        log.error(f"relay error: {e}")

# ===== post_init и Bootstrap =====
async def post_init(app: Application):
    await app.bot.delete_webhook(drop_pending_updates=True)
    await store.init()
    log.info("post_init done (webhook deleted, store initialized)")

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

    # кнопки анкеты
    app.add_handler(CallbackQueryHandler(on_gender, pattern=r"^gender:(M|F)$"))
    app.add_handler(CallbackQueryHandler(on_age, pattern=r"^age:(12-20|21-30|31-40)$"))

    # релеим всё, кроме команд/сервисных апдейтов
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.StatusUpdate, relay))

    log.info("Bot starting (run_polling)…")
    app.run_polling()

if __name__ == "__main__":
    main()