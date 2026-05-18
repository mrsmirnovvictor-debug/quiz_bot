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
ZAZYVALA_BOT = "@ZazyvalaTag2Bot"
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
    
    for place_info in players_ranking:
        place = place_info["place"]
        for player in place_info["players"]:
            user_id = player["user_id"]
            username = player["username"]
            score = player["score"]
            avg_time_all = avg_times_all.get(user_id, 0)
            avg_time_correct = avg_times_correct.get(user_id, 0)
            correct_percent = (player["correct_count"] / player["total_answered"]) * 100 if player["total_answered"] > 0 else 0
            elo = calculate_elo(score, max_possible_score, avg_time_correct, len(game.registered), place)
            
            row = [date_str, str(game.chat_id), game.pack["title"], username, place, score, 
                   round(avg_time_all, 2), round(avg_time_correct, 2), elo, round(correct_percent, 2)]
            
            # Данные по вопросам - для каждого вопроса отдельно
            answers_detail = player_answers_detail.get(user_id, [])
            for q_idx in range(total_questions):
                if q_idx < len(answers_detail):
                    row.append(answers_detail[q_idx].get("answer", "-"))
                    row.append(answers_detail[q_idx].get("points", 0))
                    row.append(round(answers_detail[q_idx].get("time", 0), 2))
                else:
                    row.append("-")
                    row.append(0)
                    row.append(0)
            
            games_sheet.append_row(row)
    
    # Обновляем статистику игроков
    try:
        existing_data = players_sheet.get_all_records()
        player_stats = {row["Игрок"]: row for row in existing_data}
    except:
        player_stats = {}
    
    for place_info in players_ranking:
        for player in place_info["players"]:
            username = player["username"]
            score = player["score"]
            max_possible = len(game.pack["questions"]) * 15
            correct_percent = (player["correct_count"] / player["total_answered"]) * 100 if player["total_answered"] > 0 else 0
            avg_time_all = avg_times_all.get(player["user_id"], 0)
            avg_time_correct = avg_times_correct.get(player["user_id"], 0)
            elo = calculate_elo(score, max_possible, avg_time_correct, len(game.registered), place_info["place"])
            
            if username in player_stats:
                stats = player_stats[username]
                old_games = stats["Количество игр"]
                new_games = old_games + 1
                new_total_score = stats["Всего очков"] + score
                
                # Находим строку игрока
                row_idx = None
                for idx, row in enumerate(existing_data):
                    if row["Игрок"] == username:
                        row_idx = idx + 2
                        break
                
                if row_idx:
                    players_sheet.update([[new_games]], f"B{row_idx}")
                    players_sheet.update([[new_total_score]], f"C{row_idx}")
                    players_sheet.update([[round(new_total_score / new_games, 2)]], f"D{row_idx}")
                    
                    # Среднее время ответа
                    old_avg_all = stats["Среднее время ответа"]
                    if old_avg_all > 0 and avg_time_all > 0:
                        new_avg_all = (old_avg_all * old_games + avg_time_all) / new_games
                    elif avg_time_all > 0:
                        new_avg_all = avg_time_all
                    else:
                        new_avg_all = old_avg_all
                    players_sheet.update([[round(new_avg_all, 2)]], f"E{row_idx}")
                    
                    # Среднее время правильных ответов
                    old_avg_correct = stats["Среднее время (правильные)"]
                    if old_avg_correct > 0 and avg_time_correct > 0:
                        new_avg_correct = (old_avg_correct * old_games + avg_time_correct) / new_games
                    elif avg_time_correct > 0:
                        new_avg_correct = avg_time_correct
                    else:
                        new_avg_correct = old_avg_correct
                    players_sheet.update([[round(new_avg_correct, 2)]], f"F{row_idx}")
                    
                    # Процент правильных ответов
                    old_percent = stats["% правильных ответов"]
                    if old_percent > 0 and correct_percent > 0:
                        new_percent = (old_percent * old_games + correct_percent) / new_games
                    elif correct_percent > 0:
                        new_percent = correct_percent
                    else:
                        new_percent = old_percent
                    players_sheet.update([[round(new_percent, 2)]], f"G{row_idx}")
                    
                    new_elo = max(stats["ELO"], elo)
                    players_sheet.update([[new_elo]], f"H{row_idx}")
            else:
                players_sheet.append_row([
                    username, 1, score, round(score, 2),
                    round(avg_time_all, 2), round(avg_time_correct, 2),
                    round(correct_percent, 2), elo
                ])
    
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
        self.question_msg_id = None
        self.video_msg_id = None
        self.reg_timer_job = None
        self.question_timer_job = None
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
                "correct_count": self.user_correct_count.get(uid, 0),
                "total_answered": self.user_total_answered.get(uid, 0)
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

# -------------------- Остальные функции (регистрация, вопросы, финал) --------------------
# (здесь идут все остальные функции, которые были в предыдущей версии)
# Для экономии места они не переписаны заново, но в полной версии они есть.

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
    app.add_handler(CallbackQueryHandler(register_callback, pattern="register"))
    app.add_handler(CallbackQueryHandler(start_early_callback, pattern="start_early"))
    app.add_handler(CallbackQueryHandler(answer_callback, pattern=r"ans_\d+"))

    print("🚀 Бот запущен в режиме polling")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
