"""Единая точка конфигурации. Всё, что раньше было хардкодом, живёт здесь."""

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

# -------------------- Обязательные переменные окружения --------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# -------------------- Хранилище --------------------
# На Railway смонтируйте том на /data и оставьте путь по умолчанию.
# Файлы вне тома НЕ переживают редеплой.
DB_PATH = os.environ.get("DB_PATH", "/data/quiz.db")
PACKS_DIR = os.environ.get("PACKS_DIR", "packs")

# -------------------- Google Sheets (витрина, не первичное хранилище) ------
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS", "")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
SHEETS_ENABLED = bool(GOOGLE_CREDENTIALS_JSON and GOOGLE_SHEET_ID)

# -------------------- Медиа --------------------
TIMER_VIDEO_URL = os.environ.get("TIMER_VIDEO_URL", "")

# -------------------- Логирование --------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Timings:
    """Тайминги квиза в секундах."""

    question: int = _int_env("T_QUESTION", 20)          # время на ответ
    between_questions: int = _int_env("T_BETWEEN", 5)    # пауза между вопросами
    pre_start_warning: int = _int_env("T_WARNING", 30)   # предупреждение до старта
    early_start_delay: int = _int_env("T_EARLY", 5)      # задержка при досрочном старте
    finish_delay: int = _int_env("T_FINISH", 5)          # пауза перед итогами
    reg_refresh: int = _int_env("T_REG_REFRESH", 15)     # обновление списка регистрации
    podium_pause: int = _int_env("T_PODIUM", 3)          # пауза между объявлениями мест
    scheduler_tick: int = _int_env("T_TICK", 60)     # период опроса расписания


# Во сколько по Москве публиковать анонс игрового дня. Пусто — не публиковать.
ANNOUNCE_AT = os.environ.get("ANNOUNCE_AT", "12:00")


@dataclass(frozen=True)
class Audio:
    """Музыкальные вопросы."""

    question_seconds: int = _int_env("T_QUESTION_AUDIO", 20)

    # Способ отправки: "voice" или "audio".
    #
    # voice — голосовое сообщение. Telegram не выстраивает их в плейлист,
    #   поэтому после короткого отрывка НЕ начинает играть предыдущие вопросы.
    #   Именно это и нужно для квиза. Метаданные файла в плеере не видны,
    #   так что название трека ответ не выдаст.
    # audio — карточка с названием и исполнителем. Красивее, но клиент
    #   автоматически переходит к соседним аудио в ленте.
    mode: str = os.environ.get("AUDIO_MODE", "voice")

    # Используются только в режиме audio.
    performer: str = os.environ.get("AUDIO_PERFORMER", "Квиз")
    title_template: str = os.environ.get("AUDIO_TITLE", "Вопрос {n}")


@dataclass(frozen=True)
class Rules:
    """Правила начисления очков и рейтинга."""

    base_points: int = 10
    # (порог в секундах, бонус). Проверяются по порядку.
    speed_bonus: tuple = ((5, 5), (10, 4), (13, 3), (16, 2), (19, 1))
    # Очки рейтинга за место: 1-е, 2-е, 3-е, 4-е, 5-е
    rating_by_place: tuple = (10, 5, 3, 2, 1)
    # Если пропущено больше вопросов — очки рейтинга обнуляются
    max_missed_for_rating: int = 8
    # Сколько игр нужно для попадания в /stats и /rank
    calibration_games: int = _int_env("CALIBRATION_GAMES", 10)
    # Как показывать промежуточный рейтинг:
    #   inline   — приклеить к разбору ответа (не плодит сообщений)
    #   separate — отдельным сообщением раз в leaderboard_every вопросов
    #   off      — не показывать
    leaderboard_mode: str = os.environ.get("LEADERBOARD_MODE", "inline")
    leaderboard_every: int = _int_env("LEADERBOARD_EVERY", 4)
    # Сколько строк рейтинга показывать (в группе бывает 40+ игроков)
    leaderboard_limit: int = _int_env("LEADERBOARD_LIMIT", 10)


TIMINGS = Timings()
RULES = Rules()
AUDIO = Audio()


def _parse_chat_list(name: str) -> set[int]:
    result = set()
    for chunk in os.environ.get(name, "").split(","):
        chunk = chunk.strip()
        if chunk:
            try:
                result.add(int(chunk))
            except ValueError:
                continue
    return result


# Группы, где победители могут заказывать темы.
THEMES_CHATS = _parse_chat_list("THEMES_CHATS")
# Максимум заказов на игрока за сезон, сколько бы побед он ни набрал.
THEME_LIMIT = _int_env("THEME_LIMIT", 3)


def themes_enabled(chat_id: int) -> bool:
    return chat_id in THEMES_CHATS


def _parse_calibration_overrides() -> dict[int, int]:
    """CALIBRATION_BY_CHAT='-1003453018572:1,-1002440363847:10'

    Порог калибровки нужен большим группам с длинной историей и мешает
    молодым: при семи сыгранных квизах порог в 10 игр прячет всю группу.
    """
    raw = os.environ.get("CALIBRATION_BY_CHAT", "")
    result: dict[int, int] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            chat_id, value = chunk.split(":")
            result[int(chat_id)] = max(1, int(value))
        except ValueError:
            continue
    return result


CALIBRATION_BY_CHAT = _parse_calibration_overrides()


def calibration_for(chat_id: int) -> int:
    """Сколько игр нужно игроку этой группы для попадания в /stats и /rank."""
    return CALIBRATION_BY_CHAT.get(chat_id, RULES.calibration_games)

# Максимум очков за вопрос — нужен для расчёта ELO
MAX_POINTS_PER_QUESTION = RULES.base_points + (RULES.speed_bonus[0][1] if RULES.speed_bonus else 0)
