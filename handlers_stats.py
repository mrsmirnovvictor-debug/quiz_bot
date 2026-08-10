"""Статистика. Все агрегаты берутся из SQLite одним запросом."""

import logging
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

import db
import engine
import packs
import texts
from config import MSK, calibration_for

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4000


async def _reply_long(update: Update, text: str, **kwargs):
    if len(text) <= TELEGRAM_LIMIT:
        await update.message.reply_text(text, **kwargs)
        return
    for i in range(0, len(text), TELEGRAM_LIMIT):
        await update.message.reply_text(text[i:i + TELEGRAM_LIMIT], **kwargs)


def username_of(user) -> str:
    return f"@{user.username}" if user.username else f"id{user.id}"


# ==================== /stats ====================

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if update.effective_chat.type == "private":
        await update.message.reply_text("📊 Команда /stats работает в группах.")
        return

    min_games = calibration_for(chat_id)
    rows = await engine.to_db(db.player_stats, chat_id, min_games)
    if not rows:
        await update.message.reply_text(
            f"❌ Пока нет игроков, сыгравших {min_games}+ квизов."
        )
        return

    header = "🏆 ТОП ИГРОКОВ (по ELO)" if min_games <= 1 else \
             f"🏆 ТОП ИГРОКОВ (от {min_games} игр, по ELO)"
    lines = [header, ""]

    for place, row in enumerate(rows[:20], 1):
        answered = (row["total_correct"] or 0) + (row["total_incorrect"] or 0)
        avg_time = (row["total_time"] or 0) / answered if answered else 0
        percent = (row["total_correct"] / row["total_questions"] * 100
                   if row["total_questions"] else 0)
        lines.append(texts.stats_entry(
            place, row["username"], row["games_count"],
            round(row["total_score"] or 0), percent, avg_time,
            int(round(row["avg_elo"] or 0)),
        ))
    await _reply_long(update, "\n".join(lines))


# ==================== /rating ====================

async def rating_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if update.effective_chat.type == "private":
        await update.message.reply_text("📊 Команда /rating работает в группах.")
        return

    rows = await engine.to_db(db.rating_table, chat_id)
    if not rows:
        await update.message.reply_text("❌ Рейтинг пока пуст — сыграйте первую игру.")
        return

    table = texts.rating_table(
        [(r["username"], r["games_count"], r["total_points"] or 0) for r in rows]
    )
    await _reply_long(update, table, parse_mode="Markdown")


# ==================== /rank ====================

async def rank_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Динамика ELO. Считается на лету — лист Ranking больше не нужен как источник."""
    chat_id = update.effective_chat.id
    if update.effective_chat.type == "private":
        await update.message.reply_text("📊 Команда /rank работает в группах.")
        return

    dates = await engine.to_db(db.game_dates, chat_id)
    if not dates:
        await update.message.reply_text("❌ В этой группе пока нет сыгранных квизов.")
        return

    last = await engine.to_db(db.elo_snapshot, chat_id, dates[-1] + "T23:59:59+00:00")
    prev = ({} if len(dates) < 2
            else await engine.to_db(db.elo_snapshot, chat_id, dates[-2] + "T23:59:59+00:00"))

    min_games = calibration_for(chat_id)
    current = sorted(((u, g, e) for u, (g, e) in last.items() if g >= min_games),
                     key=lambda x: -x[2])
    if not current:
        await update.message.reply_text(
            f"❌ Нет игроков с {min_games}+ играми."
        )
        return

    places_now = {u: i for i, (u, _, _) in enumerate(current, 1)}
    previous = sorted(((u, g, e) for u, (g, e) in prev.items()
                       if g >= min_games and u in places_now), key=lambda x: -x[2])
    places_prev = {u: i for i, (u, _, _) in enumerate(previous, 1)}

    lines = ["📊 ДИНАМИКА РЕЙТИНГА", ""]
    for username, games, elo in current:
        place = places_now[username]
        if username in places_prev:
            delta_place = places_prev[username] - place
            delta_elo = elo - prev[username][1]
        else:
            delta_place = delta_elo = None
        lines.append(texts.rank_entry(place, username, games, elo,
                                      delta_place, delta_elo))
    await _reply_long(update, "\n".join(lines))


# ==================== /history ====================

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "📩 История доступна в личных сообщениях с ботом. Напишите мне /history в личку."
        )
        return

    username = username_of(update.effective_user)
    rows = await engine.to_db(db.user_history, username, 10)
    if not rows:
        await update.message.reply_text(f"❌ {username}, у вас пока нет сыгранных квизов.")
        return

    total = await engine.to_db(db.user_history_count, username)
    parts = [f"📜 ИСТОРИЯ ИГРОКА {username}\n"]
    for i, r in enumerate(rows, 1):
        played = datetime.fromisoformat(r["played_at"]).astimezone(MSK)
        # Здесь был баг: процент делился на 100 и превращался в «0.8%».
        percent = r["correct"] / r["question_count"] * 100 if r["question_count"] else 0
        parts.append(
            f"{i}. {r['title']}\n"
            f"   📅 {played:%d.%m.%Y %H:%M}\n"
            f"   🏆 Место: {r['place']}\n"
            f"   ⭐ Очки: {r['score']}\n"
            f"   ⏱ Среднее время: {r['avg_time']:.1f} сек\n"
            f"   ✅ Правильных: {percent:.1f}%\n"
            f"   🎯 ELO: {r['elo']}\n"
        )
    if total > len(rows):
        parts.append(f"…и ещё {total - len(rows)} игр.")
    await _reply_long(update, "\n".join(parts))


# ==================== /games ====================

async def games_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "📩 Список квизов отправляю только в личку. Напишите мне /games."
        )
        return

    titles = await engine.to_db(packs.pack_titles)
    if not titles:
        await update.message.reply_text("❌ Нет доступных квизов.")
        return

    lines = ["📚 Список доступных квизов:\n"]
    for pack_id, title in titles.items():
        short = title if len(title) <= 50 else title[:47] + "..."
        lines.append(f"`{pack_id}` — {short}")
    await _reply_long(update, "\n".join(lines), parse_mode="Markdown")


# ==================== /help ====================

HELP = (
    "🤖 Квиз-бот\n\n"
    "*В группе:*\n"
    "`/quiz 0007 | 2026-05-15 | 20:00` — назначить квиз\n"
    "`/schedule` — автозапуск по расписанию\n"
    "`/skip` — пропустить сегодняшний автозапуск\n"
    "`/pause` `/resume` `/abort` — управление ходом\n"
    "`/stats` `/rating` `/rank` — статистика\n"
    "`/rename @старый @новый` — игрок сменил ник\n"
    "`/export` — обновить Google-таблицы\n\n"
    "*В личке:*\n"
    "`/games` — список пакетов\n"
    "`/history` — ваши последние игры"
)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="Markdown")
