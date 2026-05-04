import asyncio
import json
import os
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# ==================== Хранилище игр ====================
games = {}

class Game:
    def __init__(self, chat_id, pack):
        self.chat_id = chat_id
        self.pack = pack
        self.status = "registration"  # registration | active | finished
        self.registered = {}   # user_id: {"name": str, "score": int}
        self.current_question = 0
        self.answers = {}      # user_id: (option_index, timestamp)
        self.question_start_time = None
        self.reg_msg_id = None
        self.question_msg_id = None
        self.jobs = {}

    def add_player(self, user_id, name):
        if user_id not in self.registered:
            self.registered[user_id] = {"name": name, "score": 0}

    def record_answer(self, user_id, option_idx):
        if self.status == "active" and user_id in self.registered:
            now = datetime.utcnow()
            self.answers[user_id] = (option_idx, now)

    def calculate_scores(self):
        q = self.pack["questions"][self.current_question]
        correct = q["correct"]
        for uid, (ans, ts) in self.answers.items():
            if ans == correct:
                points = 10
                delta = (ts - self.question_start_time).total_seconds()
                if delta <= 10:
                    points += 5
                elif delta <= 20:
                    points += 4
                elif delta <= 30:
                    points += 3
                elif delta <= 40:
                    points += 2
                elif delta <= 50:
                    points += 1
                self.registered[uid]["score"] += points

    def get_leaderboard(self):
        return sorted(
            self.registered.items(),
            key=lambda x: (-x[1]["score"], x[1]["name"].lower())
        )

# ==================== Загрузка пакета ====================
def load_pack(pack_id: str):
    path = f"packs/{pack_id}.json"
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# ==================== Проверка прав ====================
async def is_admin(update: Update, user_id: int) -> bool:
    try:
        member = await update.effective_chat.get_member(user_id)
        return member.status in ("creator", "administrator")
    except:
        return False

# ==================== Команда /quiz ====================
async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if not await is_admin(update, user.id):
        await update.message.reply_text("❌ Только администраторы могут запускать викторину.")
        return

    if chat_id in games and games[chat_id].status != "finished":
        await update.message.reply_text("❌ Викторина уже идёт в этой группе.")
        return

    args = context.args
    if not args or len(args[0]) != 4 or not args[0].isdigit():
        await update.message.reply_text("Используйте: /quiz XXXX (4 цифры)")
        return

    pack_id = args[0]
    pack = load_pack(pack_id)
    if not pack:
        await update.message.reply_text(f"❌ Пакет {pack_id} не найден в базе.")
        return

    game = Game(chat_id, pack)
    games[chat_id] = game

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📝 Зарегистрироваться", callback_data="register")
    ]])
    msg = await update.message.reply_text(
        f"🎉 Добро пожаловать на викторину «{pack['title']}»!\n\n"
        f"📋 Правила:\n"
        f"• У вас 5 минут на регистрацию\n"
        f"• 10 баллов за правильный ответ\n"
        f"• Бонус за скорость: +5 (0-10с), +4 (11-20с), +3 (21-30с), +2 (31-40с), +1 (41-50с)\n\n"
        f"⏳ Регистрация открыта! Нажмите кнопку ниже.\n\n"
        f"Участники:",
        reply_markup=keyboard
    )
    game.reg_msg_id = msg.id

    context.job_queue.run_once(
        end_registration,
        when=300,
        chat_id=chat_id,
        data=chat_id
    )
    game.jobs["reg_timer"] = True

# ==================== Регистрация ====================
async def register_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    user = update.effective_user

    game = games.get(chat_id)
    if not game or game.status != "registration":
        await query.answer("Регистрация закрыта.", show_alert=True)
        return

    if user.is_bot:
        await query.answer("Боты не участвуют.", show_alert=True)
        return

    name = user.full_name or user.username or str(user.id)
    game.add_player(user.id, name)

    users_list = "\n".join(
        f"• {p['name']}" for uid, p in game.registered.items()
    )
    text = (
        f"🎉 Регистрация на викторину «{game.pack['title']}»\n"
        f"⏳ Осталось меньше 5 минут\n\n"
        f"Зарегистрировано: {len(game.registered)}\n{users_list}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📝 Зарегистрироваться", callback_data="register")
    ]])
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=game.reg_msg_id,
            text=text,
            reply_markup=keyboard
        )
    except:
        pass

# ==================== Завершение регистрации ====================
async def end_registration(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    game = games.get(chat_id)
    if not game or game.status != "registration":
        return

    game.status = "active"

    users_list = "\n".join(
        f"• {p['name']}" for uid, p in game.registered.items()
    )
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=game.reg_msg_id,
        text=(
            f"🎉 Регистрация завершена. Начинаем викторину «{game.pack['title']}»!\n"
            f"Участников: {len(game.registered)}\n{users_list}"
        )
    )
    context.job_queue.run_once(
        start_question,
        when=5,
        chat_id=chat_id,
        data=chat_id
    )

# ==================== Старт вопроса ====================
async def start_question(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    game = games.get(chat_id)
    if not game or game.status != "active":
        return

    if game.current_question >= len(game.pack["questions"]):
        await finish_quiz(context)
        return

    q = game.pack["questions"][game.current_question]
    buttons = [
        InlineKeyboardButton(opt, callback_data=f"ans_{i}")
        for i, opt in enumerate(q["options"])
    ]
    keyboard = InlineKeyboardMarkup([[btn] for btn in buttons])

    question_text = f"❓ Вопрос {game.current_question + 1}/{len(game.pack['questions'])}\n\n{q['text']}"

    if q.get("image"):
        msg = await context.bot.send_photo(
            chat_id,
            photo=q["image"],
            caption=question_text,
            reply_markup=keyboard
        )
    else:
        msg = await context.bot.send_message(
            chat_id,
            text=question_text,
            reply_markup=keyboard
        )

    game.question_msg_id = msg.id
    game.question_start_time = datetime.utcnow()
    game.answers.clear()

    context.job_queue.run_once(
        end_question,
        when=60,
        chat_id=chat_id,
        data=chat_id
    )

# ==================== Завершение вопроса ====================
async def end_question(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    game = games.get(chat_id)
    if not game or game.status != "active":
        return

    q = game.pack["questions"][game.current_question]
    game.calculate_scores()

    total_answers = len(game.answers)
    counts = [0] * len(q["options"])
    for uid, (ans_idx, _) in game.answers.items():
        if 0 <= ans_idx < len(counts):
            counts[ans_idx] += 1

    percents = []
    for cnt in counts:
        perc = (cnt / total_answers * 100) if total_answers > 0 else 0
        percents.append(perc)

    stats_lines = []
    for idx, (opt, perc) in enumerate(zip(q["options"], percents)):
        marker = " ✅" if idx == q["correct"] else ""
        stats_lines.append(f"{opt}: {perc:.1f}%{marker}")
    stats_text = "📊 Статистика ответов:\n" + "\n".join(stats_lines)

    question_text = f"❓ Вопрос {game.current_question + 1}/{len(game.pack['questions'])}\n\n{q['text']}\n\n{stats_text}"

    try:
        if q.get("image"):
            await context.bot.edit_message_caption(
                chat_id=chat_id,
                message_id=game.question_msg_id,
                caption=question_text
            )
        else:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=game.question_msg_id,
                text=question_text
            )
    except:
        pass

    leaderboard = game.get_leaderboard()
    rating_lines = []
    for i, (uid, data) in enumerate(leaderboard, 1):
        rating_lines.append(f"{i}. {data['name']} — {data['score']} очк.")
    rating_text = "🏆 Текущий рейтинг:\n" + "\n".join(rating_lines)
    await context.bot.send_message(chat_id, text=rating_text)

    game.current_question += 1
    if game.current_question < len(game.pack["questions"]):
        context.job_queue.run_once(
            start_question,
            when=5,
            chat_id=chat_id,
            data=chat_id
        )
    else:
        game.status = "finished"
        await context.bot.send_message(
            chat_id,
            "🎉 Ура, викторина закончена! Подводим результаты..."
        )
        context.job_queue.run_once(
            finish_quiz,
            when=5,
            chat_id=chat_id,
            data=chat_id
        )

# ==================== Финальная таблица ====================
async def finish_quiz(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    game = games.pop(chat_id, None)
    if not game:
        return

    leaderboard = game.get_leaderboard()
    if not leaderboard:
        await context.bot.send_message(chat_id, "Нет участников.")
        return

    final_lines = []
    rank = 1
    i = 0
    while i < len(leaderboard):
        same_score_players = []
        score = leaderboard[i][1]["score"]
        while i < len(leaderboard) and leaderboard[i][1]["score"] == score:
            same_score_players.append(leaderboard[i])
            i += 1
        for uid, data in same_score_players:
            medal = ""
            if rank == 1:
                medal = "🥇"
            elif rank == 2:
                medal = "🥈"
            elif rank == 3:
                medal = "🥉"
            final_lines.append(f"{medal} {rank} место. {data['name']} — {data['score']} очк.")
        rank += len(same_score_players)

    table = "🏁 Итоговое положение:\n\n" + "\n".join(final_lines)
    await context.bot.send_message(chat_id, text=table)

# ==================== Обработка ответов ====================
async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Ответ принят ✅")
    user = update.effective_user
    chat_id = update.effective_chat.id

    game = games.get(chat_id)
    if not game or game.status != "active":
        return

    data = query.data
    try:
        option_idx = int(data.split("_")[1])
    except:
        return

    game.record_answer(user.id, option_idx)

# ==================== Запуск ====================
def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise ValueError("❌ Не задан BOT_TOKEN в переменных окружения")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("quiz", quiz_command))
    app.add_handler(CallbackQueryHandler(register_callback, pattern="register"))
    app.add_handler(CallbackQueryHandler(answer_callback, pattern=r"ans_\d+"))

    webhook_url = os.environ.get("WEBHOOK_URL")
    port = int(os.environ.get("PORT", "8443"))

    if webhook_url:
        print(f"🚀 Запуск webhook на {webhook_url}/webhook")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=f"{webhook_url}/webhook",
            drop_pending_updates=True
        )
    else:
        print("🚀 Запуск polling")
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
