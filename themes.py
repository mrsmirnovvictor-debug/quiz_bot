"""Заказ тем победителями квизов.

Правила:
  * одна победа даёт право заказать одну тему;
  * за сезон нельзя заказать больше THEME_LIMIT тем, сколько бы побед ни было.

Сами темы игроки заказывают у организатора в личке; бот только ведёт учёт
квот и заказов. Ни слотов, ни автозапуска заказанных тем здесь нет —
расписание живёт своей жизнью и берёт пакеты по порядку.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import db
from config import MSK, THEME_LIMIT


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


def season_bounds(chat_id: int) -> tuple[str, str, str] | None:
    """(название, начало, конец) активного сезона или None.

    В отличие от ensure_season ничего не создаёт: вызывается из выгрузки
    в Sheets, где неожиданное появление сезона было бы сюрпризом.
    """
    season = db.active_season(chat_id)
    if not season:
        return None
    return season["name"], season["starts_on"], season["ends_on"]


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


# ==================== Заказы ====================

def add_order(chat_id: int, season, username: str, theme: str,
              created_by: int | None) -> int:
    """Регистрирует заказ. Слота и пакета у заказа больше нет:
    договорённость о теме идёт в личке, бот только считает квоты."""
    theme = theme.strip()
    if not theme:
        raise ThemeError("не указана тема")
    if len(theme) > 200:
        raise ThemeError("тема слишком длинная, уложитесь в 200 символов")

    return db.add_theme_order(
        chat_id=chat_id, season_id=season["id"], username=username,
        theme=theme, slot_utc=None, status="legacy", created_by=created_by,
    )
