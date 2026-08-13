"""Точка входа.

Запуск на Railway: одна реплика, том смонтирован на /data, Serverless выключен.
"""

import logging
import sys

from telegram import Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

import db
import engine
import handlers_admin as admin
import handlers_stats as stats
import handlers_themes as themes_h
import packs
import scheduler
import sheets
from config import BOT_TOKEN, LOG_LEVEL, SHEETS_ENABLED

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раньше исключения глушились голыми except и терялись."""
    log.error("Необработанная ошибка при обработке %s", update, exc_info=context.error)


async def on_startup(application: Application) -> None:
    problems = packs.validate_all()
    for p in problems:
        log.warning("Пакет не прошёл валидацию: %s", p)

    context = ContextTypes.DEFAULT_TYPE(application=application)
    await engine.recover(context)

    if SHEETS_ENABLED:
        try:
            count = await engine.to_db(sheets.export_pending)
            if count:
                log.info("Догружено в Sheets игр: %s", count)
        except Exception:
            log.exception("Догрузка в Sheets не удалась")


def main() -> None:
    if not BOT_TOKEN:
        sys.exit("❌ Не задан BOT_TOKEN")

    db.init()

    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", stats.help_command))
    app.add_handler(CommandHandler("help", stats.help_command))

    app.add_handler(CommandHandler("quiz", admin.quiz_command))
    app.add_handler(CommandHandler("pause", admin.pause_quiz))
    app.add_handler(CommandHandler("resume", admin.resume_quiz))
    app.add_handler(CommandHandler("abort", admin.abort_quiz))
    app.add_handler(CommandHandler("schedule", admin.schedule_command))
    app.add_handler(CommandHandler("skip", admin.skip_command))
    app.add_handler(CommandHandler("rename", admin.rename_command))
    app.add_handler(CommandHandler("export", admin.export_command))
    app.add_handler(CommandHandler("refresh", admin.export_command))   # старое имя

    app.add_handler(CommandHandler("themes", themes_h.themes_command))
    app.add_handler(CommandHandler("order", themes_h.order_command))
    app.add_handler(CommandHandler("order_del", themes_h.order_del_command))
    app.add_handler(CommandHandler("season", themes_h.season_command))

    app.add_handler(CommandHandler("stats", stats.stats_command))
    app.add_handler(CommandHandler("rating", stats.rating_command))
    app.add_handler(CommandHandler("rank", stats.rank_command))
    app.add_handler(CommandHandler("history", stats.history_command))
    app.add_handler(CommandHandler("games", stats.games_command))

    app.add_handler(CallbackQueryHandler(engine.register_callback, pattern=r"^register$"))
    app.add_handler(CallbackQueryHandler(engine.start_early_callback, pattern=r"^start_early$"))
    app.add_handler(CallbackQueryHandler(engine.answer_callback, pattern=r"^ans:\d+:\d+$"))

    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, engine.purge_handler), group=1
    )
    app.add_error_handler(on_error)

    scheduler.install(app)

    log.info("🚀 Бот запущен")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
