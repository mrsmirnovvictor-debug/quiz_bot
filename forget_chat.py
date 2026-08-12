"""Удаление группы из статистики бота.

Нужен, когда в базе осталась заброшенная или тестовая группа: она попадает
в выборки и мешает читать сводки.

    railway ssh
    python forget_chat.py -1003930793462            # сухой прогон
    python forget_chat.py -1003930793462 --apply    # удаление

Удаляются игры, результаты, ответы, расписания, заказы и сезоны этой группы.
Записи в листе Games в Google Sheets НЕ трогаются — это журнал, он остаётся
как есть. Листы Players_/Rating_/Ranking_ этой группы можно потом удалить
в самой таблице вручную.

Операция необратима. Перед запуском стоит сделать снимок тома в Railway
(вкладка Backups у сервиса).
"""

import argparse
import sys
from datetime import datetime

import db
from config import MSK


def show(chat_id: int) -> bool:
    games = db._rows(
        "SELECT COUNT(*) n, MIN(substr(finished_at,1,10)) a, "
        "MAX(substr(finished_at,1,10)) b FROM games WHERE chat_id = ?",
        (chat_id,),
    )[0]
    if not games["n"]:
        print(f"Группа {chat_id} в базе не найдена — удалять нечего.")
        return False

    results = db._row(
        "SELECT COUNT(*) n, COUNT(DISTINCT username) u FROM results WHERE chat_id = ?",
        (chat_id,),
    )
    schedules = db._row(
        "SELECT COUNT(*) n FROM schedules WHERE chat_id = ?", (chat_id,))
    orders = db._row(
        "SELECT COUNT(*) n FROM theme_orders WHERE chat_id = ?", (chat_id,))

    print(f"Группа {chat_id}")
    print(f"  игр:          {games['n']}  ({games['a']} — {games['b']})")
    print(f"  результатов:  {results['n']}  игроков: {results['u']}")
    print(f"  расписаний:   {schedules['n']}")
    print(f"  заказов тем:  {orders['n']}")

    top = db._rows(
        "SELECT username, COUNT(*) n FROM results WHERE chat_id = ? "
        "GROUP BY username ORDER BY n DESC LIMIT 5",
        (chat_id,),
    )
    if top:
        print("  участники:    " + ", ".join(f"{r['username']} ({r['n']})" for r in top))
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("chat_id", type=int, help="ID группы, например -1003930793462")
    parser.add_argument("--apply", action="store_true", help="удалить (по умолчанию — показ)")
    args = parser.parse_args()

    db.init()

    print("Группы в базе:\n")
    for row in db._rows(
        "SELECT chat_id, COUNT(DISTINCT game_id) g, MAX(substr(played_at,1,10)) d "
        "FROM results GROUP BY chat_id ORDER BY g DESC"
    ):
        mark = "  <-- удаляем" if row["chat_id"] == args.chat_id else ""
        print(f"  {row['chat_id']}  игр: {row['g']:4}  последняя: {row['d']}{mark}")
    print()

    if not show(args.chat_id):
        return 1

    if not args.apply:
        print(f"\nСухой прогон. Для удаления:")
        print(f"  python forget_chat.py {args.chat_id} --apply")
        return 0

    counts = db.forget_chat(args.chat_id)
    print("\n✅ Удалено:")
    for key, value in counts.items():
        print(f"  {key}: {value}")
    print("\nЛист Games в таблице не изменён. Листы Players_/Rating_/Ranking_ "
          "этой группы удалите в Google Sheets вручную.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
