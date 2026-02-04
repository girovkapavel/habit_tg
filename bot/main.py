import os
import re
import sqlite3
import logging
from datetime import datetime, timedelta

import matplotlib.pyplot as plt
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# ================== CONFIG ==================

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

DATA_DIR = "data"
DB_PATH = f"{DATA_DIR}/habits.db"
os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

request = HTTPXRequest(read_timeout=30, write_timeout=30)

CHOOSING, ADDING, REMOVING, EDITING, EDITING_VALUE = range(5)

# ================== DATABASE ==================

def get_db():
    return sqlite3.connect(DB_PATH)

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS habits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT
        );

        CREATE TABLE IF NOT EXISTS habit_logs (
            user_id INTEGER,
            habit_id INTEGER,
            date TEXT,
            value INTEGER
        );

        CREATE TABLE IF NOT EXISTS mood (
            user_id INTEGER,
            date TEXT,
            value INTEGER
        );

        CREATE TABLE IF NOT EXISTS reminders (
            user_id INTEGER PRIMARY KEY,
            time TEXT
        );
        """)

# ================== BOT ==================

class HabitBot:
    def __init__(self):
        init_db()
        self.app = ApplicationBuilder().token(TOKEN).request(request).build()
        self._handlers()
        self._jobs()

    # ---------- HELPERS ----------

    def user_habits(self, uid):
        with get_db() as db:
            return list(db.execute(
                "SELECT id, name FROM habits WHERE user_id=?",
                (uid,),
            ))

    async def send(self, update: Update, text, reply_markup=None, edit=False):
        if update.callback_query:
            if edit:
                await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
            else:
                await update.callback_query.message.reply_text(text, reply_markup=reply_markup)
        elif update.message:
            await update.message.reply_text(text, reply_markup=reply_markup)

    # ---------- HANDLERS ----------

    def _handlers(self):
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("menu", self.menu))
        self.app.add_handler(CommandHandler("week", self.week))
        self.app.add_handler(CommandHandler("month", self.month))
        self.app.add_handler(CommandHandler("calendar", self.calendar))
        self.app.add_handler(CommandHandler("mood", self.mood))
        self.app.add_handler(CommandHandler("mood_progress", self.mood_progress))
        self.app.add_handler(CommandHandler("remind", self.remind))

        self.app.add_handler(CallbackQueryHandler(self.toggle_habit, pattern="^h_"))
        self.app.add_handler(CallbackQueryHandler(self.save_mood, pattern="^mood_"))
        self.app.add_handler(CallbackQueryHandler(self.calendar_pick, pattern="^cal_"))

        self.app.add_handler(
            ConversationHandler(
                entry_points=[CommandHandler("customize", self.customize)],
                states={
                    CHOOSING: [CallbackQueryHandler(self.customize_action)],
                    ADDING: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_habit)],
                    REMOVING: [CallbackQueryHandler(self.remove_habit)],
                    EDITING: [CallbackQueryHandler(self.edit_select)],
                    EDITING_VALUE: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_save)
                    ],
                },
                fallbacks=[],
            )
        )

    # ---------- JOBS ----------

    def _jobs(self):
        self.app.job_queue.run_repeating(self.send_reminders, 60, first=10)

    async def send_reminders(self, ctx):
        now = datetime.now().strftime("%H:%M")
        with get_db() as db:
            for user_id, t in db.execute("SELECT user_id, time FROM reminders"):
                if t == now:
                    await ctx.bot.send_message(user_id, "⏰ Time to mark habits! /start")

    # ---------- CORE ----------

    async def start(self, update: Update, ctx):
        uid = update.effective_user.id
        today = datetime.now().strftime("%Y-%m-%d")

        with get_db() as db:
            habits = self.user_habits(uid)
            if not habits:
                for h in ["🏃 Sport", "💧 Water", "📚 Study"]:
                    db.execute(
                        "INSERT INTO habits (user_id, name) VALUES (?,?)",
                        (uid, h),
                    )
                db.commit()
                habits = self.user_habits(uid)

            keyboard = []
            for hid, name in habits:
                row = db.execute(
                    "SELECT value FROM habit_logs WHERE user_id=? AND habit_id=? AND date=?",
                    (uid, hid, today),
                ).fetchone()
                mark = "✅" if row and row[0] else "⬜"
                keyboard.append(
                    [InlineKeyboardButton(f"{mark} {name}", callback_data=f"h_{hid}")]
                )

        await self.send(update, "Your habits today:", InlineKeyboardMarkup(keyboard), edit=bool(update.callback_query))

    async def toggle_habit(self, update: Update, ctx):
        q = update.callback_query
        await q.answer()

        uid = q.from_user.id
        hid = int(q.data.split("_")[1])
        today = datetime.now().strftime("%Y-%m-%d")

        with get_db() as db:
            row = db.execute(
                "SELECT value FROM habit_logs WHERE user_id=? AND habit_id=? AND date=?",
                (uid, hid, today),
            ).fetchone()

            if row:
                db.execute(
                    "UPDATE habit_logs SET value=? WHERE user_id=? AND habit_id=? AND date=?",
                    (1 - row[0], uid, hid, today),
                )
            else:
                db.execute(
                    "INSERT INTO habit_logs VALUES (?,?,?,1)",
                    (uid, hid, today),
                )
            db.commit()

        await self.start(update, ctx)

    # ---------- CUSTOMIZE ----------

    async def customize(self, update: Update, ctx):
        kb = [
            [InlineKeyboardButton("➕ Add", callback_data="add")],
            [InlineKeyboardButton("✏️ Edit", callback_data="edit")],
            [InlineKeyboardButton("➖ Remove", callback_data="remove")],
        ]
        await update.message.reply_text("Customize habits:", reply_markup=InlineKeyboardMarkup(kb))
        return CHOOSING

    async def customize_action(self, update: Update, ctx):
        q = update.callback_query
        await q.answer()

        action = q.data
        habits = self.user_habits(q.from_user.id)

        if action == "add":
            await q.edit_message_text("Send new habit name:")
            return ADDING

        if not habits:
            await q.edit_message_text("No habits yet")
            return ConversationHandler.END

        kb = [
            [InlineKeyboardButton(name, callback_data=f"{action}_{hid}")]
            for hid, name in habits
        ]

        await q.edit_message_text("Choose habit:", reply_markup=InlineKeyboardMarkup(kb))
        return REMOVING if action == "remove" else EDITING

    async def add_habit(self, update: Update, ctx):
        with get_db() as db:
            db.execute(
                "INSERT INTO habits (user_id, name) VALUES (?,?)",
                (update.effective_user.id, update.message.text.strip()),
            )
            db.commit()

        await update.message.reply_text("Habit added ✅")
        return ConversationHandler.END

    async def remove_habit(self, update: Update, ctx):
        q = update.callback_query
        await q.answer()

        hid = int(q.data.split("_")[1])
        with get_db() as db:
            db.execute("DELETE FROM habits WHERE id=?", (hid,))
            db.commit()

        await q.edit_message_text("Habit removed ❌")
        return ConversationHandler.END

    async def edit_select(self, update: Update, ctx):
        q = update.callback_query
        await q.answer()

        ctx.user_data["edit_habit_id"] = int(q.data.split("_")[1])
        await q.edit_message_text("Send new habit name:")
        return EDITING_VALUE

    async def edit_save(self, update: Update, ctx):
        hid = ctx.user_data.get("edit_habit_id")
        with get_db() as db:
            db.execute(
                "UPDATE habits SET name=? WHERE id=?",
                (update.message.text.strip(), hid),
            )
            db.commit()

        await update.message.reply_text("Habit updated ✨")
        return ConversationHandler.END

    # ---------- SIMPLE ----------

    async def menu(self, update: Update, ctx):
        await self.send(update, "/start\n/customize\n/week\n/month\n/calendar\n/mood\n/mood_progress\n/remind HH:MM")

    async def week(self, update: Update, ctx):
        await self.send(update, "Weekly stats coming soon ✅")

    async def month(self, update: Update, ctx):
        await self.send(update, "Monthly stats coming soon ✅")

    async def calendar(self, update: Update, ctx):
        today = datetime.now()
        kb = []
        for i in range(7):
            d = today - timedelta(days=i)
            kb.append([InlineKeyboardButton(d.strftime("%d %b"), callback_data=f"cal_{d:%Y-%m-%d}")])
        await self.send(update, "Select date:", InlineKeyboardMarkup(kb))

    async def calendar_pick(self, update: Update, ctx):
        q = update.callback_query
        await q.answer()
        await q.edit_message_text(f"Selected date: {q.data[4:]}")

    async def mood(self, update: Update, ctx):
        kb = [[InlineKeyboardButton(str(i), callback_data=f"mood_{i}") for i in range(11)]]
        await self.send(update, "Your mood:", InlineKeyboardMarkup(kb))

    async def save_mood(self, update: Update, ctx):
        q = update.callback_query
        await q.answer()
        val = int(q.data.split("_")[1])
        uid = q.from_user.id
        today = datetime.now().strftime("%Y-%m-%d")

        with get_db() as db:
            db.execute("REPLACE INTO mood VALUES (?,?,?)", (uid, today, val))
            db.commit()

        await q.edit_message_text(f"Mood saved: {val}/10")

    async def mood_progress(self, update: Update, ctx):
        uid = update.effective_user.id
        with get_db() as db:
            rows = db.execute("SELECT date, value FROM mood WHERE user_id=?", (uid,)).fetchall()

        if not rows:
            await self.send(update, "No mood data yet 😶")
            return

        dates = [r[0] for r in rows]
        values = [r[1] for r in rows]

        plt.figure(figsize=(6, 3))
        plt.plot(dates, values, marker="o")
        plt.ylim(0, 10)
        plt.xticks(rotation=45)
        plt.tight_layout()

        path = f"{DATA_DIR}/mood_{uid}.png"
        plt.savefig(path)
        plt.close()

        await update.message.reply_photo(open(path, "rb"))

    async def remind(self, update: Update, ctx):
        uid = update.effective_user.id
        if not ctx.args:
            await self.send(update, "/remind HH:MM or /remind off")
            return

        with get_db() as db:
            if ctx.args[0] == "off":
                db.execute("DELETE FROM reminders WHERE user_id=?", (uid,))
                db.commit()
                await self.send(update, "Reminder off")
                return

            if not re.match(r"\d{2}:\d{2}", ctx.args[0]):
                await self.send(update, "Format HH:MM")
                return

            db.execute("REPLACE INTO reminders VALUES (?,?)", (uid, ctx.args[0]))
            db.commit()

        await self.send(update, "Reminder set ⏰")

    def run(self):
        logger.info("Bot started")
        self.app.run_polling()

# ================== START ==================

if __name__ == "__main__":
    HabitBot().run()
