"""Слой хранения на SQLite.

Первичное хранилище состояния и статистики. Google Sheets стал витриной:
туда данные только выгружаются, оттуда ничего не читается.

Синхронные функции обёрнуты в asyncio.to_thread на уровне вызова, поэтому
event loop не блокируется. Соединение одно, доступ сериализован блокировкой.
"""

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from config import DB_PATH, RULES

log = logging.getLogger(__name__)

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id             INTEGER NOT NULL,
    thread_id           INTEGER,
    pack_id             TEXT    NOT NULL,
    title               TEXT    NOT NULL,
    question_count      INTEGER NOT NULL,
    status              TEXT    NOT NULL,          -- registration|active|paused|finished|aborted
    source              TEXT    NOT NULL DEFAULT 'manual',   -- manual|schedule
    creator_id          INTEGER,
    scheduled_start_utc TEXT,
    created_at          TEXT    NOT NULL,
    finished_at         TEXT,
    current_question    INTEGER NOT NULL DEFAULT 0,
    reg_msg_id          INTEGER,
    exported            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_games_chat_status ON games(chat_id, status);

CREATE TABLE IF NOT EXISTS participants (
    game_id  INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    username TEXT    NOT NULL,
    PRIMARY KEY (game_id, user_id)
);

-- Ключ (game_id, user_id, q_idx) полностью исключает старый баг со сдвигом
-- ответов при пропущенных вопросах.
CREATE TABLE IF NOT EXISTS answers (
    game_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    q_idx       INTEGER NOT NULL,
    option_idx  INTEGER NOT NULL,
    answer_text TEXT    NOT NULL,
    is_correct  INTEGER NOT NULL DEFAULT 0,
    points      INTEGER NOT NULL DEFAULT 0,
    elapsed     REAL    NOT NULL DEFAULT 0,   -- СЕКУНДЫ, не сотые
    PRIMARY KEY (game_id, user_id, q_idx)
);

CREATE TABLE IF NOT EXISTS results (
    game_id          INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    chat_id          INTEGER NOT NULL,
    username         TEXT    NOT NULL,
    played_at        TEXT    NOT NULL,
    place            INTEGER NOT NULL,
    score            INTEGER NOT NULL,
    question_count   INTEGER NOT NULL,
    correct          INTEGER NOT NULL,
    incorrect        INTEGER NOT NULL,
    no_answer        INTEGER NOT NULL,
    total_time       REAL    NOT NULL,
    total_time_ok    REAL    NOT NULL,
    avg_time         REAL    NOT NULL,
    avg_time_ok      REAL    NOT NULL,
    elo              INTEGER NOT NULL,
    rating_points    INTEGER NOT NULL,
    PRIMARY KEY (game_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_results_chat ON results(chat_id, played_at);
CREATE INDEX IF NOT EXISTS idx_results_user ON results(username);

CREATE TABLE IF NOT EXISTS schedules (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id          INTEGER NOT NULL,
    thread_id        INTEGER,
    days             TEXT    NOT NULL,       -- 'mon,wed,fri' или 'daily'
    time_msk         TEXT    NOT NULL,       -- 'HH:MM'
    pack_source      TEXT    NOT NULL DEFAULT 'auto',  -- 'auto' или '0007'
    pack_pool        TEXT    NOT NULL DEFAULT '',      -- префикс ID для авто-выбора
    reg_lead_minutes INTEGER NOT NULL DEFAULT 60,
    enabled          INTEGER NOT NULL DEFAULT 1,
    last_run_date    TEXT,
    skip_date        TEXT,
    created_by       INTEGER,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_chat ON schedules(chat_id);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init() -> None:
    """Создаёт файл БД и схему. Вызывается один раз при старте."""
    global _conn
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.execute("PRAGMA journal_mode=WAL")       # переживает жёсткий рестарт
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        _conn.commit()
    log.info("SQLite готов: %s", DB_PATH)


@contextmanager
def tx():
    """Транзакция с сериализацией доступа между потоками."""
    assert _conn is not None, "db.init() не вызван"
    with _lock:
        try:
            yield _conn
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


def _rows(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return _conn.execute(sql, params).fetchall()


def _row(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    with _lock:
        return _conn.execute(sql, params).fetchone()


# ==================== Игры ====================

def create_game(chat_id: int, thread_id: int | None, pack_id: str, title: str,
                question_count: int, creator_id: int | None,
                scheduled_start_utc: datetime, source: str = "manual") -> int:
    with tx() as c:
        cur = c.execute(
            """INSERT INTO games (chat_id, thread_id, pack_id, title, question_count,
                                  status, source, creator_id, scheduled_start_utc, created_at)
               VALUES (?, ?, ?, ?, ?, 'registration', ?, ?, ?, ?)""",
            (chat_id, thread_id, pack_id, title, question_count, source,
             creator_id, scheduled_start_utc.isoformat(), _utcnow()),
        )
        return cur.lastrowid


def update_game(game_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with tx() as c:
        c.execute(f"UPDATE games SET {cols} WHERE id = ?", (*fields.values(), game_id))


def get_game(game_id: int) -> sqlite3.Row | None:
    return _row("SELECT * FROM games WHERE id = ?", (game_id,))


def active_game_for_chat(chat_id: int) -> sqlite3.Row | None:
    return _row(
        "SELECT * FROM games WHERE chat_id = ? AND status IN "
        "('registration', 'active', 'paused') ORDER BY id DESC LIMIT 1",
        (chat_id,),
    )


def unfinished_games() -> list[sqlite3.Row]:
    """Для восстановления после рестарта."""
    return _rows(
        "SELECT * FROM games WHERE status IN ('registration', 'active', 'paused') "
        "ORDER BY id"
    )


def played_pack_ids(chat_id: int) -> set[str]:
    rows = _rows(
        "SELECT DISTINCT pack_id FROM games WHERE chat_id = ? AND status = 'finished'",
        (chat_id,),
    )
    return {r["pack_id"] for r in rows}


def last_played_at(chat_id: int) -> dict[str, str]:
    rows = _rows(
        "SELECT pack_id, MAX(finished_at) AS ts FROM games "
        "WHERE chat_id = ? AND status = 'finished' GROUP BY pack_id",
        (chat_id,),
    )
    return {r["pack_id"]: r["ts"] or "" for r in rows}


# ==================== Участники и ответы ====================

def add_participant(game_id: int, user_id: int, username: str) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO participants (game_id, user_id, username) VALUES (?, ?, ?) "
            "ON CONFLICT(game_id, user_id) DO UPDATE SET username = excluded.username",
            (game_id, user_id, username),
        )


def participants(game_id: int) -> list[sqlite3.Row]:
    return _rows(
        "SELECT user_id, username FROM participants WHERE game_id = ? ORDER BY rowid",
        (game_id,),
    )


def save_answer(game_id: int, user_id: int, q_idx: int, option_idx: int,
                answer_text: str, is_correct: bool, points: int, elapsed: float) -> None:
    with tx() as c:
        c.execute(
            """INSERT OR IGNORE INTO answers
               (game_id, user_id, q_idx, option_idx, answer_text, is_correct, points, elapsed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (game_id, user_id, q_idx, option_idx, answer_text,
             int(is_correct), points, elapsed),
        )


def game_answers(game_id: int) -> list[sqlite3.Row]:
    return _rows("SELECT * FROM answers WHERE game_id = ?", (game_id,))


# ==================== Результаты ====================

def save_results(rows: list[dict]) -> None:
    if not rows:
        return
    with tx() as c:
        c.executemany(
            """INSERT OR REPLACE INTO results
               (game_id, user_id, chat_id, username, played_at, place, score,
                question_count, correct, incorrect, no_answer, total_time,
                total_time_ok, avg_time, avg_time_ok, elo, rating_points)
               VALUES (:game_id, :user_id, :chat_id, :username, :played_at, :place,
                       :score, :question_count, :correct, :incorrect, :no_answer,
                       :total_time, :total_time_ok, :avg_time, :avg_time_ok,
                       :elo, :rating_points)""",
            rows,
        )


def game_results(game_id: int) -> list[sqlite3.Row]:
    return _rows("SELECT * FROM results WHERE game_id = ? ORDER BY place", (game_id,))


# ==================== Агрегаты (раньше считались в Python три раза) ====================

def player_stats(chat_id: int, min_games: int = 0) -> list[sqlite3.Row]:
    """Сводка по игрокам группы. Полный аналог листа Players_{chat_id}."""
    return _rows(
        """SELECT username,
                  COUNT(*)                              AS games_count,
                  SUM(score)                            AS total_score,
                  AVG(score)                            AS avg_score,
                  SUM(total_time)                       AS total_time,
                  SUM(total_time_ok)                    AS total_time_ok,
                  SUM(correct)                          AS total_correct,
                  SUM(incorrect)                        AS total_incorrect,
                  SUM(question_count)                   AS total_questions,
                  AVG(elo)                              AS avg_elo
           FROM results
           WHERE chat_id = ?
           GROUP BY username
           HAVING COUNT(*) >= ?
           ORDER BY avg_elo DESC""",
        (chat_id, min_games),
    )


def rating_table(chat_id: int) -> list[sqlite3.Row]:
    """Аналог листа Rating_{chat_id} — рейтинг по очкам за призовые места."""
    return _rows(
        """SELECT username,
                  COUNT(*)            AS games_count,
                  SUM(rating_points)  AS total_points
           FROM results
           WHERE chat_id = ?
           GROUP BY username
           ORDER BY total_points DESC, games_count ASC""",
        (chat_id,),
    )


def rating_table_period(chat_id: int, since: str, until: str) -> list[sqlite3.Row]:
    """То же, но за период — для сезонных зачётов на лидербордах."""
    return _rows(
        """SELECT username,
                  COUNT(*)            AS games_count,
                  SUM(rating_points)  AS total_points
           FROM results
           WHERE chat_id = ? AND played_at >= ? AND played_at < ?
           GROUP BY username
           ORDER BY total_points DESC, games_count ASC""",
        (chat_id, since, until),
    )


def elo_snapshot(chat_id: int, until: str | None = None) -> dict[str, tuple[int, float]]:
    """{username: (сыграно, среднее ELO)} на момент времени `until`."""
    sql = ("SELECT username, COUNT(*) AS games, AVG(elo) AS elo FROM results "
           "WHERE chat_id = ?")
    params: tuple = (chat_id,)
    if until:
        sql += " AND played_at <= ?"
        params += (until,)
    sql += " GROUP BY username"
    return {r["username"]: (r["games"], r["elo"]) for r in _rows(sql, params)}


def game_dates(chat_id: int) -> list[str]:
    rows = _rows(
        "SELECT DISTINCT substr(played_at, 1, 10) AS d FROM results "
        "WHERE chat_id = ? ORDER BY d",
        (chat_id,),
    )
    return [r["d"] for r in rows]


def user_history(username: str, limit: int = 10) -> list[sqlite3.Row]:
    return _rows(
        """SELECT r.*, g.title
           FROM results r JOIN games g ON g.id = r.game_id
           WHERE r.username = ?
           ORDER BY r.played_at DESC
           LIMIT ?""",
        (username, limit),
    )


def user_history_count(username: str) -> int:
    row = _row("SELECT COUNT(*) AS n FROM results WHERE username = ?", (username,))
    return row["n"] if row else 0


def find_player(username: str) -> dict | None:
    """Сводка по игроку для проверки перед переименованием."""
    row = _row(
        "SELECT COUNT(*) AS games, MIN(substr(played_at,1,10)) AS first_game, "
        "MAX(substr(played_at,1,10)) AS last_game FROM results WHERE username = ?",
        (username,),
    )
    if not row or not row["games"]:
        return None
    return dict(row)


def rename_player(old: str, new: str) -> dict[str, int]:
    """Меняет ник во всей истории.

    Ник в Telegram глобальный, поэтому правим по всем группам сразу.
    Если игрок уже играл под новым ником, строки просто сольются: первичный
    ключ results — (game_id, user_id), конфликта не возникает, а агрегаты
    по username объединятся сами.
    """
    with tx() as c:
        results = c.execute(
            "UPDATE results SET username = ? WHERE username = ?", (new, old)
        ).rowcount
        participants = c.execute(
            "UPDATE participants SET username = ? WHERE username = ?", (new, old)
        ).rowcount
    return {"results": results, "participants": participants}


def player_chats(username: str) -> list[int]:
    rows = _rows(
        "SELECT DISTINCT chat_id FROM results WHERE username = ?", (username,)
    )
    return [r["chat_id"] for r in rows]


# ==================== Расписание ====================

def add_schedule(chat_id: int, thread_id: int | None, days: str, time_msk: str,
                 pack_source: str, pack_pool: str, reg_lead_minutes: int,
                 created_by: int) -> int:
    with tx() as c:
        cur = c.execute(
            """INSERT INTO schedules (chat_id, thread_id, days, time_msk, pack_source,
                                      pack_pool, reg_lead_minutes, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, thread_id, days, time_msk, pack_source, pack_pool,
             reg_lead_minutes, created_by, _utcnow()),
        )
        return cur.lastrowid


def list_schedules(chat_id: int | None = None) -> list[sqlite3.Row]:
    if chat_id is None:
        return _rows("SELECT * FROM schedules WHERE enabled = 1 ORDER BY id")
    return _rows("SELECT * FROM schedules WHERE chat_id = ? ORDER BY id", (chat_id,))


def delete_schedule(schedule_id: int, chat_id: int) -> bool:
    with tx() as c:
        cur = c.execute("DELETE FROM schedules WHERE id = ? AND chat_id = ?",
                        (schedule_id, chat_id))
        return cur.rowcount > 0


def set_schedule_enabled(schedule_id: int, chat_id: int, enabled: bool) -> bool:
    with tx() as c:
        cur = c.execute("UPDATE schedules SET enabled = ? WHERE id = ? AND chat_id = ?",
                        (int(enabled), schedule_id, chat_id))
        return cur.rowcount > 0


def claim_schedule_run(schedule_id: int, run_date: str) -> bool:
    """Атомарно помечает слот отработанным на дату.

    Возвращает True ровно один раз за дату — это и есть защита от двойного
    запуска при гонке тика планировщика или быстром рестарте.
    """
    with tx() as c:
        cur = c.execute(
            "UPDATE schedules SET last_run_date = ? "
            "WHERE id = ? AND (last_run_date IS NULL OR last_run_date < ?)",
            (run_date, schedule_id, run_date),
        )
        return cur.rowcount > 0


def set_skip_date(chat_id: int, skip_date: str) -> int:
    with tx() as c:
        cur = c.execute("UPDATE schedules SET skip_date = ? WHERE chat_id = ? AND enabled = 1",
                        (skip_date, chat_id))
        return cur.rowcount


# ==================== Экспорт ====================

def games_pending_export() -> list[int]:
    rows = _rows("SELECT id FROM games WHERE status = 'finished' AND exported = 0")
    return [r["id"] for r in rows]


def mark_exported(game_id: int) -> None:
    update_game(game_id, exported=1)


def calibrated_chats() -> list[int]:
    rows = _rows("SELECT DISTINCT chat_id FROM results")
    return [r["chat_id"] for r in rows]
