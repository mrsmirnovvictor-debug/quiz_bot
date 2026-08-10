"""Команды заказа тем победителями.

Игрок:  /themes  /slots  /book
Админ:  /themes all, /theme_add, /theme_pack, /theme_del, /season
"""

import logging
import re
from datetime import date, datetime, timezone

import db
import engine
import packs
import themes
from config import MSK, THEME_LIMIT, themes_enabled
from telegram import Update
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)

STATUS_LABELS = {
    "booked": "ждёт игры",
    "played": "отыграна",
    "legacy": "отыграна ранее",
}


def username_of(user) -> str:
    return f"@{user.username}" if user.username else f"id{user.id}"


def _args(text: str, command: str) -> str:
    return text[len(command) + 1:].strip()


async def _guard(update: Update) -> tuple[int, object] | None:
    """Проверяет, что фича включена для этой группы. Возвращает (chat_id, сезон)."""
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("🎯 Заказ тем работает в группе с квизами.")
        return None
    if not themes_enabled(chat.id):
        await update.message.reply_text("🎯 В этой группе заказ тем не включён.")
        return None
    season = await engine.to_db(themes.ensure_season, chat.id)
    return chat.id, season


async def _is_admin(update: Update) -> bool:
    try:
        member = await update.effective_chat.get_member(update.effective_user.id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False


# ==================== /themes ====================

async def themes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    guard = await _guard(update)
    if not guard:
        return
    chat_id, season = guard

    if _args(update.message.text, "/themes").lower() in ("all", "все", "всё"):
        await _themes_overview(update, chat_id, season)
        return

    username = username_of(update.effective_user)
    quota = await engine.to_db(themes.quota_for, chat_id, season, username)
    orders = await engine.to_db(db.theme_orders, chat_id, season["id"], username)

    lines = [f"🎯 Заказ тем · сезон {season['name']}", ""]
    if quota.wins == 0:
        lines.append("У вас пока нет побед в этом сезоне.")
        lines.append("Победитель квиза получает право заказать одну тему.")
    else:
        lines.append(f"Побед: {quota.wins}")
        if quota.capped:
            lines.append(f"Лимит сезона: {THEME_LIMIT} (побед больше, чем лимит)")
        lines.append(f"Заказано: {quota.used} · Осталось: {quota.left}")

    if orders:
        lines.append("")
        lines.append("Ваши темы:")
        for order in orders:
            lines.append("  " + _order_line(order))

    if quota.left > 0:
        lines.append("")
        lines.append("Свободные слоты — /slots, заказ — /book")
    await update.message.reply_text("\n".join(lines))


def _order_line(order) -> str:
    label = STATUS_LABELS.get(order["status"], order["status"])
    if order["slot_utc"]:
        when = themes.format_slot(datetime.fromisoformat(order["slot_utc"]))
        head = f"#{order['id']} {when} — {order['theme']}"
    else:
        head = f"#{order['id']} {order['theme']}"
    pack = f" [пакет {order['pack_id']}]" if order["pack_id"] else ""
    return f"{head} ({label}){pack}"


async def _themes_overview(update: Update, chat_id: int, season):
    rows = await engine.to_db(themes.season_overview, chat_id, season)
    orders = await engine.to_db(db.theme_orders, chat_id, season["id"])

    lines = [f"🎯 Заказы тем · сезон {season['name']} "
             f"({season['starts_on']} — {season['ends_on']})", ""]

    if not rows:
        lines.append("Побед в сезоне пока нет.")
    else:
        lines.append("Квоты победителей:")
        for r in rows:
            tail = " ✅ всё заказано" if r["left"] == 0 and r["used"] else ""
            lines.append(
                f"  {themes.plain(r['username'])}: побед {r['wins']}, "
                f"заказано {r['used']}, осталось {r['left']}{tail}"
            )

    booked = [o for o in orders if o["status"] == "booked"]
    if booked:
        lines.append("")
        lines.append("Забронированные слоты:")
        for order in booked:
            mark = "" if order["pack_id"] else "  ⚠️ пакет не привязан"
            lines.append(f"  {_order_line(order)}{mark}")

    history = [o for o in orders if o["status"] in ("played", "legacy")]
    if history:
        lines.append("")
        lines.append(f"Отыграно ранее: {len(history)}")
        for order in history[-10:]:
            lines.append(f"  {_order_line(order)}")

    await update.message.reply_text("\n".join(lines))


# ==================== /slots ====================

async def slots_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    guard = await _guard(update)
    if not guard:
        return
    chat_id, season = guard

    now = datetime.now(timezone.utc)
    free = await engine.to_db(themes.available_slots, chat_id, season, now, 20)
    if not free:
        await update.message.reply_text(
            "Свободных слотов до конца сезона не осталось."
        )
        return

    lines = ["🗓 Свободные слоты (время московское)", ""]
    current_day = None
    for number, slot in free:
        msk = slot.astimezone(MSK)
        day = msk.date()
        if day != current_day:
            current_day = day
            weekday = themes.WEEKDAY_NAMES[msk.weekday()]
            lines.append(f"{weekday} {msk:%d.%m}")
        lines.append(f"   {number} — {msk:%H:%M}")

    lines.append("")
    lines.append("Заказ: /book <номер> | Тема")
    lines.append("Например: /book " + str(free[0][0]) + " | Русский рок 90-х")
    await update.message.reply_text("\n".join(lines))


# ==================== /book ====================

async def book_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    guard = await _guard(update)
    if not guard:
        return
    chat_id, season = guard

    raw = _args(update.message.text, "/book")
    parts = re.split(r"\s*\|\s*", raw, maxsplit=1)
    if len(parts) != 2:
        await update.message.reply_text(
            "Формат: /book <номер слота> | Тема\n"
            "Например: /book 53 | Русский рок 90-х\n"
            "Свободные слоты — /slots"
        )
        return

    username = username_of(update.effective_user)
    now = datetime.now(timezone.utc)
    try:
        order_id, slot = await engine.to_db(
            themes.book, chat_id, season, username, parts[0], parts[1], now,
            update.effective_user.id,
        )
    except themes.ThemeError as e:
        await update.message.reply_text(f"❌ {e}")
        return
    except Exception:
        log.exception("Ошибка бронирования темы")
        await update.message.reply_text(
            "❌ Не удалось забронировать — возможно, слот только что заняли. "
            "Посмотрите /slots и попробуйте другой."
        )
        return

    quota = await engine.to_db(themes.quota_for, chat_id, season, username)
    await update.message.reply_text(
        f"✅ Тема заказана!\n\n"
        f"#{order_id} · {themes.format_slot(slot)}\n"
        f"«{parts[1].strip()}»\n\n"
        f"Осталось заказов в сезоне: {quota.left}"
    )


# ==================== Админские команды ====================

async def theme_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Внести заказ задним числом: /theme_add @ник | Тема"""
    guard = await _guard(update)
    if not guard:
        return
    chat_id, season = guard
    if not await _is_admin(update):
        await update.message.reply_text("❌ Только администраторы группы.")
        return

    parts = re.split(r"\s*\|\s*", _args(update.message.text, "/theme_add"), maxsplit=1)
    if len(parts) != 2 or not parts[0].strip():
        await update.message.reply_text(
            "Формат: /theme_add @ник | Название темы\n"
            "Так отмечаются заказы, которые уже отыграны."
        )
        return

    username = parts[0].strip()
    if not username.startswith(("@", "id")):
        username = "@" + username

    order_id = await engine.to_db(
        db.add_theme_order, chat_id, season["id"], username, parts[1].strip(),
        None, "legacy", update.effective_user.id, "внесено администратором",
    )
    quota = await engine.to_db(themes.quota_for, chat_id, season, username)
    await update.message.reply_text(
        f"✅ Записан заказ #{order_id} для {username}: «{parts[1].strip()}»\n"
        f"Побед: {quota.wins} · заказано: {quota.used} · осталось: {quota.left}"
    )


async def theme_pack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Привязать пакет к заказу: /theme_pack 12 0150"""
    guard = await _guard(update)
    if not guard:
        return
    chat_id, _ = guard
    if not await _is_admin(update):
        await update.message.reply_text("❌ Только администраторы группы.")
        return

    parts = _args(update.message.text, "/theme_pack").split()
    if len(parts) != 2:
        await update.message.reply_text("Формат: /theme_pack <id заказа> <id пакета>")
        return

    try:
        order_id = int(parts[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("❌ Номер заказа — число.")
        return

    try:
        pack = await engine.to_db(packs.load_pack, parts[1])
    except packs.PackError as e:
        await update.message.reply_text(f"❌ {e}")
        return

    order = await engine.to_db(db.get_theme_order, order_id, chat_id)
    if not order:
        await update.message.reply_text("❌ Заказ не найден.")
        return
    if not order["slot_utc"]:
        await update.message.reply_text(
            "❌ У этого заказа нет слота — привязывать пакет не к чему."
        )
        return

    await engine.to_db(db.update_theme_order, order_id, pack_id=pack.pack_id)
    slot = datetime.fromisoformat(order["slot_utc"])
    await update.message.reply_text(
        f"✅ Заказ #{order_id} ({order['username']}) → пакет {pack.pack_id}\n"
        f"«{pack.title}»\n"
        f"Запустится автоматически: {themes.format_slot(slot)}"
    )


async def theme_del_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    guard = await _guard(update)
    if not guard:
        return
    chat_id, season = guard
    if not await _is_admin(update):
        await update.message.reply_text("❌ Только администраторы группы.")
        return

    raw = _args(update.message.text, "/theme_del").lstrip("#")
    try:
        order_id = int(raw)
    except ValueError:
        await update.message.reply_text("Формат: /theme_del <id заказа>")
        return

    order = await engine.to_db(db.get_theme_order, order_id, chat_id)
    if not order:
        await update.message.reply_text("❌ Заказ не найден.")
        return

    await engine.to_db(db.update_theme_order, order_id, status="cancelled")
    quota = await engine.to_db(themes.quota_for, chat_id, season, order["username"])
    await update.message.reply_text(
        f"🗑 Заказ #{order_id} отменён, слот освобождён.\n"
        f"У {order['username']} снова доступно заказов: {quota.left}"
    )


async def season_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    guard = await _guard(update)
    if not guard:
        return
    chat_id, season = guard

    raw = _args(update.message.text, "/season")
    if not raw:
        grid = await engine.to_db(themes.season_grid, season)
        busy = await engine.to_db(db.busy_slots, chat_id, season["id"])
        await update.message.reply_text(
            f"📅 Сезон {season['name']}\n"
            f"{season['starts_on']} — {season['ends_on']}\n"
            f"Слотов в сетке: {len(grid)}, занято: {len(busy)}\n\n"
            f"Новый сезон: /season set ГГГГ-ММ-ДД | ГГГГ-ММ-ДД | Название"
        )
        return

    if not await _is_admin(update):
        await update.message.reply_text("❌ Только администраторы группы.")
        return

    action, _, rest = raw.partition(" ")
    if action.lower() != "set":
        await update.message.reply_text(
            "Формат: /season set 2026-10-01 | 2026-12-31 | Q4 2026"
        )
        return

    parts = re.split(r"\s*\|\s*", rest)
    if len(parts) < 2:
        await update.message.reply_text(
            "Формат: /season set 2026-10-01 | 2026-12-31 | Q4 2026"
        )
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

    name = parts[2].strip() if len(parts) > 2 else f"{starts:%Y-%m-%d}"
    await engine.to_db(db.create_season, chat_id, name,
                       starts.isoformat(), ends.isoformat())
    await update.message.reply_text(
        f"✅ Новый сезон «{name}»: {starts} — {ends}\n"
        f"Квоты и заказы отсчитываются заново. Прошлый сезон закрыт."
    )
