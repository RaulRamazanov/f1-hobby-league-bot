"""Парсер анкеты пилота:
    1. <game_nick>
    2. <display_name>
    3. <car_number>
Терпит префиксы "[21.09.2026 13:59] Ruduk:" и "Илья:" в начале строки.
"""
import re


PREFIX_RE = re.compile(r"^(?:\[[^\]]+\]\s*)?[\wА-Яа-яЁё\- .]+:\s*", re.UNICODE)
LINE_RE = re.compile(r"^\s*(\d)\s*[.)\]:]\s*(.+?)\s*$", re.UNICODE)


def looks_like_profile(text: str) -> bool:
    """Похоже ли сообщение на попытку прислать анкету (есть строки '1.' / '2.' / '3.')."""
    if not text:
        return False
    hits = 0
    for line in text.splitlines():
        if LINE_RE.match(PREFIX_RE.sub("", line.strip(), count=1)) or LINE_RE.match(line.strip()):
            hits += 1
    return hits >= 1


def parse_profile(text: str) -> tuple[str, str, int] | None:
    if not text:
        return None

    fields: dict[int, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Сначала пробуем строку как есть, и только потом срезаем префикс «Ник:»
        m = LINE_RE.match(line)
        if not m:
            m = LINE_RE.match(PREFIX_RE.sub("", line, count=1).strip())
        if m:
            fields[int(m.group(1))] = m.group(2).strip()

    if not {1, 2, 3}.issubset(fields):
        return None

    game_nick = fields[1].strip()
    display_name = fields[2].strip()
    number_str = fields[3].lstrip("№#").strip()

    try:
        car_number = int(number_str)
    except ValueError:
        return None

    if not game_nick or not display_name:
        return None

    return game_nick, display_name, car_number