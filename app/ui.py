"""Одно сообщение-экран в личке с ботом.

* Навигация по кнопкам правит текущее сообщение. Если править нельзя
  (фото ↔ текст, длинная подпись), старое сообщение удаляется и вместо
  него приходит новое — в чате остаётся одно.
* Когда бот отвечает на команду или ввод, предыдущий экран удаляется.
* Сообщения пользователя в личке (команды, ответы админа в мастерах)
  удаляются после обработки — см. CleanPrivateInput.

Уведомления (объявление этапа, «ты едешь», лобби, «Ознакомился») экранами
не считаются и не удаляются: это важная информация.
"""
import logging

from aiogram import BaseMiddleware, Bot
from aiogram.types import FSInputFile, Message

from app.config import F1_PHOTO
from app.db import connect

log = logging.getLogger(__name__)


def is_private(chat_id: int) -> bool:
    return chat_id > 0


async def _get_last(chat_id: int) -> int | None:
    async with connect() as db:
        cur = await db.execute("SELECT message_id FROM ui_screens WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
    return row[0] if row else None


async def remember(chat_id: int, message_id: int) -> None:
    if not is_private(chat_id):
        return
    async with connect() as db:
        await db.execute(
            "INSERT INTO ui_screens (chat_id, message_id) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET message_id=excluded.message_id",
            (chat_id, message_id),
        )
        await db.commit()


async def safe_delete(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception as exc:  # уже удалено / старше 48 часов
        log.debug("delete %s/%s: %s", chat_id, message_id, exc)


async def drop_previous(bot: Bot, chat_id: int, keep: int | None = None) -> None:
    """Удаляет прошлый экран в личке (кроме keep)."""
    if not is_private(chat_id):
        return
    last = await _get_last(chat_id)
    if last and last != keep:
        await safe_delete(bot, chat_id, last)


async def adopt(chat_id: int, message_id: int, bot: Bot) -> None:
    """Сообщение, которое только что отредактировали, становится текущим экраном."""
    if not is_private(chat_id):
        return
    await drop_previous(bot, chat_id, keep=message_id)
    await remember(chat_id, message_id)


async def send_screen(bot: Bot, chat_id: int, text: str, kb=None, photo: bool = False,
                      photo_file=None) -> Message | None:
    """Новый экран: в личке удаляет предыдущий и запоминает новый."""
    await drop_previous(bot, chat_id)
    try:
        if photo or photo_file is not None:
            src = photo_file if photo_file is not None else (FSInputFile(F1_PHOTO) if F1_PHOTO.exists() else None)
            if src is not None and len(text) <= 1024:
                msg = await bot.send_photo(chat_id, photo=src, caption=text, reply_markup=kb)
            else:
                msg = await bot.send_message(chat_id, text, reply_markup=kb)
        else:
            msg = await bot.send_message(chat_id, text, reply_markup=kb)
    except Exception as exc:
        log.warning("Не удалось отправить экран в %s: %s", chat_id, exc)
        return None
    if msg is not None and getattr(msg, "message_id", None):
        await remember(chat_id, msg.message_id)
    return msg


async def answer(message: Message, text: str, reply_markup=None, reply: bool = False, **kwargs):
    """Замена message.answer/reply: в личке — экран вместо нового сообщения подряд."""
    if is_private(message.chat.id):
        return await send_screen(message.bot, message.chat.id, text, reply_markup)
    if reply:
        return await message.reply(text, reply_markup=reply_markup, **kwargs)
    return await message.answer(text, reply_markup=reply_markup, **kwargs)


async def reply(message: Message, text: str, reply_markup=None, **kwargs):
    return await answer(message, text, reply_markup=reply_markup, reply=True, **kwargs)


class CleanPrivateInput(BaseMiddleware):
    """После обработки удаляет сообщение пользователя в личке с ботом."""

    async def __call__(self, handler, event, data):
        try:
            return await handler(event, data)
        finally:
            try:
                chat = getattr(event, "chat", None)
                user = getattr(event, "from_user", None)
                if chat is not None and is_private(chat.id) and user is not None and not user.is_bot:
                    await safe_delete(data.get("bot") or event.bot, chat.id, event.message_id)
            except Exception as exc:
                log.debug("clean input failed: %s", exc)
