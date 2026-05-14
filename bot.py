import asyncio
import json
import os
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)
import sys
import time

# Блокировка повторного запуска
PID_FILE = "/tmp/bot_pid.txt"

# Сначала удаляем старый webhook принудительно
try:
    from telegram import Bot
    token = os.environ.get("BOT_TOKEN")
    if token:
        temp_bot = Bot(token=token)
        import asyncio
        asyncio.get_event_loop().run_until_complete(temp_bot.delete_webhook(drop_pending_updates=True))
        print("✅ Webhook удалён")
except Exception as e:
    print(f"⚠️ Не удалось удалить webhook: {e}")

# Проверка на дублирующиеся процессы
try:
    with open(PID_FILE, 'r') as f:
        old_pid = int(f.read().strip())
        try:
            os.kill(old_pid, 0)
            print(f"❌ Процесс с PID {old_pid} уже запущен. Выход.")
            sys.exit(0)
        except OSError:
            pass
except FileNotFoundError:
    pass

with open(PID_FILE, 'w') as f:
    f.write(str(os.getpid()))
print(f"✅ Бот запущен с PID {os.getpid()}")

# ==================== Хранилище игр ====================
games = {}

class Game:
    def __init__(self, chat_id, pack, creator_id, message_thread_id=None):
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
        self.reg_timer_job = None
        self.question_timer_job = None
        self.reg_start_time = None
        self.reg_duration = 300  # 5 минут

    def add_player(self, user_id, username):
        if user_id not in self.registered:
            self.registered[user_id] = {"username": username, "score": 0}

    def record_answer(self, user_id, option_idx):
        if self.status == "active" and user_id in self.registered:
            now = datetime.now(timezone.utc)
            self.answers[user_id] = (option_idx, now)

    def calculate_scores(self):
        q = self.pack["questions"][self.current_question]
        correct = q["correct"]
        for uid, (ans, ts) in self.answers.items():
            if ans == correct:
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
            key=lambda x: (-x[1]["score"], x[1]["username"].lower())
        )

# ==================== Загрузка пакета ====================
def load_pack(pack_id: str):
    path = f"packs/{pack_id}.json"
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# ==================== Форматирование username ====================
def format_username(user) -> str:
    if user.username:
        return f"@{user.username}"
    else:
        return f"@id_{user.id}"

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
    message_thread_id = update.effective_message.message_thread_id

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

    game = Game(chat_id, pack, user.id, message_thread_id)
    games[chat_id] = game
    game.reg_start_time = datetime.now(timezone.utc)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(text="📝 Зарегистрироваться", callback_data="register")],
        [InlineKeyboardButton(text="🚀 Начать досрочно", callback_data="start_early")]
    ])
    msg = await update.message.reply_text(
        f"🎉 Добро пожаловать на викторину «{pack['title']}»!\n\n"
        f"📋 Правила:\n"
        f"• У вас 5 минут на регистрацию\n"
        f"• 10 баллов за правильный ответ\n"
        f"• Бонус за скорость: +5 (0-5с), +4 (6-10с), +3 (11-13с), +2 (14-16с), +1 (17-19с)\n\n"
        f"⏳ Осталось: 5 мин 0 сек\n\n"
        f"Участники:",
        reply_markup=keyboard
    )
    game.reg_msg_id = msg.id

    context.job_queue.run_once(
        end_registration,
        when=game.reg_duration,
        chat_id=chat_id,
        data=chat_id
    )
    game.reg_timer_job = context.job_queue.run_repeating(
        update_reg_timer,
        interval=30,
        first=30,
        chat_id=chat_id,
        data=chat_id
    )

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

    username = format_username(user)
    game.add_player(user.id, username)
    await update_reg_timer_message(context, chat_id, game)

async def update_reg_timer(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    game = games.get(chat_id)
    if not game or game.status != "registration":
        return
    await update_reg_timer_message(context, chat_id, game)

async def update_reg_timer_message(context, chat_id, game):
    elapsed = (datetime.now(timezone.utc) - game.reg_start_time).total_seconds()
    remaining = max(0, game.reg_duration - elapsed)
    mins = int(remaining // 60)
    secs = int(remaining % 60)

    users_list = "\n".join(
        f"• {p['username']}" for uid, p in game.registered.items()
    )
    text = (
        f"🎉 Регистрация на викторину «{game.pack['title']}»\n"
        f"⏳ Осталось: {mins} мин {secs} сек\n\n"
        f"Зарегистрировано: {len(game.registered)}\n{users_list}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(text="📝 Зарегистрироваться", callback_data="register")],
        [InlineKeyboardButton(text="🚀 Начать досрочно", callback_data="start_early")]
    ])
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=game.reg_msg_id,
            text=text,
            reply_markup=keyboard
        )
    except Exception as e:
        print(f"Ошибка обновления таймера: {e}")

# ==================== Досрочное завершение регистрации ====================
async def start_early_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    user = update.effective_user

    game = games.get(chat_id)
    if not game or game.status != "registration":
        await query.answer("Регистрация уже завершена.", show_alert=True)
        return

    if user.id != game.creator_id:
        await query.answer("Только организатор может начать досрочно.", show_alert=True)
        return

    if game.reg_timer_job:
        game.reg_timer_job.schedule_removal()
        game.reg_timer_job = None
    for job in context.job_queue.jobs():
        if job.data == chat_id and job.callback == end_registration:
            job.schedule_removal()
            break

    await end_registration(context, chat_id=chat_id)

# ==================== Завершение регистрации ====================
async def end_registration(context: ContextTypes.DEFAULT_TYPE, chat_id=None):
    if chat_id is None:
        chat_id = context.job.data
    game = games.get(chat_id)
    if not game or game.status != "registration":
        return

    if game.reg_timer_job:
        game.reg_timer_job.schedule_removal()
        game.reg_timer_job = None

    game.status = "active"

    users_list = "\n".join(
        f"• {p['username']}" for uid, p in game.registered.items()
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
        InlineKeyboardButton(text=opt, callback_data=f"ans_{i}")
        for i, opt in enumerate(q["options"])
    ]
    keyboard = InlineKeyboardMarkup([[btn] for btn in buttons])

    question_time = 20
    question_text = (
        f"❓ Вопрос {game.current_question + 1}/{len(game.pack['questions'])}\n"
        f"⏳ Осталось: {question_time} сек\n\n"
        f"{q['text']}"
    )

    send_kwargs = {
        "chat_id": chat_id,
        "reply_markup": keyboard
    }
    if game.message_thread_id:
        send_kwargs["message_thread_id"] = game.message_thread_id

    if q.get("image"):
        msg = await context.bot.send_photo(
            photo=q["image"],
            caption=question_text,
            **send_kwargs
        )
    else:
        msg = await context.bot.send_message(
            text=question_text,
            **send_kwargs
        )

    game.question_msg_id = msg.id
    game.question_start_time = datetime.now(timezone.utc)
    game.answers.clear()

    try:
        await context.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=msg.id,
            disable_notification=False
        )
    except Exception as e:
        print(f"Не удалось закрепить сообщение: {e}")

    game.question_timer_job = context.job_queue.run_repeating(
        update_question_timer,
        interval=5,
        first=5,
        chat_id=chat_id,
        data=chat_id
    )

    context.job_queue.run_once(
        end_question,
        when=question_time,
        chat_id=chat_id,
        data=chat_id
    )

async def update_question_timer(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    game = games.get(chat_id)
    if not game or game.status != "active":
        return

    elapsed = (datetime.now(timezone.utc) - game.question_start_time).total_seconds()
    question_time = 20
    remaining = max(0, question_time - elapsed)
    secs = int(remaining)

    q = game.pack["questions"][game.current_question]
    question_text = (
        f"❓ Вопрос {game.current_question + 1}/{len(game.pack['questions'])}\n"
        f"⏳ Осталось: {secs} сек\n\n"
        f"{q['text']}"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(text=opt, callback_data=f"ans_{i}")]
        for i, opt in enumerate(q["options"])
    ])

    try:
        if q.get("image"):
            await context.bot.edit_message_caption(
                chat_id=chat_id,
                message_id=game.question_msg_id,
                caption=question_text,
                reply_markup=keyboard
            )
        else:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=game.question_msg_id,
                text=question_text,
                reply_markup=keyboard
            )
    except Exception as e:
        print(f"Ошибка обновления вопроса: {e}")

# ==================== Завершение вопроса ====================
async def end_question(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    game = games.get(chat_id)
    if not game or game.status != "active":
        return

    if game.question_timer_job:
        game.question_timer_job.schedule_removal()
        game.question_timer_job = None

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

    correct_answer_text = f"✅ Правильный ответ: {q['options'][q['correct']]}"
    if q.get("comment"):
        correct_answer_text += f"\n💡 {q['comment']}"

    question_text = (
        f"❓ Вопрос {game.current_question + 1}/{len(game.pack['questions'])}\n"
        f"{q['text']}\n\n"
        f"{stats_text}\n\n"
        f"{correct_answer_text}"
    )

    try:
        await context.bot.unpin_chat_message(
            chat_id=chat_id,
            message_id=game.question_msg_id
        )
    except:
        pass

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

    is_last_question = (game.current_question == len(game.pack["questions"]) - 1)

    if not is_last_question:
        leaderboard = game.get_leaderboard()
        rating_lines = []
        for i, (uid, data) in enumerate(leaderboard, 1):
            rating_lines.append(f"{i}. {data['username']} — {data['score']} очк.")
        rating_text = "🏆 Текущий рейтинг:\n" + "\n".join(rating_lines)
        
        send_kwargs = {"chat_id": chat_id, "text": rating_text}
        if game.message_thread_id:
            send_kwargs["message_thread_id"] = game.message_thread_id
        await context.bot.send_message(**send_kwargs)

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
        send_kwargs = {"chat_id": chat_id, "text": "🎉 Ура, викторина закончена! Подводим результаты..."}
        if game.message_thread_id:
            send_kwargs["message_thread_id"] = game.message_thread_id
        await context.bot.send_message(**send_kwargs)
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
        send_kwargs = {"chat_id": chat_id, "text": "Нет участников."}
        if game.message_thread_id:
            send_kwargs["message_thread_id"] = game.message_thread_id
        await context.bot.send_message(**send_kwargs)
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
            final_lines.append(f"{medal} {rank} место. {data['username']} — {data['score']} очк.")
        rank += len(same_score_players)

    table = "🏁 Итоговое положение:\n\n" + "\n".join(final_lines)
    send_kwargs = {"chat_id": chat_id, "text": table}
    if game.message_thread_id:
        send_kwargs["message_thread_id"] = game.message_thread_id
    await context.bot.send_message(**send_kwargs)

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

# ==================== Команда /rules ====================
async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rules_text = """🎲 *ДРУЗЬЯ, ДОБРО ПОЖАЛОВАТЬ В НАШ КВИЗ\!*

Мы придумали для вас интеллектуальное шоу, где каждый сможет проверить свою эрудицию и скорость реакции\. Всё просто, честно и очень азартно\.

*Как это будет?*

Сначала мы запустим регистрацию\. У вас будет ровно *5 минут*, чтобы нажать кнопку «Зарегистрироваться» и занять место за игровым столом\. Опоздавшие — в игру не попадают, таковы правила\. Если все собрались раньше — ведущий \(то есть бот\) может начать досрочно\.

*А дальше начинается самое интересное\.*

Перед вами один за другим будут появляться вопросы\. К каждому вопросу — *4 варианта ответа*\. Только один из них правильный\. Ваша задача — угадать\.

Но тут есть хитрость\.

*Чем быстрее вы выбираете правильный ответ, тем больше баллов получаете\.*

Вот как работает наша система:

• Если вы ответили за *0–5 секунд* → получаете *15 баллов* 🚀
• Если за *6–10 секунд* → *14 баллов* ⚡
• Если за *11–13 секунд* → *13 баллов* 👍
• Если за *14–16 секунд* → *12 баллов* 📊
• Если за *17–19 секунд* → *11 баллов* ⏳

А если не успели за 20 секунд? Увы, бот закрывает приём ответов\. В следующий раз повезёт больше\.

*Как отвечать?*

Только через кнопки под вопросом\! Текстом в чат писать бесполезно — бот вас просто не увидит\. Такая у него архитектура\.

*После каждого вопроса мы показываем:*

• Как распределились голоса \(кто сколько процентов набрал\)
• Правильный ответ с коротким пояснением
• Текущий рейтинг — кто лидирует, кто дышит в спину

*В конце игры*

Когда все вопросы кончатся, мы подведём итоги и наградим самых быстрых и умных\. Первое место — 🥇, второе — 🥈, третье — 🥉\.

*И последнее, но важное:*

Боты не участвуют\. Спамить кнопками бессмысленно — засчитывается только первый ответ\. И пожалуйста, не пытайтесь отвечать текстом в чат, даже если очень хочется\. Бот будет невозмутим\.

*Ну что, готовы?*

Жмите «Зарегистрироваться» и готовьте пальцы — вопросы уже ждут своей очереди\! 🎯"""
    
    await update.message.reply_text(rules_text, parse_mode="MarkdownV2")

# ==================== ЗАПУСК (ТОЛЬКО POLLING) ====================
def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise ValueError("❌ Не задан BOT_TOKEN")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("quiz", quiz_command))
    app.add_handler(CommandHandler("rules", rules_command))
    app.add_handler(CallbackQueryHandler(register_callback, pattern="register"))
    app.add_handler(CallbackQueryHandler(start_early_callback, pattern="start_early"))
    app.add_handler(CallbackQueryHandler(answer_callback, pattern=r"ans_\d+"))

    print("🚀 Бот запущен в режиме polling")
    print("🤖 Готов принимать команды")

    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )

if __name__ == "__main__":
    # Даём время на завершение старых процессов
    time.sleep(2)
    main()