"""Массовое внесение заказов тем.

Запускается внутри контейнера, где живёт база:

    railway ssh
    python seed_themes.py            # сухой прогон, ничего не пишет
    python seed_themes.py --apply    # запись

SQLite в режиме WAL спокойно переживает второй процесс, поэтому останавливать
бота не нужно. Скрипт идемпотентен: повторный запуск не создаёт дублей.
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import db
import packs
import themes
from config import MSK, THEME_LIMIT

# ==================== ЧТО ВНОСИМ ====================

CHAT_ID = -1002440363847        # основная группа

# Ник Ксюши. Если /rename ещё не выполняли — поменяйте на "@ksyusha_tea".
KSYUSHA = "@ksyusha_tea_leaves"


@dataclass
class Legacy:
    """Заказ, тема которого уже отыграна. Слот не занимает, квоту расходует."""
    username: str
    pack_id: str
    slot_msk: str | None = None     # если игра привязана к конкретному слоту


@dataclass
class Booking:
    """Заказ на будущий слот. Пакет привяжете позже через /theme_pack."""
    username: str
    slot_msk: str                   # 'ДД.ММ ЧЧ:ММ' по Москве
    theme: str


ORDERS: list = [
    # --- отыграно ранее ---
    Legacy(KSYUSHA, "0103"),
    Legacy(KSYUSHA, "0107"),
    Legacy(KSYUSHA, "0110"),
    Legacy("@valery_house", "0105"),
    Legacy("@valery_house", "0106"),
    Legacy("@MishkaGammii", "0133"),
    Legacy("@MishkaGammii", "0134"),

    # --- сегодняшние игры из расписания (запускает /schedule, не механизм тем) ---
    Legacy("@valeisss", "0145", "10.08 19:00"),
    Legacy("@emil_kotsoev", "0146", "10.08 20:00"),
    Legacy("@valeisss", "0147", "10.08 21:00"),
    Legacy("@MishkaGammii", "0148", "10.08 22:00"),

    # --- забронировано на будущее ---
    Booking("@valeisss", "13.08 19:00", "Шансон"),
    Booking("@Messssssir", "13.08 20:00", "Русские в НХЛ"),
    Booking("@Messssssir", "17.08 19:00", "Русские в НБА"),
]

# ==================== Реализация ====================


def parse_msk(raw: str) -> datetime:
    """'13.08 19:00' -> момент в UTC. Год берётся из текущего сезона."""
    parsed = datetime.strptime(raw, "%d.%m %H:%M")
    year = datetime.now(MSK).year
    return parsed.replace(year=year, tzinfo=MSK).astimezone(timezone.utc)


def known_usernames(chat_id: int) -> set[str]:
    return {r["username"] for r in db._rows(
        "SELECT DISTINCT username FROM results WHERE chat_id = ?", (chat_id,))}


def similar(name: str, known: set[str]) -> list[str]:
    """Похожие ники — чтобы поймать опечатку вроде kotsoev/kostoev."""
    letters = set(name.lower().lstrip("@"))
    out = []
    for candidate in known:
        other = set(candidate.lower().lstrip("@"))
        overlap = len(letters & other) / max(len(letters | other), 1)
        if overlap > 0.75 and candidate.lower() != name.lower():
            out.append(candidate)
    return sorted(out)[:3]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="записать в базу (без флага — только проверка)")
    args = parser.parse_args()

    db.init()
    season = themes.ensure_season(CHAT_ID)
    print(f"Сезон: {season['name']} ({season['starts_on']} — {season['ends_on']})")
    print(f"Группа: {CHAT_ID}\n")

    known = known_usernames(CHAT_ID)
    existing = db.theme_orders(CHAT_ID, season["id"])
    existing_packs = {(o["username"], o["pack_id"]) for o in existing if o["pack_id"]}
    existing_slots = {o["slot_utc"] for o in existing if o["slot_utc"]}

    planned: list[dict] = []
    problems: list[str] = []
    per_user: dict[str, int] = {}

    for item in ORDERS:
        name = item.username
        if name not in known:
            hint = similar(name, known)
            problems.append(
                f"ник {name} не встречается в истории группы"
                + (f" — возможно, имелся в виду {', '.join(hint)}" if hint else "")
            )
            continue

        if isinstance(item, Legacy):
            try:
                pack = packs.load_pack(item.pack_id)
            except packs.PackError as e:
                problems.append(f"{name}: {e}")
                continue
            if (name, item.pack_id) in existing_packs:
                print(f"  = пропуск: у {name} уже записан пакет {item.pack_id}")
                continue
            slot = parse_msk(item.slot_msk).isoformat() if item.slot_msk else None
            planned.append({"username": name, "theme": pack.title,
                            "pack_id": item.pack_id, "slot": slot,
                            "status": "played" if slot else "legacy"})
        else:
            slot_dt = parse_msk(item.slot_msk)
            slot = slot_dt.isoformat()
            if slot in existing_slots:
                print(f"  = пропуск: слот {themes.format_slot(slot_dt)} уже занят")
                continue
            if slot_dt not in themes.season_grid(season):
                problems.append(
                    f"{name}: {item.slot_msk} не попадает в игровую сетку "
                    f"(пн и чт, 19–22 МСК)"
                )
                continue
            planned.append({"username": name, "theme": item.theme,
                            "pack_id": None, "slot": slot, "status": "booked"})
            existing_slots.add(slot)

        per_user[name] = per_user.get(name, 0) + 1

    # ---- проверка квот ----
    print("Квоты после внесения:\n")
    for name in sorted(per_user):
        quota = themes.quota_for(CHAT_ID, season, name)
        after = quota.used + per_user[name]
        limit = min(quota.wins, THEME_LIMIT)
        mark = "✅"
        if after > limit:
            mark = "❌"
            problems.append(
                f"{name}: побед {quota.wins} (лимит {limit}), "
                f"а заказов получается {after}"
            )
        elif after == limit:
            mark = "⚠️"
        print(f"  {mark} {name:22} побед {quota.wins:2} · было {quota.used} · "
              f"вносим {per_user[name]} · станет {after}/{limit}")

    print(f"\nК внесению: {len(planned)} записей")
    for row in planned:
        when = (themes.format_slot(datetime.fromisoformat(row["slot"]))
                if row["slot"] else "без слота")
        pack = f" [{row['pack_id']}]" if row["pack_id"] else ""
        print(f"  {row['username']:22} {when:20} {row['status']:7} "
              f"«{row['theme'][:45]}»{pack}")

    if problems:
        print("\n⚠️ Проблемы:")
        for p in problems:
            print(f"  · {p}")

    if not args.apply:
        print("\nЭто сухой прогон. Для записи: python seed_themes.py --apply")
        return 1 if problems else 0

    if problems:
        print("\n❌ Есть проблемы — запись отменена. Исправьте список в шапке.")
        return 1

    for row in planned:
        db.add_theme_order(
            chat_id=CHAT_ID, season_id=season["id"], username=row["username"],
            theme=row["theme"], slot_utc=row["slot"], status=row["status"],
            created_by=None, note="внесено скриптом seed_themes",
        )
        if row["pack_id"]:
            order_id = db._row("SELECT MAX(id) AS id FROM theme_orders")["id"]
            db.update_theme_order(order_id, pack_id=row["pack_id"])

    print(f"\n✅ Записано: {len(planned)}")
    print("Проверьте в группе: /themes all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
