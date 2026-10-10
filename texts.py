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


# ==================== Награждение по итогам сезона ====================

AWARD_EMPTY = "🏆 Награждать некого: в этом сезоне не сыграно ни одной игры."

def award_tie_note(same_games: bool) -> str:
    """Равенство очков наверху — объясняем, чем решился титул."""
    основание = ("по среднему ELO за сезон" if same_games
                 else "по меньшему числу игр за сезон")
    return (
        "ℹ️ У первого и второго места одинаковое число очков рейтинга. "
        f"Титул определён {основание} — тем же порядком, по которому "
        "таблица /rating выстраивалась весь сезон."
    )

AWARD_HELP = (
    "🏆 Награждение по итогам сезона\n\n"
    "`/award 2026-09-28 22:15` — назначить церемонию\n"
    "`/award 2026-09-28 22:15 | Второй сезон квизов Vegas` — со своим "
    "названием сезона\n"
    "`/award` — что назначено\n"
    "`/award off` — отменить\n\n"
    "Бот объявит третье, второе и первое место с паузами — между "
    "репликами успеете выложить картинки. Зачёт по очкам сезонного "
    "рейтинга, как в /rating."
)

_AWARD_SUSPENSE = {
    3: "🥁 Третье место сезона достаётся...",
    2: "🥁 Второе место сезона...",
    1: "🥁 И чемпион сезона —",
}

_AWARD_WORDS = {
    3: "Бронза сезона и законное место на подиуме.",
    2: "Серебро сезона. До титула не хватило самой малости.",
    1: "ЧЕМПИОН СЕЗОНА! Высшая ступень занята по праву.",
}


def award_intro(label: str, players: int, games: int) -> str:
    """label — готовая подпись сезона, например «Сезон Q3 2026»."""
    return (
        "🏆 🏆 🏆\n\n"
        "ИТОГИ СЕЗОНА\n\n"
        f"{label} завершён.\n"
        f"Сыграно игр: {games}\n"
        f"Участников в зачёте: {players}\n\n"
        "Очки посчитаны, таблица закрыта, спорить больше не о чем.\n"
        "Переходим к самому приятному. Награждение! 🥁"
    )


def award_suspense(place: int) -> str:
    return _AWARD_SUSPENSE[place]


def award_reveal(place: int, name: str, games: int, points: int) -> str:
    return (
        f"{MEDALS.get(place, '')} {plain_name(name)}\n\n"
        f"Очки рейтинга за сезон: {points}\n"
        f"Игр сыграно: {games}\n\n"
        f"{_AWARD_WORDS[place]}\n\nПоздравляем! 🎉"
    )


def award_final(rows: list[tuple[str, int, int]], label: str,
                limit: int = 10) -> str:
    """Финальная таблица сезона и слова на прощание."""
    return (
        f"{rating_table(rows[:limit], period=f' · {label}')}\n\n"
        "Спасибо всем, кто играл этот сезон: за скорость, за споры в чате "
        "и за то, что приходили даже в будни.\n\n"
        "Новый сезон — новый отсчёт. Рейтинг обнуляется, шансы равны. 💞💓💕"
    )


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


def game_day_announce(day, slots: list[tuple[str, str]],
                      with_image: bool = False) -> str:
    """slots: [(время ЧЧ:ММ, название пакета)] в порядке проведения.

    with_image=True — текст пойдёт подписью к картинке, поэтому
    звёздочки и слово GAMESDAY из шапки убираются: они уже на картинке.
    """
    header = "" if with_image else "⭐️⭐️⭐️⭐️⭐️⭐️\n\nGAMESDAY\n\n"
    header += (
        f"{WEEKDAY_NOMINATIVE[day.weekday()]}, 📆 {day.day} "
        f"{MONTH_GENITIVE[day.month - 1]}\n"
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
    "`/schedule time 14:00 15:00 18:00 19:00` — переписать время слотов\n"
    "`/schedule days пн,чт` — сменить дни у всех слотов\n"
    "`/schedule pool 02` — перевести все слоты на новый пул пакетов\n"
    "`/skip` — пропустить ближайший автозапуск в этой группе\n\n"
    "Время указывается по Москве. `auto` = бот сам берёт неигранный пакет.\n"
    "Дни: пн вт ср чт пт сб вс, либо `ежедневно`."
)

GAME_HELP = (
    "💡 Своя тема для квиза\n\n"
    "`/game История рок-музыки 90-х` — предложить тему\n\n"
    "Пишет кто угодно, сколько угодно раз. Все идеи попадают к "
    "организаторам одним списком."
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

def cancelled_broken_pack(pack_id: str) -> str:
    return (
        f"⚠️ Квиз отменён: пакет вопросов {pack_id} не читается.\n"
        "Кнопка регистрации выше больше не работает — организатор "
        "починит пакет и запустит игру заново."
    )


INTERRUPTED = (
    "⚠️ Квиз был прерван перезапуском бота. "
    "Результаты по уже сыгранным вопросам сохранены."
)
