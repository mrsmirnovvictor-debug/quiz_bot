"""Команды организатора и управление расписанием."""

import logging
import re
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

import awards
import db
import engine
import packs
import scheduler
import sheets
import texts
from config import MSK, SHEETS_ENABLED

log = logging.getLogger(__name__)


async def is_admin(update: Update, user_id: int) -> bool:
    try:
        member = await update.effective_chat.get_member(user_id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False


def _args(message_text: str, command: str = "") -> str:
    """Аргументы команды.

    Режем по первому пробелу, а не по длине команды: в группе Telegram
    подставляет /quiz@ИмяБота, и отсчёт по длине съедал бы первые символы
    аргумента. Так же это сделано в handlers_themes.
    """
    parts = (message_text or "").strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


# ==================== /quiz ====================

async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if update.effective_chat.type == "private":
        await update.message.reply_text("Команда работает только в группах.")
        return
    if not await is_admin(update, user.id):
        await update.message.reply_text("❌ Только администраторы могут запускать викторину.")
        return
    if chat_id in engine.LIVE:
        await update.message.reply_text("❌ Викторина уже идёт в этой группе.")
        return

    parts = re.split(r"\s*\|\s*", _args(update.message.text, "/quiz"))
    if len(parts) != 3:
        await update.message.reply_text(texts.QUIZ_HELP, parse_mode="Markdown")
        return

    pack_id, date_str, time_str = parts
    try:
        pack = packs.load_pack(pack_id.strip())
    except packs.PackError as e:
        await update.message.reply_text(f"❌ {e}")
        return

    try:
        naive = datetime.strptime(f"{date_str.strip()} {time_str.strip()}", "%Y-%m-%d %H:%M")
    except ValueError:
        await update.message.reply_text(
            "❌ Формат даты и времени: ГГГГ-ММ-ДД ЧЧ:ММ (по Москве)."
        )
        return

    start_utc = naive.replace(tzinfo=MSK).astimezone(timezone.utc)
    if start_utc < datetime.now(timezone.utc) + timedelta(minutes=2):
        await update.message.reply_text(
            "❌ Время начала должно быть не раньше чем через 2 минуты."
        )
        return

    await engine.create_game(
        context, chat_id=chat_id, thread_id=update.effective_message.message_thread_id,
        pack=pack, creator_id=user.id, start_utc=start_utc, source="manual",
    )


# ==================== Пауза / стоп ====================

async def pause_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    game = engine.LIVE.get(update.effective_chat.id)
    if not game:
        await update.message.reply_text("❌ Нет активного квиза.")
        return
    if not await _can_manage(update, game):
        return

    if game.status == "active":
        game.pause_after_question = True
        await update.message.reply_text("⏸ Квиз будет приостановлен после текущего вопроса.")
    else:
        game.status = "paused"
        await engine.to_db(db.update_game, game.game_id, status="paused")
        await update.message.reply_text("⏸ Квиз приостановлен. /resume для продолжения.")


async def resume_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    game = engine.LIVE.get(update.effective_chat.id)
    if not game or game.status != "paused":
        await update.message.reply_text("❌ Квиз не на паузе.")
        return
    if not await _can_manage(update, game):
        return

    game.status = "active"
    await engine.to_db(db.update_game, game.game_id, status="active")
    await update.message.reply_text("▶️ Квиз возобновлён.")

    if game.current_question < game.total_questions:
        engine._schedule(context, engine.job_start_question, 3,
                         game.chat_id, f"q:{game.chat_id}")
    else:
        await engine.finish_quiz(context, game)


async def abort_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    game = engine.LIVE.get(chat_id)
    if not game:
        await update.message.reply_text("❌ Нет активного квиза.")
        return
    if not await _can_manage(update, game):
        return

    engine.cancel_chat_jobs(context, chat_id)
    game.purge_messages = False
    engine.LIVE.pop(chat_id, None)

    if game.answers:
        # Не выбрасываем сыгранное: сохраняем результат по отвеченным вопросам.
        await engine.finish_quiz(context, game, interrupted=True)
    else:
        await engine.to_db(db.update_game, game.game_id, status="aborted")
        await update.message.reply_text("🛑 Квиз остановлен.")


async def _can_manage(update: Update, game) -> bool:
    user_id = update.effective_user.id
    if game.creator_id and user_id == game.creator_id:
        return True
    if await is_admin(update, user_id):
        return True
    await update.message.reply_text("❌ Только организатор или админ группы.")
    return False


# ==================== /schedule ====================

async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if update.effective_chat.type == "private":
        await update.message.reply_text("🗓 Расписание настраивается в группе.")
        return

    raw = _args(update.message.text, "/schedule")
    if not raw:
        await _show_schedule(update, chat_id)
        return

    if not await is_admin(update, update.effective_user.id):
        await update.message.reply_text("❌ Только администраторы группы.")
        return

    action, _, rest = raw.partition(" ")
    action = action.lower()

    if action in ("add", "добавить"):
        await _add_schedule(update, chat_id, rest)
    elif action in ("del", "delete", "удалить"):
        await _delete_schedule(update, chat_id, rest)
    elif action in ("on", "off", "вкл", "выкл"):
        await _toggle_schedule(update, chat_id, rest, action in ("on", "вкл"))
    elif action in ("pool", "пул"):
        await _set_pool(update, chat_id, rest)
    elif action in ("time", "время"):
        await _set_times(update, chat_id, rest)
    elif action in ("days", "дни"):
        await _set_days(update, chat_id, rest)
    else:
        await update.message.reply_text(texts.SCHEDULE_HELP, parse_mode="Markdown")


async def _show_schedule(update: Update, chat_id: int):
    rows = await engine.to_db(db.list_schedules, chat_id)
    if not rows:
        await update.message.reply_text(
            "🗓 Автозапуск для этой группы не настроен.\n\n" + texts.SCHEDULE_HELP,
            parse_mode="Markdown",
        )
        return
    lines = ["🗓 Расписание квизов:\n"]
    lines += _schedule_lines(rows)
    lines.append("\n`/schedule add ...` — добавить, `/schedule del N` — удалить")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


def _schedule_lines(rows) -> list[str]:
    return [scheduler.describe(r) for r in sorted(rows, key=lambda r: r["time_msk"])]


async def _set_times(update: Update, chat_id: int, rest: str):
    """Переписывает время всех слотов группы, не трогая остального.

    Через del+add пришлось бы заново указать пул, запас на регистрацию и
    попасть в нужную ветку — четыре слота означали бы восемь команд и
    четыре шанса потерять настройку.
    """
    куски = [c for c in re.split(r"[\s,]+", rest.strip()) if c]
    if not куски:
        await update.message.reply_text(
            "❌ Укажите время слотов: `/schedule time 14:00 15:00 18:00 19:00`",
            parse_mode="Markdown")
        return
    try:
        времена = sorted(scheduler.parse_time(c) for c in куски)
    except scheduler.ScheduleError as e:
        await update.message.reply_text(f"❌ {e}")
        return
    if len(set(времена)) != len(времена):
        await update.message.reply_text("❌ Время слотов не должно повторяться.")
        return

    rows = await engine.to_db(db.list_schedules, chat_id)
    if not rows:
        await update.message.reply_text(
            "ℹ️ В этой группе нет слотов. Добавьте их через `/schedule add`.",
            parse_mode="Markdown")
        return
    if len(времена) != len(rows):
        await update.message.reply_text(
            f"❌ Слотов в группе {len(rows)}, а времён указано {len(времена)}.\n"
            "Команда меняет время существующих слотов и их число не трогает.\n"
            "Добавить или убрать слот — `/schedule add` и `/schedule del`.",
            parse_mode="Markdown")
        return

    # Самый ранний слот получает самое раннее время, так что порядок, в
    # котором их перечислили, значения не имеет.
    по_порядку = sorted(rows, key=lambda r: (r["time_msk"], r["id"]))
    await engine.to_db(db.set_schedule_times, chat_id,
                       list(zip([r["id"] for r in по_порядку], времена)))

    rows = await engine.to_db(db.list_schedules, chat_id)
    await update.message.reply_text("\n".join(
        ["✅ Время слотов обновлено:\n"] + _schedule_lines(rows)
        + ["\nСлот, чьё время на сегодня уже прошло, сегодня не запустится — "
           "бот дождётся следующего игрового дня."]))


async def _set_days(update: Update, chat_id: int, rest: str):
    """Меняет дни недели у всех слотов группы."""
    if not rest.strip():
        await update.message.reply_text(
            "❌ Укажите дни: `/schedule days пн,чт`", parse_mode="Markdown")
        return
    try:
        days = scheduler.parse_days(rest)
    except scheduler.ScheduleError as e:
        await update.message.reply_text(f"❌ {e}")
        return

    изменено = await engine.to_db(db.set_schedule_days, chat_id, days)
    if not изменено:
        await update.message.reply_text("ℹ️ В этой группе нет слотов.")
        return

    rows = await engine.to_db(db.list_schedules, chat_id)
    await update.message.reply_text("\n".join(
        [f"✅ Дни обновлены, слотов затронуто: {изменено}.\n"] + _schedule_lines(rows)))


async def _set_pool(update: Update, chat_id: int, rest: str):
    """Переводит все авто-слоты группы на другой префикс пакетов."""
    pool = rest.strip()
    if not pool or not (pool.isascii() and pool.isdigit()):
        await update.message.reply_text(
            "❌ Пул — числовой префикс ID пакета, например `/schedule pool 02`.",
            parse_mode="Markdown")
        return

    доступно = await engine.to_db(packs.list_pack_ids, pool)
    if not доступно:
        await update.message.reply_text(
            f"❌ В пуле «{pool}» нет ни одного пакета — слоты не тронуты.")
        return

    изменено = await engine.to_db(db.set_schedule_pool, chat_id, pool)
    if not изменено:
        await update.message.reply_text(
            "ℹ️ В этой группе нет слотов с автовыбором пакета.")
        return

    сыграно = await engine.to_db(db.played_pack_ids, chat_id)
    свежие = [p for p in доступно if p not in сыграно]
    lines = [
        f"✅ Слотов переведено на пул «{pool}»: {изменено}.",
        f"Пакетов в пуле: {len(доступно)}, из них не игранных: {len(свежие)}.",
    ]
    if свежие:
        lines.append("Ближайшие в очереди: " + ", ".join(свежие[:8]))
    else:
        lines.append("⚠️ Все пакеты пула уже сыграны — бот будет брать повторы.")
    await update.message.reply_text("\n".join(lines))


async def _add_schedule(update: Update, chat_id: int, rest: str):
    parts = [p.strip() for p in re.split(r"\s*\|\s*", rest)]
    if len(parts) < 2:
        await update.message.reply_text(texts.SCHEDULE_HELP, parse_mode="Markdown")
        return

    try:
        days = scheduler.parse_days(parts[0])
        time_msk = scheduler.parse_time(parts[1])
        pack_source, pool = scheduler.parse_pack_source(parts[2] if len(parts) > 2 else "auto")
        lead = int(parts[3]) if len(parts) > 3 else 60
    except (scheduler.ScheduleError, packs.PackError) as e:
        await update.message.reply_text(f"❌ {e}")
        return
    except ValueError:
        await update.message.reply_text("❌ Время регистрации указывается числом минут.")
        return

    schedule_id = await engine.to_db(
        db.add_schedule, chat_id, update.effective_message.message_thread_id,
        days, time_msk, pack_source, pool, lead, update.effective_user.id,
    )
    rows = await engine.to_db(db.list_schedules, chat_id)
    row = next(r for r in rows if r["id"] == schedule_id)
    await update.message.reply_text(f"✅ Слот добавлен:\n{scheduler.describe(row)}")


async def _delete_schedule(update: Update, chat_id: int, rest: str):
    try:
        schedule_id = int(rest.strip().lstrip("#"))
    except ValueError:
        await update.message.reply_text("❌ Укажите номер слота: `/schedule del 3`",
                                        parse_mode="Markdown")
        return
    ok = await engine.to_db(db.delete_schedule, schedule_id, chat_id)
    await update.message.reply_text("✅ Слот удалён." if ok else "❌ Слот не найден.")


async def _toggle_schedule(update: Update, chat_id: int, rest: str, enabled: bool):
    try:
        schedule_id = int(rest.strip().lstrip("#"))
    except ValueError:
        await update.message.reply_text("❌ Укажите номер слота.")
        return
    ok = await engine.to_db(db.set_schedule_enabled, schedule_id, chat_id, enabled)
    if not ok:
        await update.message.reply_text("❌ Слот не найден.")
    else:
        await update.message.reply_text("✅ Слот включён." if enabled else "⏸ Слот выключен.")


async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пропустить сегодняшний автозапуск, не трогая расписание."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("Команда работает только в группах.")
        return
    if not await is_admin(update, update.effective_user.id):
        await update.message.reply_text("❌ Только администраторы группы.")
        return

    today = datetime.now(MSK).strftime("%Y-%m-%d")
    count = await engine.to_db(db.set_skip_date, update.effective_chat.id, today)
    if count:
        await update.message.reply_text(f"⏭ Сегодняшний автозапуск пропущен (слотов: {count}).")
    else:
        await update.message.reply_text("🗓 В этой группе нет активных слотов.")


# ==================== /rename ====================

async def rename_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rename @старый_ник @новый_ник

    Игроки в статистике группируются по нику, поэтому смена ника в Telegram
    без этой команды разделила бы человека на двух.
    """
    if update.effective_chat.type == "private":
        await update.message.reply_text("Команда работает только в группах.")
        return
    if not await is_admin(update, update.effective_user.id):
        await update.message.reply_text("❌ Только администраторы группы.")
        return

    parts = _args(update.message.text, "/rename").split()
    if len(parts) != 2:
        await update.message.reply_text(
            "❌ Формат: `/rename @старый_ник @новый_ник`",
            parse_mode="Markdown",
        )
        return

    old, new = (p if p.startswith(("@", "id")) else "@" + p for p in parts)
    if old == new:
        await update.message.reply_text("❌ Ники совпадают.")
        return

    info = await engine.to_db(db.find_player, old)
    if not info:
        await update.message.reply_text(
            f"❌ Игрок {old} в истории не найден. Проверьте написание — "
            f"ник вводится ровно так, как он отображается в таблице."
        )
        return

    merged = await engine.to_db(db.find_player, new)
    chats = await engine.to_db(db.player_chats, old)
    counts = await engine.to_db(db.rename_player, old, new)

    lines = [
        f"✅ {old} → {new}",
        f"Обновлено записей: {counts['results']} (игры с {info['first_game']} "
        f"по {info['last_game']})",
    ]
    if merged:
        lines.append(
            f"⚠️ Под ником {new} уже было {merged['games']} игр — истории объединены."
        )

    if SHEETS_ENABLED:
        for chat_id in chats:
            try:
                await engine.to_db(sheets.rebuild_chat, chat_id)
            except Exception:
                log.exception("Не удалось обновить витрину чата %s", chat_id)
        lines.append("📊 Листы Players / Rating / Ranking пересобраны.")
        lines.append(
            "ℹ️ В листе Games старые строки сохранили прежний ник — "
            "это журнал, он не переписывается."
        )

    await update.message.reply_text("\n".join(lines))


# ==================== /award ====================

async def award_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Назначает награждение по итогам сезона.

    `/award` — что назначено, `/award 2026-09-28 22:15` — назначить,
    `/award off` — отменить. Церемония проводится сама на тике: если в
    назначенный момент в чате идёт квиз, она дожидается его конца.
    """
    chat_id = update.effective_chat.id
    if update.effective_chat.type == "private":
        await update.message.reply_text("Команда работает только в группах.")
        return
    if not await is_admin(update, update.effective_user.id):
        await update.message.reply_text("❌ Только администраторы группы.")
        return

    raw = _args(update.message.text, "/award")
    # Название сезона идёт после вертикальной черты — тем же разделителем,
    # что в /quiz и /schedule.
    команда, _, название = raw.partition("|")
    название = название.strip() or None
    args = команда.split()

    if not args:
        rows = await engine.to_db(db.awards_for_chat, chat_id)
        if not rows:
            await update.message.reply_text(texts.AWARD_HELP, parse_mode="Markdown")
            return
        lines = ["🏆 Награждения этой группы:\n"]
        for r in rows:
            состояние = "проведено" if r["done_at"] else "ждёт"
            lines.append(f"• {r['run_date']} в {r['time_msk']} МСК — {состояние}")
        lines.append("\n" + texts.AWARD_HELP)
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    if args[0].lower() in ("off", "отмена", "del"):
        удалено = await engine.to_db(db.delete_pending_awards, chat_id)
        await update.message.reply_text(
            f"✅ Отменено назначений: {удалено}." if удалено
            else "ℹ️ Нечего отменять: назначенных церемоний нет."
        )
        return

    if len(args) != 2:
        await update.message.reply_text(texts.AWARD_HELP, parse_mode="Markdown")
        return

    try:
        run_date = awards.parse_date(args[0])
    except ValueError:
        await update.message.reply_text("❌ Дата указывается как ГГГГ-ММ-ДД, например 2026-09-28.")
        return
    try:
        time_msk = scheduler.parse_time(args[1])
    except scheduler.ScheduleError as e:
        await update.message.reply_text(f"❌ {e}")
        return

    if run_date < awards.today_msk():
        await update.message.reply_text("❌ Эта дата уже прошла.")
        return

    thread_id = update.message.message_thread_id
    await engine.to_db(db.set_award, chat_id, run_date, time_msk, thread_id,
                       update.effective_user.id, название)
    ответ = [f"✅ Награждение назначено на {run_date}, {time_msk} МСК."]
    if название:
        ответ.append(f"Сезон будет назван так: «{название}».")
    ответ.append("Если к этому времени игра ещё не закончится, бот дождётся её финала.")
    ответ.append("Зачёт — по очкам сезонного рейтинга, как в /rating.")
    await update.message.reply_text("\n".join(ответ))


# ==================== /announce ====================

async def announce_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перевыпуск анонса игрового дня."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("Команда работает только в группах.")
        return
    if not await is_admin(update, update.effective_user.id):
        await update.message.reply_text("❌ Только администраторы группы.")
        return

    try:
        result = await scheduler.announce_now(context, update.effective_chat.id)
    except Exception as e:
        log.exception("Не удалось опубликовать анонс")
        await update.message.reply_text(f"❌ Не получилось опубликовать анонс: {e}")
        return
    await update.message.reply_text(result)


# ==================== /export ====================

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пришёл на смену /refresh: пересобирает витрину в Google Sheets.

    Раньше пересчёт читал весь лист Games и агрегировал в Python. Теперь
    агрегаты считает SQLite, а в Sheets уходит готовая таблица.
    """
    if not await is_admin(update, update.effective_user.id):
        await update.message.reply_text("❌ Только администраторы группы.")
        return
    if not SHEETS_ENABLED:
        await update.message.reply_text("❌ Выгрузка в Google Sheets не настроена.")
        return

    chat_id = update.effective_chat.id
    await update.message.reply_text("🔄 Пересобираю таблицы...")
    try:
        pending = await engine.to_db(sheets.export_pending)
        await engine.to_db(sheets.rebuild_chat, chat_id)
    except Exception as e:
        log.exception("Ошибка выгрузки")
        await update.message.reply_text(f"❌ Ошибка выгрузки: {e}")
        return

    await update.message.reply_text(
        f"✅ Готово. Догружено игр: {pending}. Листы Players / Rating / Ranking обновлены."
    )
