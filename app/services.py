"""Общая логика создания пилота — одна «гребёнка» для заявок и ручного добавления."""
import aiosqlite

from app.config import ADMIN_IDS
from app.defaults import MAX_MAINS_PER_TEAM, DEFAULT_TEAMS, ALLOWED_CAR_NUMBERS, car_number_error


async def free_car_numbers(db: aiosqlite.Connection, exclude_driver_id: int | None = None) -> list[int]:
    """Допустимые номера, которые не заняты пилотами и не забронированы висящими заявками."""
    taken: set[int] = set()
    cur = await db.execute("SELECT id, car_number FROM drivers")
    for did, number in await cur.fetchall():
        if did != exclude_driver_id:
            taken.add(number)
    cur = await db.execute("SELECT car_number FROM registration_requests")
    taken.update(r[0] for r in await cur.fetchall())
    return sorted(ALLOWED_CAR_NUMBERS - taken)


def admins_links() -> str:
    links = [f"<a href='tg://user?id={aid}'>админ {i}</a>" for i, aid in enumerate(ADMIN_IDS, 1)]
    return ", ".join(links) if links else "админам лиги"


def free_numbers_text(free: list[int]) -> str:
    """Подсказка со свободными номерами или сообщение, что номера закончились."""
    if free:
        return "🆓 <b>Свободные номера:</b>\n" + ", ".join(map(str, free))
    return (
        "😔 <b>Свободных номеров не осталось.</b>\n"
        f"Напиши админам ({admins_links()}) — они освободят номер, "
        "после этого пришли анкету ещё раз."
    )


async def create_driver(
    db: aiosqlite.Connection,
    *,
    tg_id: int | None,
    tg_username: str | None,
    game_nick: str,
    display_name: str,
    car_number: int,
    role: str,
    team_id: int | None = None,
) -> tuple[bool, str | None]:
    """Возвращает (ok, текст_ошибки). Не делает commit."""
    if role not in ("main", "reserve"):
        return False, "Неизвестная роль"

    error = car_number_error(car_number)
    if error:
        return False, error

    if not db.in_transaction:
        await db.execute("BEGIN IMMEDIATE")

    if tg_id is not None:
        cur = await db.execute("SELECT game_nick FROM drivers WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        if row:
            return False, f"Этот пользователь уже зарегистрирован как {row[0]}"

    cur = await db.execute("SELECT game_nick FROM drivers WHERE car_number=?", (car_number,))
    row = await cur.fetchone()
    if row:
        return False, f"Номер #{car_number} уже занят пилотом {row[0]}"

    cur = await db.execute("SELECT 1 FROM drivers WHERE game_nick=?", (game_nick,))
    if await cur.fetchone():
        return False, f"Ник {game_nick} уже занят"

    if role == "main":
        if not team_id:
            return False, "Не выбрана команда"
        cur = await db.execute("SELECT name FROM teams WHERE id=?", (team_id,))
        team = await cur.fetchone()
        if not team or team[0] not in DEFAULT_TEAMS:
            return False, "Выбери одну из 11 команд чемпионата"
        cur = await db.execute(
            "SELECT COUNT(*) FROM drivers WHERE role='main' AND team_id=?", (team_id,)
        )
        if (await cur.fetchone())[0] >= MAX_MAINS_PER_TEAM:
            return False, f"В команде уже {MAX_MAINS_PER_TEAM} основных пилота"
    else:
        team_id = None

    await db.execute(
        "INSERT INTO drivers (tg_id, tg_username, game_nick, display_name, car_number, role, team_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tg_id, tg_username, game_nick, display_name, car_number, role, team_id),
    )
    cur = await db.execute("SELECT last_insert_rowid()")
    driver_id = (await cur.fetchone())[0]

    # Основной пилот сразу попадает в активный этап
    if role == "main":
        cur = await db.execute("SELECT id FROM races WHERE active=1 AND reg_closed=0")
        race = await cur.fetchone()
        if race:
            await db.execute(
                "INSERT OR IGNORE INTO entries (race_id, driver_id, status) VALUES (?, ?, 'pending')",
                (race[0], driver_id),
            )
    return True, None


async def teams_with_counts(db: aiosqlite.Connection) -> list[tuple[int, str, int]]:
    cur = await db.execute(
        "SELECT t.id, t.name, "
        "(SELECT COUNT(*) FROM drivers d WHERE d.team_id=t.id AND d.role='main') "
        "FROM teams t ORDER BY t.id"
    )
    return [row for row in await cur.fetchall() if row[1] in DEFAULT_TEAMS]