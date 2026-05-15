import os
import re
import json
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
# CONFIG
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
TIMER_VIDEO_URL = os.environ.get("TIMER_VIDEO_URL", "")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден")

games = {}

# =========================================================
# HELPERS
# =========================================================

def load_pack(pack_id: str):
    path = f"packs/{pack_id}.json"

    if not os.path.exists(path):
        return None

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def format_username(user):
    return f"@{user.username}" if user.username else user.first_name


def msk_to_utc(dt_msk: datetime) -> datetime:
    moscow = timezone(timedelta(hours=3))
    return dt_msk.replace(tzinfo=moscow).astimezone(timezone.utc)


def format_datetime_msk_multiline(dt_utc: datetime) -> str:
    msk = dt_utc + timedelta(hours=3)
    now_msk = datetime.now(timezone.utc) + timedelta(hours=3)

    if msk.date() == now_msk.date():
        return f"📅 Дата и время начала:\nсегодня, в {msk.strftime('%H:%M')}"

    return f"📅 Дата и время начала:\n{msk.strftime('%d.%m.%Y')} в {msk.strftime('%H:%M')}"


async def is_admin(update: Update, user_id: int):
    try:
        member = await update.effective_chat.get_member(user_id)
        return member.status in ("creator", "administrator")
    except Exception as e:
        print(f"Ошибка проверки админа: {e}")
        return False


# =========================================================
# GAME CLASS
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

        self.status = "registration"

        self.registered = {}

        self.current_question = 0

        self.answers = {}

        self.question_start_time = None

        self.reg_msg_id = None
        self.question_msg_id = None

        self.scheduled_start_utc = scheduled_start_utc

        self.pause_after_question = False

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

        self.answers[user_id] = (option_idx, now)

    def calculate_scores(self):

        q = self.pack["questions"][self.current_question]

        correct = q["correct"]

        for uid, (ans, ts) in self.answers.items():

            if ans != correct:
                continue

            points = 10

            delta = (ts - self.question_start_time).total_seconds()

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
# REGISTRATION
# =========================================================

async def open_registration(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int
):

    game = games.get(chat_id)

    if not game:
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "📝 Зарегистрироваться",
            callback_data="register"
        )],
        [InlineKeyboardButton(
            "🚀 Начать сейчас",
            callback_data="start_early"
        )]
    ])

    users_list = "пока никого"

    start_line = format_datetime_msk_multiline(
        game.scheduled_start_utc
    )

    text = (
        f"🎪 ОТКРЫТА РЕГИСТРАЦИЯ НА КВИЗ\n\n"
        f"✏️ Тема: {game.pack['title']}\n"
        f"{start_line}\n\n"
        f"👥 Участники:\n{users_list}"
    )

    kwargs = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": keyboard
    }

    if game.message_thread_id:
        kwargs["message_thread_id"] = game.message_thread_id

    msg = await context.bot.send_message(**kwargs)

    game.reg_msg_id = msg.id


async def update_registration_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int
):

    game = games.get(chat_id)

    if not game:
        return

    users_list = "\n".join(
        f"• {x['username']}"
        for x in game.registered.values()
    )

    if not users_list:
        users_list = "пока никого"

    start_line = format_datetime_msk_multiline(
        game.scheduled_start_utc
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "📝 Зарегистрироваться",
            callback_data="register"
        )],
        [InlineKeyboardButton(
            "🚀 Начать сейчас",
            callback_data="start_early"
        )]
    ])

    text = (
        f"🎪 ОТКРЫТА РЕГИСТРАЦИЯ НА КВИЗ\n\n"
        f"✏️ Тема: {game.pack['title']}\n"
        f"{start_line}\n\n"
        f"👥 Участники ({len(game.registered)}):\n"
        f"{users_list}"
    )

    try:

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=game.reg_msg_id,
            text=text,
            reply_markup=keyboard
        )

    except BadRequest as e:

        if "Message is not modified" not in str(e):
            print(e)

    except Exception as e:
        print(e)


# =========================================================
# COMMAND /quiz
# =========================================================

async def quiz_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = update.effective_chat.id

    user = update.effective_user

    message_thread_id = update.effective_message.message_thread_id

    if not await is_admin(update, user.id):

        await update.message.reply_text(
            "❌ Только администраторы могут запускать квиз."
        )

        return

    if chat_id in games:

        await update.message.reply_text(
            "❌ Квиз уже активен."
        )

        return

    full_text = update.message.text.strip()

    rest = full_text[5:].strip()

    parts = re.split(r'\s*\|\s*', rest)

    if len(parts) != 3:

        await update.message.reply_text(
            "/quiz 0001 | 2026-05-15 | 18:00"
        )

        return

    pack_id, date_str, time_str = parts

    pack = load_pack(pack_id)

    if not pack:

        await update.message.reply_text(
            "❌ Пакет не найден."
        )

        return

    try:

        dt_msk = datetime.strptime(
            f"{date_str} {time_str}",
            "%Y-%m-%d %H:%M"
        )

    except ValueError:

        await update.message.reply_text(
            "❌ Неверный формат даты."
        )

        return

    scheduled_start_utc = msk_to_utc(dt_msk)

    now_utc = datetime.now(timezone.utc)

    if scheduled_start_utc <= now_utc:

        await update.message.reply_text(
            "❌ Время старта уже прошло."
        )

        return

    game = Game(
        chat_id=chat_id,
        pack=pack,
        creator_id=user.id,
        message_thread_id=message_thread_id,
        scheduled_start_utc=scheduled_start_utc
    )

    games[chat_id] = game

    await open_registration(context, chat_id)

    delay = (
        scheduled_start_utc - now_utc
    ).total_seconds()

    context.job_queue.run_once(
        start_quiz_sequence,
        when=delay,
        data=chat_id
    )


# =========================================================
# CALLBACKS
# =========================================================

async def register_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    chat_id = update.effective_chat.id

    user = update.effective_user

    game = games.get(chat_id)

    if not game:
        return

    if game.status != "registration":

        await query.answer(
            "Регистрация закрыта.",
            show_alert=True
        )

        return

    if user.is_bot:
        return

    game.add_player(
        user.id,
        format_username(user)
    )

    await update_registration_message(
        context,
        chat_id
    )


async def start_early_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    chat_id = update.effective_chat.id

    user = update.effective_user

    game = games.get(chat_id)

    if not game:
        return

    if user.id != game.creator_id:

        await query.answer(
            "Только организатор.",
            show_alert=True
        )

        return

    await start_quiz_sequence(context, chat_id)


async def answer_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer("Ответ принят ✅")

    chat_id = update.effective_chat.id

    user = update.effective_user

    game = games.get(chat_id)

    if not game:
        return

    if game.status != "active":
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
# QUIZ FLOW
# =========================================================

async def start_quiz_sequence(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id=None
):

    if chat_id is None:
        chat_id = context.job.data

    game = games.get(chat_id)

    if not game:
        return

    if not game.registered:

        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Нет участников."
        )

        games.pop(chat_id, None)

        return

    game.status = "active"

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🚀 Квиз «{game.pack['title']}» начинается!"
        )
    )

    context.job_queue.run_once(
        start_question,
        when=5,
        data=chat_id
    )


async def start_question(
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = context.job.data

    game = games.get(chat_id)

    if not game:
        return

    if game.current_question >= len(game.pack["questions"]):

        await finish_quiz(context)

        return

    q = game.pack["questions"][game.current_question]

    buttons = [
        InlineKeyboardButton(
            opt,
            callback_data=f"ans_{i}"
        )
        for i, opt in enumerate(q["options"])
    ]

    keyboard = InlineKeyboardMarkup(
        [[btn] for btn in buttons]
    )

    text = (
        f"❓ Вопрос "
        f"{game.current_question + 1}/"
        f"{len(game.pack['questions'])}\n\n"
        f"{q['text']}"
    )

    if TIMER_VIDEO_URL:

        try:

            await context.bot.send_video(
                chat_id=chat_id,
                video=TIMER_VIDEO_URL,
                caption="⏳ У вас 20 секунд"
            )

        except Exception as e:
            print(e)

    if q.get("image"):

        kwargs = {
            "chat_id": chat_id,
            "photo": q["image"],
            "caption": text,
            "reply_markup": keyboard
        }

        if game.message_thread_id:
            kwargs["message_thread_id"] = game.message_thread_id

        msg = await context.bot.send_photo(**kwargs)

    else:

        kwargs = {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": keyboard
        }

        if game.message_thread_id:
            kwargs["message_thread_id"] = game.message_thread_id

        msg = await context.bot.send_message(**kwargs)

    game.question_msg_id = msg.id

    game.question_start_time = datetime.now(
        timezone.utc
    )

    game.answers.clear()

    try:

        await context.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=msg.id
        )

    except Exception as e:
        print(e)

    context.job_queue.run_once(
        end_question,
        when=20,
        data=chat_id
    )


async def end_question(
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = context.job.data

    game = games.get(chat_id)

    if not game:
        return

    q = game.pack["questions"][game.current_question]

    game.calculate_scores()

    total_answers = len(game.answers)

    counts = [0] * len(q["options"])

    for _, (ans_idx, _) in game.answers.items():

        counts[ans_idx] += 1

    percents = []

    for c in counts:

        if total_answers == 0:
            percents.append(0)
        else:
            percents.append(
                round(c / total_answers * 100, 1)
            )

    stats_lines = []

    for i, opt in enumerate(q["options"]):

        line = f"{opt}: {percents[i]}%"

        if i == q["correct"]:
            line += " ✅"

        stats_lines.append(line)

    stats_text = "\n".join(stats_lines)

    final_text = (
        f"📊 Результаты вопроса\n\n"
        f"{stats_text}\n\n"
        f"✅ Правильный ответ:\n"
        f"{q['options'][q['correct']]}"
    )

    try:

        await context.bot.unpin_chat_message(
            chat_id=chat_id,
            message_id=game.question_msg_id
        )

    except Exception as e:
        print(e)

    try:

        if q.get("image"):

            await context.bot.edit_message_caption(
                chat_id=chat_id,
                message_id=game.question_msg_id,
                caption=final_text
            )

        else:

            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=game.question_msg_id,
                text=final_text
            )

    except Exception as e:
        print(e)

    leaderboard = game.get_leaderboard()

    lines = []

    for i, (_, data) in enumerate(leaderboard):

        lines.append(
            f"{i+1}. "
            f"{data['username']} — "
            f"{data['score']} очк."
        )

    await context.bot.send_message(
        chat_id=chat_id,
        text="🏆 Рейтинг:\n\n" + "\n".join(lines)
    )

    game.current_question += 1

    if game.current_question >= len(game.pack["questions"]):

        context.job_queue.run_once(
            finish_quiz,
            when=5,
            data=chat_id
        )

        return

    context.job_queue.run_once(
        start_question,
        when=5,
        data=chat_id
    )


async def finish_quiz(
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = context.job.data

    game = games.pop(chat_id, None)

    if not game:
        return

    leaderboard = game.get_leaderboard()

    lines = []

    for i, (_, data) in enumerate(leaderboard):

        medal = ""

        if i == 0:
            medal = "🥇"
        elif i == 1:
            medal = "🥈"
        elif i == 2:
            medal = "🥉"

        lines.append(
            f"{medal} "
            f"{i+1}. "
            f"{data['username']} — "
            f"{data['score']} очк."
        )

    await context.bot.send_message(
        chat_id=chat_id,
        text="🏁 ИТОГИ КВИЗА\n\n" + "\n".join(lines)
    )


# =========================================================
# COMMANDS
# =========================================================

async def rules_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "🎯 Правила квиза:\n\n"
        "— Быстрее отвечаешь → больше очков\n"
        "— Только первый ответ засчитывается\n"
        "— Побеждает игрок с максимумом очков"
    )


async def abort_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = update.effective_chat.id

    game = games.get(chat_id)

    if not game:
        return

    for job in context.job_queue.jobs():

        if job.data == chat_id:
            job.schedule_removal()

    games.pop(chat_id, None)

    await update.message.reply_text(
        "❌ Квиз остановлен."
    )


# =========================================================
# MAIN
# =========================================================

def main():

    app = Application.builder().token(
        BOT_TOKEN
    ).build()

    app.add_handler(
        CommandHandler("quiz", quiz_command)
    )

    app.add_handler(
        CommandHandler("rules", rules_command)
    )

    app.add_handler(
        CommandHandler("abort", abort_quiz)
    )

    app.add_handler(
        CallbackQueryHandler(
            register_callback,
            pattern="register"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            start_early_callback,
            pattern="start_early"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            answer_callback,
            pattern=r"ans_\d+"
        )
    )

    print("🚀 Quiz Bot запущен")

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
