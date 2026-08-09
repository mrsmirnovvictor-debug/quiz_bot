"""Игровая логика без единого обращения к Telegram и к БД.

Отделение этого слоя даёт две вещи: логику можно тестировать без сети,
и сетевые сбои не оставляют состояние наполовину обновлённым.
"""

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import MAX_POINTS_PER_QUESTION, RULES
from packs import Pack


@dataclass
class Answer:
    q_idx: int
    option_idx: int
    text: str
    is_correct: bool
    points: int
    elapsed: float


@dataclass
class Game:
    game_id: int
    chat_id: int
    thread_id: int | None
    pack: Pack
    creator_id: int | None
    scheduled_start_utc: datetime
    source: str = "manual"

    status: str = "registration"        # registration|active|paused|finished
    current_question: int = 0
    players: dict[int, str] = field(default_factory=dict)          # user_id -> username
    scores: dict[int, int] = field(default_factory=dict)
    speed_sum: dict[int, float] = field(default_factory=dict)
    # (user_id, q_idx) -> Answer. Ключ с q_idx исключает сдвиг при пропусках.
    answers: dict[tuple[int, int], Answer] = field(default_factory=dict)

    reg_msg_id: int | None = None
    question_msg_id: int | None = None
    video_msg_id: int | None = None
    question_started_at: datetime | None = None
    shuffled_options: list[str] = field(default_factory=list)
    shuffled_correct: int = 0
    pause_after_question: bool = False
    purge_messages: bool = False
    _last_reg_text: str = ""

    # ---------- Регистрация ----------

    def add_player(self, user_id: int, username: str) -> bool:
        """True, если игрок новый."""
        is_new = user_id not in self.players
        self.players[user_id] = username
        self.scores.setdefault(user_id, 0)
        self.speed_sum.setdefault(user_id, 0.0)
        return is_new

    def usernames(self) -> list[str]:
        return list(self.players.values())

    def reg_text_changed(self, text: str) -> bool:
        """Не дёргать editMessageText, если ничего не изменилось."""
        if text == self._last_reg_text:
            return False
        self._last_reg_text = text
        return True

    # ---------- Вопросы ----------

    @property
    def total_questions(self) -> int:
        return len(self.pack)

    @property
    def is_last_question(self) -> bool:
        return self.current_question >= self.total_questions - 1

    def prepare_question(self) -> tuple[str, list[str], str]:
        """Перемешивает варианты и фиксирует позицию правильного ответа."""
        q = self.pack.questions[self.current_question]
        order = list(range(len(q.options)))
        random.shuffle(order)
        self.shuffled_options = [q.options[i] for i in order]
        self.shuffled_correct = order.index(q.correct)
        self.question_started_at = datetime.now(timezone.utc)
        return q.text, self.shuffled_options, q.image

    def record_answer(self, user_id: int, q_idx: int, option_idx: int,
                      at: datetime) -> Answer | None:
        """Регистрирует ответ. None — если ответ не принят.

        Проверка q_idx == current_question закрывает гонку, при которой нажатие,
        отправленное за миг до конца вопроса, засчитывалось следующему вопросу
        с чужим правильным вариантом.
        """
        if self.status != "active" or user_id not in self.players:
            return None
        if q_idx != self.current_question:
            return None
        if (user_id, q_idx) in self.answers:
            return None
        if self.question_started_at is None:
            return None
        if not (0 <= option_idx < len(self.shuffled_options)):
            return None

        elapsed = (at - self.question_started_at).total_seconds()
        is_correct = option_idx == self.shuffled_correct

        points = 0
        if is_correct:
            points = RULES.base_points
            for threshold, bonus in RULES.speed_bonus:
                if elapsed <= threshold:
                    points += bonus
                    break
            self.scores[user_id] += points
            self.speed_sum[user_id] += elapsed

        answer = Answer(q_idx, option_idx, self.shuffled_options[option_idx],
                        is_correct, points, elapsed)
        self.answers[(user_id, q_idx)] = answer
        return answer

    def current_question_answers(self) -> list[Answer]:
        return [a for (_, q), a in self.answers.items() if q == self.current_question]

    def answer_distribution(self) -> list[float]:
        """Проценты по вариантам текущего вопроса (в порядке перемешивания)."""
        answers = self.current_question_answers()
        counts = [0] * len(self.shuffled_options)
        for a in answers:
            counts[a.option_idx] += 1
        total = len(answers)
        return [(c / total * 100) if total else 0.0 for c in counts]

    def user_answers(self, user_id: int) -> dict[int, Answer]:
        return {q: a for (u, q), a in self.answers.items() if u == user_id}

    # ---------- Итоги ----------

    def leaderboard(self) -> list[tuple[str, int]]:
        rows = sorted(self.players.items(),
                      key=lambda kv: (-self.scores[kv[0]], self.speed_sum[kv[0]]))
        return [(name, self.scores[uid]) for uid, name in rows]

    def final_ranking(self) -> list[dict]:
        """Места с корректной обработкой дележа: 1,2,2,4."""
        players = [
            {
                "user_id": uid,
                "username": name,
                "score": self.scores[uid],
                "speed_sum": self.speed_sum[uid],
            }
            for uid, name in self.players.items()
        ]
        players.sort(key=lambda p: (-p["score"], p["speed_sum"]))

        ranking: list[dict] = []
        place = 1
        i = 0
        while i < len(players):
            score = players[i]["score"]
            group = []
            while i < len(players) and players[i]["score"] == score:
                group.append(players[i])
                i += 1
            ranking.append({"place": place, "players": group})
            place += len(group)
        return ranking


# ==================== Расчёты ====================

def calculate_elo(score: int, max_score: int, avg_time_correct: float,
                  total_players: int, place: int) -> int:
    """Рейтинг силы игрока за конкретную игру.

    Строго говоря, это не ELO Эло, а композитный балл: процент набранных
    очков + бонус за скорость + бонус за место + участие. Название сохранено
    ради совместимости с историческими данными в таблице.
    """
    if max_score <= 0:
        return 20
    score_percent = score / max_score * 100
    speed_bonus = max(0.0, 30 - avg_time_correct) if avg_time_correct > 0 else 0.0
    place_bonus = (total_players - place + 1) * 5
    value = int(score_percent + speed_bonus + place_bonus + 20)
    return max(20, min(300, value))


def rating_points(place: int, missed: int) -> int:
    if missed > RULES.max_missed_for_rating:
        return 0
    if 1 <= place <= len(RULES.rating_by_place):
        return RULES.rating_by_place[place - 1]
    return 0


def build_result_rows(game: Game, played_at: str) -> list[dict]:
    """Готовит строки для таблицы results — источник для всей статистики."""
    ranking = game.final_ranking()
    max_score = game.total_questions * MAX_POINTS_PER_QUESTION
    total_players = len(game.players)
    rows = []

    for group in ranking:
        for player in group["players"]:
            uid = player["user_id"]
            answers = game.user_answers(uid)
            answered = len(answers)
            correct = sum(1 for a in answers.values() if a.is_correct)
            incorrect = answered - correct
            no_answer = game.total_questions - answered

            total_time = sum(a.elapsed for a in answers.values())
            total_time_ok = sum(a.elapsed for a in answers.values() if a.is_correct)
            avg_time = total_time / answered if answered else 0.0
            avg_time_ok = total_time_ok / correct if correct else 0.0

            rows.append({
                "game_id": game.game_id,
                "user_id": uid,
                "chat_id": game.chat_id,
                "username": player["username"],
                "played_at": played_at,
                "place": group["place"],
                "score": player["score"],
                "question_count": game.total_questions,
                "correct": correct,
                "incorrect": incorrect,
                "no_answer": no_answer,
                "total_time": total_time,
                "total_time_ok": total_time_ok,
                "avg_time": avg_time,
                "avg_time_ok": avg_time_ok,
                "elo": calculate_elo(player["score"], max_score, avg_time_ok,
                                     total_players, group["place"]),
                "rating_points": rating_points(group["place"], no_answer),
            })
    return rows
