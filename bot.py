import os
import re
import json
import asyncio
import logging

from datetime import datetime, timezone, timedelta
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)

from telegram.error import BadRequest

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# =========================================================
# ENV
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")

TIMER_VIDEO_URL = os.environ.get(
    "TIMER_VIDEO_URL",
    ""
)

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found")

# =========================================================
# STORAGE
# =========================================================

games = {}

# =========================================================
# GAME
# =========================================================

class Game:

    def __init__(
        self,
        chat_id,
        pack,
        creator_id,
        message_thread_id=None,
        scheduled_start_utc: Optional[datetime] = None
    ):

        self.chat_id = chat_id
        self.pack = pack
        self.creator_id = creator_id
        self.message_thread_id = message_thread_id
        self.scheduled_start_utc = scheduled_start_utc

        self.status = "registration"

        self.registered = {}

        self.current_question = 0

        self.answers = {}

        self.question_start_time = None

        self.reg_msg_id = None

        self.question_msg_id = None

    def add_player(self, user_id, username):

        if user_id not in self.registered:

            self.registered[user_id] = {
                "username": username,
                "score": 0
            }

    def record_answer(self, user_id, option_idx):

        if user_id in self.answers:
            return

        if self.status != "active":
            return

        if user_id not in self.registered:
            return

        now = datetime.now(timezone.utc)

        self.answers[user_id] = (
            option_idx,
            now
        )

    def calculate_scores(self):

        q = self.pack["questions"][self.current_question]

        correct = q["correct"]

        for uid, (ans, ts) in self.answers.items():

            if ans != correct:
                continue

            points = 10

            delta = (
                ts - self.question_start_time
            ).total_seconds()

            if delta <= 5:
                points += 5

            elif delta <= 10:
                points += 4

            elif delta <= 13:
                points += 3

            elif delta <= 16:
                points += 2

            elif delta <= 19:
                points += 1

            self.registered[uid]["score"] += points

    def get_leaderboard(self):

        return sorted(
            self.registered.items(),
            key=lambda x: (
                -x[1]["score"],
                x[1]["username"].lower()
            )
        )

# =========================================================
# HELPERS
# =========================================================


def load_pack(pack_id):

    path = f"packs/{pack_id}.json"

    if not os.path.exists(path):
        return None

    with open(
        path,
        "r",
        encoding="utf-8"
    ) as f:

        return json.load(f)


def format_username(user):

    if user.username:
        return f"@{user.username}"

    return user.first_name


async def is_admin(update, user_id):

    try:

        member = await update.effective_chat.get_member(user_id)

        return member.status in (
            "administrator",
            "creator"
        )

    except Exception as e:

        logger.error(e)

        return False


# =========================================================
# UPDATE REGISTRATION
# =========================================================


async def update_registration_message(context, chat_id):

    game = games.get(chat_id)

    if not game:
        return

    users = "\n".join(
        f"• {x['username']}"
        for x in game.registered.values()
    )

    if not users:
        users = "пока никого"

    text = (
        f"🎪 РЕГИСТРАЦИЯ НА КВИЗ\n\n"
        f"🎯 {game.pack['title']}\n\n"
        f"👥 Участники ({len(game.registered)}):\n"
        f"{users}"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📝 Зарегистрироваться",
                callback_data="register"
            )
        ],
        [
            InlineKeyboardButton(
                "🚀 Начать сейчас",
                callback_data="start_now"
            )
        ]
    ])

    try:

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=game.reg_msg_id,
            text=text,
            reply_markup=keyboard
        )

    except BadRequest as e:

        if "Message is not modified" not in str(e):
            logger.error(e)


# =========================================================
# QUIZ COMMAND
# =========================================================


async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    print("QUIZ COMMAND RECEIVED")

    chat_id = update.effective_chat.id

    user = update.effective_user

    if not await is_admin(update, user.id):

        await update.message.reply_text(
            "❌ Только администратор может запускать квиз."
        )

        return

    if chat_id in games:

        await update.message.reply_text(
            "❌ Квиз уже активен."
        )

        return

    full_text = update.message.text.strip()

    args = full_text[5:].strip()

    parts = re.split(r"\s*\|\s*", args)

    if len(parts) != 3:

        await update.message.reply_text(
            "/quiz 0001 | 2026-05-15 | 21:00"
        )

        return

    pack_id, _, _ = parts

    pack = load_pack(pack_id)

    if not pack:

        await update.message.reply_text(
            "❌ Пакет не найден."
        )

        return

    game = Game(
        chat_id=chat_id,
        pack=pack,
        creator_id=user.id
    )

    games[chat_id] = game

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📝 Зарегистрироваться",
                callback_data="register"
            )
        ],
        [
            InlineKeyboardButton(
                "🚀 Начать сейчас",
                callback_data="start_now"
            )
        ]
    ])

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🎪 РЕГИСТРАЦИЯ НА КВИЗ\n\n"
            f"🎯 {pack['title']}\n\n"
            f"👥 Участники:\n"
            f"пока никого"
        ),
        reply_markup=keyboard
    )

    game.reg_msg_id = msg.id


# =========================================================
# CALLBACKS
# =========================================================


async def register_callback(update, context):

    query = update.callback_query

    await query.answer()

    chat_id = update.effective_chat.id

    user = update.effective_user

    game = games.get(chat_id)

    if not game:
        return

    game.add_player(
        user.id,
        format_username(user)
    )

    await update_registration_message(
        context,
        chat_id
    )


async def start_now_callback(update, context):

    query = update.callback_query

    await query.answer()

    chat_id = update.effective_chat.id

    game = games.get(chat_id)

    if not game:
        return

    game.status = "active"

    await context.bot.send_message(
        chat_id=chat_id,
        text="🚀 Квиз начинается!"
    )

    context.job_queue.run_once(
        start_question,
        when=3,
        data=chat_id
    )


async def answer_callback(update, context):

    query = update.callback_query

    await query.answer("✅ Ответ принят")

    chat_id = update.effective_chat.id

    user = update.effective_user

    game = games.get(chat_id)

    if not game:
        return

    try:

        option_idx = int(
            query.data.split("_")[1]
        )

    except Exception:
        return

    game.record_answer(
        user.id,
        option_idx
    )


# =========================================================
# QUESTIONS
# =========================================================


async def start_question(context):

    chat_id = context.job.data

    game = games.get(chat_id)

    if not game:
        return

    if game.current_question >= len(game.pack["questions"]):

        await finish_quiz(context)

        return

    q = game.pack["questions"][game.current_question]

    buttons = [
        [
            InlineKeyboardButton(
                option,
                callback_data=f"ans_{i}"
            )
        ]
        for i, option in enumerate(q["options"])
    ]

    keyboard = InlineKeyboardMarkup(buttons)

    text = (
        f"❓ Вопрос {game.current_question + 1}\n\n"
        f"{q['text']}"
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=keyboard
    )

    game.question_start_time = datetime.now(timezone.utc)

    game.answers.clear()

    context.job_queue.run_once(
        end_question,
        when=20,
        data=chat_id
    )


async def end_question(context):

    chat_id = context.job.data

    game = games.get(chat_id)

    if not game:
        return

    q = game.pack["questions"][game.current_question]

    game.calculate_scores()

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ Правильный ответ:\n"
            f"{q['options'][q['correct']]}"
        )
    )

    leaderboard = game.get_leaderboard()

    rating = []

    for i, (_, data) in enumerate(leaderboard):

        rating.append(
            f"{i+1}. {data['username']} — {data['score']}"
        )

    await context.bot.send_message(
        chat_id=chat_id,
        text="🏆 Рейтинг\n\n" + "\n".join(rating)
    )

    game.current_question += 1

    context.job_queue.run_once(
        start_question,
        when=5,
        data=chat_id
    )


async def finish_quiz(context):

    chat_id = context.job.data

    game = games.pop(chat_id, None)

    if not game:
        return

    leaderboard = game.get_leaderboard()

    text = "🏁 ИТОГИ\n\n"

    for i, (_, data) in enumerate(leaderboard):

        text += (
            f"{i+1}. {data['username']} — {data['score']}\n"
        )

    await context.bot.send_message(
        chat_id=chat_id,
        text=text
    )


# =========================================================
# RULES
# =========================================================


async def rules_command(update, context):

    await update.message.reply_text(
        "🎯 Правила квиза работают"
    )


# =========================================================
# MAIN
# =========================================================


def main():

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "rules",
            rules_command
        )
    )

    app.add_handler(
        CommandHandler(
            "quiz",
            quiz_command
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            register_callback,
            pattern="register"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            start_now_callback,
            pattern="start_now"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            answer_callback,
            pattern=r"ans_\d+"
        )
    )

    async def remove_webhook():
        await app.bot.delete_webhook(
            drop_pending_updates=True
        )

    asyncio.run(remove_webhook())

    print("🚀 Quiz Bot polling started")

    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()

WEBHOOK_URL полностью удалить.
