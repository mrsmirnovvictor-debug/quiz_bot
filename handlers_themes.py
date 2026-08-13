"""Учёт заказанных тем. Работает ТОЛЬКО в личке — группа не засоряется.

Игрок:  /themes           свои победы, квота и список заказов
Админ:  /themes all       сводка по группе
        /order            записать заказ
        /order_del        удалить заказ
        /season           границы сезона

Сами темы игроки заказывают у организатора в личке. Бот только считает.
"""

import logging
import re
from datetime import date

import db
import engine
import themes
from config import THEME_LIMIT, THEMES_CHATS
from telegram import Update
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)


def username_of(user) -> str:
    return f"@{user.username}" if user.username else f"id{user.id}"


def _args(text: str, command: str) -> str:
    return text[len(command) + 1:].strip()


async def _admin_chat(context, user_id: int) -> int | None:
    """Первая группа, где человек админ. В личке прав проверить негде,
    поэтому опрашиваем группы с включённым учётом тем."""
    for chat_id in sorted(THEMES_CHATS):
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
            if member.status in ("creator", "administrator"):
                return chat_id
        except Exception:
            continue
    return None


async def _chat_title(context, chat_id: int) -> str:
    try:
        chat = await context.bot.get_chat(chat_id)
        return chat.title or str(chat_id)
    except Exception:
        return str(chat_id)


def _private_only(update: Update) -> bool:
    return update.effective_chat.type == "private"


# ==================== /themes ====================

async def themes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _private_only(update):
        await update.message.reply_text(
            "📩 Напишите мне /themes в личку — покажу победы и заказы."
        )
        return

    user = update.effective_user
    username = username_of(user)
    admin_chat = await _admin_chat(context, user.id)

    if admin_chat and _args(update.message.text, "/themes").lower() in ("all", "все"):
        await _overview(update, context, admin_chat)
        return

    lines = ["🎯 Заказ тем\n"]
    found = False
    can_order = False

    for chat_id in sorted(THEMES_CHATS):
        season = await engine.to_db(themes.ensure_season, chat_id)
        quota = await engine.to_db(themes.quota_for, chat_id, season, username)
        orders = await engine.to_db(db.theme_orders, chat_id, season["id"], username)
        if quota.wins == 0 and not orders:
            continue

        found = True
        can_order = can_order or quota.left > 0
        title = await _chat_title(context, chat_id)
        lines.append(f"{title} · сезон {season['name']}")
        lines.append(f"  побед: {quota.wins} · заказано: {quota.used} · "
                     f"осталось: {quota.left}")
        if quota.capped:
            lines.append(f"  лимит сезона — {THEME_LIMIT} тем")
        for order in orders:
            lines.append(f"    • {order['theme']}")
        lines.append("")

    if not found:
        lines.append("У вас пока нет побед в квизах.")
        lines.append("Победитель получает право заказать тему следующего квиза.")
    elif can_order:
        lines.append("Чтобы заказать тему, напишите организатору.")

    if admin_chat:
        lines.append("")
        lines.append("Админ: /themes all · /order · /order_del · /season")

    await update.message.reply_text("\n".join(lines))


async def _overview(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    season = await engine.to_db(themes.ensure_season, chat_id)
    rows = await engine.to_db(themes.season_overview, chat_id, season)
    title = await _chat_title(context, chat_id)

    lines = [f"🎯 {title} · сезон {season['name']}",
             f"{season['starts_on']} — {season['ends_on']}", ""]

    available = [r for r in rows if r["left"] > 0]
    done = [r for r in rows if r["left"] == 0 and r["used"]]

    if available:
        lines.append("Могут заказать:")
        for r in available:
            lines.append(f"  {themes.plain(r['username'])} — {r['left']} "
                         f"(побед {r['wins']}, заказано {r['used']})")
    if done:
        lines.append("")
        lines.append("Квота исчерпана:")
        for r in done:
            lines.append(f"  {themes.plain(r['username'])} — {r['used']}")

    orders = await engine.to_db(db.theme_orders, chat_id, season["id"])
    if orders:
        lines.append("")
        lines.append(f"Заказы сезона ({len(orders)}):")
        for order in orders:
            lines.append(f"  #{order['id']} {themes.plain(order['username'])} — "
                         f"{order['theme'][:50]}")

    await update.message.reply_text("\n".join(lines))


# ==================== /order ====================

async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/order @ник | Тема"""
    if not _private_only(update):
        await update.message.reply_text("📩 Эта команда работает в личке.")
        return

    chat_id = await _admin_chat(context, update.effective_user.id)
    if not chat_id:
        await update.message.reply_text("❌ Команда доступна администраторам групп.")
        return

    parts = re.split(r"\s*\|\s*", _args(update.message.text, "/order"), maxsplit=1)
    if len(parts) != 2 or not parts[0].strip():
        await update.message.reply_text(
            "Формат: /order @ник | Название темы\n\n"
            "Отмечает, что игрок использовал один заказ."
        )
        return

    username = parts[0].strip()
    if not username.startswith(("@", "id")):
        username = "@" + username

    season = await engine.to_db(themes.ensure_season, chat_id)
    before = await engine.to_db(themes.quota_for, chat_id, season, username)

    warning = ""
    if before.wins == 0:
        warning = "\n⚠️ У игрока нет побед в этом сезоне."
    elif before.left <= 0:
        warning = f"\n⚠️ Квота уже была исчерпана ({before.used} из {before.limit})."

    try:
        order_id = await engine.to_db(
            themes.add_order, chat_id, season, username, parts[1],
            update.effective_user.id,
        )
    except themes.ThemeError as e:
        await update.message.reply_text(f"❌ {e}")
        return

    after = await engine.to_db(themes.quota_for, chat_id, season, username)
    await update.message.reply_text(
        f"✅ #{order_id} · {username}\n«{parts[1].strip()}»\n\n"
        f"побед {after.wins} · заказано {after.used} · осталось {after.left}{warning}"
    )


async def order_del_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _private_only(update):
        await update.message.reply_text("📩 Эта команда работает в личке.")
        return

    chat_id = await _admin_chat(context, update.effective_user.id)
    if not chat_id:
        await update.message.reply_text("❌ Команда доступна администраторам групп.")
        return

    raw = _args(update.message.text, "/order_del").lstrip("#")
    try:
        order_id = int(raw)
    except ValueError:
        await update.message.reply_text(
            "Формат: /order_del <номер>\nНомера смотрите в /themes all"
        )
        return

    order = await engine.to_db(db.get_theme_order, order_id, chat_id)
    if not order:
        await update.message.reply_text("❌ Заказ не найден.")
        return

    await engine.to_db(db.update_theme_order, order_id, status="cancelled")
    season = await engine.to_db(themes.ensure_season, chat_id)
    quota = await engine.to_db(themes.quota_for, chat_id, season, order["username"])
    await update.message.reply_text(
        f"🗑 #{order_id} удалён: «{order['theme'][:50]}»\n"
        f"У {order['username']} снова доступно: {quota.left}"
    )


# ==================== /season ====================

async def season_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _private_only(update):
        await update.message.reply_text("📩 Эта команда работает в личке.")
        return

    chat_id = await _admin_chat(context, update.effective_user.id)
    if not chat_id:
        await update.message.reply_text("❌ Команда доступна администраторам групп.")
        return

    raw = _args(update.message.text, "/season")
    season = await engine.to_db(themes.ensure_season, chat_id)

    if not raw:
        title = await _chat_title(context, chat_id)
        await update.message.reply_text(
            f"📅 {title}\n"
            f"Сезон {season['name']}: {season['starts_on']} — {season['ends_on']}\n\n"
            f"Новый: /season set 2026-10-01 | 2026-12-31 | Q4 2026"
        )
        return

    action, _, rest = raw.partition(" ")
    if action.lower() != "set":
        await update.message.reply_text(
            "Формат: /season set 2026-10-01 | 2026-12-31 | Q4 2026")
        return

    parts = re.split(r"\s*\|\s*", rest)
    if len(parts) < 2:
        await update.message.reply_text(
            "Формат: /season set 2026-10-01 | 2026-12-31 | Q4 2026")
        return

    try:
        starts = date.fromisoformat(parts[0].strip())
        ends = date.fromisoformat(parts[1].strip())
    except ValueError:
        await update.message.reply_text("❌ Даты в формате ГГГГ-ММ-ДД.")
        return
    if ends <= starts:
        await update.message.reply_text("❌ Конец сезона должен быть позже начала.")
        return

    name = parts[2].strip() if len(parts) > 2 else str(starts)
    await engine.to_db(db.create_season, chat_id, name,
                       starts.isoformat(), ends.isoformat())
    await update.message.reply_text(
        f"✅ Сезон «{name}»: {starts} — {ends}\nКвоты отсчитываются заново."
    )
