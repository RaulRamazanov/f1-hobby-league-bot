import aiosqlite

from app.config import DB_PATH
from app.defaults import DEFAULT_TEAMS, LEGACY_TEAM_RENAMES


SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS drivers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id         INTEGER UNIQUE,
    tg_username   TEXT,
    game_nick     TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    car_number    INTEGER NOT NULL UNIQUE,
    role          TEXT NOT NULL CHECK(role IN ('main','reserve')),
    team_id       INTEGER REFERENCES teams(id),
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS registration_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id         INTEGER NOT NULL UNIQUE,
    tg_username   TEXT,
    game_nick     TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    car_number    INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS races (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    race_at          TEXT NOT NULL,
    main_deadline    TEXT NOT NULL,
    offer_timeout    INTEGER NOT NULL DEFAULT 60,
    active           INTEGER NOT NULL DEFAULT 1,
    reg_closed       INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS entries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id        INTEGER NOT NULL REFERENCES races(id),
    driver_id      INTEGER NOT NULL REFERENCES drivers(id),
    status         TEXT NOT NULL DEFAULT 'pending',
    assigned_team  INTEGER REFERENCES teams(id),
    replaces       INTEGER REFERENCES drivers(id),
    offer_deadline TEXT,
    marked_at      TEXT,
    UNIQUE(race_id, driver_id)
);

CREATE TABLE IF NOT EXISTS reserve_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id    INTEGER NOT NULL REFERENCES races(id),
    driver_id  INTEGER NOT NULL REFERENCES drivers(id),
    queued_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(race_id, driver_id)
);

-- Штрафные баллы: каждая запись — одно решение админа (points может быть < 0 — снятие)
CREATE TABLE IF NOT EXISTS penalties (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id   INTEGER NOT NULL REFERENCES drivers(id),
    points      INTEGER NOT NULL,
    reason      TEXT,
    race_ban    INTEGER NOT NULL DEFAULT 0,
    admin_id    INTEGER,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Текущее сообщение-экран бота в личке (чтобы удалять старые)
CREATE TABLE IF NOT EXISTS ui_screens (
    chat_id     INTEGER PRIMARY KEY,
    message_id  INTEGER NOT NULL
);

-- Фото расписания и турнирных таблиц (файл хранится на сервере)
CREATE TABLE IF NOT EXISTS media (
    key         TEXT PRIMARY KEY,
    path        TEXT NOT NULL,
    file_id     TEXT,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Служебные сообщения этапа (статусы, «вылетел», замены) — удаляются после «Завершить этап»
CREATE TABLE IF NOT EXISTS service_msgs (
    race_id     INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL
);

-- Подтверждение за час до гонки
CREATE TABLE IF NOT EXISTS final_checks (
    race_id     INTEGER NOT NULL,
    driver_id   INTEGER NOT NULL,
    answer      TEXT NOT NULL,
    answered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (race_id, driver_id)
);

-- Отметки «Я в лобби» по этапу
CREATE TABLE IF NOT EXISTS lobby_checks (
    race_id     INTEGER NOT NULL REFERENCES races(id),
    driver_id   INTEGER NOT NULL REFERENCES drivers(id),
    checked_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (race_id, driver_id)
);
"""


async def seed_defaults() -> None:
    """Переименовывает старые команды и добавляет недостающие из списка 2026."""
    async with aiosqlite.connect(DB_PATH) as db:
        for old_name, new_name in LEGACY_TEAM_RENAMES.items():
            cur = await db.execute("SELECT 1 FROM teams WHERE name=?", (new_name,))
            if await cur.fetchone():
                continue
            await db.execute("UPDATE teams SET name=? WHERE name=?", (new_name, old_name))
        for name in DEFAULT_TEAMS:
            await db.execute("INSERT OR IGNORE INTO teams (name) VALUES (?)", (name,))
        await db.commit()


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        # миграция для уже существующей bot.db
        cur = await db.execute("PRAGMA table_info(races)")
        cols = {r[1] for r in await cur.fetchall()}
        if "reg_closed" not in cols:
            await db.execute("ALTER TABLE races ADD COLUMN reg_closed INTEGER NOT NULL DEFAULT 0")
        for column, definition in (
            ("thread_id", "INTEGER"),
            ("started", "INTEGER NOT NULL DEFAULT 0"),
            ("lobby_sent_at", "TEXT"),
            ("reminder_sent", "INTEGER NOT NULL DEFAULT 0"),
            # 1 — регистрация закроется сама в main_deadline. У старых этапов 0 (как было — вручную).
            ("auto_close", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in cols:
                await db.execute(f"ALTER TABLE races ADD COLUMN {column} {definition}")
        cur = await db.execute("PRAGMA table_info(drivers)")
        driver_cols = {r[1] for r in await cur.fetchall()}
        if "ban_pending" not in driver_cols:
            # Сколько ближайших этапов пилот пропускает (штраф, назначает админ)
            await db.execute("ALTER TABLE drivers ADD COLUMN ban_pending INTEGER NOT NULL DEFAULT 0")
        await db.commit()
    await seed_defaults()


def connect() -> aiosqlite.Connection:
    return aiosqlite.connect(DB_PATH)