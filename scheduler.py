"""Автозапуск квизов по расписанию.

Схема принципиально другая, чем run_once на весь день вперёд: раз в минуту
тик сверяет текущее время со слотами. Такой планировщик переживает рестарт —
после подъёма бот сам увидит, что слот наступил, и откроет регистрацию.

Защита от двойного запуска — атомарный UPDATE last_run_date в SQLite
(db.claim_schedule_run), а не флаг в памяти.
"""

import logging
from datetime import datetime, timedelta, timezone

from telegram.ext import ContextTypes

import db
import engine
import packs
from config import MSK, TIMINGS

log = logging.getLogger(__name__)

# Если бот лежал и проспал слот — запускаем с опозданием не больше этого.
LATE_GRACE = timedelta(minutes=15)
# Минимальный запас на регистрацию при опоздавшем старте.
LATE_LEAD = timedelta(minutes=3)

DAY_ALIASES = {
    "пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}
DAY_NAMES = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
DAILY = {"ежедневно", "daily", "всегда", "каждый день"}


class ScheduleError(Exception):
    pass


# ==================== Разбор пользовательского ввода ====================

def parse_days(raw: str) -> str:
    """'пн,ср,пт' | 'ежедневно' -> нормализованная строка для БД."""
    value = raw.strip().lower()
    if value in DAILY:
        return "daily"
    parts = [p.strip() for p in value.replace(" ", ",").split(",") if p.strip()]
    days = set()
    for p in parts:
        if p not in DAY_ALIASES:
            raise ScheduleError(f"не понимаю день «{p}». Используйте пн вт ср чт пт сб вс")
        days.add(DAY_ALIASES[p])
    if not days:
        raise ScheduleError("не указан ни один день")
    return ",".join(DAY_NAMES[d] for d in sorted(days))


def parse_time(raw: str) -> str:
    try:
        parsed = datetime.strptime(raw.strip(), "%H:%M")
    except ValueError:
        raise ScheduleError("время указывается как ЧЧ:ММ, например 20:00") from None
    return parsed.strftime("%H:%M")


def parse_pack_source(raw: str) -> tuple[str, str]:
    """Возвращает (pack_source, pack_pool)."""
    value = raw.strip().lower()
    if value in ("auto", "авто", ""):
        return "auto", ""
    if value.startswith("auto:"):
        return "auto", value.split(":", 1)[1].strip()
    packs.load_pack(value)          # валидируем сразу, а не в момент запуска
    return value, ""


def days_match(days: str, weekday: int) -> bool:
    if days == "daily":
        return True
    return DAY_NAMES[weekday] in days.split(",")


def describe(row) -> str:
    days = "ежедневно" if row["days"] == "daily" else row["days"]
    if row["pack_source"] == "auto":
        pack = f"авто{':' + row['pack_pool'] if row['pack_pool'] else ''}"
    else:
        pack = row["pack_source"]
    state = "" if row["enabled"] else " (выключен)"
    lead = row["reg_lead_minutes"]
    return (f"#{row['id']} — {days} в {row['time_msk']} МСК, пакет: {pack}, "
            f"регистрация за {lead} мин{state}")


# ==================== Выбор пакета ====================

def pick_pack(chat_id: int, pack_source: str, pool: str) -> tuple[packs.Pack, bool]:
    """(пакет, признак повторного показа).

    Приоритет — ни разу не игранные в этой группе пакеты. Когда пул исчерпан,
    берётся тот, что не выпадал дольше всех, и бот честно об этом сообщает.
    """
    if pack_source != "auto":
        return packs.load_pack(pack_source), False

    available = packs.list_pack_ids(pool)
    if not available:
        raise ScheduleError(f"в пуле «{pool or 'все'}» нет ни одного пакета")

    # Пакеты под заказанные темы держим отдельно: их запустит механизм тем
    # в свой слот, а не общий автовыбор.
    played = db.played_pack_ids(chat_id) | db.reserved_pack_ids(chat_id)
    unplayed = [p for p in available if p not in played]
    if unplayed:
        pack, _ = packs.load_first_valid(unplayed)
        return pack, False

    last = db.last_played_at(chat_id)
    available.sort(key=lambda p: last.get(p, ""))
    pack, _ = packs.load_first_valid(available)
    return pack, True


# ==================== Тик ====================

def install(application) -> None:
    application.job_queue.run_repeating(
        tick, interval=TIMINGS.scheduler_tick, first=20, name="scheduler",
    )
    log.info("Планировщик запущен, период %s с", TIMINGS.scheduler_tick)


async def tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    now_utc = datetime.now(timezone.utc)
    now_msk = now_utc.astimezone(MSK)
    today = now_msk.strftime("%Y-%m-%d")

    try:
        rows = await engine.to_db(db.list_schedules)
    except Exception:
        log.exception("Не удалось прочитать расписание")
        return

    # Заказанные темы имеют приоритет: игрок выиграл право на конкретный слот,
    # и обычное расписание не должно занять его первым.
    try:
        await _run_theme_orders(context, now_utc)
    except Exception:
        log.exception("Ошибка обработки заказанных тем")

    for row in rows:
        try:
            await _maybe_run(context, row, now_utc, now_msk, today)
        except Exception:
            log.exception("Слот #%s: ошибка обработки", row["id"])


async def _run_theme_orders(context, now_utc) -> None:
    """Запускает квизы по заказанным темам, у которых привязан пакет.

    Регистрация открывается заранее, поэтому окно смотрим вперёд на величину
    THEME_REG_LEAD, а назад — на LATE_GRACE, чтобы подхватить пропущенное
    после простоя.
    """
    from config import THEME_REG_LEAD_MINUTES

    horizon = now_utc + timedelta(minutes=THEME_REG_LEAD_MINUTES)
    floor = now_utc - LATE_GRACE
    orders = await engine.to_db(db.due_theme_orders, floor.isoformat(),
                                horizon.isoformat())

    for order in orders:
        chat_id = order["chat_id"]
        if chat_id in engine.LIVE:
            continue
        if await engine.to_db(db.active_game_for_chat, chat_id):
            continue

        slot = datetime.fromisoformat(order["slot_utc"])
        # Помечаем сразу: повторный тик не должен запустить игру второй раз.
        await engine.to_db(db.update_theme_order, order["id"], status="played")

        try:
            pack = await engine.to_db(packs.load_pack, order["pack_id"])
        except Exception as e:
            log.exception("Заказ #%s: пакет не читается", order["id"])
            await _notify(context, chat_id, None,
                          f"⚠️ Тема «{order['theme']}» не запущена: {e}")
            continue

        start = slot if slot > now_utc else now_utc + LATE_LEAD
        game = await engine.create_game(
            context, chat_id=chat_id, thread_id=None, pack=pack,
            creator_id=order["created_by"], start_utc=start, source="theme",
        )
        log.info("Заказ #%s запустил квиз %s (пакет %s)",
                 order["id"], game.game_id, order["pack_id"])
        await _notify(
            context, chat_id, None,
            f"🎯 Тема этого квиза заказана игроком {order['username']}: "
            f"«{order['theme']}»"
        )


async def _maybe_run(context, row, now_utc, now_msk, today) -> None:
    if not days_match(row["days"], now_msk.weekday()):
        return
    if row["last_run_date"] == today or row["skip_date"] == today:
        return

    hour, minute = (int(x) for x in row["time_msk"].split(":"))
    start_msk = now_msk.replace(hour=hour, minute=minute, second=0, microsecond=0)
    start_utc = start_msk.astimezone(timezone.utc)
    open_at = start_utc - timedelta(minutes=row["reg_lead_minutes"])

    if now_utc < open_at:
        return                       # ещё рано
    if now_utc > start_utc + LATE_GRACE:
        return                       # слот безнадёжно проспан, ждём следующего дня

    chat_id = row["chat_id"]

    # Если на это же время есть заказанная тема — расписание молча уступает.
    booked = await engine.to_db(db.booked_order_at, chat_id, start_utc.isoformat())
    if booked:
        log.info("Слот #%s уступает заказу #%s (%s)",
                 row["id"], booked["id"], booked["theme"])
        await engine.to_db(db.claim_schedule_run, row["id"], today)
        return

    if chat_id in engine.LIVE:
        log.info("Слот #%s пропущен: в чате уже идёт квиз", row["id"])
        return
    existing = await engine.to_db(db.active_game_for_chat, chat_id)
    if existing:
        log.info("Слот #%s пропущен: незакрытая игра %s", row["id"], existing["id"])
        return

    # Атомарная заявка. Если параллельный тик успел раньше — выходим.
    if not await engine.to_db(db.claim_schedule_run, row["id"], today):
        return

    try:
        pack, repeat = await engine.to_db(pick_pack, chat_id, row["pack_source"],
                                          row["pack_pool"])
    except Exception as e:
        log.exception("Слот #%s: не удалось выбрать пакет", row["id"])
        await _notify(context, chat_id, row["thread_id"],
                      f"⚠️ Автозапуск квиза отменён: {e}")
        return

    # Опоздали к слоту — даём людям хотя бы несколько минут на регистрацию.
    actual_start = start_utc if start_utc > now_utc else now_utc + LATE_LEAD

    game = await engine.create_game(
        context, chat_id=chat_id, thread_id=row["thread_id"], pack=pack,
        creator_id=row["created_by"], start_utc=actual_start, source="schedule",
    )
    log.info("Слот #%s запустил квиз %s (пакет %s)", row["id"], game.game_id, pack.pack_id)

    if repeat:
        await _notify(context, chat_id, row["thread_id"],
                      "ℹ️ Все пакеты из пула уже сыграны в этой группе — "
                      "берём самый давний. Пора добавить новые вопросы.")


async def _notify(context, chat_id: int, thread_id, text: str) -> None:
    kwargs = {"chat_id": chat_id, "text": text}
    if thread_id:
        kwargs["message_thread_id"] = thread_id
    try:
        await context.bot.send_message(**kwargs)
    except Exception:
        log.warning("Не удалось уведомить чат %s", chat_id, exc_info=True)
