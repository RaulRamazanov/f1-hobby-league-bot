import html
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from app.db import connect
from app.events import send_race_message, answer_or_message, service_to_admins
from app.handlers.common import _show_profile, edit_or_send
from app.race_ops import withdraw_after_close

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
        if previous == "banned":
            await call.answer("Ты пропускаешь этот этап по решению админа (штраф)", show_alert=True)
            return
        if closed:
            await call.answer(
                "Регистрация завершена. Отказаться до старта можно кнопкой «Отказаться от участия» в профиле"
                if previous == "going" else "Регистрация завершена",
                show_alert=True,
            )
            await db.rollback()
            await _show_profile(call.message, driver_id, edit=True)
            return
        await db.execute(
            "UPDATE entries SET status=?, marked_at=? WHERE race_id=? AND driver_id=?",
            (status, datetime.now(timezone.utc).isoformat(), race_id, driver_id),
        )
        await db.commit()
    await call.answer("Участие подтверждено ✅" if status == "going" else "Отказ принят")
    if previous != status:
        action = "✅ Подтвердил участие" if status == "going" else "❌ Отказался от участия"
        # Подтверждения больше не идут в группу — только админам в личку (удаляются после этапа)
        await service_to_admins(call.bot, race_id,
                                f"{action}: <b>{html.escape(name)}</b> — <b>{html.escape(race_name)}</b>.")
    await _show_profile(call.message, driver_id, edit=True)


# ---------------------------------------------------------------
# Отказ от участия ПОСЛЕ закрытия регистрации (до старта гонки).
# Место сразу получает случайный резерв из листа ожидания.
# ---------------------------------------------------------------

@router.callback_query(F.data == "race:withdraw")
async def cb_withdraw(call: CallbackQuery):
    await call.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, отказываюсь", callback_data="race:withdraw:yes")],
        [InlineKeyboardButton(text="⬅️ Нет, я еду", callback_data="profile")],
    ])
    await edit_or_send(
        call,
        "⚠️ <b>Точно отказаться от участия?</b>\n\n"
        "Регистрация уже закрыта: твоё место сразу получит случайный резерв "
        "из листа ожидания. Вернуть место потом не получится.",
        kb,
    )


@router.callback_query(F.data == "race:withdraw:yes")
async def cb_withdraw_yes(call: CallbackQuery):
    text, driver_id = await withdraw_after_close(call.bot, call.from_user.id)
    await answer_or_message(call, text, show_alert=True)
    if driver_id:
        await _show_profile(call.message, driver_id, edit=True)


# ---------------------------------------------------------------
# Подтверждение за час до гонки. Ответ уходит админам в личку (не в группу),
# сообщения админам удаляются после «Завершить этап».
# ---------------------------------------------------------------

@router.callback_query(F.data.startswith("fc:"))
async def cb_final_check(call: CallbackQuery):
    try:
        _, answer, race_s, driver_s = call.data.split(":")
        race_id, driver_id = int(race_s), int(driver_s)
        if answer not in ("yes", "no"):
            raise ValueError
    except ValueError:
        await call.answer("Кнопка устарела", show_alert=True)
        return
    async with connect() as db:
        cur = await db.execute(
            "SELECT d.tg_id, d.display_name, d.car_number, r.name, r.active, e.status "
            "FROM drivers d JOIN races r ON r.id=? "
            "LEFT JOIN entries e ON e.race_id=r.id AND e.driver_id=d.id WHERE d.id=?",
            (race_id, driver_id),
        )
        row = await cur.fetchone()
        if not row or row[0] != call.from_user.id:
            await call.answer("Эта кнопка не для тебя", show_alert=True)
            return
        _, name, number, race_name, active, status = row
        if not active:
            await call.answer("Этот этап уже завершён", show_alert=True)
            return
        if status != "going":
            await call.answer("Ты сейчас не в составе этого этапа", show_alert=True)
            return
        cur = await db.execute("SELECT answer FROM final_checks WHERE race_id=? AND driver_id=?",
                               (race_id, driver_id))
        prev = await cur.fetchone()
        await db.execute(
            "INSERT INTO final_checks (race_id, driver_id, answer) VALUES (?, ?, ?) "
            "ON CONFLICT(race_id, driver_id) DO UPDATE SET answer=excluded.answer, "
            "answered_at=CURRENT_TIMESTAMP", (race_id, driver_id, answer))
        cur = await db.execute(
            "SELECT COUNT(*), SUM(fc.answer='yes') FROM entries e "
            "LEFT JOIN final_checks fc ON fc.race_id=e.race_id AND fc.driver_id=e.driver_id "
            "WHERE e.race_id=? AND e.status='going'", (race_id,))
        total, yes = await cur.fetchone()
        await db.commit()

    yes = yes or 0
    if answer == "yes":
        await call.answer("Отлично, ждём на гонке! ✅")
        pilot_text = f"⏰ <b>{html.escape(race_name)}</b>\n✅ Ты подтвердил: <b>будешь на гонке</b>. Удачи!"
        admin_text = (f"✅ <b>#{number} {html.escape(name)}</b> подтвердил: будет на гонке "
                      f"({yes}/{total}).")
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="❌ Передумал — не смогу", callback_data=f"fc:no:{race_id}:{driver_id}")]])
    else:
        await call.answer("Понял, админы найдут замену", show_alert=True)
        pilot_text = (f"⏰ <b>{html.escape(race_name)}</b>\n❌ Ты ответил: <b>не сможешь</b>. "
                      "Админы предупреждены.")
        admin_text = (f"❌ <b>#{number} {html.escape(name)}</b> не сможет быть на гонке "
                      f"<b>{html.escape(race_name)}</b>.\n"
                      "Замени его: /admin → «🔀 Снять с этапа / замена».")
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="✅ Всё-таки буду", callback_data=f"fc:yes:{race_id}:{driver_id}")]])
    try:
        await call.message.edit_text(pilot_text, reply_markup=kb)
    except Exception:
        pass
    if not prev or prev[0] != answer:
        await service_to_admins(call.bot, race_id, admin_text)
