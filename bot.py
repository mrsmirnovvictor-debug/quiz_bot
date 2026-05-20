import sys
import os
import re
import json
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# -------------------- Google Sheets импорт --------------------
try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    GOOGLE_SHEETS_AVAILABLE = True
except ImportError:
    GOOGLE_SHEETS_AVAILABLE = False
    print("⚠️ gspread не установлен, статистика не будет сохраняться")

# -------------------- Константы --------------------
TIMER_VIDEO_URL = os.environ.get("TIMER_VIDEO_URL", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS", "")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")

# -------------------- Функции для работы с Google Sheets --------------------
def init_google_sheets():
    if not GOOGLE_SHEETS_AVAILABLE:
        return None
    if not GOOGLE_CREDENTIALS_JSON:
        print("⚠️ GOOGLE_CREDENTIALS не заданы")
        return None
    if not GOOGLE_SHEET_ID:
        print("⚠️ GOOGLE_SHEET_ID не задан")
        return None
    
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(GOOGLE_SHEET_ID)
        return sheet
    except Exception as e:
        print(f"❌ Ошибка подключения: {e}")
        return None

def ensure_sheets_exist(sheet):
    try:
        try:
            games_sheet = sheet.worksheet("Games")
        except gspread.WorksheetNotFound:
            games_sheet = sheet.add_worksheet(title="Games", rows=1, cols=100)
            headers = ["Дата", "Chat ID", "Название квиза", "Игрок", "Место", "Общий счёт",
                       "Количество вопросов", "Правильные ответы", "Неправильные ответы", "Без ответа",
                       "Общее время ответов", "Общее время правильных ответов",
                       "Среднее время ответа", "Среднее время (правильные)", "ELO после игры", "% правильных ответов"]
            for i in range(1, 17):
                headers.append(f"Вопрос {i} ответ")
                headers.append(f"Вопрос {i} баллы")
                headers.append(f"Вопрос {i} время")
            games_sheet.append_row(headers)
            print("✅ Лист Games создан")
        
        try:
            players_sheet = sheet.worksheet("Players")
        except gspread.WorksheetNotFound:
            players_sheet = sheet.add_worksheet(title="Players", rows=1, cols=20)
            players_sheet.append_row(["Игрок", "Количество игр", "Всего очков", "Средний балл за квиз",
                                      "Среднее время ответа", "Среднее время (правильные)",
                                      "% правильных ответов", "ELO"])
            print("✅ Лист Players создан")
        
        return games_sheet, players_sheet
    except Exception as e:
        print(f"Ошибка при создании листов: {e}")
        return None, None

def calculate_elo(score: int, max_score: int, avg_time_correct: float, total_players: int, place: int) -> int:
    if max_score == 0:
        return 20
    score_percent = (score / max_score) * 100
    speed_bonus = max(0, 30 - avg_time_correct) if avg_time_correct > 0 else 0
    place_bonus = (total_players - place + 1) * 5
    participation_bonus = 20
    elo = int(score_percent + speed_bonus + place_bonus + participation_bonus)
    return max(20, min(300, elo))

def save_game_results(game, players_ranking, avg_times_all, avg_times_correct, player_answers_detail):
    sheet = init_google_sheets()
    if not sheet:
        return
    
    games_sheet, players_sheet = ensure_sheets_exist(sheet)
    if not games_sheet or not players_sheet:
        return
    
    now_moscow = datetime.now(timezone.utc) + timedelta(hours=3)
    date_str = now_moscow.strftime("%Y-%m-%d %H:%M:%S")
    max_possible_score = len(game.pack["questions"]) * 15
    total_questions = len(game.pack["questions"])
    
    # ---------- 1. Добавляем строки в лист Games ----------
    for place_info in players_ranking:
        place = place_info["place"]
        for player in place_info["players"]:
            user_id = player["user_id"]
            username = player["username"]
            score = player["score"]
            correct_count = player["correct_count"]
            total_answered = sum(1 for a in player_answers_detail.get(user_id, []) if a.get("time", 0) >= 0)
            incorrect_count = total_answered - correct_count
            no_answer = total_questions - total_answered
            avg_time_all = avg_times_all.get(user_id, 0)
            avg_time_correct = avg_times_correct.get(user_id, 0)
            correct_percent = (correct_count / total_questions) * 100 if total_questions > 0 else 0
            elo = calculate_elo(score, max_possible_score, avg_time_correct, len(game.registered), place)
            
            # Время в секундах (НЕ умножаем на 100)
            row = [date_str, str(game.chat_id), game.pack["title"], username, place, score,
                   total_questions, correct_count, incorrect_count, no_answer,
                   round(avg_time_all * total_answered, 2),   # K: Общее время ответов (сек)
                   round(avg_time_correct * correct_count, 2), # L: Общее время правильных (сек)
                   round(avg_time_all, 2),   # M: Среднее время ответа (сек) - вспомогательное
                   round(avg_time_correct, 2), # N: Среднее время правильных (сек) - вспомогательное
                   elo, round(correct_percent, 2)]
            
            answers_detail = player_answers_detail.get(user_id, [])
            for q_idx in range(total_questions):
                if q_idx < len(answers_detail):
                    row.append(answers_detail[q_idx].get("answer", "-"))
                    row.append(answers_detail[q_idx].get("points", 0))
                    row.append(round(answers_detail[q_idx].get("time", 0), 2) if answers_detail[q_idx].get("time") else 0)
                else:
                    row.append("-")
                    row.append(0)
                    row.append(0)
            
            games_sheet.append_row(row)
    
    # ---------- 2. Полностью пересчитываем статистику для Players ----------
    try:
        all_games = games_sheet.get_all_records()
    except Exception as e:
        print(f"Ошибка чтения Games для пересчета Players: {e}")
        return
    
    player_stats = {}
    for row in all_games:
        username = row.get("Игрок")
        if not username:
            continue
        
        def to_float(v):
            if isinstance(v, str):
                v = v.replace(',', '.')
            try:
                return float(v)
            except:
                return 0.0
        
        def to_int(v):
            if isinstance(v, str):
                v = v.replace(',', '.')
            try:
                return int(float(v))
            except:
                return 0
        
        score = to_float(row.get("Общий счёт", 0))
        total_questions = to_int(row.get("Количество вопросов", 0))
        correct = to_int(row.get("Правильные ответы", 0))
        incorrect = to_int(row.get("Неправильные ответы", 0))
        total_time_all = to_float(row.get("Общее время ответов", 0))      # колонка K
        total_time_correct = to_float(row.get("Общее время правильных ответов", 0))  # колонка L
        elo = to_float(row.get("ELO после игры", 0))
        
        if username not in player_stats:
            player_stats[username] = {
                "games_count": 0,
                "total_score": 0.0,
                "total_questions": 0,
                "total_correct": 0,
                "total_incorrect": 0,
                "total_time_all": 0.0,      # сумма K
                "total_time_correct": 0.0,  # сумма L
                "elos": []
            }
        stats = player_stats[username]
        stats["games_count"] += 1
        stats["total_score"] += score
        stats["total_questions"] += total_questions
        stats["total_correct"] += correct
        stats["total_incorrect"] += incorrect
        stats["total_time_all"] += total_time_all
        stats["total_time_correct"] += total_time_correct
        stats["elos"].append(elo)
    
    # Формируем новые строки для Players с ПРАВИЛЬНЫМИ формулами
    new_rows = []
    for username, stats in player_stats.items():
        games_count = stats["games_count"]
        total_score = stats["total_score"]
        avg_score = total_score / games_count if games_count > 0 else 0
        
        total_correct = stats["total_correct"]
        total_incorrect = stats["total_incorrect"]
        total_answered = total_correct + total_incorrect
        
        # ПРАВИЛЬНАЯ формула: среднее время ответа = сумма K / (H + I)
        avg_time_all = stats["total_time_all"] / total_answered if total_answered > 0 else 0
        
        # ПРАВИЛЬНАЯ формула: среднее время правильных = сумма L / H
        avg_time_correct = stats["total_time_correct"] / total_correct if total_correct > 0 else 0
        
        total_questions = stats["total_questions"]
        percent_correct = (total_correct / total_questions) * 100 if total_questions > 0 else 0
        
        avg_elo = int(round(sum(stats["elos"]) / len(stats["elos"]))) if stats["elos"] else 0
        
        new_rows.append([
            username,
            games_count,
            round(total_score),      # целое число
            round(avg_score, 1),
            round(avg_time_all, 1),   # теперь 4.9, а не 4.1
            round(avg_time_correct, 1),
            round(percent_correct, 1),
            avg_elo
        ])
    
    # ---------- 3. Очищаем Players и записываем новые данные ----------
    try:
        all_cells = players_sheet.get_all_values()
        if len(all_cells) > 1:
            players_sheet.delete_rows(2, len(all_cells) - 1)
        if new_rows:
            players_sheet.append_rows(new_rows, value_input_option='USER_ENTERED')
        print(f"✅ Статистика Players обновлена для {len(new_rows)} игроков")
    except Exception as e:
        print(f"Ошибка обновления Players: {e}")
    
    print(f"✅ Результаты сохранены в Google Sheets")

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
        self.reminder_msg_id = None
        self.question_msg_id = None
        self.video_msg_id = None
        self.reg_timer_job = None
        self.question_timer_job = None
        self.reminder_timer_job = None
        self.scheduled_start_utc = scheduled_start_utc
        self.paused = False
        self.pause_after_question = False
        self.user_speed_sum = {}
        self.user_correct_count = {}
        self.user_total_answered = {}
        self.current_question_image = ""
        self.user_answers_detail = defaultdict(list)

    def add_player(self, user_id, username):
        if user_id not in self.registered:
            self.registered[user_id] = {"username": username, "score": 0}
            self.user_speed_sum[user_id] = 0
            self.user_correct_count[user_id] = 0
            self.user_total_answered[user_id] = 0

    def record_answer(self, user_id, option_idx, answer_time: datetime):
        if self.status != "active" or user_id not in self.registered or self.paused:
            return
        if user_id in self.answers:
            return
        
        q = self.pack["questions"][self.current_question]
        is_correct = (option_idx == q["correct"])
        delta = (answer_time - self.question_start_time).total_seconds()
        answer_text = q["options"][option_idx]
        
        points = 0
        if is_correct:
            points = 10
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
            self.registered[user_id]["score"] += points
            self.user_speed_sum[user_id] += delta
            self.user_correct_count[user_id] += 1
        
        self.user_total_answered[user_id] += 1
        self.user_answers_detail[user_id].append({
            "answer": answer_text,
            "points": points,
            "time": delta,
            "is_correct": is_correct
        })
        self.answers[user_id] = (option_idx, answer_time, is_correct, points)

    def get_leaderboard(self):
        items = list(self.registered.items())
        items.sort(key=lambda x: (-x[1]["score"], self.user_speed_sum.get(x[0], 0)))
        return items

    def calculate_final_ranking(self):
        players = []
        for uid, data in self.registered.items():
            players.append({
                "user_id": uid,
                "username": data["username"],
                "score": data["score"],
                "speed_sum": self.user_speed_sum.get(uid, 0),
                "correct_count": self.user_correct_count.get(uid, 0)
            })
        players.sort(key=lambda x: (-x["score"], x["speed_sum"]))
        
        ranking = []
        current_place = 1
        i = 0
        while i < len(players):
            current_score = players[i]["score"]
            same_score_players = []
            while i < len(players) and players[i]["score"] == current_score:
                same_score_players.append(players[i])
                i += 1
            ranking.append({
                "place": current_place,
                "players": same_score_players
            })
            current_place += len(same_score_players)
        return ranking

    def get_player_avg_times(self):
        avg_times_all = {}
        avg_times_correct = {}
        for uid, answers in self.user_answers_detail.items():
            times = [a["time"] for a in answers]
            correct_times = [a["time"] for a in answers if a["is_correct"]]
            avg_times_all[uid] = sum(times) / len(times) if times else 0
            avg_times_correct[uid] = sum(correct_times) / len(correct_times) if correct_times else 0
        return avg_times_all, avg_times_correct

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
        reminder_delay = delay - 15 * 60
        if reminder_delay > 0:
            context.job_queue.run_once(send_15_min_reminder, when=reminder_delay, chat_id=chat_id, data=chat_id)
    else:
        await start_quiz_sequence(context, chat_id)

# -------------------- Напоминание за 15 минут --------------------
async def send_15_min_reminder(context: ContextTypes.DEFAULT_TYPE, chat_id: int = None):
    if chat_id is None:
        chat_id = context.job.data
    game = games.get(chat_id)
    if not game or game.status != "registration":
        return
    
    reminder_text = (
        f"⏰ Через 15 минут начнётся квиз на тему: \"{game.pack['title']}\".\n"
        f"Успевайте зарегистрироваться по ссылке: {game.reg_msg_link if hasattr(game, 'reg_msg_link') else 'регистрация в этом чате'}"
    )
    msg = await context.bot.send_message(chat_id=chat_id, text=reminder_text)
    game.reminder_msg_id = msg.message_id
    
    try:
        await context.bot.pin_chat_message(chat_id=chat_id, message_id=msg.message_id, disable_notification=False)
    except Exception as e:
        print(f"Не удалось закрепить напоминание: {e}")

# -------------------- Регистрация и запуск --------------------
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
    game.reg_msg_link = f"https://t.me/c/{str(chat_id)[4:]}/{msg.message_id}"
    
    try:
        await context.bot.pin_chat_message(chat_id=chat_id, message_id=msg.id, disable_notification=False)
    except:
        pass
    
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
    except:
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
    except:
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
        if job.chat_id == chat_id and job.callback == send_15_min_reminder:
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
    
    if game.reminder_msg_id:
        try:
            await context.bot.unpin_chat_message(chat_id=chat_id, message_id=game.reminder_msg_id)
        except:
            pass
    
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
            mentions.append("Участник")
    mention_text = " ".join(mentions) if mentions else "Участники"
    warning_text = (
        f"{mention_text}\n\n"
        f"Квиз начнется через 30 секунд! Даём Вам время зайти в Телеграм, проверить ваш VPN и настроиться быстро, "
        f"а главное правильно отвечать на вопросы!"
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
    game.current_question_image = q.get("image", "")
    
    if TIMER_VIDEO_URL:
        try:
            video_msg = await context.bot.send_video(
                video=TIMER_VIDEO_URL,
                width=200,
                height=150,
                supports_streaming=True,
                **base_kwargs
            )
            game.video_msg_id = video_msg.message_id
        except Exception as e:
            print(f"Ошибка отправки видео: {e}")
    
    await asyncio.sleep(0.5)
    
    if q.get("image"):
        msg = await context.bot.send_photo(
            photo=q["image"],
            caption=question_text,
            reply_markup=keyboard,
            **base_kwargs
        )
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
    
    total_answers = len(game.answers)
    counts = [0] * len(q["options"])
    for uid, (opt_idx, _, is_correct, _) in game.answers.items():
        if 0 <= opt_idx < len(counts):
            counts[opt_idx] += 1
    
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
    
    final_text = f"❓ Вопрос {game.current_question+1}/{len(game.pack['questions'])}\n{q['text']}\n\n{stats_text}\n\n{correct_answer_text}"
    
    try:
        await context.bot.unpin_chat_message(chat_id=chat_id, message_id=game.question_msg_id)
    except:
        pass
    
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=game.question_msg_id)
    except:
        pass
    
    if game.video_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=game.video_msg_id)
        except:
            pass
    
    send_kwargs = {"chat_id": chat_id}
    if game.message_thread_id:
        send_kwargs["message_thread_id"] = game.message_thread_id
    
    if game.current_question_image:
        await context.bot.send_photo(
            photo=game.current_question_image,
            caption=final_text,
            **send_kwargs
        )
    else:
        await context.bot.send_message(text=final_text, **send_kwargs)
    
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
        send_kwargs = {"chat_id": chat_id, "text": "Викторина закончена! Подводим итоги..."}
        if game.message_thread_id:
            send_kwargs["message_thread_id"] = game.message_thread_id
        await context.bot.send_message(**send_kwargs)
        context.job_queue.run_once(finish_quiz, when=5, chat_id=chat_id, data=chat_id)

async def finish_quiz(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    game = games.get(chat_id)
    if not game:
        return
    
    players_ranking = game.calculate_final_ranking()
    avg_times_all, avg_times_correct = game.get_player_avg_times()
    
    try:
        save_game_results(game, players_ranking, avg_times_all, avg_times_correct, game.user_answers_detail)
    except Exception as e:
        print(f"Ошибка при сохранении в Google Sheets: {e}")
    
    if not players_ranking:
        send_kwargs = {"chat_id": chat_id, "text": "Нет участников."}
        if game.message_thread_id:
            send_kwargs["message_thread_id"] = game.message_thread_id
        await context.bot.send_message(**send_kwargs)
        games.pop(chat_id, None)
        return
    
    # 3 место
    third_place = None
    for medal in players_ranking:
        if medal["place"] == 3:
            third_place = medal
            break
    
    if third_place:
        mentions = []
        for p in third_place["players"]:
            username = p["username"]
            mentions.append(username)
        mention_str = " и ".join(mentions)
        if len(third_place["players"]) == 1:
            text_3rd = f"Почетное 3 место занимает {mention_str}. Поздравляем!"
        else:
            text_3rd = f"Почетное 3 место разделили игроки {mention_str}. Поздравляем!"
        send_kwargs = {"chat_id": chat_id, "text": text_3rd}
        if game.message_thread_id:
            send_kwargs["message_thread_id"] = game.message_thread_id
        await context.bot.send_message(**send_kwargs)
        await asyncio.sleep(3)
    
    # 2 место
    second_place = None
    for medal in players_ranking:
        if medal["place"] == 2:
            second_place = medal
            break
    
    if second_place:
        mentions = []
        for p in second_place["players"]:
            username = p["username"]
            mentions.append(username)
        mention_str = " и ".join(mentions)
        if len(second_place["players"]) == 1:
            text_2nd = f"Немного не хватило для победы, 2 место занимает {mention_str}. Поздравляем!"
        else:
            text_2nd = f"Немного не хватило для победы, 2 место разделили игроки {mention_str}. Поздравляем!"
        send_kwargs = {"chat_id": chat_id, "text": text_2nd}
        if game.message_thread_id:
            send_kwargs["message_thread_id"] = game.message_thread_id
        await context.bot.send_message(**send_kwargs)
        await asyncio.sleep(3)
    
    # 1 место с пином
    first_place = players_ranking[0] if players_ranking else None
    
    if first_place:
        mentions = []
        for p in first_place["players"]:
            username = p["username"]
            mentions.append(username)
        mention_str = " и ".join(mentions)
        if len(first_place["players"]) == 1:
            text_1st = f"Поздравляем победителя нашей викторины — {mention_str}! 🎉🥳"
        else:
            text_1st = f"Поздравляем победителей нашей викторины — {mention_str}! 🎉🥳"
        send_kwargs = {"chat_id": chat_id, "text": text_1st}
        if game.message_thread_id:
            send_kwargs["message_thread_id"] = game.message_thread_id
        msg = await context.bot.send_message(**send_kwargs)
        try:
            await context.bot.pin_chat_message(chat_id=chat_id, message_id=msg.message_id, disable_notification=False)
        except:
            pass
        await asyncio.sleep(3)
    
    # Полная таблица
    final_lines = ["🏁 Итоговое положение:\n"]
    for medal in players_ranking:
        place = medal["place"]
        players_list = medal["players"]
        if place == 1:
            medal_emoji = "🥇"
        elif place == 2:
            medal_emoji = "🥈"
        elif place == 3:
            medal_emoji = "🥉"
        else:
            medal_emoji = f"{place}."
        for p in players_list:
            final_lines.append(f"{medal_emoji} {p['username']} — {p['score']} очк.")
    table = "\n".join(final_lines)
    send_kwargs = {"chat_id": chat_id, "text": table}
    if game.message_thread_id:
        send_kwargs["message_thread_id"] = game.message_thread_id
    await context.bot.send_message(**send_kwargs)
    games.pop(chat_id, None)

async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    chat_id = update.effective_chat.id
    game = games.get(chat_id)
    
    if not game or game.status != "active" or game.paused:
        await query.answer("Квиз не активен", show_alert=False)
        return
    
    if user.id in game.answers:
        await query.answer("Вы уже ответили на этот вопрос!", show_alert=True)
        return
    
    try:
        option_idx = int(query.data.split("_")[1])
    except:
        await query.answer("Ошибка", show_alert=False)
        return
    
    await query.answer("Ответ принят ✅", show_alert=False)
    now = datetime.now(timezone.utc)
    game.record_answer(user.id, option_idx, now)

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
        await update.message.reply_text("❌ Только организатор может остановить квиз.")
        return
    for job in context.job_queue.jobs():
        if job.chat_id == chat_id:
            job.schedule_removal()
    games.pop(chat_id, None)
    send_kwargs = {"chat_id": chat_id, "text": "Квиз остановлен. Необходимо запустить заново."}
    if game.message_thread_id:
        send_kwargs["message_thread_id"] = game.message_thread_id
    await context.bot.send_message(**send_kwargs)

# -------------------- Команда /stats (общая статистика по группе) --------------------
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("🔄 Загружаю статистику по этой группе...")

    sheet = init_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Статистика временно недоступна.")
        return

    try:
        games_sheet = sheet.worksheet("Games")
        all_games = games_sheet.get_all_records()

        # Фильтруем только игры текущего чата
        chat_games = [row for row in all_games if str(row.get("Chat ID", "")) == str(chat_id)]

        if not chat_games:
            await update.message.reply_text("❌ В этой группе пока нет сыгранных квизов.")
            return

        player_agg = defaultdict(lambda: {
            "total_score": 0.0,
            "total_correct": 0,
            "total_incorrect": 0,
            "total_time_all": 0.0,
            "total_time_correct": 0.0,
            "total_questions": 0,
            "games_count": 0,
        })

        for row in chat_games:
            def to_float(v):
                if isinstance(v, str):
                    v = v.replace(',', '.')
                try:
                    return float(v)
                except:
                    return 0.0

            def to_int(v):
                if isinstance(v, str):
                    v = v.replace(',', '.')
                try:
                    return int(float(v))
                except:
                    return 0

            username = row.get("Игрок", "")
            if not username:
                continue

            correct = to_int(row.get("Правильные ответы", 0))
            incorrect = to_int(row.get("Неправильные ответы", 0))
            total_questions = to_int(row.get("Количество вопросов", 0))
            score = to_float(row.get("Общий счёт", 0))
            
            # Используем колонки K и L (общее время в секундах)
            total_time_all = to_float(row.get("Общее время ответов", 0))
            total_time_correct = to_float(row.get("Общее время правильных ответов", 0))

            agg = player_agg[username]
            agg["total_score"] += score
            agg["total_correct"] += correct
            agg["total_incorrect"] += incorrect
            agg["total_time_all"] += total_time_all
            agg["total_time_correct"] += total_time_correct
            agg["total_questions"] += total_questions
            agg["games_count"] += 1

        if not player_agg:
            await update.message.reply_text("❌ Нет данных для отображения.")
            return

        # Сортируем по общему счёту
        sorted_players = sorted(player_agg.items(), key=lambda x: x[1]["total_score"], reverse=True)

        message = "🏆 ОБЩАЯ СТАТИСТИКА ПО ГРУППЕ\n\n"
        for i, (username, agg) in enumerate(sorted_players[:20], 1):
            medal = ""
            if i == 1:
                medal = "🥇"
            elif i == 2:
                medal = "🥈"
            elif i == 3:
                medal = "🥉"

            games_count = agg["games_count"]
            total_score = agg["total_score"]
            avg_score = total_score / games_count if games_count > 0 else 0

            total_answered = agg["total_correct"] + agg["total_incorrect"]
            avg_time_all = agg["total_time_all"] / total_answered if total_answered > 0 else 0
            avg_time_correct = agg["total_time_correct"] / agg["total_correct"] if agg["total_correct"] > 0 else 0

            total_questions_sum = agg["total_questions"]
            correct_percent = (agg["total_correct"] / total_questions_sum) * 100 if total_questions_sum > 0 else 0

            message += f"{medal} {i}. {username}\n"
            message += f"   📊 Игр: {games_count}\n"
            message += f"   ⭐ Всего очков: {total_score:.0f}\n"
            message += f"   📈 Средний балл: {avg_score:.1f}\n"
            message += f"   ⏱️ Среднее время: {avg_time_all:.1f} сек\n"
            message += f"   ⏱️ Среднее время (правильные): {avg_time_correct:.1f} сек\n"
            message += f"   ✅ % правильных ответов: {correct_percent:.1f}%\n\n"

        await update.message.reply_text(message)

    except Exception as e:
        print(f"Ошибка получения статистики: {e}")
        await update.message.reply_text("❌ Ошибка загрузки статистики.")

# -------------------- Команда /history (только в личку, по всем группам) --------------------
async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Проверяем, что команда вызвана в личном чате
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "📩 История ваших игр доступна только в личных сообщениях с ботом.\n"
            "Пожалуйста, напишите /history мне в личку."
        )
        return

    user = update.effective_user
    username = format_username(user)
    await update.message.reply_text("🔄 Загружаю вашу историю...")

    sheet = init_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ История временно недоступна.")
        return

    try:
        games_sheet = sheet.worksheet("Games")
        all_games = games_sheet.get_all_records()

        # Фильтруем все игры этого игрока (во всех группах)
        user_games = [row for row in all_games if row.get("Игрок") == username]

        if not user_games:
            await update.message.reply_text(f"❌ {username}, у вас пока нет сыгранных квизов.")
            return

        user_games.sort(key=lambda x: x.get("Дата", ""), reverse=True)

        message = f"📜 ИСТОРИЯ ИГРОКА {username} (ВСЕ ГРУППЫ)\n\n"
        for i, game_record in enumerate(user_games[:10], 1):
            def to_float_val(v):
                if isinstance(v, str):
                    v = v.replace(',', '.')
                try:
                    return float(v)
                except:
                    return 0.0

            avg_time = to_float_val(game_record.get("Среднее время ответа", 0))
            correct_percent = to_float_val(game_record.get("% правильных ответов", 0))

            message += f"{i}. {game_record.get('Название квиза', '-')}\n"
            message += f"   📅 Дата: {game_record.get('Дата', '-')}\n"
            message += f"   🏆 Место: {game_record.get('Место', '-')}\n"
            message += f"   ⭐ Очки: {game_record.get('Общий счёт', 0):.0f}\n"
            message += f"   ⏱️ Среднее время: {avg_time:.1f} сек\n"
            message += f"   ✅ % правильных ответов: {correct_percent:.1f}%\n"
            message += f"   🎯 ELO после игры: {game_record.get('ELO после игры', 0)}\n\n"

        if len(user_games) > 10:
            message += f"и ещё {len(user_games) - 10} игр..."

        await update.message.reply_text(message)

    except Exception as e:
        print(f"Ошибка получения истории: {e}")
        await update.message.reply_text("❌ Ошибка загрузки истории.")

# -------------------- Диагностическая команда /debug_stats --------------------
async def debug_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("🔍 Получаю сырые данные...")
    
    sheet = init_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Нет доступа к Sheets")
        return
    
    try:
        games_sheet = sheet.worksheet("Games")
        all_games = games_sheet.get_all_records()
        
        chat_games = [row for row in all_games if str(row.get("Chat ID", "")) == str(chat_id)]
        
        if not chat_games:
            await update.message.reply_text("Нет игр")
            return
        
        message = "🔍 Сырые данные (первые 5 игр):\n\n"
        for i, row in enumerate(chat_games[:5], 1):
            message += f"Игра {i}:\n"
            message += f"  Игрок: {row.get('Игрок')}\n"
            message += f"  K (Общее время ответов): {row.get('Общее время ответов')}\n"
            message += f"  L (Общее время правильных): {row.get('Общее время правильных ответов')}\n"
            message += f"  M (Среднее время ответа): {row.get('Среднее время ответа')}\n"
            message += f"  N (Среднее время правильные): {row.get('Среднее время (правильные)')}\n"
            message += f"  Правильные ответы: {row.get('Правильные ответы')}\n"
            message += f"  Кол-во вопросов: {row.get('Количество вопросов')}\n\n"
        
        await update.message.reply_text(message)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

# -------------------- ЗАПУСК --------------------
def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise ValueError("❌ Не задан BOT_TOKEN")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("quiz", quiz_command))
    app.add_handler(CommandHandler("pause", pause_quiz))
    app.add_handler(CommandHandler("resume", resume_quiz))
    app.add_handler(CommandHandler("abort", abort_quiz))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("debug_stats", debug_stats_command))  # диагностическая команда
    app.add_handler(CallbackQueryHandler(register_callback, pattern="register"))
    app.add_handler(CallbackQueryHandler(start_early_callback, pattern="start_early"))
    app.add_handler(CallbackQueryHandler(answer_callback, pattern=r"ans_\d+"))

    print("🚀 Бот запущен в режиме polling")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
