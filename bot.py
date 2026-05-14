import sys
import os
import asyncio
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
)

# ==================== БЛОКИРОВКА ЗАПУСКА ====================
PID_FILE = "/tmp/bot_pid.txt"

def check_single_instance():
    try:
        with open(PID_FILE, 'r') as f:
            old_pid = int(f.read().strip())
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
# ============================================================

games = {}

class Game:
    def __init__(self, chat_id, pack, creator_id, message_thread_id=None, scheduled_start: Optional[datetime] = None):
        self.chat_id = chat_id
        self.pack = pack
        self.creator_id = creator_id
        self.message_thread_id = message_thread_id
        self.status = "scheduled"
        self.registered = {}
        self.current_question = 0
        self.answers = {}
        self.question_start_time = None
        self.reg_msg_id = None
        self.question_msg_id = None
        self.reg_timer_job = None
        self.question_timer_job = None
        self.reg_start_time = None
        self.reg_duration = 300
        self.scheduled_start = scheduled_start
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

# ==================== Команда /quiz ====================
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

    args = context.args
    if not args or len(args[0]) != 4 or not args[0].isdigit():
        await update.message.reply_text("Используйте: /quiz XXXX (4 цифры)")
        return

    pack_id = args[0]
    pack = load_pack(pack_id)
    if not pack:
        await update.message.reply_text(f"❌ Пакет {pack_id} не найден в базе.")
        return

    context.user_data['temp_quiz'] = {
        'pack_id': pack_id,
        'pack': pack,
        'chat_id': chat_id,
        'message_thread_id': message_thread_id,
        'creator_id': user.id
    }
    await show_calendar(update, context)

async def show_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(timezone.utc)
    today = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    keyboard = []
    for label, offset in [("Сегодня", 0), ("Завтра", 1), ("Послезавтра", 2)]:
        dt = today + timedelta(days=offset)
        keyboard.append([InlineKeyboardButton(f"{label} в {dt.strftime('%H:%M')}", callback_data=f"datetime_{dt.timestamp()}")])
    keyboard.append([InlineKeyboardButton("📅 Выбрать дату и время (вручную)", callback_data="manual_date")])
    await update.message.reply_text(
        "📅 Выберите дату и время начала викторины (UTC).\n"
        "Минимум через 2 минуты от текущего момента.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def datetime_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("datetime_"):
        ts = float(data.split("_")[1])
        selected = datetime.fromtimestamp(ts, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        if selected < now + timedelta(minutes=2):
            await query.edit_message_text("❌ Время должно быть минимум через 2 минуты. Попробуйте снова.")
            await show_calendar(update, context)
            return
        temp = context.user_data.get('temp_quiz')
        if not temp:
            await query.edit_message_text("Ошибка. Начните заново с /quiz")
            return
        game = Game(
            chat_id=temp['chat_id'],
            pack=temp['pack'],
            creator_id=temp['creator_id'],
            message_thread_id=temp['message_thread_id'],
            scheduled_start=selected
        )
        games[temp['chat_id']] = game
        game.reg_start_time = selected - timedelta(minutes=5)   # регистрация начнётся за 5 минут до старта
        game.reg_duration = 300
        game.status = "scheduled"
        now_utc = datetime.now(timezone.utc)
        delay = (game.reg_start_time - now_utc).total_seconds()
        if delay > 0:
            context.job_queue.run_once(start_registration, when=delay, chat_id=temp['chat_id'], data=temp['chat_id'])
            await query.edit_message_text(
                f"✅ Викторина запланирована.\n"
                f"📅 Регистрация начнётся: {game.reg_start_time.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
                f"🎬 Старт вопросов: {selected.strftime('%Y-%m-%d %H:%M:%S')} UTC"
            )
        else:
            await start_registration(context, chat_id=temp['chat_id'])
            await query.edit_message_text("✅ Регистрация открыта немедленно.")
        del context.user_data['temp_quiz']
    elif data == "manual_date":
        await query.edit_message_text(
            "Отправьте дату и время в формате: ГГГГ-ММ-ДД ЧЧ:ММ (UTC)\n"
            "Пример: 2025-05-20 14:30\n"
            "Внимание: время должно быть в будущем (минимум +2 минуты)."
        )
        context.user_data['awaiting_manual_date'] = True

async def handle_manual_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('awaiting_manual_date'):
        text = update.message.text.strip()
        try:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M")
            dt = dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if dt < now + timedelta(minutes=2):
                await update.message.reply_text("❌ Время должно быть в будущем (минимум +2 минуты). Попробуйте снова.")
                return
            temp = context.user_data.get('temp_quiz')
            if not temp:
                await update.message.reply_text("Ошибка. Используйте /quiz сначала.")
                return
            game = Game(
                chat_id=temp['chat_id'],
                pack=temp['pack'],
                creator_id=temp['creator_id'],
                message_thread_id=temp['message_thread_id'],
                scheduled_start=dt
            )
            games[temp['chat_id']] = game
            game.reg_start_time = dt - timedelta(minutes=5)
            game.reg_duration = 300
            game.status = "scheduled"
            now_utc = datetime.now(timezone.utc)
            delay = (game.reg_start_time - now_utc).total_seconds()
            if delay > 0:
                context.job_queue.run_once(start_registration, when=delay, chat_id=temp['chat_id'], data=temp['chat_id'])
                await update.message.reply_text(f"✅ Квиз запланирован. Регистрация начнётся в {game.reg_start_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
            else:
                await start_registration(context, chat_id=temp['chat_id'])
                await update.message.reply_text("✅ Регистрация открыта.")
            del context.user_data['temp_quiz']
            del context.user_data['awaiting_manual_date']
        except ValueError:
            await update.message.reply_text("Неверный формат. Используйте: ГГГГ-ММ-ДД ЧЧ:ММ")

# ==================== Регистрация ====================
async def start_registration(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = games.get(chat_id)
    if not game or game.status != "scheduled":
        return
    game.status = "registration"
    game.reg_start_time = datetime.now(timezone.utc)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Зарегистрироваться", callback_data="register")],
        [InlineKeyboardButton("🚀 Начать сейчас", callback_data="start_early")]
    ])
    scheduled_str = game.scheduled_start.strftime("%Y-%m-%d %H:%M:%S") if game.scheduled_start else "Сейчас"
    text = (
        f"🎪 ОТКРЫТА РЕГИСТРАЦИЯ НА КВИЗ\n\n"
        f"✏️ Тема квиза: {game.pack['title']}\n"
        f"📅 Дата и время начала: {scheduled_str} UTC\n\n"
        f"👥 Список участников:\n(пока никого)\n"
    )
    send_kwargs = {"chat_id": chat_id, "text": text, "reply_markup": keyboard}
    if game.message_thread_id:
        send_kwargs["message_thread_id"] = game.message_thread_id
    msg = await context.bot.send_message(**send_kwargs)
    game.reg_msg_id = msg.id
    # Запускаем таймер окончания регистрации (через 5 минут)
    context.job_queue.run_once(end_registration, when=game.reg_duration, chat_id=chat_id, data=chat_id)
    game.reg_timer_job = context.job_queue.run_repeating(update_reg_timer, interval=30, first=30, chat_id=chat_id, data=chat_id)

    # Отправляем напоминание через бота-зазывалу за 5 минут до старта
    # Сейчас регистрация открыта за 5 минут до старта, значит сразу после открытия регистрации нужно отправить /all
    call_text = f"/all@ZazyvalaTag2Bot Квиз «{game.pack['title']}» начнётся через 5 минут! Успейте зарегистрироваться!"
    call_kwargs = {"chat_id": chat_id, "text": call_text}
    if game.message_thread_id:
        call_kwargs["message_thread_id"] = game.message_thread_id
    await context.bot.send_message(**call_kwargs)

async def update_reg_timer(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    game = games.get(chat_id)
    if not game or game.status != "registration":
        return
    elapsed = (datetime.now(timezone.utc) - game.reg_start_time).total_seconds()
    remaining = max(0, game.reg_duration - elapsed)
    mins, secs = int(remaining // 60), int(remaining % 60)
    users_list = "\n".join(f"• {p['username']}" for p in game.registered.values()) or "пока никого"
    scheduled_str = game.scheduled_start.strftime("%Y-%m-%d %H:%M:%S") if game.scheduled_start else "Сейчас"
    text = (
        f"🎪 ОТКРЫТА РЕГИСТРАЦИЯ НА КВИЗ\n\n"
        f"✏️ Тема квиза: {game.pack['title']}\n"
        f"📅 Дата и время начала: {scheduled_str} UTC\n"
        f"⏳ Регистрация закроется через: {mins} мин {secs} сек\n\n"
        f"👥 Список участников ({len(game.registered)}):\n{users_list}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Зарегистрироваться", callback_data="register")],
        [InlineKeyboardButton("🚀 Начать сейчас", callback_data="start_early")]
    ])
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=game.reg_msg_id, text=text, reply_markup=keyboard)
    except Exception as e:
        print(f"Ошибка обновления: {e}")

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
    if game.reg_timer_job:
        game.reg_timer_job.schedule_removal()
        game.reg_timer_job = None
    for job in context.job_queue.jobs():
        if job.data == chat_id and job.callback == end_registration:
            job.schedule_removal()
            break
    # Отменяем запланированный автоматический старт, если он был
    for job in context.job_queue.jobs():
        if job.chat_id == chat_id and job.callback == start_quiz_at_scheduled_time:
            job.schedule_removal()
            break
    await end_registration(context, chat_id=chat_id, early_start=True)

async def end_registration(context: ContextTypes.DEFAULT_TYPE, chat_id=None, early_start=False):
    if chat_id is None:
        chat_id = context.job.data
    game = games.get(chat_id)
    if not game or game.status != "registration":
        return
    if game.reg_timer_job:
        game.reg_timer_job.schedule_removal()
        game.reg_timer_job = None
    game.status = "active"
    users_list = "\n".join(f"• {p['username']}" for p in game.registered.values())
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=game.reg_msg_id,
        text=f"🎉 Регистрация завершена. Начинаем викторину «{game.pack['title']}»!\nУчастников: {len(game.registered)}\n{users_list}"
    )
    if early_start:
        # Если нажали "Начать сейчас" – сразу стартуем вопросы без задержки и без 30-секундного предупреждения
        context.job_queue.run_once(start_question, when=5, chat_id=chat_id, data=chat_id)
    else:
        # Автоматический старт: ждём наступления scheduled_start
        now = datetime.now(timezone.utc)
        delay = (game.scheduled_start - now).total_seconds()
        if delay > 0:
            # Сначала отправляем тег участникам за 0 секунд до начала? Лучше сразу отправить сообщение с предупреждением и задержкой 30 секунд
            await send_pre_start_warning(context, chat_id)
            # Запускаем таймер на фактическое начало вопросов через 30 секунд после предупреждения
            context.job_queue.run_once(start_question, when=30, chat_id=chat_id, data=chat_id)
        else:
            # Если опоздали – сразу стартуем
            context.job_queue.run_once(start_question, when=5, chat_id=chat_id, data=chat_id)

async def send_pre_start_warning(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = games.get(chat_id)
    if not game:
        return
    # Формируем список тегов участников
    mentions = []
    for uid in game.registered.keys():
        try:
            chat_member = await context.bot.get_chat_member(chat_id, uid)
            if chat_member.user.username:
                mentions.append(f"@{chat_member.user.username}")
            else:
                # Если нет username, используем упоминание по ID (не работает в обычных группах без премиум)
                mentions.append(f"[{chat_member.user.first_name}](tg://user?id={uid})")
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

# ==================== Логика вопросов ====================
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

# ==================== Команды организатора ====================
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

# ==================== Команда /rules ====================
async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rules_text = "Ваши правила квиза..."  # замените на свой текст
    await update.message.reply_text(rules_text)

# ==================== ЗАПУСК ====================
def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise ValueError("❌ Не задан BOT_TOKEN")
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("quiz", quiz_command))
    app.add_handler(CommandHandler("rules", rules_command))
    app.add_handler(CommandHandler("pause", pause_quiz))
    app.add_handler(CommandHandler("resume", resume_quiz))
    app.add_handler(CommandHandler("abort", abort_quiz))
    app.add_handler(CallbackQueryHandler(datetime_callback, pattern="^(datetime_|manual_date)"))
    app.add_handler(CallbackQueryHandler(register_callback, pattern="register"))
    app.add_handler(CallbackQueryHandler(start_early_callback, pattern="start_early"))
    app.add_handler(CallbackQueryHandler(answer_callback, pattern=r"ans_\d+"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_manual_date))

    print("🚀 Бот запущен в режиме polling")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()