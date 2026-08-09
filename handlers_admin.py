"""Команды организатора и управление расписанием."""

import logging
import re
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

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


def _args(message_text: str, command: str) -> str:
    return message_text[len(command) + 1:].strip()


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
    lines += [scheduler.describe(r) for r in rows]
    lines.append("\n`/schedule add ...` — добавить, `/schedule del N` — удалить")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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
