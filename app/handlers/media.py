"""Расписание гонок и турнирные таблицы (командный и личный зачёт).

Админ: /admin → «🖼 Расписание и таблицы» → выбрать раздел → прислать фото.
Фото хранится на сервере в папке media/. При загрузке нового старый файл
удаляется с диска (и при нажатии «Удалить»).

Пилот: кнопки «📅 Расписание» и «🏆 Турнирная таблица» в меню и профиле.
Фото показывается в том же сообщении, таблицы переключаются кнопками.
"""
import html
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message

from app.config import ADMIN_IDS, F1_PHOTO, MEDIA_DIR
from app.db import connect
from app.handlers.common import edit_or_send, cb_menu
from app import ui

log = logging.getLogger(__name__)
router = Router()
MSK = timezone(timedelta(hours=3))

TITLES = {
    "schedule": "📅 Расписание гонок",
    "st_driver": "👤 Личный зачёт",
    "st_team": "🏆 Командный зачёт",
}


class MediaFlow(StatesGroup):
    photo = State()


def _btn(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb)


def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def get_media(key: str):
    """(path, file_id, updated_at) или None, если фото не загружено/файл пропал."""
    async with connect() as db:
        cur = await db.execute("SELECT path, file_id, updated_at FROM media WHERE key=?", (key,))
        row = await cur.fetchone()
    if not row or not Path(row[0]).exists():
        return None
    return row


async def _save_file_id(key: str, msg) -> None:
    """После первой отправки с диска запоминаем file_id — дальше Telegram не перезаливает файл."""
    photos = getattr(msg, "photo", None)
    if not photos:
        return
    async with connect() as db:
        await db.execute("UPDATE media SET file_id=? WHERE key=? AND file_id IS NULL", (photos[-1].file_id, key))
        await db.commit()


def _fmt(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(MSK).strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return str(ts)


async def show_photo(call: CallbackQuery, photo, caption: str, kb, key: str | None = None) -> None:
    """Показывает фото в том же сообщении (edit_media). Не получилось — заменяет сообщение."""
    msg = call.message
    try:
        result = await msg.edit_media(InputMediaPhoto(media=photo, caption=caption), reply_markup=kb)
        await ui.adopt(msg.chat.id, getattr(msg, "message_id", None), call.bot)
        if key:
            await _save_file_id(key, result)
        return
    except Exception as exc:
        if "not modified" in str(exc):
            return
        log.debug("edit_media failed: %s", exc)
    await ui.safe_delete(call.bot, msg.chat.id, getattr(msg, "message_id", None))
    sent = await ui.send_screen(call.bot, msg.chat.id, caption, kb, photo_file=photo)
    if key and sent is not None:
        await _save_file_id(key, sent)


def _photo_src(row):
    path, file_id, _ = row
    return file_id or FSInputFile(path)


# ----------------------------------------------------------------------
# Пилот
# ----------------------------------------------------------------------

def _viewer_kb(key: str) -> InlineKeyboardMarkup:
    rows = []
    if key in ("st_team", "st_driver"):
        rows.append([
            _btn(("• " if key == "st_driver" else "") + "👤 Личный", "md:v:st_driver"),
            _btn(("• " if key == "st_team" else "") + "🏆 Командный", "md:v:st_team"),
        ])
    rows.append([_btn("⬅️ Назад", "md:back")])
    return _kb(rows)


@router.callback_query(F.data.startswith("md:v:"))
async def cb_view(call: CallbackQuery):
    key = call.data.split(":")[2]
    if key not in TITLES:
        await call.answer("Кнопка устарела", show_alert=True)
        return
    row = await get_media(key)
    if not row:
        await call.answer(f"{TITLES[key]}: админ ещё не загрузил фото.", show_alert=True)
        return
    await call.answer()
    caption = f"<b>{TITLES[key]}</b>\n🕐 Обновлено: {_fmt(row[2])} МСК"
    await show_photo(call, _photo_src(row), caption, _viewer_kb(key), key)


@router.callback_query(F.data == "md:back")
async def cb_back(call: CallbackQuery):
    """Возвращаем фирменное фото и профиль/меню в том же сообщении."""
    if F1_PHOTO.exists():
        try:
            await call.message.edit_media(InputMediaPhoto(media=FSInputFile(F1_PHOTO), caption="🏎"))
        except Exception as exc:
            log.debug("restore F1 photo failed: %s", exc)
    await cb_menu(call)


# ----------------------------------------------------------------------
# Админ
# ----------------------------------------------------------------------

async def _admin_text_kb():
    lines = ["🖼 <b>Расписание и таблицы</b>", "━━━━━━━━━━━━━━━━",
             "Пилоты видят их кнопками «📅 Расписание» и «🏆 Турнирная таблица».", ""]
    rows = []
    for key, title in TITLES.items():
        row = await get_media(key)
        state = f"✅ {_fmt(row[2])}" if row else "❌ нет"
        lines.append(f"{title} — {state}")
        rows.append([_btn(f"{title} · {state}", f"md:a:{key}")])
    rows.append([_btn("⬅️ Назад", "admin:menu")])
    return "\n".join(lines), _kb(rows)


async def _restore_f1(call: CallbackQuery, text: str, kb) -> None:
    if F1_PHOTO.exists():
        try:
            await call.message.edit_media(InputMediaPhoto(media=FSInputFile(F1_PHOTO), caption=text),
                                          reply_markup=kb)
            await ui.adopt(call.message.chat.id, getattr(call.message, "message_id", None), call.bot)
            return
        except Exception as exc:
            log.debug("restore F1 failed: %s", exc)
    await edit_or_send(call, text, kb)


@router.callback_query(F.data == "md:admin")
async def cb_admin(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Только для админов", show_alert=True)
        return
    await call.answer()
    await state.clear()
    await _restore_f1(call, *await _admin_text_kb())


def _item_kb(key: str, has: bool) -> InlineKeyboardMarkup:
    rows = [[_btn("📤 Загрузить новое фото" if has else "📤 Загрузить фото", f"md:up:{key}")]]
    if has:
        rows.append([_btn("🗑 Удалить", f"md:del:{key}")])
    rows.append([_btn("⬅️ Назад", "md:admin")])
    return _kb(rows)


@router.callback_query(F.data.startswith("md:a:"))
async def cb_item(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Только для админов", show_alert=True)
        return
    key = call.data.split(":")[2]
    if key not in TITLES:
        await call.answer("Кнопка устарела", show_alert=True)
        return
    await call.answer()
    await state.clear()
    row = await get_media(key)
    if row:
        await show_photo(call, _photo_src(row), f"<b>{TITLES[key]}</b>\n🕐 Загружено: {_fmt(row[2])} МСК",
                         _item_kb(key, True), key)
    else:
        await _restore_f1(call, f"<b>{TITLES[key]}</b>\n\nФото ещё не загружено.", _item_kb(key, False))


@router.callback_query(F.data.startswith("md:up:"))
async def cb_upload(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Только для админов", show_alert=True)
        return
    key = call.data.split(":")[2]
    if key not in TITLES:
        await call.answer("Кнопка устарела", show_alert=True)
        return
    await call.answer()
    await state.set_state(MediaFlow.photo)
    await state.update_data(media_key=key)
    await _restore_f1(call, f"📤 <b>{TITLES[key]}</b>\n\nПришли фото одним сообщением "
                            "(можно файлом-картинкой, чтобы не терять качество).",
                      _kb([[_btn("❌ Отмена", f"md:a:{key}")]]))


@router.message(MediaFlow.photo, F.photo | F.document)
async def msg_photo(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    key = (await state.get_data()).get("media_key")
    if key not in TITLES:
        await state.clear()
        return
    file_id = None
    if message.photo:
        tg_file = message.photo[-1]
        file_id = tg_file.file_id
        ext = ".jpg"
    else:
        doc = message.document
        if not (doc.mime_type or "").startswith("image/"):
            await ui.answer(message, "⚠️ Нужна картинка (фото или файл-изображение).",
                            _kb([[_btn("❌ Отмена", f"md:a:{key}")]]))
            return
        tg_file = doc
        ext = Path(doc.file_name or "img.jpg").suffix.lower() or ".jpg"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    new_path = MEDIA_DIR / f"{key}_{int(time.time())}{ext}"
    await message.bot.download(tg_file, destination=new_path)

    async with connect() as db:
        cur = await db.execute("SELECT path FROM media WHERE key=?", (key,))
        old = await cur.fetchone()
        await db.execute(
            "INSERT INTO media (key, path, file_id, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET path=excluded.path, file_id=excluded.file_id, "
            "updated_at=excluded.updated_at",
            (key, str(new_path), file_id),
        )
        await db.commit()
    # Старое фото удаляем с сервера
    if old and old[0] != str(new_path):
        try:
            Path(old[0]).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Не удалось удалить старый файл %s: %s", old[0], exc)
    await state.clear()
    row = await get_media(key)
    await ui.send_screen(message.bot, message.chat.id,
                         f"✅ <b>{TITLES[key]}</b> обновлено.\n🕐 {_fmt(row[2])} МСК",
                         _item_kb(key, True), photo_file=_photo_src(row))


@router.callback_query(F.data.startswith("md:del:"))
async def cb_delete(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Только для админов", show_alert=True)
        return
    key = call.data.split(":")[2]
    async with connect() as db:
        cur = await db.execute("SELECT path FROM media WHERE key=?", (key,))
        row = await cur.fetchone()
        await db.execute("DELETE FROM media WHERE key=?", (key,))
        await db.commit()
    if row:
        try:
            Path(row[0]).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Не удалось удалить файл %s: %s", row[0], exc)
    await call.answer("Удалено 🗑")
    await _restore_f1(call, *await _admin_text_kb())
