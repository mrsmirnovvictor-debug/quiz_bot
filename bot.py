import sys
import os
import re
import json
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# -------------------- Google Sheets импорт --------------------
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# -------------------- Константы --------------------
TIMER_VIDEO_URL = os.environ.get("TIMER_VIDEO_URL", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS", "")

# -------------------- Инициализация Google Sheets --------------------
def init_google_sheets():
    """Инициализация подключения к Google Sheets"""
    if not GOOGLE_CREDENTIALS_JSON:
        print("⚠️ GOOGLE_CREDENTIALS не заданы, статистика не будет сохраняться")
        return None
    
    try:
        # Загружаем credentials из JSON строки
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        
        # ID таблицы (можно задать в переменной окружения)
        sheet_id = os.environ.get("GOOGLE_SHEET_ID", "")
        if not sheet_id:
            print("⚠️ GOOGLE_SHEET_ID не задан")
            return None
        
        sheet = client.open_by_key(sheet_id).sheet1
        print("✅ Google Sheets подключена")
        return sheet
    except Exception as e:
        print(f"❌ Ошибка подключения к Google Sheets: {e}")
        return None

# -------------------- Функции работы с Google Sheets --------------------
async def save_quiz_results(game):
    """Сохраняет результаты квиза в Google Sheets"""
    sheet = init_google_sheets()
    if not sheet:
        return
    
    try:
        now = datetime.now(timezone.utc) + timedelta(hours=3)  # Московское время
        date_str = now.strftime("%Y-%m-%d %H:%M:%S")
        max_points = len(game.pack["questions"]) * 15  # 15 баллов максимум за вопрос
        
        for user_id, data in game.registered.items():
            row = [
                date_str,
                str(game.chat_id),
                game.pack["title"],
                str(user_id),
                data["username"],
                data["score"],
                max_points
            ]
            sheet.append_row(row)
        
        print(f"✅ Сохранено {len(game.registered)} результатов в Google Sheets")
    except Exception as e:
        print(f"❌ Ошибка сохранения в Google Sheets: {e}")

async def get_leaderboard_stats(chat_id: int = None) -> List[Dict]:
    """Получает общую статистику игроков из Google Sheets"""
    sheet = init_google_sheets()
    if not sheet:
        return []
    
    try:
        records = sheet.get_all_records()
        stats = defaultdict(lambda: {"games": 0, "total_score": 0, "username": ""})
        
        for record in records:
            user_id = str(record.get("User ID", ""))
            if not user_id:
                continue
            
            username = record.get("Username", user_id)
            score = record.get("Набранные очки", 0)
            
            stats[user_id]["games"] += 1
            stats[user_id]["total_score"] += score
            stats[user_id]["username"] = username
        
        # Формируем список для вывода
        leaderboard = []
        for user_id, data in stats.items():
            avg_score = data["total_score"] / data["games"] if data["games"] > 0 else 0
            leaderboard.append({
                "user_id": user_id,
                "username": data["username"],
                "games": data["games"],
                "total_score": data["total_score"],
                "avg_score": round(avg_score, 2)
            })
        
        # Сортируем по суммарным очкам (от большего к меньшему)
        leaderboard.sort(key=lambda x: x["total_score"], reverse=True)
        return leaderboard
    except Exception as e:
        print(f"❌ Ошибка получения статистики: {e}")
        return []

async def get_user_stats(user_id: int) -> Dict:
    """Получает статистику конкретного игрока"""
    leaderboard = await get_leaderboard_stats()
    for player in leaderboard:
        if player["user_id"] == str(user_id):
            return player
    return None

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
        self.video_msg_id = None
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
    return f"@{user.username}" if user.username else f"id{user.id}"

async def is_admin(update: Update, user_id: int) -> bool:
    try:
        member = await update.effective_chat.get_member(user_id)
        return member.status in ("creator", "administrator")
    except:
        return False

# -------------------- Функции перевода времени --------------------
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
    await update_reg_timer_by_chat(context, chat_id)

async def update_reg_timer_by_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
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
                mentions.append(f"{member.user.first_name}")
        except:
            mentions.append(f"Участник")
    mention_text = " ".join(mentions) if mentions else "Участники"
    warning_text = (
        f"{mention_text}\n\n"
        f"🚀 Квиз сейчас начнётся! Даём вам 30 секунд зайти в Телеграм, проверить ваш VPN и настроиться быстро, "
        f"а главное — правильно отвечать на вопросы!"
    )
    send_kwargs = {"chat_id": chat_id, "text": warning_text}
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
    
    base_kwargs = {"chat_id": chat_id}
    if game.message_thread_id:
        base_kwargs["message_thread_id"] = game.message_thread_id
    
    q = game.pack["questions"][game.current_question]
    buttons = [InlineKeyboardButton(opt, callback_data=f"ans_{i}") for i, opt in enumerate(q["options"])]
    keyboard = InlineKeyboardMarkup([[btn] for btn in buttons])
    
    question_text = (
        f"❓ Вопрос {game.current_question+1}/{len(game.pack['questions'])}\n\n"
        f"{q['text']}"
    )
    
    if TIMER_VIDEO_URL:
        try:
            msg = await context.bot.send_video(
                video=TIMER_VIDEO_URL,
                caption=question_text,
                reply_markup=keyboard,
                width=200,
                height=150,
                supports_streaming=True,
                **base_kwargs
            )
            game.video_msg_id = msg.message_id
            game.question_msg_id = msg.message_id
        except Exception as e:
            print(f"Ошибка отправки видео: {e}")
            msg = await context.bot.send_message(
                text=question_text,
                reply_markup=keyboard,
                **base_kwargs
            )
            game.question_msg_id = msg.id
    else:
        msg = await context.bot.send_message(
            text=question_text,
            reply_markup=keyboard,
            **base_kwargs
        )
        game.question_msg_id = msg.id
    
    game.question_start_time = datetime.now(timezone.utc)
    game.answers.clear()
    
    try:
        await context.bot.pin_chat_message(chat_id=chat_id, message_id=msg.id, disable_notification=False)
    except:
        pass
    
    context.job_queue.run_once(end_question, when=20, chat_id=chat_id, data=chat_id)

async def end_question(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    game = games.get(chat_id)
    if not game or game.status != "active":
        return
    
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
    
    if game.video_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=game.video_msg_id)
        except Exception as e:
            print(f"Не удалось удалить видео: {e}")
    
    send_kwargs = {"chat_id": chat_id, "text": final_text}
    if game.message_thread_id:
        send_kwargs["message_thread_id"] = game.message_thread_id
    await context.bot.send_message(**send_kwargs)
    
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
        send_kwargs = {"chat_id": chat_id, "text": "⏸ Квиз приостановлен. /resume для продолжения."}
        if game.message_thread_id:
            send_kwargs["message_thread_id"] = game.message_thread_id
        await context.bot.send_message(**send_kwargs)
        return
    
    if game.current_question < len(game.pack["questions"]):
        context.job_queue.run_once(start_question, when=5, chat_id=chat_id, data=chat_id)
    else:
        game.status = "finished"
        send_kwargs = {"chat_id": chat_id, "text": "🎉 Викторина закончена! Подводим итоги..."}
        if game.message_thread_id:
            send_kwargs["message_thread_id"] = game.message_thread_id
        await context.bot.send_message(**send_kwargs)
        context.job_queue.run_once(finish_quiz, when=5, chat_id=chat_id, data=chat_id)

async def finish_quiz(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    game = games.pop(chat_id, None)
    if not game:
        return
    
    # Сохраняем результаты в Google Sheets
    await save_quiz_results(game)
    
    leaderboard = game.get_leaderboard()
    if not leaderboard:
        send_kwargs = {"chat_id": chat_id, "text": "Нет участников."}
        if game.message_thread_id:
            send_kwargs["message_thread_id"] = game.message_thread_id
        await context.bot.send_message(**send_kwargs)
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
    table = "🏁 Итоговое положение:\n\n" + "\n".join(final_lines)
    send_kwargs = {"chat_id": chat_id, "text": table}
    if game.message_thread_id:
        send_kwargs["message_thread_id"] = game.message_thread_id
    await context.bot.send_message(**send_kwargs)

# -------------------- Команда /leaderboard --------------------
async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает общий рейтинг всех игроков по итогам всех квизов"""
    await update.message.reply_text("🔄 Загружаю статистику...")
    
    leaderboard = await get_leaderboard_stats()
    if not leaderboard:
        await update.message.reply_text("❌ Пока нет сохранённых результатов. Проведите хотя бы один квиз!")
        return
    
    # Формируем сообщение
    message = "🏆 *ОБЩИЙ РЕЙТИНГ ИГРОКОВ*\n\n"
    for i, player in enumerate(leaderboard[:20], 1):  # Показываем топ-20
        medal = ""
        if i == 1:
            medal = "🥇"
        elif i == 2:
            medal = "🥈"
        elif i == 3:
            medal = "🥉"
        
        message += f"{medal} *{i}. {player['username']}*\n"
        message += f"   📊 Игр: {player['games']}\n"
        message += f"   ⭐ Сумма очков: {player['total_score']}\n"
        message += f"   📈 Среднее: {player['avg_score']}\n\n"
    
    if len(leaderboard) > 20:
        message += f"*И ещё {len(leaderboard) - 20} игроков...*"
    
    await update.message.reply_text(message, parse_mode="Markdown")

# -------------------- Команда /stats --------------------
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает личную статистику игрока"""
    user = update.effective_user
    username = format_username(user)
    
    await update.message.reply_text("🔄 Загружаю вашу статистику...")
    
    stats = await get_user_stats(user.id)
    if not stats:
        await update.message.reply_text(f"❌ {username}, у вас пока нет сохранённых результатов. Сыграйте в квиз!")
        return
    
    message = f"📊 *ЛИЧНАЯ СТАТИСТИКА*\n\n"
    message += f"👤 Игрок: {stats['username']}\n"
    message += f"🎮 Сыграно квизов: {stats['games']}\n"
    message += f"⭐ Сумма очков: {stats['total_score']}\n"
    message += f"📈 Средний балл за квиз: {stats['avg_score']}\n"
    
    await update.message.reply_text(message, parse_mode="Markdown")

# -------------------- Остальные команды --------------------
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
    if game.current_question < len(game.pack["questions"]):
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

# -------------------- ЗАПУСК --------------------
def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise ValueError("❌ Не задан BOT_TOKEN")

    app = Application.builder().token(token).build()
    
    app.add_handler(CommandHandler("quiz", quiz_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("pause", pause_quiz))
    app.add_handler(CommandHandler("resume", resume_quiz))
