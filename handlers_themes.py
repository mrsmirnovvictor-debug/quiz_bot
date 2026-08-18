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
from config import THEME_LIMIT, THEMES_CHATS, themes_enabled
from telegram import Update
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)


def username_of(user) -> str:
    return f"@{user.username}" if user.username else f"id{user.id}"


def _args(text: str, command: str) -> str:
    return text[len(command) + 1:].strip()


async def _admin_chats(context, user_id: int) -> list[int]:
    """Все группы с учётом тем, где человек админ."""
    out = []
    for chat_id in THEMES_CHATS:
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
            if member.status in ("creator", "administrator"):
                out.append(chat_id)
        except Exception:
            continue
    return out


async def _admin_chat(context, user_id: int) -> int | None:
    """Одна группа для админских команд в личке.

    Раньше бралась первая по сортировке, из-за чего сводка всегда
    показывала тестовую группу вместо основной. Теперь предпочитаем
    ту, где больше сыгранных игр — почти всегда это и есть боевая.
    """
    chats = await _admin_chats(context, user_id)
    if not chats:
        return None
    if len(chats) == 1:
        return chats[0]
    counts = await engine.to_db(_games_per_chat)
    return max(chats, key=lambda c: counts.get(c, 0))


def _games_per_chat() -> dict[int, int]:
    return {r["chat_id"]: r["n"] for r in db._rows(
        "SELECT chat_id, COUNT(DISTINCT game_id) n FROM results GROUP BY chat_id")}


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
    user = update.effective_user
    username = username_of(user)
    wants_all = _args(update.message.text, "/themes").lower() in ("all", "все")

    # В группе сводка берётся по этой же группе — гадать не нужно.
    if not _private_only(update):
        chat_id = update.effective_chat.id
        if not themes_enabled(chat_id):
            await update.message.reply_text("🎯 В этой группе учёт тем не включён.")
            return
        if wants_all:
            await _overview(update, context, chat_id, compact=True)
        else:
            await _personal_in_group(update, context, chat_id, username)
        return

    admin_chat = await _admin_chat(context, user.id)
    if admin_chat and wants_all:
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


async def _personal_in_group(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             chat_id: int, username: str):
    """Короткий ответ игроку прямо в группе — три строки, не больше."""
    season = await engine.to_db(themes.ensure_season, chat_id)
    quota = await engine.to_db(themes.quota_for, chat_id, season, username)

    if quota.wins == 0:
        await update.message.reply_text(
            f"{themes.plain(username)}: побед в сезоне пока нет. "
            f"Победитель квиза получает право заказать тему."
        )
        return

    tail = " — можно заказывать" if quota.left else " — квота исчерпана"
    await update.message.reply_text(
        f"🎯 {themes.plain(username)} · сезон {season['name']}\n"
        f"побед {quota.wins} · заказано {quota.used} · осталось {quota.left}{tail}"
    )


async def _overview(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    chat_id: int, compact: bool = False):
    season = await engine.to_db(themes.ensure_season, chat_id)
    rows = await engine.to_db(themes.season_overview, chat_id, season)
    orders = await engine.to_db(db.theme_orders, chat_id, season["id"])

    available = [r for r in rows if r["left"] > 0]
    done = [r for r in rows if r["left"] == 0 and r["used"]]

    if compact:
        # В группе — только суть: кто ещё может заказать и что заказано недавно.
        lines = [f"🎯 Заказ тем · сезон {season['name']}", ""]
        if available:
            lines.append("Доступно заказов:")
            for r in available:
                lines.append(f"  {themes.plain(r['username'])} — {r['left']}")
        else:
            lines.append("Свободных заказов нет.")
        if orders:
            lines.append("")
            recent = orders[-5:]
            head = "Последние темы:" if len(orders) > 5 else "Заказанные темы:"
            lines.append(head)
            for order in recent:
                lines.append(f"  {themes.plain(order['username'])} — "
                             f"{order['theme'][:40]}")
            if len(orders) > 5:
                lines.append(f"  …всего {len(orders)}, полный список — /themes all в личке")
        await update.message.reply_text("\n".join(lines))
        return

    title = await _chat_title(context, chat_id)
    lines = [f"🎯 {title} · сезон {season['name']}",
             f"{season['starts_on']} — {season['ends_on']}", ""]

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
