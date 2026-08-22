"""Все тексты бота. Раньше строки были размазаны по 25 местам вместе с send_kwargs."""

from datetime import datetime

from config import MSK


def fmt_start_time(dt_utc: datetime) -> str:
    msk = dt_utc.astimezone(MSK)
    now_msk = datetime.now(MSK)
    if msk.date() == now_msk.date():
        when = f"сегодня, в {msk:%H:%M}"
    else:
        when = f"{msk:%d.%m.%Y}, в {msk:%H:%M}"
    return f"📅 Дата и время начала:\n{when}"


def registration(title: str, dt_utc: datetime, players: list[str]) -> str:
    if players:
        lst = "\n".join(f"• {p}" for p in players)
        head = f"👥 Список участников ({len(players)}):"
    else:
        lst = "пока никого"
        head = "👥 Список участников:"
    return (
        "🎪 ОТКРЫТА РЕГИСТРАЦИЯ НА КВИЗ\n\n"
        f"✏️ Тема квиза: {title}\n"
        f"{fmt_start_time(dt_utc)}\n\n"
        f"{head}\n{lst}"
    )


def registration_closed(title: str, dt_utc: datetime, players: list[str]) -> str:
    lst = "\n".join(f"• {p}" for p in players) or "нет участников"
    return (
        f"🎉 Регистрация завершена. Начинаем викторину «{title}»!\n"
        f"{fmt_start_time(dt_utc)}\n"
        f"Участников: {len(players)}\n{lst}"
    )


def pre_start_warning(mentions: list[str], seconds: int) -> str:
    who = " ".join(mentions) if mentions else "Участники"
    return (
        f"{who}\n\n"
        f"Квиз начнётся через {seconds} секунд! Даём вам время зайти в Телеграм, "
        f"проверить ваш VPN и настроиться быстро, а главное — правильно отвечать на вопросы!"
    )


def question(idx: int, total: int, text: str) -> str:
    return f"❓ Вопрос {idx + 1}/{total}\n\n{text}"


def question_result(idx: int, total: int, text: str, stats_lines: list[str],
                    correct_answer: str, comment: str = "") -> str:
    stats = "📊 Статистика ответов:\n" + "\n".join(stats_lines)
    tail = f"✅ Правильный ответ: {correct_answer}"
    if comment:
        tail += f"\n💡 {comment}"
    return f"❓ Вопрос {idx + 1}/{total}\n{text}\n\n{stats}\n\n{tail}"


def plain_name(username: str) -> str:
    """Убирает @, чтобы строка рейтинга не превращалась в упоминание.

    Telegram шлёт уведомление на каждое @имя в тексте. После каждого вопроса
    это означало бы 16 пингов за игру каждому участнику.
    """
    return username[1:] if username.startswith("@") else username


def leaderboard(rows: list[tuple[str, int]], limit: int = 10) -> str:
    shown = rows[:limit]
    lines = [f"{i}. {plain_name(name)} — {score} очк."
             for i, (name, score) in enumerate(shown, 1)]
    if len(rows) > limit:
        lines.append(f"…и ещё {len(rows) - limit} участников")
    return "🏆 Текущий рейтинг:\n" + "\n".join(lines)


# ==================== Таблицы для мобильного экрана ====================
#
# Экран телефона вмещает примерно 30 моноширинных символов. Всё, что шире,
# переносится и разваливает выравнивание.
#
# Эмодзи внутри выровненных колонок использовать нельзя: Python считает 🥇
# за один символ, а Telegram рисует его в две позиции — колонки съезжают.
# Поэтому медали ставятся в конец строки, после всех числовых колонок.

TABLE_WIDTH = 28
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def clip(name: str, width: int) -> str:
    name = plain_name(name)
    return name if len(name) <= width else name[: width - 1] + "…"


def rating_table(rows: list[tuple[str, int, int]], period: str = "") -> str:
    """rows: (ник, игр, очки)"""
    out = [f"🏆 РЕЙТИНГ ПО ОЧКАМ{period}", "```"]
    out.append(f"{'#':>2} {'Игрок':<15}{'Игр':>4}{'Очки':>5}")
    out.append("─" * TABLE_WIDTH)
    for i, (name, games, points) in enumerate(rows, 1):
        line = f"{i:2} {clip(name, 15):<15}{games:4}{points:5}"
        medal = MEDALS.get(i)
        out.append(f"{line} {medal}" if medal else line)
    out.append("```")
    return "\n".join(out)


def stats_entry(place: int, name: str, games: int, score: int,
                percent: float, avg_time: float, elo: int) -> str:
    """Двухстрочная карточка игрока: шесть колонок в ширину экрана не влезают."""
    medal = MEDALS.get(place, "")
    head = f"{place}. {plain_name(name)} {medal}".rstrip()
    body = f"    {games} игр · {score} очк · {percent:.0f}% · {avg_time:.1f}с · ELO {elo}"
    return f"{head}\n{body}"


def rank_entry(place: int, name: str, games: int, elo: float,
               delta_place: int | None, delta_elo: float | None) -> str:
    if delta_place is None:
        movement = "🆕"
    elif delta_place > 0:
        movement = f"▲{delta_place}"
    elif delta_place < 0:
        movement = f"▼{abs(delta_place)}"
    else:
        movement = "—"

    head = f"{place}. {plain_name(name)} {movement}"
    tail = "" if delta_elo is None else f" ({delta_elo:+.1f})"
    body = f"    {games} игр · ELO {elo:.0f}{tail}"
    return f"{head}\n{body}"


def podium(place: int, names: list[str]) -> str:
    joined = " и ".join(names)
    many = len(names) > 1
    if place == 3:
        return (f"Почётное 3 место {'разделили игроки' if many else 'занимает'} {joined}. "
                f"Поздравляем!")
    if place == 2:
        return (f"Немного не хватило для победы, 2 место "
                f"{'разделили игроки' if many else 'занимает'} {joined}. Поздравляем!")
    return f"Поздравляем {'победителей' if many else 'победителя'} нашей викторины — {joined}! 🎉🥳"


def final_table(ranking: list[dict]) -> str:
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = ["🏁 Итоговое положение:\n"]
    for group in ranking:
        marker = medals.get(group["place"], f"{group['place']}.")
        for p in group["players"]:
            lines.append(f"{marker} {p['username']} — {p['score']} очк.")
    return "\n".join(lines)


# ==================== Анонс игрового дня ====================

WEEKDAY_NOMINATIVE = ["ПОНЕДЕЛЬНИК", "ВТОРНИК", "СРЕДА", "ЧЕТВЕРГ",
                      "ПЯТНИЦА", "СУББОТА", "ВОСКРЕСЕНЬЕ"]

MONTH_GENITIVE = ["января", "февраля", "марта", "апреля", "мая", "июня",
                  "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def short_title(title: str) -> str:
    """Убирает из названия пакета служебный хвост.

    «🎭 Угадай сериал: 16 вопросов по 20 секунд» → «🎭 Угадай сериал»
    """
    cleaned = title.strip()
    for marker in (". 16 ", ": 16 ", ", 16 ", ". 20 "):
        if marker in cleaned:
            cleaned = cleaned.split(marker)[0]
            break
    cleaned = cleaned.rstrip(" .:,;")
    return cleaned


def game_day_announce(day, slots: list[tuple[str, str]]) -> str:
    """slots: [(время ЧЧ:ММ, название пакета)] в порядке проведения."""
    header = (
        "⭐️⭐️⭐️⭐️⭐️⭐️\n\n"
        "GAMESDAY\n\n"
        f"{WEEKDAY_NOMINATIVE[day.weekday()]}, 📆 {day.day} {MONTH_GENITIVE[day.month - 1]}\n"
    )
    if slots:
        header += f"Начало в {slots[0][0]}\n"

    body = ["", "Расписание игр на сегодня:", ""]
    for time_msk, title in slots:
        body.append(f"➡️ {time_msk} {short_title(title)}")
        body.append("")

    tail = ("\nРегистрация стартует за 45-60 минут до начала.\n\n"
            "Всем удачи!\n\n"
            "💞💓💕")
    return header + "\n".join(body).rstrip() + "\n" + tail


SCHEDULE_HELP = (
    "🗓 Управление автозапуском квизов\n\n"
    "`/schedule` — показать расписание группы\n"
    "`/schedule add пн,ср,пт | 20:00 | auto` — добавить слот\n"
    "`/schedule add ежедневно | 19:30 | 0007` — слот с фиксированным пакетом\n"
    "`/schedule del 3` — удалить слот по номеру\n"
    "`/schedule on 3` / `/schedule off 3` — включить/выключить слот\n"
    "`/skip` — пропустить ближайший автозапуск в этой группе\n\n"
    "Время указывается по Москве. `auto` = бот сам берёт неигранный пакет.\n"
    "Дни: пн вт ср чт пт сб вс, либо `ежедневно`."
)

QUIZ_HELP = (
    "❌ Неверный формат. Используйте:\n"
    "`/quiz 0007 | 2026-05-15 | 14:00`\n"
    "Дата и время — по Москве (UTC+3). Разделитель — вертикальная черта."
)

DELAYED_START = (
    "⚙️ Бот перезапускался и пропустил момент старта. "
    "Квиз начнётся через минуту — регистрация ещё открыта."
)

CANCELLED_AFTER_RESTART = (
    "⚠️ Квиз отменён: бот был недоступен дольше допустимого, "
    "и время старта прошло. Организатор может запустить его заново через /quiz."
)

INTERRUPTED = (
    "⚠️ Квиз был прерван перезапуском бота. "
    "Результаты по уже сыгранным вопросам сохранены."
)
