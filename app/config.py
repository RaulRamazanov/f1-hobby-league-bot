import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
DB_PATH = BASE_DIR.parent / "bot.db"
# Основное фото бота (если balera.png нет на сервере — используется старое f1.jpg)
F1_PHOTO = ASSETS_DIR / "balera.png"
if not F1_PHOTO.exists():
    F1_PHOTO = ASSETS_DIR / "f1.jpg"
# Загруженные админом фото (расписание, таблицы)
MEDIA_DIR = BASE_DIR.parent / "media"

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GROUP_ID = int(os.getenv("GROUP_ID", "0"))
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
REGISTRATION_THREAD_ID = int(os.getenv("REGISTRATION_THREAD_ID", "0"))
# Необязательно: отдельная тема мероприятий. Иначе используется тема /open.
EVENTS_THREAD_ID = int(os.getenv("EVENTS_THREAD_ID", "0"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в .env")
