"""Церемония награждения по итогам сезона.

Отдельно от scheduler: тот следит за игровыми слотами, а это разовое
событие в конце сезона со своей логикой ожидания. Общее у них только то,
что оба живут на тике раз в минуту.

Зачёт берётся тот же, что показывает /rating, — иначе на церемонии
объявили бы одно, а в таблице весь сезон висело другое.
"""

import asyncio
import logging
from datetime import datetime, timezone

from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db
import engine
import texts
import themes
from config import MSK, TIMINGS, award_photo_url

log = logging.getLogger(__name__)

# Слоты, об ожидании которых уже написали в лог.
_reported_busy: set[tuple[int, str]] = set()


async def _say(context, chat_id: int, thread_id: int | None, text: str, **kwargs):
    """Отправка в чат или ветку. Церемония идёт без объекта игры."""
    kw = dict(chat_id=chat_id, text=text, **kwargs)
    if thread_id:
        kw["message_thread_id"] = thread_id
    try:
        return await context.bot.send_message(**kw)
    except TelegramError:
        log.exception("Церемония: сообщение в чат %s не отправилось", chat_id)
        return None


async def _say_photo(context, chat_id: int, thread_id: int | None, photo: str,
                     caption: str):
    """Карточка награждения. Недоступная ссылка не должна съесть объявление."""
    kw = dict(chat_id=chat_id, photo=photo, caption=caption)
    if thread_id:
        kw["message_thread_id"] = thread_id
    try:
        return await context.bot.send_photo(**kw)
    except TelegramError:
        log.warning("Карточка %s не отправилась, объявляем текстом", photo,
                    exc_info=True)
        return await _say(context, chat_id, thread_id, caption)


def season_standings(chat_id: int) -> tuple[str | None, list, int]:
    """(название сезона, строки зачёта, сыграно игр).

    Порядок строк — как в /rating: по очкам рейтинга, при равенстве очков
    выше тот, кто сыграл меньше игр.
    """
    bounds = themes.season_bounds(chat_id)
    if bounds:
        name, starts, ends = bounds
        until = ends + "T23:59:59+00:00"
        return name, db.rating_table_period(chat_id, starts, until), \
            db.games_played(chat_id, starts, until)
    return None, db.rating_table(chat_id), db.games_played(chat_id)


async def run(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
              thread_id: int | None, title: str | None = None) -> bool:
    """Проводит церемонию.

    title — как назвать сезон вслух: в базе он зовётся «Q3 2026», а на кубке
    выгравировано другое, и расхождение в одной церемонии бросается в глаза.
    """
    name, rows, games = await engine.to_db(season_standings, chat_id)
    if not rows:
        await _say(context, chat_id, thread_id, texts.AWARD_EMPTY)
        return False

    зачёт = [(r["username"], r["games_count"], r["total_points"] or 0) for r in rows]
    # Своё название подставляется как есть: «Второй сезон квизов Vegas» уже
    # содержит слово «сезон», и приписывать его ещё раз нельзя.
    подпись_сезона = title or (f"Сезон {name}" if name else "Сезон")

    await _say(context, chat_id, thread_id,
               texts.award_intro(подпись_сезона, len(зачёт), games))
    await asyncio.sleep(TIMINGS.award_pause)

    champion = None
    for place in (3, 2, 1):
        if len(зачёт) < place:
            continue
        await _say(context, chat_id, thread_id, texts.award_suspense(place))
        await asyncio.sleep(TIMINGS.award_suspense)

        username, сыграл, очки = зачёт[place - 1]
        реплика = texts.award_reveal(place, username, сыграл, очки)
        картинка = award_photo_url(username, place)
        if картинка:
            msg = await _say_photo(context, chat_id, thread_id, картинка, реплика)
        else:
            msg = await _say(context, chat_id, thread_id, реплика)
        if place == 1:
            champion = msg
        # Длинная пауза: сюда организатор вставляет картинку награждения.
        await asyncio.sleep(TIMINGS.award_pause)

    # Очки наверху могли совпасть — тогда титул решил следующий критерий,
    # и об этом честнее сказать прямо, чем оставить чат разбираться самому.
    if len(зачёт) > 1 and зачёт[0][2] == зачёт[1][2]:
        await _say(context, chat_id, thread_id,
                   texts.award_tie_note(зачёт[0][1] == зачёт[1][1]))

    await _say(context, chat_id, thread_id,
               texts.award_final(зачёт, подпись_сезона), parse_mode="Markdown")

    if champion:
        try:
            await context.bot.pin_chat_message(chat_id=chat_id,
                                               message_id=champion.message_id)
        except TelegramError:
            log.warning("Церемония: сообщение чемпиона не закрепилось", exc_info=True)

    log.info("Церемония награждения проведена в чате %s (сезон %s)", chat_id,
             подпись_сезона)
    return True


async def maybe_run(context: ContextTypes.DEFAULT_TYPE, now_utc, now_msk,
                    today: str) -> None:
    """Проверяет назначенные на сегодня церемонии.

    Опоздать здесь не страшно, а вот влезть в идущий квиз — страшно:
    посреди боя объявлять итоги сезона нельзя. Поэтому слот ждёт, пока
    чат освободится, и проводится хоть с опозданием. Границей служит сам
    день: назавтра запись уже не подойдёт по дате.
    """
    for row in await engine.to_db(db.pending_awards, today):
        chat_id, run_date = row["chat_id"], row["run_date"]
        ключ = (chat_id, run_date)

        hour, minute = (int(x) for x in row["time_msk"].split(":"))
        start_msk = now_msk.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now_utc < start_msk.astimezone(timezone.utc):
            continue

        if chat_id in engine.LIVE or await engine.to_db(db.active_game_for_chat, chat_id):
            if ключ not in _reported_busy:
                _reported_busy.add(ключ)
                log.info("Церемония в чате %s ждёт: в чате идёт квиз", chat_id)
            continue
        _reported_busy.discard(ключ)

        # Атомарная заявка: второй тик церемонию не повторит.
        if not await engine.to_db(db.claim_award, chat_id, run_date):
            continue
        await run(context, chat_id, row["thread_id"], row["title"])


def parse_date(raw: str) -> str:
    """'2026-09-28' -> та же строка. Бросает ValueError на мусоре."""
    return datetime.strptime(raw.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")


def today_msk() -> str:
    return datetime.now(timezone.utc).astimezone(MSK).strftime("%Y-%m-%d")
