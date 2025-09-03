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

# ===== In-memory store (без внешних зависимостей) =====
class MemStore:
    def __init__(self):
        self.profiles: Dict[int, dict] = {}     # uid -> {"gender": "M/F", "age_range": "..."}
        self.pairs: Dict[int, int] = {}         # uid -> peer_uid
        self.queue: list[int] = []              # простая очередь

    async def get_profile(self, uid: int) -> dict:
        return self.profiles.get(uid, {"gender": None, "age_range": None})

    async def save_profile(self, uid: int, data: dict):
        self.profiles[uid] = data

    async def reset_profile(self, uid: int):
        self.profiles.pop(uid, None)

    async def get_peer(self, uid: int) -> Optional[int]:
        return self.pairs.get(uid)

    async def set_pair(self, a: int, b: int):
        self.pairs[a] = b
        self.pairs[b] = a

    async def clear_pair(self, uid: int):
        peer = self.pairs.pop(uid, None)
        if peer is not None:
            self.pairs.pop(peer, None)

    async def push_queue(self, uid: int):
        if uid not in self.queue:
            self.queue.append(uid)

    async def pop_queue(self) -> Optional[int]:
        if self.queue:
            return self.queue.pop(0)
        return None

    async def remove_from_queue(self, uid: int):
        if uid in self.queue:
            self.queue.remove(uid)

store = MemStore()

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

# ===== анкета =====
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
    await q.edit_message_text("Возраст сохранён ✅\n\nТвой профиль:\n" + profile_str(p))
    await q.message.reply_text(menu_text("Отлично!"))

# ===== команды =====
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

async def try_match(uid: int) -> Optional[int]:
    # простейший матчинг: пара из очереди, кроме самого себя
    while True:
        other = await store.pop_queue()
        if other is None:
            return None
        if other == uid:
            # вернём себя обратно и выйдем
            await store.push_queue(other)
            return None
        if await store.get_peer(other):
            # уже занят — ищем следующего
            continue
        return other

async def announce_pair(context: ContextTypes.DEFAULT_TYPE, a: int, b: int):
    text = "Собеседник найден 👤\n\n/next — искать нового собеседника\n/stop — закончить диалог"
    await context.bot.send_message(chat_id=a, text=text)
    await context.bot.send_message(chat_id=b, text=text)

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = await store.get_profile(uid)
    if not (p.get("gender") and p.get("age_range")):
        await update.message.reply_text("Сначала заполни профиль: /start")
        return

    if await store.get_peer(uid):
        await update.message.reply_text("Ты уже в диалоге.\n/next — новый собеседник\n/stop — закончить диалог")
        return

    peer = await try_match(uid)
    if peer:
        await store.set_pair(uid, peer)
        await announce_pair(context, uid, peer)
    else:
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
    await cmd_stop(update, context)
    await cmd_search(update, context)

# ===== релеинг сообщений =====
async def relay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    uid = update.effective_user.id
    peer = await store.get_peer(uid)
    if not peer:
        return
    try:
        await update.message.copy(chat_id=peer)
    except Exception as e:
        log.error(f"relay error: {e}")

# ===== запуск =====
def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is not set")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("reset_profile", cmd_reset_profile))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("next", cmd_next))

    app.add_handler(CallbackQueryHandler(on_gender, pattern=r"^gender:(M|F)$"))
    app.add_handler(CallbackQueryHandler(on_age, pattern=r"^age:(12-20|21-30|31-40)$"))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.StatusUpdate, relay))

    app.run_polling()

if __name__ == "__main__":
    main()