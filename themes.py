"""Заказ тем победителями квизов.

Правила:
  * одна победа даёт право заказать одну тему;
  * за сезон нельзя заказать больше THEME_LIMIT тем, сколько бы побед ни было;
  * слот выбирается из игровой сетки (пн и чт, 19–22 МСК) до конца сезона;
  * исторические заказы админ вносит вручную — они тоже расходуют квоту.

Слой чистый: ни Telegram, ни ввода-вывода, только вычисления над данными из БД.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

import db
from config import (MSK, THEME_HOURS, THEME_LEAD_HOURS, THEME_LIMIT,
                    THEME_WEEKDAYS)

WEEKDAY_NAMES = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def plain(username: str) -> str:
    """Ник без @, чтобы список не превращался в череду упоминаний."""
    return username[1:] if username.startswith("@") else username


class ThemeError(Exception):
    """Ошибка, текст которой можно показать пользователю как есть."""


@dataclass
class Quota:
    wins: int
    limit: int          # min(wins, THEME_LIMIT)
    used: int
    left: int

    @property
    def capped(self) -> bool:
        """Побед больше, чем позволяет лимит."""
        return self.wins > THEME_LIMIT


# ==================== Сезон ====================

def current_quarter(today: date) -> tuple[str, str, str]:
    """(название, начало, конец) квартала, в который попадает дата."""
    quarter = (today.month - 1) // 3 + 1
    first_month = 3 * (quarter - 1) + 1
    starts = date(today.year, first_month, 1)
    if quarter == 4:
        ends = date(today.year, 12, 31)
    else:
        ends = date(today.year, first_month + 3, 1) - timedelta(days=1)
    return f"Q{quarter} {today.year}", starts.isoformat(), ends.isoformat()


def ensure_season(chat_id: int, today: date | None = None):
    """Возвращает активный сезон, при отсутствии заводит текущий квартал."""
    season = db.active_season(chat_id)
    if season:
        return season
    name, starts, ends = current_quarter(today or datetime.now(MSK).date())
    db.create_season(chat_id, name, starts, ends)
    return db.active_season(chat_id)


# ==================== Квота ====================

def quota_for(chat_id: int, season, username: str) -> Quota:
    wins = db.count_wins(chat_id, username, season["starts_on"], season["ends_on"])
    used = db.count_theme_orders(chat_id, season["id"], username)
    limit = min(wins, THEME_LIMIT)
    return Quota(wins=wins, limit=limit, used=used, left=max(0, limit - used))


def season_overview(chat_id: int, season) -> list[dict]:
    """Сводка по всем победителям сезона: сколько заказано, сколько осталось."""
    orders = db.theme_orders(chat_id, season["id"])
    used_by: dict[str, int] = {}
    for order in orders:
        used_by[order["username"]] = used_by.get(order["username"], 0) + 1

    rows = []
    seen = set()
    for winner in db.winners_in_season(chat_id, season["starts_on"], season["ends_on"]):
        name = winner["username"]
        seen.add(name)
        limit = min(winner["wins"], THEME_LIMIT)
        used = used_by.get(name, 0)
        rows.append({
            "username": name,
            "wins": winner["wins"],
            "limit": limit,
            "used": used,
            "left": max(0, limit - used),
        })

    # Игрок мог получить заказ вручную, не имея побед в этом сезоне.
    for name, used in used_by.items():
        if name not in seen:
            rows.append({"username": name, "wins": 0, "limit": 0,
                         "used": used, "left": 0})

    rows.sort(key=lambda r: (-r["left"], -r["wins"], r["username"]))
    return rows


# ==================== Слоты ====================

def season_grid(season) -> list[datetime]:
    """Все слоты сезона в UTC, от первого дня до последнего.

    Нумерация ведётся именно по этому списку, а не по свободным слотам:
    иначе номера сдвигались бы после каждой брони, и игрок, отправивший
    «/book 3» через минуту после просмотра списка, получал бы чужой слот.
    """
    starts = date.fromisoformat(season["starts_on"])
    ends = date.fromisoformat(season["ends_on"])

    slots = []
    day = starts
    while day <= ends:
        if day.weekday() in THEME_WEEKDAYS:
            for hour in THEME_HOURS:
                moment = datetime.combine(day, time(hour, 0), tzinfo=MSK)
                slots.append(moment.astimezone(timezone.utc))
        day += timedelta(days=1)
    return slots


def available_slots(chat_id: int, season, now_utc: datetime,
                    limit: int = 20) -> list[tuple[int, datetime]]:
    """[(сквозной номер, слот)] — только свободные и достаточно далёкие."""
    earliest = now_utc + timedelta(hours=THEME_LEAD_HOURS)
    busy = db.busy_slots(chat_id, season["id"])
    out = []
    for number, slot in enumerate(season_grid(season), 1):
        if slot < earliest or slot.isoformat() in busy:
            continue
        out.append((number, slot))
        if len(out) >= limit:
            break
    return out


def format_slot(slot_utc: datetime) -> str:
    msk = slot_utc.astimezone(MSK)
    return f"{WEEKDAY_NAMES[msk.weekday()]} {msk:%d.%m} в {msk:%H:%M}"


def resolve_slot(raw: str, chat_id: int, season, now_utc: datetime) -> datetime:
    """Номер из /slots или дата со временем «14.08 20:00» -> момент в UTC."""
    raw = raw.strip()
    grid = season_grid(season)
    earliest = now_utc + timedelta(hours=THEME_LEAD_HOURS)
    busy = db.busy_slots(chat_id, season["id"])

    if raw.isdigit():
        number = int(raw)
        if not (1 <= number <= len(grid)):
            raise ThemeError(f"нет слота с номером {number}, посмотрите /slots")
        slot = grid[number - 1]
    else:
        slot = None
        for fmt in ("%d.%m %H:%M", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(raw, fmt)
            except ValueError:
                continue
            if parsed.year == 1900:
                parsed = parsed.replace(year=date.fromisoformat(
                    season["starts_on"]).year)
            slot = parsed.replace(tzinfo=MSK).astimezone(timezone.utc)
            break
        if slot is None:
            raise ThemeError(
                "не понимаю слот. Укажите номер из /slots или «14.08 20:00»"
            )
        if slot not in grid:
            raise ThemeError(
                "игры проходят по понедельникам и четвергам в 19:00, 20:00, "
                "21:00 и 22:00 — посмотрите свободные слоты в /slots"
            )

    if slot.isoformat() in busy:
        raise ThemeError(f"слот {format_slot(slot)} уже занят, выберите другой")
    if slot < earliest:
        raise ThemeError(
            f"до слота меньше {THEME_LEAD_HOURS} ч — организатор не успеет "
            f"подготовить пакет. Выберите слот подальше"
        )
    return slot


# ==================== Бронирование ====================

def book(chat_id: int, season, username: str, slot_raw: str, theme: str,
         now_utc: datetime, created_by: int | None) -> tuple[int, datetime]:
    theme = theme.strip()
    if not theme:
        raise ThemeError("не указана тема")
    if len(theme) > 200:
        raise ThemeError("тема слишком длинная, уложитесь в 200 символов")

    quota = quota_for(chat_id, season, username)
    if quota.wins == 0:
        raise ThemeError("заказывать темы могут только победители квизов")
    if quota.left <= 0:
        if quota.capped:
            raise ThemeError(
                f"за сезон можно заказать не больше {THEME_LIMIT} тем, "
                f"ваш лимит исчерпан"
            )
        raise ThemeError(
            f"побед: {quota.wins}, уже заказано: {quota.used}. "
            f"Свободных заказов нет — выиграйте ещё квиз"
        )

    slot = resolve_slot(slot_raw, chat_id, season, now_utc)

    order_id = db.add_theme_order(
        chat_id=chat_id, season_id=season["id"], username=username, theme=theme,
        slot_utc=slot.isoformat(), status="booked", created_by=created_by,
    )
    return order_id, slot
