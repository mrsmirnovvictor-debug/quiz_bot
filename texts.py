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

INTERRUPTED = (
    "⚠️ Квиз был прерван перезапуском бота. "
    "Результаты по уже сыгранным вопросам сохранены."
)
