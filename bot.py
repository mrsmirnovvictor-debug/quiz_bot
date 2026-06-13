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
    Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
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
SCHEDULE_ENABLED = os.environ.get("SCHEDULE_ENABLED", "true").lower() == "true"

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
    """Создаёт листы Games, Players и Schedule, если их нет"""
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
        
        try:
            schedule_sheet = sheet.worksheet("Schedule")
        except gspread.WorksheetNotFound:
            schedule_sheet = sheet.add_worksheet(title="Schedule", rows=1, cols=6)
            schedule_sheet.append_row(["Chat ID", "Пакет", "Дата и время (МСК)", "Статус", "Message Thread ID", "Last Processed"])
            print("✅ Лист Schedule создан")
        
        return games_sheet, players_sheet, schedule_sheet
    except Exception as e:
        print(f"Ошибка при создании листов: {e}")
        return None, None, None

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
    
    games_sheet, players_sheet, _ = ensure_sheets_exist(sheet)
    if not games_sheet or not players_sheet:
        return
    
    now_moscow = datetime.now(timezone.utc) + timedelta(hours=3)
    date_str = now_moscow.strftime("%Y-%m-%d %H:%M:%S")
    max_possible_score = len(game.pack["questions"]) * 15
    total_questions = len(game.pack["questions"])
    
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
            
            row = [date_str, str(game.chat_id), game.pack["title"], username, place, score,
                   total_questions, correct_count, incorrect_count, no_answer,
                   round(avg_time_all * total_answered, 2) if total_answered > 0 else 0,
                   round(avg_time_correct * correct_count, 2) if correct_count > 0 else 0,
                   round(avg_time_all, 2), round(avg_time_correct, 2), elo, round(correct_percent, 2)]
            
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
    
    # Обновляем общую статистику игроков в Players
    try:
        existing_data = players_sheet.get_all_records()
        player_stats = {row["Игрок"]: row for row in existing_data}
    except:
        player_stats = {}
    
    for place_info in players_ranking:
        for player in place_info["players"]:
            username = player["username"]
            score = player["score"]
            correct_count = player["correct_count"]
            total_questions = len(game.pack["questions"])
            correct_percent = (correct_count / total_questions) * 100 if total_questions > 0 else 0
            avg_time_all = avg_times_all.get(player["user_id"], 0)
            avg_time_correct = avg_times_correct.get(player["user_id"], 0)
            elo = calculate_elo(score, max_possible_score, avg_time_correct, len(game.registered), place_info["place"])
            
            if username in player_stats:
                stats = player_stats[username]
                old_games = stats["Количество игр"]
                new_games = old_games + 1
                new_total_score = stats["Всего очков"] + score
                
                row_idx = None
                for idx, row in enumerate(existing_data):
                    if row["Игрок"] == username:
                        row_idx = idx + 2
                        break
                
                if row_idx:
                    new_avg_score = new_total_score / new_games
                    new_avg_time_all = (stats["Среднее время ответа"] * old_games + avg_time_all) / new_games if avg_time_all > 0 else stats["Среднее время ответа"]
                    new_avg_time_correct = (stats["Среднее время (правильные)"] * old_games + avg_time_correct) / new_games if avg_time_correct > 0 else stats["Среднее время (правильные)"]
                    new_correct_percent = (stats["% правильных ответов"] * old_games + correct_percent) / new_games
                    new_elo = max(stats["ELO"], elo)
                    
                    players_sheet.update([[new_games]], f"B{row_idx}")
                    players_sheet.update([[new_total_score]], f"C{row_idx}")
                    players_sheet.update([[round(new_avg_score, 2)]], f"D{row_idx}")
                    players_sheet.update([[round(new_avg_time_all, 2)]], f"E{row_idx}")
                    players_sheet.update([[round(new_avg_time_correct, 2)]], f"F{row_idx}")
                    players_sheet.update([[round(new_correct_percent, 2)]], f"G{row_idx}")
                    players_sheet.update([[new_elo]], f"H{row_idx}")
            else:
                players_sheet.append_row([
                    username, 1, score, round(score, 2),
                    round(avg_time_all, 2), round(avg_time_correct, 2),
                    round(correct_percent, 2), elo
                ])
    
    print(f"✅ Результаты сохранены в Google Sheets")

# -------------------- Функции расписания --------------------
async def check_schedule(context: ContextTypes.DEFAULT_TYPE):
    """Проверяет расписание в Google Sheets и запускает регистрацию на квизы, до которых осталось 45 минут"""
    if not SCHEDULE_ENABLED:
        return
    
    sheet = init_google_sheets()
    if not sheet:
        return
    
    _, _, schedule_sheet = ensure_sheets_exist(sheet)
    if not schedule_sheet:
        return
    
    try:
        records = schedule_sheet.get_all_records()
        now_utc = datetime.now(timezone.utc)
        now_msk = now_utc + timedelta(hours=3)
        
        for record in records:
            if record.get("Статус") != "active":
                continue
            
            chat_id_str = str(record.get("Chat ID", ""))
            if not chat_id_str:
                continue
            
            try:
                chat_id = int(float(chat_id_str))
            except:
                print(f"Некорректный Chat ID: {chat_id_str}")
                continue
            
            # Проверяем, не запущен ли уже квиз в этом чате
            if chat_id in games and games[chat_id].status not in ("finished",):
                continue
            
            pack_id = record.get("Пакет")
            dt_msk_str = record.get("Дата и время (МСК)")
            message_thread_id = record.get("Message Thread ID")
            last_processed = record.get("Last Processed", "")
            
            try:
                dt_msk = datetime.strptime(dt_msk_str, "%Y-%m-%d %H:%M:%S")
            except:
                print(f"Неверный формат даты: {dt_msk_str}")
                continue
            
            time_left = (dt_msk - now_msk).total_seconds()
            
            # Запускаем регистрацию за 45 минут до начала (с запасом 5 минут)
            if 40 * 60 <= time_left <= 50 * 60:
                if last_processed == "registration_started":
                    continue
                
                pack = load_pack(pack_id)
                if not pack:
                    print(f"Пакет {pack_id} не найден для чата {chat_id}")
                    continue
                
                game = Game(
                    chat_id=chat_id,
                    pack=pack,
                    creator_id=None,
                    message_thread_id=int(message_thread_id) if message_thread_id and str(message_thread_id).isdigit() else None,
                    scheduled_start_utc=msk_to_utc(dt_msk)
                )
                games[chat_id] = game
                
                await open_registration(context, chat_id)
                
                delay = time_left
                context.job_queue.run_once(start_quiz_sequence, when=delay, chat_id=chat_id, data=chat_id)
                
                reminder_delay = delay - 15 * 60
                if reminder_delay > 0:
                    context.job_queue.run_once(send_15_min_reminder, when=reminder_delay, chat_id=chat_id, data=chat_id)
                
                for idx, row in enumerate(records):
                    if str(row.get("Chat ID")) == chat_id_str and row.get("Дата и время (МСК)") == dt_msk_str:
                        schedule_sheet.update_cell(idx + 2, 6, "registration_started")
                        break
                
                print(f"✅ Автоматически запущена регистрация для чата {chat_id} на квиз «{pack['title']}»")
            
            elif time_left < 0 and last_processed != "expired":
                for idx, row in enumerate(records):
                    if str(row.get("Chat ID")) == chat_id_str and row.get("Дата и время (МСК)") == dt_msk_str:
                        schedule_sheet.update_cell(idx + 2, 6, "expired")
                        break
                print(f"⏰ Пропущен квиз для чата {chat_id} (время {dt_msk_str})")
    
    except Exception as e:
        print(f"Ошибка при проверке расписания: {e}")

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
        self.delete_messages = False

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

# -------------------- Команда /quiz (ручной запуск) --------------------
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

# -------------------- Регистрация --------------------
async def open_registration(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = games.get(chat_id)
    if not game:
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Зарегистрироваться", callback_data="register")],
        [InlineKeyboardButton("🚀 Начать сейчас", callback_data="start_early")]
    ])
    start_line = format_datetime_msk_multiline(game.scheduled_start_utc) if game.scheduled_start_utc else "📅 Дата и время начала:\nсейчас"
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
    start_line = format_datetime_msk_multiline(game.scheduled_start_utc) if game.scheduled_start_utc else "📅 Дата и время начала:\nсейчас"
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
    start_line = format_datetime_msk_multiline(game.scheduled_start_utc) if game.scheduled_start_utc else "📅 Дата и время начала:\nсейчас"
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
    game.delete_messages = True
    users_list = "\n".join(f"• {p['username']}" for p in game.registered.values())
    start_line = format_datetime_msk_multiline(game.scheduled_start_utc) if game.scheduled_start_utc else "📅 Дата и время начала:\nсейчас"
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
    
    game.delete_messages = False
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
    game.delete_messages = False
    games.pop(chat_id, None)
    send_kwargs = {"chat_id": chat_id, "text": "Квиз остановлен. Необходимо запустить заново."}
    if game.message_thread_id:
        send_kwargs["message_thread_id"] = game.message_thread_id
    await context.bot.send_message(**send_kwargs)

# -------------------- Команда /stats --------------------
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("🔄 Загружаю статистику...")
    
    sheet = init_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Статистика временно недоступна.")
        return
    
    try:
        games_sheet = sheet.worksheet("Games")
        all_games = games_sheet.get_all_records()
        
        chat_games = [row for row in all_games if str(row.get("Chat ID", "")) == str(chat_id)]
        
        if not chat_games:
            await update.message.reply_text("❌ В этой группе пока нет сыгранных квизов.")
            return
        
        player_agg = defaultdict(lambda: {
            "total_score": 0,
            "total_correct": 0,
            "total_incorrect": 0,
            "total_time": 0,
            "total_time_correct": 0,
            "total_questions": 0,
            "games_count": 0
        })
        
        for row in chat_games:
            def to_float(v):
                if isinstance(v, str):
                    v = v.replace(',', '.')
                try:
                    return float(v)
                except:
                    return 0
            def to_int(v):
                if isinstance(v, str):
                    v = v.replace(',', '.')
                try:
                    return int(float(v))
                except:
                    return 0
            
            username = row["Игрок"]
            correct = to_int(row.get("Правильные ответы", 0))
            incorrect = to_int(row.get("Неправильные ответы", 0))
            total_questions = to_int(row.get("Количество вопросов", 0))
            score = to_float(row.get("Общий счёт", 0))
            total_time = to_float(row.get("Общее время ответов", 0))
            total_time_correct = to_float(row.get("Общее время правильных ответов", 0))
            
            player_agg[username]["total_score"] += score
            player_agg[username]["total_correct"] += correct
            player_agg[username]["total_incorrect"] += incorrect
            player_agg[username]["total_time"] += total_time
            player_agg[username]["total_time_correct"] += total_time_correct
            player_agg[username]["total_questions"] += total_questions
            player_agg[username]["games_count"] += 1
        
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
            
            total_answered = agg["total_correct"] + agg["total_incorrect"]
            total_time_sec = agg["total_time"] / 1_000_000
            avg_time_all = total_time_sec / total_answered if total_answered > 0 else 0
            
            total_time_correct_sec = agg["total_time_correct"] / 1_000_000
            avg_time_correct = total_time_correct_sec / agg["total_correct"] if agg["total_correct"] > 0 else 0
            
            correct_percent = (agg["total_correct"] / agg["total_questions"]) * 100 if agg["total_questions"] > 0 else 0
            avg_score = agg["total_score"] / agg["games_count"] if agg["games_count"] > 0 else 0
            
            message += f"{medal} {i}. {username}\n"
            message += f"   📊 Игр: {agg['games_count']}\n"
            message += f"   ⭐ Всего очков: {agg['total_score']:.1f}\n"
            message += f"   📈 Средний балл: {avg_score:.1f}\n"
            message += f"   ⏱️ Среднее время: {avg_time_all:.1f} сек\n"
            message += f"   ⏱️ Среднее время (правильные): {avg_time_correct:.1f} сек\n"
            message += f"   ✅ % правильных ответов: {correct_percent:.1f}%\n\n"
        
        await update.message.reply_text(message)
    except Exception as e:
        print(f"Ошибка получения статистики: {e}")
        await update.message.reply_text("❌ Ошибка загрузки статиститки.")

# -------------------- Команда /history --------------------
async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = format_username(user)
    chat_id = update.effective_chat.id
    
    await update.message.reply_text("🔄 Загружаю вашу историю...")
    
    sheet = init_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ История временно недоступна.")
        return
    
    try:
        games_sheet = sheet.worksheet("Games")
        all_games = games_sheet.get_all_records()
        
        user_games = [row for row in all_games if str(row.get("Chat ID", "")) == str(chat_id) and row.get("Игрок") == username]
        
        if not user_games:
            await update.message.reply_text(f"❌ {username}, у вас пока нет сыгранных квизов в этой группе.")
            return
        
        user_games.sort(key=lambda x: x.get("Дата", ""), reverse=True)
        message = f"📜 ИСТОРИЯ ИГРОКА {username} В ЭТОЙ ГРУППЕ\n\n"
        
        for i, game_record in enumerate(user_games[:10], 1):
            def to_float(v):
                if isinstance(v, str):
                    v = v.replace(',', '.')
                try:
                    return float(v)
                except:
                    return 0
            
            avg_time = to_float(game_record.get("Среднее время ответа", 0))
            correct_percent = to_float(game_record.get("% правильных ответов", 0))
            
            message += f"{i}. {game_record.get('Название квиза', '-')}\n"
            message += f"   📅 Дата: {game_record.get('Дата', '-')}\n"
            message += f"   🏆 Место: {game_record.get('Место', '-')}\n"
            message += f"   ⭐ Очки: {game_record.get('Общий счёт', 0)}\n"
            message += f"   ⏱️ Среднее время: {avg_time:.1f} сек\n"
            message += f"   ✅ % правильных ответов: {correct_percent:.1f}%\n"
            message += f"   🎯 ELO после игры: {game_record.get('ELO после игры', 0)}\n\n"
        
        if len(user_games) > 10:
            message += f"и ещё {len(user_games) - 10} игр..."
        
        await update.message.reply_text(message)
    except Exception as e:
        print(f"Ошибка получения истории: {e}")
        await update.message.reply_text("❌ Ошибка загрузки истории.")

# -------------------- Команды управления расписанием (только в ЛС) --------------------
async def schedule_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает все запланированные квизы (только в личных сообщениях)"""
    if update.effective_chat.type != "private":
        await update.message.reply_text("❌ Эта команда работает только в личных сообщениях с ботом.")
        return
    
    sheet = init_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Статистика временно недоступна.")
        return
    
    try:
        _, _, schedule_sheet = ensure_sheets_exist(sheet)
        records = schedule_sheet.get_all_records()
        
        if not records:
            await update.message.reply_text("📭 В расписании нет активных квизов.")
            return
        
        # Группируем по Chat ID
        grouped = defaultdict(list)
        for r in records:
            if r.get("Статус") == "active":
                grouped[str(r.get("Chat ID"))].append(r)
        
        if not grouped:
            await update.message.reply_text("📭 Нет активных запланированных квизов.")
            return
        
        message = "📅 *ЗАПЛАНИРОВАННЫЕ КВИЗЫ*\n\n"
        for chat_id, games_list in grouped.items():
            # Пытаемся получить название группы через бота
            group_name = chat_id
            try:
                chat = await context.bot.get_chat(chat_id=int(float(chat_id)))
                group_name = chat.title if chat.title else chat_id
            except:
                pass
            message += f"*Группа:* {group_name}\n"
            for g in games_list:
                pack_id = g.get("Пакет")
                dt_msk = g.get("Дата и время (МСК)")
                pack = load_pack(pack_id)
                title = pack["title"] if pack else pack_id
                message += f"  • «{title}» — {dt_msk}\n"
            message += "\n"
        
        await update.message.reply_text(message, parse_mode="Markdown")
    except Exception as e:
        print(f"Ошибка в schedule_list: {e}")
        await update.message.reply_text("❌ Ошибка загрузки расписания.")


async def schedule_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавляет квиз в расписание. Использование в ЛС:
    /schedule_add -1001234567890 | 0007 | 2026-06-20 19:00
    
    Или с указанием ветки:
    /schedule_add -1001234567890 | 0007 | 2026-06-20 19:00 | 12345
    """
    if update.effective_chat.type != "private":
        await update.message.reply_text("❌ Эта команда работает только в личных сообщениях с ботом.")
        return
    
    full_text = update.message.text.strip()
    rest = full_text[14:].strip()  # убираем "/schedule_add "
    parts = re.split(r'\s*\|\s*', rest)
    
    if len(parts) not in [3, 4]:
        await update.message.reply_text(
            "❌ Неверный формат.\n"
            "Используйте:\n"
            "`/schedule_add -1001234567890 | 0007 | 2026-06-20 19:00`\n"
            "или с веткой:\n"
            "`/schedule_add -1001234567890 | 0007 | 2026-06-20 19:00 | 12345`",
            parse_mode="Markdown"
        )
        return
    
    chat_id_str = parts[0]
    pack_id = parts[1]
    dt_msk_str = parts[2]
    message_thread_id = parts[3] if len(parts) == 4 else ""
    
    # Проверяем Chat ID
    try:
        chat_id = int(float(chat_id_str))
    except:
        await update.message.reply_text(f"❌ Некорректный Chat ID: {chat_id_str}")
        return
    
    # Проверяем пакет
    pack = load_pack(pack_id)
    if not pack:
        await update.message.reply_text(f"❌ Пакет {pack_id} не найден.")
        return
    
    # Проверяем дату
    try:
        dt_msk = datetime.strptime(dt_msk_str, "%Y-%m-%d %H:%M")
    except:
        await update.message.reply_text("❌ Неверный формат даты. Используйте: ГГГГ-ММ-ДД ЧЧ:ММ")
        return
    
    now_msk = datetime.now(timezone.utc) + timedelta(hours=3)
    if dt_msk <= now_msk:
        await update.message.reply_text("❌ Время должно быть в будущем.")
        return
    
    # Проверяем, есть ли уже активный квиз в этой группе на это же время
    sheet = init_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Ошибка подключения к Google Sheets.")
        return
    
    _, _, schedule_sheet = ensure_sheets_exist(sheet)
    records = schedule_sheet.get_all_records()
    
    for record in records:
        if (str(record.get("Chat ID")) == str(chat_id) and 
            record.get("Дата и время (МСК)") == dt_msk_str and 
            record.get("Статус") == "active"):
            await update.message.reply_text(f"❌ Квиз на {dt_msk_str} уже запланирован для этой группы.")
            return
    
    # Добавляем запись
    schedule_sheet.append_row([
        str(chat_id),
        pack_id,
        dt_msk_str,
        "active",
        message_thread_id,
        ""
    ])
    
    # Пытаемся получить название группы
    group_name = str(chat_id)
    try:
        chat = await context.bot.get_chat(chat_id=chat_id)
        group_name = chat.title if chat.title else str(chat_id)
    except:
        pass
    
    await update.message.reply_text(
        f"✅ Квиз «{pack['title']}» добавлен в расписание для группы *{group_name}* на {dt_msk_str}.",
        parse_mode="Markdown"
    )


async def schedule_remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет квиз из расписания. Использование в ЛС:
    /schedule_remove -1001234567890 | 0007 | 2026-06-20 19:00
    """
    if update.effective_chat.type != "private":
        await update.message.reply_text("❌ Эта команда работает только в личных сообщениях с ботом.")
        return
    
    full_text = update.message.text.strip()
    rest = full_text[16:].strip()  # убираем "/schedule_remove "
    parts = re.split(r'\s*\|\s*', rest)
    
    if len(parts) != 3:
        await update.message.reply_text(
            "❌ Неверный формат.\n"
            "Используйте:\n"
            "`/schedule_remove -1001234567890 | 0007 | 2026-06-20 19:00`",
            parse_mode="Markdown"
        )
        return
    
    chat_id_str = parts[0]
    pack_id = parts[1]
    dt_msk_str = parts[2]
    
    # Проверяем Chat ID
    try:
        chat_id = int(float(chat_id_str))
    except:
        await update.message.reply_text(f"❌ Некорректный Chat ID: {chat_id_str}")
        return
    
    sheet = init_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Ошибка подключения к Google Sheets.")
        return
    
    _, _, schedule_sheet = ensure_sheets_exist(sheet)
    records = schedule_sheet.get_all_records()
    
    deleted = False
    for idx, record in enumerate(records):
        if (str(record.get("Chat ID")) == str(chat_id) and 
            record.get("Пакет") == pack_id and 
            record.get("Дата и время (МСК)") == dt_msk_str):
            # Меняем статус на "cancelled"
            schedule_sheet.update_cell(idx + 2, 4, "cancelled")
            deleted = True
            break
    
    if deleted:
        pack = load_pack(pack_id)
        title = pack["title"] if pack else pack_id
        await update.message.reply_text(f"✅ Квиз «{title}» на {dt_msk_str} удалён из расписания.")
    else:
        await update.message.reply_text(f"❌ Квиз {pack_id} на {dt_msk_str} не найден в расписании.")

# -------------------- Удаление сообщений во время активной игры --------------------
async def delete_chat_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет сообщения пользователей только в той ветке (топике), где идёт квиз."""
    chat_id = update.effective_chat.id
    message = update.effective_message
    
    # Не удаляем сообщения самого бота
    if message.from_user and message.from_user.id == context.bot.id:
        return
    
    # Не удаляем команды для управления
    if message.text and message.text.startswith('/'):
        return
    
    game = games.get(chat_id)
    if not game or not game.delete_messages:
        return
    
    # Получаем ID ветки, в которой находится сообщение
    message_thread_id = message.message_thread_id
    
    # Если квиз идёт в определённой ветке, а сообщение — в другой — пропускаем
    if game.message_thread_id is not None and message_thread_id != game.message_thread_id:
        return
    
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message.message_id)
    except Exception as e:
        print(f"Не удалось удалить сообщение {message.message_id}: {e}")

# -------------------- ЗАПУСК --------------------
def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise ValueError("❌ Не задан BOT_TOKEN")

    app = Application.builder().token(token).build()
    
    # Основные команды
    app.add_handler(CommandHandler("quiz", quiz_command))
    app.add_handler(CommandHandler("pause", pause_quiz))
    app.add_handler(CommandHandler("resume", resume_quiz))
    app.add_handler(CommandHandler("abort", abort_quiz))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("history", history_command))
    
    # Команды расписания
    app.add_handler(CommandHandler("schedule_list", schedule_list_command))
    app.add_handler(CommandHandler("schedule_add", schedule_add_command))
    app.add_handler(CommandHandler("schedule_remove", schedule_remove_command))
    
    # Callback handlers
    app.add_handler(CallbackQueryHandler(register_callback, pattern="register"))
    app.add_handler(CallbackQueryHandler(start_early_callback, pattern="start_early"))
    app.add_handler(CallbackQueryHandler(answer_callback, pattern=r"ans_\d+"))
    
    # Обработчик удаления сообщений
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, delete_chat_messages), group=1)

    # Запускаем фоновую проверку расписания (каждые 15 минут)
    if SCHEDULE_ENABLED:
        app.job_queue.run_repeating(check_schedule, interval=900, first=10)  # 900 секунд = 15 минут
        print("📅 Планировщик расписания запущен (проверка каждые 15 минут)")

    print("🚀 Бот запущен в режиме polling")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
