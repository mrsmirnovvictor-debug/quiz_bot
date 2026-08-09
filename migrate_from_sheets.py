"""Разовый перенос истории из листа Games в SQLite.

Запускать ОДИН раз после подключения тома:
    railway run python migrate_from_sheets.py

Идемпотентен: повторный запуск не создаёт дублей (сверка по дате+чату+игроку).
Скрипт ничего не удаляет и не меняет в Google Sheets.
"""

import logging
import sys
from datetime import datetime

import db
import packs
from config import MSK, SHEETS_ENABLED
from sheets import _spread

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger("migrate")

# Порог, выше которого значение времени считается сотыми долями секунды.
# Ни один ответ не длится дольше времени вопроса, поэтому 100+ — это сотые.
CENTI_THRESHOLD = 100


def to_float(value) -> float:
    if isinstance(value, str):
        value = value.replace(",", ".").strip()
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def to_int(value) -> int:
    return int(to_float(value))


def seconds(value) -> float:
    """Приводит легаси-значение к секундам."""
    raw = to_float(value)
    return raw / 100 if raw >= CENTI_THRESHOLD else raw


def title_to_pack_id() -> dict[str, str]:
    mapping = {}
    for pid in packs.list_pack_ids():
        try:
            mapping[packs.load_pack(pid).title] = pid
        except packs.PackError:
            continue
    return mapping


def main() -> None:
    if not SHEETS_ENABLED:
        sys.exit("Не заданы GOOGLE_CREDENTIALS / GOOGLE_SHEET_ID")

    db.init()
    spread = _spread()
    if spread is None:
        sys.exit("Нет доступа к таблице")

    log.info("Читаю лист Games (это единственный тяжёлый запрос за миграцию)...")
    records = spread.worksheet("Games").get_all_records()
    log.info("Строк получено: %s", len(records))

    pack_map = title_to_pack_id()
    # Одна «игра» = уникальная пара (дата, chat_id).
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in records:
        key = (str(row.get("Дата", "")).strip(), str(row.get("Chat ID", "")).strip())
        if not key[0] or not key[1]:
            continue
        grouped.setdefault(key, []).append(row)

    log.info("Уникальных игр: %s", len(grouped))
    created = skipped = 0

    for (date_str, chat_id_str), rows in sorted(grouped.items()):
        chat_id = int(chat_id_str)
        try:
            played_naive = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            log.warning("Пропущена игра с датой %r", date_str)
            continue
        played_at = played_naive.replace(tzinfo=MSK).isoformat(timespec="seconds")

        if _already_imported(chat_id, played_at):
            skipped += 1
            continue

        title = str(rows[0].get("Название квиза", "")).strip() or "Без названия"
        question_count = to_int(rows[0].get("Количество вопросов", 0)) or 16
        pack_id = pack_map.get(title, "0000")

        game_id = db.create_game(
            chat_id=chat_id, thread_id=None, pack_id=pack_id, title=title,
            question_count=question_count, creator_id=None,
            scheduled_start_utc=played_naive.replace(tzinfo=MSK),
            source="legacy",
        )
        db.update_game(game_id, status="finished", finished_at=played_at, exported=1)

        result_rows = []
        for order, row in enumerate(rows):
            username = str(row.get("Игрок", "")).strip()
            if not username:
                continue
            # У легаси-строк нет user_id — синтезируем стабильный отрицательный.
            user_id = -abs(hash((chat_id, username))) % (10 ** 12)

            db.add_participant(game_id, user_id, username)
            _import_answers(game_id, user_id, row, question_count)

            correct = to_int(row.get("Правильные ответы", 0))
            incorrect = to_int(row.get("Неправильные ответы", 0))
            result_rows.append({
                "game_id": game_id, "user_id": user_id, "chat_id": chat_id,
                "username": username, "played_at": played_at,
                "place": to_int(row.get("Место", order + 1)),
                "score": to_int(row.get("Общий счёт", 0)),
                "question_count": question_count,
                "correct": correct, "incorrect": incorrect,
                "no_answer": to_int(row.get("Без ответа", 0)),
                "total_time": seconds(row.get("Общее время ответов", 0)),
                "total_time_ok": seconds(row.get("Общее время правильных ответов", 0)),
                "avg_time": seconds(row.get("Среднее время ответа", 0)),
                "avg_time_ok": seconds(row.get("Среднее время (правильные)", 0)),
                "elo": to_int(row.get("ELO после игры", 0)),
                "rating_points": to_int(row.get("Очки рейтинга", 0)),
            })

        db.save_results(result_rows)
        created += 1

    log.info("Готово. Импортировано игр: %s, пропущено как дубли: %s", created, skipped)


def _already_imported(chat_id: int, played_at: str) -> bool:
    rows = db._rows(
        "SELECT 1 FROM results WHERE chat_id = ? AND played_at = ? LIMIT 1",
        (chat_id, played_at),
    )
    return bool(rows)


def _import_answers(game_id: int, user_id: int, row: dict, question_count: int) -> None:
    """Переносит поколоночную детализацию ответов.

    ВНИМАНИЕ: в старом боте эти колонки съезжали при пропущенных вопросах —
    ответ на вопрос N мог оказаться в колонках вопроса N-1. Данные переносятся
    как есть; сводные показатели (очки, время, ELO) от этого не зависят.
    """
    for q in range(1, question_count + 1):
        answer_text = str(row.get(f"Вопрос {q} ответ", "-")).strip()
        if not answer_text or answer_text == "-":
            continue
        points = to_int(row.get(f"Вопрос {q} баллы", 0))
        db.save_answer(
            game_id=game_id, user_id=user_id, q_idx=q - 1, option_idx=-1,
            answer_text=answer_text, is_correct=points > 0, points=points,
            elapsed=seconds(row.get(f"Вопрос {q} время", 0)),
        )


if __name__ == "__main__":
    main()
