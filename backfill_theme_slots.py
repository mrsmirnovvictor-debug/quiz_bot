"""Восстановление слотов у заказов, внесённых без даты.

Слот берётся из фактического времени игры: находим в таблице games запись
с тем же номером пакета и округляем время окончания вниз до часа. Квиз,
завершившийся в 19:09, начинался в 19:00.

    railway ssh
    python backfill_theme_slots.py            # сухой прогон
    python backfill_theme_slots.py --apply    # запись

Идемпотентен: заказы, у которых слот уже есть, пропускаются.
"""

import argparse
import sys
from datetime import datetime, timezone

import db
import themes
from config import MSK

CHAT_ID = -1002440363847


def slot_from_game(row) -> datetime | None:
    """Время окончания игры -> начало её часа по Москве, в UTC.

    Квиз длится около десяти минут, поэтому час окончания совпадает с часом
    старта: игра, завершившаяся в 19:09, начиналась в 19:00.
    """
    source = row["finished_at"] or row["scheduled_start_utc"]
    if not source:
        return None
    msk = datetime.fromisoformat(source).astimezone(MSK)
    start_msk = msk.replace(minute=0, second=0, microsecond=0)
    return start_msk.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db.init()
    season = themes.ensure_season(CHAT_ID)
    print(f"Сезон: {season['name']} ({season['starts_on']} — {season['ends_on']})")

    orders = db.theme_orders(CHAT_ID, season["id"])
    targets = [o for o in orders if not o["slot_utc"] and o["pack_id"]]
    busy = db.busy_slots(CHAT_ID, season["id"])

    if not targets:
        print("\nЗаказов без слота нет — всё уже заполнено.")
        return 0

    print(f"\nЗаказов без слота: {len(targets)}\n")
    grid = {s.isoformat() for s in themes.season_grid(season)}
    planned, problems = [], []

    for order in targets:
        games = db._rows(
            "SELECT * FROM games WHERE chat_id = ? AND pack_id = ? "
            "AND status = 'finished' ORDER BY finished_at",
            (CHAT_ID, order["pack_id"]),
        )
        if not games:
            problems.append(
                f"#{order['id']} {order['username']} [{order['pack_id']}]: "
                f"игра с этим пакетом не найдена"
            )
            continue
        if len(games) > 1:
            dates = ", ".join(
                (g["finished_at"] or "?")[:16].replace("T", " ") for g in games
            )
            problems.append(
                f"#{order['id']} {order['username']} [{order['pack_id']}]: "
                f"пакет игрался {len(games)} раз ({dates}) — выберите вручную"
            )
            continue

        slot_utc = slot_from_game(games[0])
        if slot_utc is None:
            problems.append(
                f"#{order['id']} [{order['pack_id']}]: у игры не записано время"
            )
            continue
        slot_iso = slot_utc.isoformat()

        if slot_iso in busy:
            problems.append(
                f"#{order['id']} {order['username']} [{order['pack_id']}]: "
                f"слот {themes.format_slot(slot_utc)} уже занят другим заказом"
            )
            continue

        in_grid = "" if slot_iso in grid else "  (вне сетки пн/чт 19–22)"
        busy.add(slot_iso)
        planned.append({
            "id": order["id"],
            "username": order["username"],
            "pack_id": order["pack_id"],
            "theme": order["theme"],
            "slot_iso": slot_iso,
            "label": themes.format_slot(slot_utc) + in_grid,
            "finished": games[0]["finished_at"],
        })

    for row in planned:
        finished = (row["finished"] or "")[:16].replace("T", " ")
        print(f"  #{row['id']:<3} {row['username']:22} [{row['pack_id']}] "
              f"{row['label']:32} (игра завершилась {finished} UTC)")
        print(f"       «{row['theme'][:60]}»")

    if problems:
        print("\n⚠️ Требуют внимания:")
        for p in problems:
            print(f"  · {p}")

    if not args.apply:
        print(f"\nСухой прогон. К записи готово: {len(planned)}")
        print("Для записи: python backfill_theme_slots.py --apply")
        return 0

    for row in planned:
        db.update_theme_order(row["id"], slot_utc=row["slot_iso"], status="played")
    print(f"\n✅ Обновлено заказов: {len(planned)}")
    print("Проверьте: /themes all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
