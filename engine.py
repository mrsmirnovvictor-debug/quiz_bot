"""Оркестрация квиза поверх Telegram.

Живые игры лежат в памяти (быстро), но каждое значимое изменение сразу
пишется в SQLite, поэтому редеплой на Railway не уничтожает результаты.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db
import sheets
import texts
from config import (AUDIO, MSK, RULES, SHEETS_ENABLED, TIMER_VIDEO_URL,
                    TIMINGS)

# Насколько бот готов опоздать со стартом после возвращения из даунтайма.
RECOVERY_GRACE = timedelta(minutes=15)
# Задержка перед стартом восстановленного квиза: людям нужно вернуться в чат.
RECOVERY_DELAY = 60
from game import Answer, Game, build_result_rows
from packs import Pack, load_pack

log = logging.getLogger(__name__)

# chat_id -> Game
LIVE: dict[int, Game] = {}


async def to_db(fn, *args, **kwargs):
    """SQLite синхронный — уводим его с event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


# ==================== Отправка сообщений ====================

async def say(context: ContextTypes.DEFAULT_TYPE, game: Game, text: str, **kwargs):
    """Единый хелпер вместо 25 копий блока с message_thread_id."""
    kw = dict(chat_id=game.chat_id, text=text, **kwargs)
    if game.thread_id:
        kw["message_thread_id"] = game.thread_id
    try:
        return await context.bot.send_message(**kw)
    except TelegramError:
        log.exception("Не удалось отправить сообщение в чат %s", game.chat_id)
        return None


async def say_photo(context: ContextTypes.DEFAULT_TYPE, game: Game, photo: str,
                    caption: str, **kwargs):
    kw = dict(chat_id=game.chat_id, photo=photo, caption=caption, **kwargs)
    if game.thread_id:
        kw["message_thread_id"] = game.thread_id
    try:
        return await context.bot.send_photo(**kw)
    except TelegramError:
        log.warning("Фото не отправилось, шлём текстом", exc_info=True)
        return await say(context, game, caption, **kwargs)


async def say_audio(context: ContextTypes.DEFAULT_TYPE, game: Game, question,
                    caption: str, **kwargs):
    """Отправляет музыкальный вопрос.

    По умолчанию — голосовым сообщением: Telegram не собирает их в плейлист,
    поэтому короткий отрывок не тянет за собой соседние аудио из ленты.
    Заодно в голосовом не видно тегов файла, которые выдали бы ответ.
    """
    path = question.audio
    mode = question.audio_mode or AUDIO.mode
    is_voice = mode == "voice"

    kw = dict(chat_id=game.chat_id, caption=caption, **kwargs)
    if game.thread_id:
        kw["message_thread_id"] = game.thread_id
    if not is_voice:
        kw["performer"] = AUDIO.performer
        kw["title"] = AUDIO.title_template.format(n=game.current_question + 1)

    send = context.bot.send_voice if is_voice else context.bot.send_audio
    field = "voice" if is_voice else "audio"

    def file_id_of(msg):
        media = msg.voice if is_voice else msg.audio
        return media.file_id if media else None

    # Ссылку Telegram скачивает сам, кэшировать нечего.
    if path.startswith(("http://", "https://")):
        try:
            return await send(**{field: path}, **kw)
        except TelegramError:
            log.exception("Аудио по ссылке не отправилось: %s", path)
            return await say(context, game, caption, **kwargs)

    cache_key = f"{mode}:{path}"
    cached = await to_db(db.get_cached_file_id, cache_key)
    if cached:
        try:
            return await send(**{field: cached}, **kw)
        except TelegramError:
            log.warning("file_id протух, перезаливаем %s", path)
            await to_db(db.drop_cached_file_id, cache_key)

    try:
        with open(path, "rb") as f:
            msg = await send(**{field: f}, **kw)
    except (TelegramError, OSError):
        log.exception("Не удалось отправить аудио %s", path)
        return await say(context, game, caption, **kwargs)

    file_id = file_id_of(msg) if msg else None
    if file_id:
        await to_db(db.cache_file_id, cache_key, file_id)
    return msg


async def quiet_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                       message_id: int | None):
    if not message_id:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        pass


def _schedule(context, callback, when: float, chat_id: int, name: str):
    """Джоба с именем — чтобы её можно было прицельно снять."""
    for job in context.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    return context.job_queue.run_once(callback, when=when, chat_id=chat_id,
                                      data=chat_id, name=name)


def cancel_chat_jobs(context, chat_id: int):
    for job in context.job_queue.jobs():
        if job.chat_id == chat_id:
            job.schedule_removal()


def _reg_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Зарегистрироваться", callback_data="register")],
        [InlineKeyboardButton("🚀 Начать сейчас", callback_data="start_early")],
    ])


# ==================== Создание игры ====================

async def create_game(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                      thread_id: int | None, pack: Pack, creator_id: int | None,
                      start_utc: datetime, source: str = "manual") -> Game:
    game_id = await to_db(
        db.create_game, chat_id, thread_id, pack.pack_id, pack.title,
        len(pack), creator_id, start_utc, source,
    )
    game = Game(game_id=game_id, chat_id=chat_id, thread_id=thread_id, pack=pack,
                creator_id=creator_id, scheduled_start_utc=start_utc, source=source)
    LIVE[chat_id] = game

    await open_registration(context, game)

    delay = (start_utc - datetime.now(timezone.utc)).total_seconds()
    _schedule(context, job_start_sequence, max(delay, 1), chat_id, f"start:{chat_id}")
    return game


async def open_registration(context: ContextTypes.DEFAULT_TYPE, game: Game):
    text = texts.registration(game.pack.title, game.scheduled_start_utc, [])
    game.reg_text_changed(text)
    msg = await say(context, game, text, reply_markup=_reg_keyboard())
    if msg is None:
        return
    game.reg_msg_id = msg.message_id
    await to_db(db.update_game, game.game_id, reg_msg_id=msg.message_id)

    try:
        await context.bot.pin_chat_message(chat_id=game.chat_id,
                                           message_id=msg.message_id)
    except TelegramError:
        pass

    context.job_queue.run_repeating(
        job_refresh_registration, interval=TIMINGS.reg_refresh, first=TIMINGS.reg_refresh,
        chat_id=game.chat_id, data=game.chat_id, name=f"reg:{game.chat_id}",
    )


async def job_refresh_registration(context: ContextTypes.DEFAULT_TYPE):
    game = LIVE.get(context.job.data)
    if not game or game.status != "registration":
        context.job.schedule_removal()
        return
    await refresh_registration(context, game)


async def refresh_registration(context: ContextTypes.DEFAULT_TYPE, game: Game):
    text = texts.registration(game.pack.title, game.scheduled_start_utc, game.usernames())
    if not game.reg_text_changed(text):
        return                      # экономим запросы к Telegram
    try:
        await context.bot.edit_message_text(
            chat_id=game.chat_id, message_id=game.reg_msg_id,
            text=text, reply_markup=_reg_keyboard(),
        )
    except TelegramError:
        pass


# ==================== Колбэки регистрации ====================

async def register_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    game = LIVE.get(update.effective_chat.id)

    if not game or game.status != "registration":
        await query.answer("Регистрация закрыта.", show_alert=True)
        return
    if user.is_bot:
        await query.answer("Боты не участвуют.", show_alert=True)
        return

    username = f"@{user.username}" if user.username else f"id{user.id}"
    is_new = game.add_player(user.id, username)
    await query.answer("Вы в игре ✅" if is_new else "Вы уже зарегистрированы")
    if is_new:
        await to_db(db.add_participant, game.game_id, user.id, username)
        await refresh_registration(context, game)


async def start_early_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game = LIVE.get(update.effective_chat.id)

    if not game or game.status != "registration":
        await query.answer("Регистрация не активна.", show_alert=True)
        return
    if game.creator_id and update.effective_user.id != game.creator_id:
        await query.answer("Только организатор может начать досрочно.", show_alert=True)
        return

    await query.answer("Стартуем!")
    for job in context.job_queue.get_jobs_by_name(f"start:{game.chat_id}"):
        job.schedule_removal()
    await start_sequence(context, game, early=True)


# ==================== Старт квиза ====================

async def job_start_sequence(context: ContextTypes.DEFAULT_TYPE):
    game = LIVE.get(context.job.data)
    if game and game.status == "registration":
        await start_sequence(context, game, early=False)


async def start_sequence(context: ContextTypes.DEFAULT_TYPE, game: Game, early: bool):
    for job in context.job_queue.get_jobs_by_name(f"reg:{game.chat_id}"):
        job.schedule_removal()

    game.status = "active"
    game.purge_messages = True
    await to_db(db.update_game, game.game_id, status="active")

    try:
        await context.bot.edit_message_text(
            chat_id=game.chat_id, message_id=game.reg_msg_id,
            text=texts.registration_closed(game.pack.title, game.scheduled_start_utc,
                                           game.usernames()),
        )
    except TelegramError:
        pass

    if not game.players:
        await say(context, game, "❌ Никто не зарегистрировался, квиз отменён.")
        await to_db(db.update_game, game.game_id, status="aborted")
        LIVE.pop(game.chat_id, None)
        return

    if early:
        delay = TIMINGS.early_start_delay
    else:
        await say(context, game,
                  texts.pre_start_warning(game.usernames(), TIMINGS.pre_start_warning))
        delay = TIMINGS.pre_start_warning

    _schedule(context, job_start_question, delay, game.chat_id, f"q:{game.chat_id}")


# ==================== Ход вопросов ====================

async def job_start_question(context: ContextTypes.DEFAULT_TYPE):
    game = LIVE.get(context.job.data)
    if not game or game.status != "active":
        return
    if game.current_question >= game.total_questions:
        await finish_quiz(context, game)
        return
    await start_question(context, game)


async def start_question(context: ContextTypes.DEFAULT_TYPE, game: Game):
    text, options, image = game.prepare_question()
    idx = game.current_question

    # Номер вопроса внутри callback_data закрывает гонку на границе вопросов.
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(opt, callback_data=f"ans:{idx}:{i}")]
        for i, opt in enumerate(options)
    ])
    caption = texts.question(idx, game.total_questions, text)
    question = game.pack.questions[idx]

    # У аудиовопроса плеер сам служит таймером, видео только мешало бы.
    if question.is_audio:
        seconds = question.duration or AUDIO.question_seconds
    else:
        seconds = question.duration or TIMINGS.question

    if TIMER_VIDEO_URL and not question.is_audio:
        try:
            video = await context.bot.send_video(
                chat_id=game.chat_id, video=TIMER_VIDEO_URL, width=200, height=150,
                supports_streaming=True,
                **({"message_thread_id": game.thread_id} if game.thread_id else {}),
            )
            game.video_msg_id = video.message_id
        except TelegramError:
            log.warning("Видео-таймер не отправился", exc_info=True)
        await asyncio.sleep(0.5)

    if question.is_audio:
        msg = await say_audio(context, game, question, caption, reply_markup=keyboard)
    elif image:
        msg = await say_photo(context, game, image, caption, reply_markup=keyboard)
    else:
        msg = await say(context, game, caption, reply_markup=keyboard)

    if msg is None:
        # Не смогли задать вопрос — пропускаем его, а не вешаем квиз.
        log.error("Вопрос %s не отправлен, пропускаем", idx)
        game.current_question += 1
        _schedule(context, job_start_question, 2, game.chat_id, f"q:{game.chat_id}")
        return

    game.question_msg_id = msg.message_id
    # Момент старта отсчитываем от факта доставки сообщения, а не от вызова.
    game.question_started_at = datetime.now(timezone.utc)
    await to_db(db.update_game, game.game_id, current_question=idx)

    _schedule(context, job_end_question, seconds, game.chat_id,
              f"end:{game.chat_id}")


async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    game = LIVE.get(update.effective_chat.id)
    at = datetime.now(timezone.utc)

    if not game or game.status != "active":
        await query.answer("Квиз не активен")
        return

    try:
        _, q_idx_s, opt_s = query.data.split(":")
        q_idx, option_idx = int(q_idx_s), int(opt_s)
    except (ValueError, AttributeError):
        await query.answer("Ошибка")
        return

    if user.id not in game.players:
        # Раньше такой игрок получал «Ответ принят ✅» и не попадал в итоги.
        await query.answer("Вы не зарегистрированы на этот квиз 🙁", show_alert=True)
        return
    if q_idx != game.current_question:
        await query.answer("Вопрос уже закрыт")
        return

    answer, status = game.record_answer(user.id, q_idx, option_idx, at)

    if status == "same":
        await query.answer("Этот вариант уже выбран")
        return
    if status == "used_up":
        await query.answer("Поменять ответ можно только один раз 🙂", show_alert=True)
        return
    if answer is None or status == "rejected":
        await query.answer("Ответ не принят")
        return

    if status == "changed":
        await query.answer("Ответ изменён 🔁 Замена израсходована", show_alert=True)
    else:
        await query.answer("Ответ принят ✅ Поменять можно один раз")

    await to_db(db.save_answer, game.game_id, user.id, q_idx, option_idx,
                answer.text, answer.is_correct, answer.points, answer.elapsed,
                answer.changed)


async def job_end_question(context: ContextTypes.DEFAULT_TYPE):
    game = LIVE.get(context.job.data)
    if game and game.status == "active":
        await end_question(context, game)


async def end_question(context: ContextTypes.DEFAULT_TYPE, game: Game):
    idx = game.current_question
    question = game.pack.questions[idx]

    percents = game.answer_distribution()
    stats_lines = [
        f"{opt}: {perc:.1f}%{' ✅' if i == game.shuffled_correct else ''}"
        for i, (opt, perc) in enumerate(zip(game.shuffled_options, percents))
    ]
    result_text = texts.question_result(
        idx, game.total_questions, question.text, stats_lines,
        question.options[question.correct], question.comment,
    )

    await quiet_delete(context, game.chat_id, game.question_msg_id)
    await quiet_delete(context, game.chat_id, game.video_msg_id)
    game.question_msg_id = game.video_msg_id = None

    is_last = game.is_last_question
    board = None
    if not is_last and RULES.leaderboard_mode != "off":
        if RULES.leaderboard_mode == "inline":
            board = texts.leaderboard(game.leaderboard(), RULES.leaderboard_limit)
        elif (idx + 1) % RULES.leaderboard_every == 0:
            board = texts.leaderboard(game.leaderboard(), RULES.leaderboard_limit)

    # Рейтинг клеится к разбору ответа: отдельным сообщением после каждого
    # вопроса вышло бы 4 сообщения на вопрос, а лимит Telegram — около 20
    # в минуту на группу. Подпись к фото ограничена 1024 символами, поэтому
    # при переполнении рейтинг уходит отдельно.
    inline_board = board if RULES.leaderboard_mode == "inline" else None
    combined = f"{result_text}\n\n{inline_board}" if inline_board else result_text
    caption_overflow = question.image and len(combined) > 1000

    if question.image:
        await say_photo(context, game, question.image,
                        result_text if caption_overflow else combined)
    else:
        await say(context, game, combined)

    leftover = board if (caption_overflow or inline_board is None) else None
    if leftover:
        await say(context, game, leftover)

    game.current_question += 1
    await to_db(db.update_game, game.game_id, current_question=game.current_question)

    if game.pause_after_question:
        game.pause_after_question = False
        game.status = "paused"
        await to_db(db.update_game, game.game_id, status="paused")
        await say(context, game, "⏸ Квиз приостановлен. /resume для продолжения.")
        return

    if game.current_question < game.total_questions:
        _schedule(context, job_start_question, TIMINGS.between_questions,
                  game.chat_id, f"q:{game.chat_id}")
    else:
        await say(context, game, "Викторина закончена! Подводим итоги...")
        _schedule(context, job_finish, TIMINGS.finish_delay, game.chat_id,
                  f"fin:{game.chat_id}")


# ==================== Итоги ====================

async def job_finish(context: ContextTypes.DEFAULT_TYPE):
    game = LIVE.get(context.job.data)
    if game:
        await finish_quiz(context, game)


async def finish_quiz(context: ContextTypes.DEFAULT_TYPE, game: Game,
                      interrupted: bool = False):
    if game.status == "finished":
        return
    game.status = "finished"
    game.purge_messages = False
    LIVE.pop(game.chat_id, None)
    cancel_chat_jobs(context, game.chat_id)

    played_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    await to_db(db.update_game, game.game_id, status="finished", finished_at=played_at)

    ranking = game.final_ranking()
    if not ranking:
        await say(context, game, "Нет участников.")
        return

    rows = build_result_rows(game, played_at)
    await to_db(db.save_results, rows)

    if interrupted:
        await say(context, game, texts.INTERRUPTED)

    # Церемония: 3 → 2 → 1
    by_place = {g["place"]: g for g in ranking}
    for place in (3, 2, 1):
        group = by_place.get(place)
        if not group:
            continue
        names = [p["username"] for p in group["players"]]
        msg = await say(context, game, texts.podium(place, names))
        if place == 1 and msg:
            try:
                await context.bot.pin_chat_message(chat_id=game.chat_id,
                                                   message_id=msg.message_id)
            except TelegramError:
                pass
        await asyncio.sleep(TIMINGS.podium_pause)

    await say(context, game, texts.final_table(ranking))

    if SHEETS_ENABLED:
        # Выгрузка идёт фоном: медленный Google не должен держать бота.
        asyncio.create_task(_export_later(game.game_id))


async def _export_later(game_id: int):
    try:
        await asyncio.to_thread(sheets.export_game, game_id)
    except Exception:
        log.exception("Экспорт игры %s в Sheets не удался", game_id)


# ==================== Уборка чата во время квиза ====================

async def purge_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or (message.from_user and message.from_user.id == context.bot.id):
        return
    game = LIVE.get(update.effective_chat.id)
    if not game or not game.purge_messages:
        return
    if game.thread_id is not None and message.message_thread_id != game.thread_id:
        return
    await quiet_delete(context, game.chat_id, message.message_id)


# ==================== Восстановление после рестарта ====================

async def recover(context: ContextTypes.DEFAULT_TYPE):
    """Разбирает игры, оборванные редеплоем.

    Раньше состояние жило только в памяти: любой рестарт Railway убивал
    и незавершённый квиз, и запланированный run_once.
    """
    now = datetime.now(timezone.utc)
    for row in await to_db(db.unfinished_games):
        game_id, chat_id = row["id"], row["chat_id"]
        try:
            pack = load_pack(row["pack_id"])
        except Exception:
            log.exception("Пакет игры %s не читается, помечаем aborted", game_id)
            await to_db(db.update_game, game_id, status="aborted")
            continue

        start_utc = datetime.fromisoformat(row["scheduled_start_utc"])

        if row["status"] == "registration":
            late = now - start_utc

            if late > RECOVERY_GRACE:
                # Опоздали безнадёжно. Молча ронять игру нельзя: в чате висит
                # закреплённое приглашение, и люди будут ждать зря.
                game = _rebuild_game(row, pack)
                await say(context, game, texts.CANCELLED_AFTER_RESTART)
                await to_db(db.update_game, game_id, status="aborted")
                log.info("Игра %s отменена: старт просрочен на %s", game_id, late)
                continue

            game = _rebuild_game(row, pack)
            game.reg_msg_id = row["reg_msg_id"]
            for p in await to_db(db.participants, game_id):
                game.add_player(p["user_id"], p["username"])
            LIVE[chat_id] = game

            if start_utc > now:
                delay = (start_utc - now).total_seconds()
                log.info("Восстановлена регистрация игры %s", game_id)
            else:
                # Момент старта пришёлся на даунтайм — стартуем с задержкой.
                delay = RECOVERY_DELAY
                await say(context, game, texts.DELAYED_START)
                log.info("Игра %s стартует с опозданием %s", game_id, late)

            _schedule(context, job_start_sequence, delay, chat_id, f"start:{chat_id}")
            context.job_queue.run_repeating(
                job_refresh_registration, interval=TIMINGS.reg_refresh,
                first=TIMINGS.reg_refresh, chat_id=chat_id, data=chat_id,
                name=f"reg:{chat_id}",
            )
            continue

        # Всё остальное закрываем: доигрывать вопрос с потерянным таймером
        # ненадёжно, а результаты по сыгранному сохранить нужно.
        await _finalize_interrupted(context, row, pack)


def _rebuild_game(row, pack) -> Game:
    return Game(game_id=row["id"], chat_id=row["chat_id"], thread_id=row["thread_id"],
                pack=pack, creator_id=row["creator_id"],
                scheduled_start_utc=datetime.fromisoformat(row["scheduled_start_utc"]),
                source=row["source"])


async def _finalize_interrupted(context, row, pack):
    game = _rebuild_game(row, pack)
    game.status = "active"
    game.current_question = row["current_question"]

    for p in await to_db(db.participants, row["id"]):
        game.add_player(p["user_id"], p["username"])
    for a in await to_db(db.game_answers, row["id"]):
        game.answers[(a["user_id"], a["q_idx"])] = Answer(
            a["q_idx"], a["option_idx"], a["answer_text"], bool(a["is_correct"]),
            a["points"], a["elapsed"],
        )
        if a["is_correct"]:
            game.scores[a["user_id"]] += a["points"]
            game.speed_sum[a["user_id"]] += a["elapsed"]

    if not game.answers:
        await say(context, game, texts.CANCELLED_AFTER_RESTART)
        await to_db(db.update_game, row["id"], status="aborted")
        log.info("Игра %s прервана без ответов, помечена aborted", row["id"])
        return

    LIVE[game.chat_id] = game
    await finish_quiz(context, game, interrupted=True)
