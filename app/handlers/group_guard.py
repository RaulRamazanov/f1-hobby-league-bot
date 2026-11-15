"""Пользовательские команды (/start, /help, /status …) работают только в личке.

В группе такая команда удаляется (нужно право бота «Удалять сообщения»),
а вместо неё на 15 секунд появляется подсказка со ссылкой на личку с ботом.
Админские команды (/open, /close, /admin …) в группе работают как раньше.
"""
import asyncio
import logging

from aiogram import Router, Bot
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from app.ui import is_private, safe_delete

log = logging.getLogger(__name__)
router = Router()

USER_COMMANDS = {"start", "help", "status", "whoami", "roster_reserves"}
HINT_TTL = 15
_bot_username: str | None = None


def is_user_command_in_group(message: Message) -> bool:
    if is_private(message.chat.id) or not message.text or not message.text.startswith("/"):
        return False
    command = message.text.split()[0][1:].split("@")[0].lower()
    return command in USER_COMMANDS


async def _username(bot: Bot) -> str | None:
    global _bot_username
    if _bot_username is None:
        try:
            _bot_username = (await bot.get_me()).username
        except Exception as exc:
            log.warning("get_me failed: %s", exc)
    return _bot_username


async def _delete_later(bot: Bot, chat_id: int, message_id: int, delay: int) -> None:
    await asyncio.sleep(delay)
    await safe_delete(bot, chat_id, message_id)


@router.message(is_user_command_in_group)
async def user_command_in_group(message: Message):
    bot = message.bot
    await safe_delete(bot, message.chat.id, message.message_id)
    username = await _username(bot)
    kb = None
    if username:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💬 Открыть бота", url=f"https://t.me/{username}?start=menu")
        ]])
    try:
        hint = await bot.send_message(
            message.chat.id,
            "👋 Команды бота работают только в личке — нажми кнопку ниже.",
            message_thread_id=message.message_thread_id,
            reply_markup=kb,
        )
        asyncio.create_task(_delete_later(bot, message.chat.id, hint.message_id, HINT_TTL))
    except Exception as exc:
        log.debug("hint failed: %s", exc)
