import sys
import os
import re
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# -------------------- Блокировка повторного запуска --------------------
PID_FILE = "/tmp/bot_pid.txt"

def check_single_instance():
    try:
        with open(PID_FILE, 'r') as f:
            old_pid = int(f.read().strip())
            if old_pid == 1:
                print("⚠️ PID 1 обнаружен (контейнер), продолжаем запуск.")
            else:
                try:
                    os.kill(old_pid, 0)
                    print(f"❌ Бот уже запущен с PID {old_pid}. Выход.")
                    sys.exit(0)
                except OSError:
                    pass
    except FileNotFoundError:
        pass
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))
    print(f"✅ Бот запущен с PID {os.getpid()}")

check_single_instance()
# ---------------------------------------------------------------------

games = {}

class Game:
    def __init__(self, chat_id, pack, creator_id, message_thread_id=None, scheduled_start_utc: Optional[datetime] = None):
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
        self.scheduled_start_utc = scheduled_start_utc
        self.paused = False
        self.pause_after_question = False

    def add_player(self, user_id, username):
        if user_id not in self.registered:
            self.registered[user_id] = {"username": username, "score": 0}

    def record_answer(self, user_id, option_idx):
        if self.status == "active" and user_id in self.registered and not self.paused:
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
        return sorted(self.registered.items(), key=lambda x: (-x[1]["score"], x[1]["username"].lower()))

def load_pack(pack_id: str):
    path = f"packs/{pack_id}.json"
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def format_username(user):
    return f"@{user.username}" if user.username else f"@id_{user.id}"

async def is_admin(update: Update, user_id: int) -> bool:
    try:
        member = await update.effective_chat.get_member(user_id)
        return member.status in ("creator", "administrator")
    except:
        return False

# -------------------- Функции перевода времени (Москва UTC+3) --------------------
def msk_to_utc(dt_msk: datetime) -> datetime:
    return dt_msk.replace(tzinfo=timezone.utc) - timedelta(hours=3)

def format_datetime_msk_multiline(dt_utc: datetime) -> str:
    msk = dt_utc + timedelta(hours=3)
    now_msk = datetime.now(timezone.utc) + timedelta(hours=3)
    if msk.date() == now_msk.date():
        return f"📅 Дата и время начала:\nсегодня, в {msk.strftime('%H:%M')}"
    else:
        return f"📅 Дата и время начала:\n{msk.strftime('%d.%m.%Y')}, в {msk.strftime('%H:%M')}"

# -------------------- Команда /quiz --------------------
async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    message_thread_id = update.effective_message.message_thread_id

    if not await is_admin(update, user.id):
        await update.message.reply_text("❌ Только администраторы могут запускать викторину.")
        return

    if chat_id in games and games[chat_id].status not in ("finished",):
        await update.message.reply_text("❌ Викторина уже идёт в этой группе.")
        return

    full_text = update.message.text.strip()
    rest = full_text[5:].strip()
    parts = re.split(r'\s*\|\s*', rest)
    if len(parts) != 3:
        await update.message.reply_text(
            "❌ Неверный формат. Используйте:\n`/quiz 0007 | 2026-05-15 | 14:00`\n"
            "Дата и время указываются по МОСКВЕ (UTC+3). Разделитель – вертикальная черта.",
            parse_mode="Markdown"
        )
        return
    pack_id, date_str, time_str = parts
    if len(pack_id) != 4 or not pack_id.isdigit():
        await update.message.reply_text("ID пакета должен быть 4 цифры. Пример: 0007")
        return
    pack = load_pack(pack_id)
    if not pack:
        await update.message.reply_text(f"❌ Пакет {pack_id} не найден.")
        return
    try:
        dt_msk = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        await update.message.reply_text("Неверный формат даты/времени. Используйте: ГГГГ-ММ-ДД ЧЧ:ММ (московское время)")
        return
    scheduled_start_utc = msk_to_utc(dt_msk)
    now_utc = datetime.now(timezone.utc)
    if scheduled_start_utc < now_utc + timedelta(minutes=2):
        await update.message.reply_text("❌ Время начала должно быть не менее чем через 2 минуты от текущего (по Москве).")
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

    delay = (scheduled_start_utc - now_utc).total_seconds()
    if delay > 0:
        context.job_queue.run_once(start_quiz_sequence, when=delay, chat_id=chat_id, data=chat_id)
    else:
        await start_quiz_sequence(context, chat_id)

async def open_registration(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = games.get(chat_id)
    if not game:
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Зарегистрироваться", callback_data="register")],
        [InlineKeyboardButton("🚀 Начать сейчас", callback_data="start_early")]
    ])
    start_line = format_datetime_msk_multiline(game.scheduled_start_utc)
    text = (
        f"🎪 ОТКРЫТА РЕГИСТРАЦИЯ НА КВИЗ\n\n"
        f"✏️ Тема квиза: {game.pack['title']}\n"
        f"{start_line}\n\n"
        f"👥 Список участников:\n(пока никого)\n"
    )
    send_kwargs = {"chat_id": chat_id, "text": text, "reply_markup": keyboard}
    if game.message_thread_id:
        send_kwargs["message_thread_id"] = game.message_thread_id
    msg = await context.bot.send_message(**send_kwargs)
    game.reg_msg_id = msg.id
    game.reg_timer_job = context.job_queue.run_repeating(update_reg_timer, interval=10, first=5, chat_id=chat_id, data=chat_id)

async def update_reg_timer(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    game = games.get(chat_id)
    if not game or game.status != "registration":
        return
    users_list = "\n".join(f"• {p['username']}" for p in game.registered.values()) or "пока никого"
    start_line = format_datetime_msk_multiline(game.scheduled_start_utc)
    text = (
        f"🎪 ОТКРЫТА РЕГИСТРАЦИЯ НА КВИЗ\n\n"
        f"✏️ Тема квиза: {game.pack['title']}\n"
        f"{start_line}\n\n"
        f"👥 Список участников ({len(game.registered)}):\n{users_list}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Зарегистрироваться", callback_data="register")],
        [InlineKeyboardButton("🚀 Начать сейчас", callback_data="start_early")]
    ])
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=game.reg_msg_id, text=text, reply_markup=keyboard)
    except Exception:
        pass

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
    game.add_player(user.id, format_username(user))
    await update_reg_timer(context, chat_id)

async def start_early_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    user = update.effective_user
    game = games.get(chat_id)
    if not game or game.status != "registration":
        await query.answer("Регистрация не активна.", show_alert=True)
        return
    if user.id != game.creator_id:
        await query.answer("Только организатор может начать досрочно.", show_alert=True)
        return
    for job in context.job_queue.jobs():
        if job.chat_id == chat_id and job.callback == start_quiz_sequence:
            job.schedule_removal()
            break
    await close_registration_and_start(context, chat_id, early=True)

async def close_registration_and_start(context: ContextTypes.DEFAULT_TYPE, chat_id: int, early: bool = False):
    game = games.get(chat_id)
    if not game or game.status != "registration":
        return
    if game.reg_timer_job:
        game.reg_timer_job.schedule_removal()
        game.reg_timer_job = None
    game.status = "active"
    users_list = "\n".join(f"• {p['username']}" for p in game.registered.values())
    start_line = format_datetime_msk_multiline(game.scheduled_start_utc)
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=game.reg_msg_id,
        text=f"🎉 Регистрация завершена. Начинаем викторину «{game.pack['title']}»!\n"
             f"{start_line}\nУчастников: {len(game.registered)}\n{users_list}"
    )
    if early:
        context.job_queue.run_once(start_question, when=5, chat_id=chat_id, data=chat_id)

async def start_quiz_sequence(context: ContextTypes.DEFAULT_TYPE, chat_id: int = None):
    if chat_id is None:
        chat_id = context.job.data
    game = games.get(chat_id)
    if not game:
        return
    if game.status == "registration":
        await close_registration_and_start(context, chat_id, early=False)
    await send_pre_start_warning(context, chat_id)
    context.job_queue.run_once(start_question, when=30, chat_id=chat_id, data=chat_id)

async def send_pre_start_warning(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = games.get(chat_id)
    if not game:
        return
    mentions = []
    for uid in game.registered.keys():
        try:
            member = await context.bot.get_chat_member(chat_id, uid)
            if member.user.username:
                mentions.append(f"@{member.user.username}")
            else:
                mentions.append(f"[{member.user.first_name}](tg://user?id={uid})")
        except:
            mentions.append(f"id{uid}")
    mention_text = " ".join(mentions) if mentions else "Участники"
    warning_text = (
        f"{mention_text}\n\n"
        f"🚀 Квиз сейчас начнётся! Даём вам 30 секунд зайти в Телеграм, проверить ваш VPN и настроиться быстро, "
        f"а главное — правильно отвечать на вопросы!"
    )
    send_kwargs = {"chat_id": chat_id, "text": warning_text, "parse_mode": "Markdown"}
    if game.message_thread_id:
        send_kwargs["message_thread_id"] = game.message_thread_id
    await context.bot.send_message(**send_kwargs)

# -------------------- Логика вопросов --------------------
async def start_question(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    game = games.get(chat_id)
    if not game or game.status != "active" or game.paused:
        return
    if game.current_question >= len(game.pack["questions"]):
        await finish_quiz(context)
        return
    q = game.pack["questions"][game.current_question]
    buttons = [InlineKeyboardButton(opt, callback_data=f"ans_{i}") for i, opt in enumerate(q["options"])]
    keyboard = InlineKeyboardMarkup([[btn] for btn in buttons])
    text = (
        f"❓ Вопрос {game.current_question+1}/{len(game.pack['questions'])}\n"
        f"⏳ Осталось: 20 сек\n\n{q['text']}"
    )
    send_kwargs = {"chat_id": chat_id, "reply_markup": keyboard}
    if game.message_thread_id:
        send_kwargs["message_thread_id"] = game.message_thread_id
    if q.get("image"):
        msg = await context.bot.send_photo(photo=q["image"], caption=text, **send_kwargs)
    else:
        msg = await context.bot.send_message(text=text, **send_kwargs)
    game.question_msg_id = msg.id
    game.question_start_time = datetime.now(timezone.utc)
    game.answers.clear()
    try:
        await context.bot.pin_chat_message(chat_id=chat_id, message_id=msg.id, disable_notification=False)
    except:
        pass
    game.question_timer_job = context.job_queue.run_repeating(update_question_timer, interval=5, first=5, chat_id=chat_id, data=chat_id)
    context.job_queue.run_once(end_question, when=20, chat_id=chat_id, data=chat_id)

async def update_question_timer(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    game = games.get(chat_id)
    if not game or game.status != "active" or game.paused:
        return
    elapsed = (datetime.now(timezone.utc) - game.question_start_time).total_seconds()
    remaining = max(0, 20 - elapsed)
    secs = int(remaining)
    q = game.pack["questions"][game.current_question]
    text = f"❓ Вопрос {game.current_question+1}/{len(game.pack['questions'])}\n⏳ Осталось: {secs} сек\n\n{q['text']}"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(opt, callback_data=f"ans_{i}") for i, opt in enumerate(q["options"])]])
    try:
        if q.get("image"):
            await context.bot.edit_message_caption(chat_id=chat_id, message_id=game.question_msg_id, caption=text, reply_markup=keyboard)
        else:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=game.question_msg_id, text=text, reply_markup=keyboard)
    except Exception:
        pass

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
    for _, (ans_idx, _) in game.answers.items():
        if 0 <= ans_idx < len(counts):
            counts[ans_idx] += 1
    percents = [cnt/total_answers*100 if total_answers else 0 for cnt in counts]
    stats_lines = [f"{opt}: {perc:.1f}%{' ✅' if i==q['correct'] else ''}" for i, (opt, perc) in enumerate(zip(q["options"], percents))]
    stats_text = "📊 Статистика ответов:\n" + "\n".join(stats_lines)
    correct_text = f"✅ Правильный ответ: {q['options'][q['correct']]}"
    if q.get("comment"):
        correct_text += f"\n💡 {q['comment']}"
    final_text = f"❓ Вопрос {game.current_question+1}/{len(game.pack['questions'])}\n{q['text']}\n\n{stats_text}\n\n{correct_text}"
    try:
        await context.bot.unpin_chat_message(chat_id=chat_id, message_id=game.question_msg_id)
    except:
        pass
    try:
        if q.get("image"):
            await context.bot.edit_message_caption(chat_id=chat_id, message_id=game.question_msg_id, caption=final_text)
        else:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=game.question_msg_id, text=final_text)
    except:
        pass
    leaderboard = game.get_leaderboard()
    rating_lines = [f"{i+1}. {data['username']} — {data['score']} очк." for i, (_, data) in enumerate(leaderboard)]
    rating_text = "🏆 Текущий рейтинг:\n" + "\n".join(rating_lines)
    send_kwargs = {"chat_id": chat_id, "text": rating_text}
    if game.message_thread_id:
        send_kwargs["message_thread_id"] = game.message_thread_id
    await context.bot.send_message(**send_kwargs)
    game.current_question += 1
    if game.pause_after_question:
        game.pause_after_question = False
        game.status = "paused"
        await context.bot.send_message(chat_id=chat_id, text="⏸ Квиз приостановлен. /resume для продолжения.")
        return
    if game.current_question < len(game.pack["questions"]):
        context.job_queue.run_once(start_question, when=5, chat_id=chat_id, data=chat_id)
    else:
        game.status = "finished"
        await context.bot.send_message(chat_id=chat_id, text="🎉 Викторина закончена! Подводим итоги...")
        context.job_queue.run_once(finish_quiz, when=5, chat_id=chat_id, data=chat_id)

async def finish_quiz(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    game = games.pop(chat_id, None)
    if not game:
        return
    leaderboard = game.get_leaderboard()
    if not leaderboard:
        await context.bot.send_message(chat_id=chat_id, text="Нет участников.")
        return
    final_lines, rank, i = [], 1, 0
    while i < len(leaderboard):
        same_score = []
        score = leaderboard[i][1]["score"]
        while i < len(leaderboard) and leaderboard[i][1]["score"] == score:
            same_score.append(leaderboard[i])
            i += 1
        for _, data in same_score:
            medal = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else ""
            final_lines.append(f"{medal} {rank} место. {data['username']} — {data['score']} очк.")
        rank += len(same_score)
    await context.bot.send_message(chat_id=chat_id, text="🏁 Итоговое положение:\n\n" + "\n".join(final_lines))

async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Ответ принят ✅")
    user = update.effective_user
    chat_id = update.effective_chat.id
    game = games.get(chat_id)
    if not game or game.status != "active" or game.paused:
        return
    try:
        option_idx = int(query.data.split("_")[1])
    except:
        return
    game.record_answer(user.id, option_idx)

# -------------------- Команды организатора --------------------
async def pause_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    game = games.get(chat_id)
    if not game or game.status not in ("active", "registration"):
        await update.message.reply_text("❌ Нет активного квиза.")
        return
    if user.id != game.creator_id:
        await update.message.reply_text("❌ Только организатор может приостановить.")
        return
    if game.status == "active":
        game.pause_after_question = True
        await update.message.reply_text("⏸ Квиз будет приостановлен после текущего вопроса.")
    else:
        game.status = "paused"
        await update.message.reply_text("⏸ Квиз приостановлен. /resume для продолжения.")

async def resume_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    game = games.get(chat_id)
    if not game or game.status != "paused":
        await update.message.reply_text("❌ Квиз не на паузе.")
        return
    if user.id != game.creator_id:
        await update.message.reply_text("❌ Только организатор может возобновить.")
        return
    game.status = "active"
    await update.message.reply_text("▶ Квиз возобновлён.")
    if not game.question_timer_job and game.current_question < len(game.pack["questions"]):
        context.job_queue.run_once(start_question, when=2, chat_id=chat_id, data=chat_id)

async def abort_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    game = games.get(chat_id)
    if not game:
        await update.message.reply_text("❌ Нет активного квиза.")
        return
    if user.id != game.creator_id:
        await update.message.reply_text("❌ Только организатор может отменить квиз.")
        return
    for job in context.job_queue.jobs():
        if job.chat_id == chat_id:
            job.schedule_removal()
    games.pop(chat_id, None)
    await update.message.reply_text(
        "Упс, что-то пошло не так. Мы остановили квиз и пошли исправлять ошибки.\n"
        "Данный квиз продолжен уже не будет. Ждите подробной информации о восстановлении сервиса в ближайшее время."
    )

# -------------------- Команда /rules --------------------
async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rules_text = """🎲 *ДРУЗЬЯ, ДОБРО ПОЖАЛОВАТЬ В НАШ КВИЗ!*

Мы придумали для вас интеллектуальное шоу, где каждый сможет проверить свою эрудицию и скорость реакции. Всё просто, честно и очень азартно.

*Как это будет?*

Сначала мы запустим регистрацию. У вас будет время до указанного времени старта, чтобы нажать кнопку «Зарегистрироваться» и занять место за игровым столом. Ведущий (бот) может начать досрочно кнопкой «Начать сейчас».

*А дальше начинается самое интересное.*

Перед вами один за другим будут появляться вопросы. К каждому вопросу — 4 варианта ответа. Только один из них правильный. Ваша задача — угадать.

*Чем быстрее вы выбираете правильный ответ, тем больше баллов получаете.*

• 0–5 секунд → +5 бонуса (всего 15)
• 6–10 секунд → +4 бонуса (всего 14)
• 11–13 секунд → +3 бонуса (всего 13)
• 14–16 секунд → +2 бонуса (всего 12)
• 17–19 секунд → +1 бонус (всего 11)

*Как отвечать?*

Только через кнопки под вопросом! Текстом в чат писать бесполезно — бот вас просто не увидит.

*После каждого вопроса мы показываем:*

• Статистику ответов (кто сколько процентов набрал)
• Правильный ответ с пояснением
• Текущий рейтинг

*В конце игры*

Когда все вопросы кончатся, мы подведём итоги и наградим самых быстрых и умных. Первое место — 🥇, второе — 🥈, третье — 🥉.

*И последнее, но важное:*

Боты не участвуют. Спамить кнопками бессмысленно — засчитывается только первый ответ.

*Ну что, готовы?*

Жмите «Зарегистрироваться» и готовьте пальцы — вопросы уже ждут своей очереди! 🎯"""
    await update.message.reply_text(rules_text, parse_mode="Markdown")

# -------------------- ЗАПУСК (синхронный, правильный) --------------------
def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise ValueError("❌ Не задан BOT_TOKEN")

    # Удаляем webhook синхронно
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = Bot(token=token)
    loop.run_until_complete(bot.delete_webhook(drop_pending_updates=True))
    print("✅ Webhook удалён")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("quiz", quiz_command))
    app.add_handler(CommandHandler("rules", rules_command))
    app.add_handler(CommandHandler("pause", pause_quiz))
    app.add_handler(CommandHandler("resume", resume_quiz))
    app.add_handler(CommandHandler("abort", abort_quiz))
    app.add_handler(CallbackQueryHandler(register_callback, pattern="register"))
    app.add_handler(CallbackQueryHandler(start_early_callback, pattern="start_early"))
    app.add_handler(CallbackQueryHandler(answer_callback, pattern=r"ans_\d+"))

    print("🚀 Бот запущен в режиме polling")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
