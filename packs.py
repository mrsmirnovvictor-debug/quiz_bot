"""Загрузка, валидация и кэширование пакетов вопросов.

Главное отличие от старой версии: пакет проверяется на этапе /quiz, а не
падает с KeyError посреди игры.
"""

import json
import logging
import os
from dataclasses import dataclass, field

from config import PACKS_DIR

log = logging.getLogger(__name__)


class PackError(Exception):
    pass


@dataclass
class Question:
    text: str
    options: list[str]
    correct: int
    image: str = ""
    comment: str = ""
    audio: str = ""          # URL или путь к mp3 относительно корня проекта
    duration: int = 0        # 0 = взять значение по умолчанию
    audio_mode: str = ""     # voice|audio, пусто = из настроек

    @property
    def is_audio(self) -> bool:
        return bool(self.audio)


@dataclass
class Pack:
    pack_id: str
    title: str
    questions: list[Question] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.questions)


def _parse_question(raw: dict, n: int) -> Question:
    # Поддерживаем оба формата ключей: старый (text/correct) и тот,
    # что используется в JSON-файлах контента (question/correctIndex).
    text = raw.get("text") or raw.get("question")
    if not text:
        raise PackError(f"вопрос {n}: пустой текст")

    options = raw.get("options")
    if not isinstance(options, list) or len(options) < 2:
        raise PackError(f"вопрос {n}: нужно минимум 2 варианта ответа")
    if any(not str(o).strip() for o in options):
        raise PackError(f"вопрос {n}: есть пустой вариант ответа")

    correct = raw.get("correct")
    if correct is None:
        correct = raw.get("correctIndex")
    if not isinstance(correct, int) or not (0 <= correct < len(options)):
        raise PackError(f"вопрос {n}: неверный индекс правильного ответа ({correct!r})")

    audio = str(raw.get("audio") or "").strip()
    if audio and not audio.startswith(("http://", "https://")):
        if not os.path.exists(audio):
            raise PackError(f"вопрос {n}: аудиофайл не найден ({audio})")

    duration = raw.get("duration") or 0
    if not isinstance(duration, int) or duration < 0 or duration > 300:
        raise PackError(f"вопрос {n}: неверное значение duration ({duration!r})")

    audio_mode = str(raw.get("audio_mode") or "").strip().lower()
    if audio_mode and audio_mode not in ("voice", "audio"):
        raise PackError(
            f"вопрос {n}: audio_mode может быть voice или audio, а не {audio_mode!r}"
        )

    return Question(
        text=str(text).strip(),
        options=[str(o).strip() for o in options],
        correct=correct,
        image=str(raw.get("image") or "").strip(),
        comment=str(raw.get("comment") or "").strip(),
        audio=audio,
        duration=duration,
        audio_mode=audio_mode,
    )


def load_pack(pack_id: str) -> Pack:
    """Читает и валидирует пакет. Бросает PackError с понятным сообщением."""
    if not (len(pack_id) == 4 and pack_id.isdigit()):
        raise PackError("ID пакета должен состоять из 4 цифр, например 0007")

    path = os.path.join(PACKS_DIR, f"{pack_id}.json")
    if not os.path.exists(path):
        raise PackError(f"пакет {pack_id} не найден")

    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise PackError(f"пакет {pack_id}: некорректный JSON ({e})") from e

    title = str(raw.get("title") or "").strip()
    if not title:
        raise PackError(f"пакет {pack_id}: не указан title")

    raw_questions = raw.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        raise PackError(f"пакет {pack_id}: нет вопросов")

    questions = []
    for i, item in enumerate(raw_questions):
        try:
            questions.append(_parse_question(item, i + 1))
        except PackError as e:
            # Без ID пакета такое предупреждение в логах бесполезно.
            raise PackError(f"пакет {pack_id} ({path}): {e}") from None
    return Pack(pack_id=pack_id, title=title, questions=questions)


def list_pack_ids(pool_prefix: str = "") -> list[str]:
    """Все корректные ID пакетов, опционально отфильтрованные по префиксу."""
    if not os.path.isdir(PACKS_DIR):
        return []
    ids = []
    for name in os.listdir(PACKS_DIR):
        if not name.endswith(".json"):
            continue
        pid = name[:-5]
        if len(pid) == 4 and pid.isdigit() and pid.startswith(pool_prefix):
            ids.append(pid)
    return sorted(ids)


def pack_titles() -> dict[str, str]:
    """{pack_id: title} для /games. Битые пакеты помечаются явно."""
    out = {}
    for pid in list_pack_ids():
        try:
            out[pid] = load_pack(pid).title
        except PackError as e:
            log.warning("Пакет %s не прошёл валидацию: %s", pid, e)
            out[pid] = "[ошибка чтения]"
    return out


def load_first_valid(candidates: list[str]) -> tuple[Pack, list[str]]:
    """Первый читаемый пакет из списка + список пропущенных битых.

    Нужен планировщику: один испорченный файл не должен срывать автозапуск.
    """
    skipped = []
    for pid in candidates:
        try:
            return load_pack(pid), skipped
        except PackError as e:
            log.warning("Пропускаю пакет %s: %s", pid, e)
            skipped.append(pid)
    raise PackError("ни один пакет из пула не проходит валидацию")


def validate_all() -> list[str]:
    """Прогоняет все пакеты при старте бота. Возвращает список проблем."""
    problems = []
    for pid in list_pack_ids():
        try:
            load_pack(pid)
        except PackError as e:
            problems.append(str(e))
    return problems
