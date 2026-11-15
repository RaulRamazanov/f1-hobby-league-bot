import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import ErrorEvent

from app.config import BOT_TOKEN
from app.db import init_db
from app.handlers import common, admin, roster, driver, reserve, register, penalties

log = logging.getLogger(__name__)


async def report_error(event: ErrorEvent, bot: Bot):
    exc = event.exception
    log.error("Ошибка обработки update=%s", event.update.update_id,
              exc_info=(type(exc), exc, exc.__traceback__))
    call = event.update.callback_query
    message = call.message if call else event.update.message
    if message and hasattr(message, 'chat'):
        try:
            await bot.send_message(
                message.chat.id,
                "⚠️ Не удалось завершить действие. Проверь «Статус Гран-при» перед повторной попыткой. "
                "Подробности ошибки записаны в терминале бота.",
                message_thread_id=message.message_thread_id,
            )
        except TelegramAPIError:
            log.warning("Не удалось отправить сообщение об ошибке")
    return True


def create_dispatcher():
    # Последовательная обработка действий одного пользователя защищает мастер
    # от двойных нажатий. Изменения базы отдельно защищены транзакциями.
    dp = Dispatcher(events_isolation=SimpleEventIsolation())
    # Команды и мастер админа должны обрабатываться до приёма анкет в группе.
    for module in (admin, roster, penalties, driver, reserve, register, common):
        dp.include_router(module.router)
    dp.errors.register(report_error)
    return dp


async def final_check_loop(bot: Bot) -> None:
    """Раз в минуту: автозакрытие регистрации по времени."""
    from app.race_ops import auto_close_due
    while True:
        try:
            await auto_close_due(bot)
        except Exception:
            log.exception("Ошибка автозакрытия регистрации")
        await asyncio.sleep(60)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    log.info("F1 bot: мастер Гран-при v2; admin=%s; keyboards=%s", admin.__file__, admin.admin_menu_kb.__module__)
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = create_dispatcher()
    await init_db()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        reminders = asyncio.create_task(final_check_loop(bot))
        try:
            await dp.start_polling(bot)
        finally:
            reminders.cancel()
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())