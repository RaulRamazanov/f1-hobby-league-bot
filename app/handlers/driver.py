import html
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.types import CallbackQuery

from app.db import connect
from app.events import send_race_message
from app.handlers.common import _show_profile

router = Router()


@router.callback_query(F.data == "main:yes")
async def cb_main_yes(call: CallbackQuery):
    await _set_main_status(call, "going")


@router.callback_query(F.data == "main:no")
async def cb_main_no(call: CallbackQuery):
    await _set_main_status(call, "not_going")


async def _set_main_status(call: CallbackQuery, status: str):
    async with connect() as db:
        # Ответ и закрытие регистрации не могут пересечься в середине изменения.
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT d.id, d.display_name, r.id, r.name, r.reg_closed, e.status "
            "FROM drivers d JOIN entries e ON e.driver_id=d.id "
            "JOIN races r ON r.id=e.race_id "
            "WHERE d.tg_id=? AND d.role='main' AND r.active=1",
            (call.from_user.id,),
        )
        row = await cur.fetchone()
        if not row:
            await call.answer("Нет активного этапа", show_alert=True)
            return
        driver_id, name, race_id, race_name, closed, previous = row
        if closed:
            await call.answer("Регистрация завершена", show_alert=True)
            return
        await db.execute(
            "UPDATE entries SET status=?, marked_at=? WHERE race_id=? AND driver_id=?",
            (status, datetime.now(timezone.utc).isoformat(), race_id, driver_id),
        )
        await db.commit()
    await call.answer("Участие подтверждено ✅" if status == "going" else "Отказ принят")
    if previous != status:
        action = "✅ Подтвердил участие" if status == "going" else "❌ Отказался от участия"
        await send_race_message(call.bot, race_id,
                                f"{action}: <b>{html.escape(name)}</b> — <b>{html.escape(race_name)}</b>.")
    await _show_profile(call.message, driver_id, edit=True)
