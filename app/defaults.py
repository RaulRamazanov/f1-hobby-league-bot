# Сезон Ф1 2026: 11 команд (22 пилота).
DEFAULT_TEAMS = [
    "⬜ Mercedes",         # Серебряные стрелы
    "🟥 Ferrari",          # Классический гоночный красный
    "🟧 McLaren",          # Фирменный папайевый оранжевый
    "🟦 Red Bull Racing",  # Матовый тёмно-синий
    "🟩 Aston Martin",     # Британский гоночный зелёный
    "🟦 Williams",         # Традиционный синий (нет отдельного квадрата — ромб как альтернатива)
    "🟦 Racing Bulls",     # Глянцевый синий (тоже нет свободного квадрата)
    "🟪 Alpine",           # Розовая ливрея BWT — компромисс в фиолетовом
    "⬛ Haas",             # Угольно-чёрный
    "🔳 Audi",             # Строгий чёрный/серый (светлая рамка, чтоб отличался от Haas)
    "🟨 Cadillac",         # Золотисто-жёлтый
]

# Старые названия из прошлой версии -> новые (для уже существующей bot.db)
LEGACY_TEAM_RENAMES = {
    "RB": "Racing Bulls",
    "Kick Sauber": "Audi",
}

MAX_MAINS_PER_TEAM = 2

# Допустимые номера болидов
MIN_CAR_NUMBER = 2
MAX_CAR_NUMBER = 99

ALLOWED_CAR_NUMBERS = frozenset({
    2, 8, 9, 13, 15, 19, 20, 21, 24, 25, 26, 28,
    29, 32, 34, 35, 36, 37, 38, 39, 40, 42, 45, 46,
    47, 48, 49, 50, 51, 52, 53, 54, 56, 57, 58, 59,
    60, 61, 62, 64, 65, 66, 67, 68, 69, 70, 71, 72,
    73, 74, 75, 76, 78, 79, 80, 82, 83, 84, 85, 86,
    88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99,
})
ALLOWED_CAR_NUMBERS_TEXT = ", ".join(map(str, sorted(ALLOWED_CAR_NUMBERS)))
GRID_CAPACITY = len(DEFAULT_TEAMS) * MAX_MAINS_PER_TEAM


def car_number_error(number: int) -> str | None:
    if number not in ALLOWED_CAR_NUMBERS:
        return (
            f"Номер #{number} закреплён за реальным пилотом — игра не даст его выбрать. "
            "Выбери номер из списка допустимых."
        )
    return None
