"""Выгрузка в Google Sheets.

Sheets больше не первичное хранилище — только витрина для HTML-лидербордов.
Отсюда три следствия:
  * клиент авторизуется один раз и кэшируется (было — при каждом вызове);
  * все строки пишутся одним batch-запросом (было — по строке на игрока);
  * агрегаты считает SQLite, а не Python поверх get_all_records() всего листа.

ФОРМАТ КОЛОНОК СОХРАНЁН БАЙТ-В-БАЙТ, включая время в СОТЫХ долях секунды,
иначе сломаются лидерборды, читающие gviz/tq.
"""

import json
import logging
import threading
from datetime import datetime

import db
from config import GOOGLE_CREDENTIALS_JSON, GOOGLE_SHEET_ID, MSK, RULES, SHEETS_ENABLED

log = logging.getLogger(__name__)

# В шапке всегда 16 блоков по вопросам, даже если пакет короче. Иначе колонка
# «Очки рейтинга» уезжает влево и лидерборды читают чужие данные.
MAX_QUESTION_COLUMNS = 16

BASE_HEADERS = [
    "Дата", "Chat ID", "Название квиза", "Игрок", "Место", "Общий счёт",
    "Количество вопросов", "Правильные ответы", "Неправильные ответы", "Без ответа",
    "Общее время ответов", "Общее время правильных ответов",
    "Среднее время ответа", "Среднее время (правильные)", "ELO после игры",
    "% правильных ответов",
]

_client = None
_spreadsheet = None
_lock = threading.RLock()


def _centi(seconds: float) -> int:
    """Секунды -> сотые доли. Единственное место конвертации в проекте."""
    return int(round(seconds * 100))


def _spread():
    """Ленивая, единожды выполняемая авторизация."""
    global _client, _spreadsheet
    if not SHEETS_ENABLED:
        return None
    with _lock:
        if _spreadsheet is not None:
            return _spreadsheet
        try:
            import gspread
            from google.oauth2.service_account import Credentials

            creds = Credentials.from_service_account_info(
                json.loads(GOOGLE_CREDENTIALS_JSON),
                scopes=["https://www.googleapis.com/auth/spreadsheets",
                        "https://www.googleapis.com/auth/drive"],
            )
            _client = gspread.authorize(creds)
            _spreadsheet = _client.open_by_key(GOOGLE_SHEET_ID)
            log.info("Google Sheets подключён")
            return _spreadsheet
        except Exception:
            log.exception("Не удалось подключиться к Google Sheets")
            return None


def _worksheet(name: str, headers: list[str]):
    """Возвращает лист, создавая его с нужной шапкой при отсутствии."""
    sp = _spread()
    if sp is None:
        return None
    import gspread
    try:
        return sp.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sp.add_worksheet(title=name, rows=1, cols=max(len(headers), 20))
        ws.append_row(headers)
        log.info("Создан лист %s", name)
        return ws


def games_headers() -> list[str]:
    headers = list(BASE_HEADERS)
    for i in range(1, MAX_QUESTION_COLUMNS + 1):
        headers += [f"Вопрос {i} ответ", f"Вопрос {i} баллы", f"Вопрос {i} время"]
    headers.append("Очки рейтинга")
    return headers


def _game_row(result, title: str, answers_by_q: dict[int, dict]) -> list:
    played = datetime.fromisoformat(result["played_at"]).astimezone(MSK)
    correct_percent = (result["correct"] / result["question_count"] * 100
                       if result["question_count"] else 0)

    row = [
        played.strftime("%Y-%m-%d %H:%M:%S"),
        str(result["chat_id"]),
        title,
        result["username"],
        result["place"],
        result["score"],
        result["question_count"],
        result["correct"],
        result["incorrect"],
        result["no_answer"],
        _centi(result["total_time"]),
        _centi(result["total_time_ok"]),
        _centi(result["avg_time"]),
        _centi(result["avg_time_ok"]),
        result["elo"],
        round(correct_percent, 2),
    ]

    for q_idx in range(MAX_QUESTION_COLUMNS):
        a = answers_by_q.get(q_idx)
        if a:
            row += [a["answer_text"], a["points"], _centi(a["elapsed"])]
        else:
            row += ["-", 0, 0]

    row.append(result["rating_points"])
    return row


def export_game(game_id: int) -> bool:
    """Выгружает одну завершённую игру. Синхронно — вызывать через to_thread."""
    if not SHEETS_ENABLED:
        return False

    game = db.get_game(game_id)
    results = db.game_results(game_id)
    if not game or not results:
        return False

    answers = db.game_answers(game_id)
    by_user: dict[int, dict[int, dict]] = {}
    for a in answers:
        by_user.setdefault(a["user_id"], {})[a["q_idx"]] = dict(a)

    ws = _worksheet("Games", games_headers())
    if ws is None:
        return False

    rows = [_game_row(r, game["title"], by_user.get(r["user_id"], {})) for r in results]
    try:
        # Один запрос вместо N. Раньше при 20 игроках было 20 обращений подряд.
        ws.append_rows(rows, value_input_option="USER_ENTERED")
    except Exception:
        log.exception("Не удалось выгрузить игру %s в Games", game_id)
        return False

    chat_id = game["chat_id"]
    _rebuild_players(chat_id)
    _rebuild_rating(chat_id)
    _rebuild_ranking(chat_id)
    db.mark_exported(game_id)
    log.info("Игра %s выгружена в Sheets (%s строк)", game_id, len(rows))
    return True


def _replace_rows(ws, rows: list[list]) -> None:
    """Перезаписывает лист ниже шапки одним запросом."""
    if ws is None:
        return
    try:
        ws.batch_clear([f"A2:ZZ{max(ws.row_count, 2)}"])
        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
    except Exception:
        log.exception("Не удалось перезаписать лист %s", ws.title)


def _rebuild_players(chat_id: int) -> None:
    headers = ["Игрок", "Количество игр", "Всего очков", "Средний балл за квиз",
               "Среднее время ответа", "Среднее время (правильные)",
               "% правильных ответов", "ELO"]
    ws = _worksheet(f"Players_{chat_id}", headers)
    rows = []
    for s in db.player_stats(chat_id):
        answered = (s["total_correct"] or 0) + (s["total_incorrect"] or 0)
        avg_time = (s["total_time"] or 0) / answered if answered else 0
        avg_time_ok = (s["total_time_ok"] or 0) / s["total_correct"] if s["total_correct"] else 0
        percent = (s["total_correct"] / s["total_questions"] * 100
                   if s["total_questions"] else 0)
        rows.append([
            s["username"], s["games_count"], round(s["total_score"] or 0),
            round(s["avg_score"] or 0, 1), round(avg_time, 1), round(avg_time_ok, 1),
            round(percent, 1), int(round(s["avg_elo"] or 0)),
        ])
    _replace_rows(ws, rows)


def _rebuild_rating(chat_id: int) -> None:
    headers = ["Игрок", "Количество игр", "Всего очков рейтинга"]
    ws = _worksheet(f"Rating_{chat_id}", headers)
    rows = [[r["username"], r["games_count"], r["total_points"] or 0]
            for r in db.rating_table(chat_id)]
    _replace_rows(ws, rows)


def _rebuild_ranking(chat_id: int) -> None:
    """Динамика мест между последней и предпоследней игровыми датами."""
    headers = [
        "Игрок", "Предпоследняя дата игр", "Игр на предпоследнюю дату игр",
        "Среднее ELO на предпоследнюю дату игр", "Место на предпоследнюю дату игр",
        "Последняя дата игр", "Игр на последнюю дату игр",
        "Среднее ELO на текущий момент", "Текущее место",
        "Изменение ELO", "Изменение места",
    ]
    dates = db.game_dates(chat_id)
    if len(dates) < 2:
        return
    last_date, prev_date = dates[-1], dates[-2]

    last = db.elo_snapshot(chat_id, last_date + "T23:59:59+00:00")
    prev = db.elo_snapshot(chat_id, prev_date + "T23:59:59+00:00")

    min_games = RULES.calibration_games
    current = sorted(
        ((u, g, e) for u, (g, e) in last.items() if g >= min_games),
        key=lambda x: -x[2],
    )
    if not current:
        return
    places_now = {u: i for i, (u, _, _) in enumerate(current, 1)}

    previous = sorted(
        ((u, g, e) for u, (g, e) in prev.items() if g >= min_games and u in places_now),
        key=lambda x: -x[2],
    )
    places_prev = {u: i for i, (u, _, _) in enumerate(previous, 1)}

    rows = []
    for username, games, elo in current:
        if username in places_prev:
            p_games, p_elo = prev[username]
            rows.append([
                username, prev_date, p_games, round(p_elo, 2), places_prev[username],
                last_date, games, round(elo, 2), places_now[username],
                round(elo - p_elo, 2), places_prev[username] - places_now[username],
            ])
        else:
            rows.append([
                username, "", "", "", "",
                last_date, games, round(elo, 2), places_now[username], "", "",
            ])
    rows.sort(key=lambda r: r[8])
    _replace_rows(_worksheet(f"Ranking_{chat_id}", headers), rows)


def rebuild_chat(chat_id: int) -> None:
    """Пересобирает витрину одной группы из SQLite."""
    _rebuild_players(chat_id)
    _rebuild_rating(chat_id)
    _rebuild_ranking(chat_id)


def export_pending() -> int:
    """Догоняет выгрузку игр, не попавших в Sheets (например, из-за квоты)."""
    exported = 0
    for game_id in db.games_pending_export():
        if export_game(game_id):
            exported += 1
    return exported
