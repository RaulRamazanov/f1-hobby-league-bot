"""Ручное редактирование состава из админки:
перенос основа ↔ резерв, смена команды, смена номера, удаление пилота."""
import html
import logging

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import ADMIN_IDS
from app.db import connect
from app.defaults import MAX_MAINS_PER_TEAM, DEFAULT_TEAMS, car_number_error
from app.handlers.common import edit_or_send, send_with_photo
from app.keyboards import team_pick_kb
from app.services import teams_with_counts, free_car_numbers, free_numbers_text

log = logging.getLogger(__name__)
from app import ui

router = Router()

PAGE_SIZE = 8


class RosterFlow(StatesGroup):
    number = State()


def esc(v) -> str:
    return html.escape(str(v), quote=False)


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def _btn(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb)


async def _dm(bot: Bot, tg_id: int | None, text: str) -> None:
    if not tg_id:
        return
    try:
        await bot.send_message(tg_id, text)
    except Exception as e:
        log.debug("DM %s failed: %s", tg_id, e)


# ----------------------------------------------------------------------
# Список пилотов (основа / резерв) с пагинацией
# ----------------------------------------------------------------------

@router.callback_query(F.data.startswith("ros:l:"))
async def cb_list(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await call.answer()
    await state.clear()

    _, _, role, page_s = call.data.split(":")
    if role not in ("main", "reserve"):
        return
    page = max(int(page_s), 0)

    async with connect() as db:
        cur = await db.execute("SELECT COUNT(*) FROM drivers WHERE role=?", (role,))
        total = (await cur.fetchone())[0]
        pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
        page = min(page, pages - 1)
        cur = await db.execute(
            "SELECT d.id, d.display_name, d.car_number, t.name FROM drivers d "
            "LEFT JOIN teams t ON t.id=d.team_id WHERE d.role=? "
            "ORDER BY COALESCE(t.name, ''), d.car_number LIMIT ? OFFSET ?",
            (role, PAGE_SIZE, page * PAGE_SIZE),
        )
        rows = await cur.fetchall()

    kb_rows = []
    for did, name, num, team in rows:
        label = f"#{num} {name}" + (f" · {team}" if team else "")
        kb_rows.append([_btn(label, f"ros:d:{did}")])
    nav = []
    if page > 0:
        nav.append(_btn("⬅️", f"ros:l:{role}:{page - 1}"))
    if page < pages - 1:
        nav.append(_btn("➡️", f"ros:l:{role}:{page + 1}"))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([_btn("⬅️ Назад", "admin:menu")])

    title = "🏎 <b>Основа</b>" if role == "main" else "🔄 <b>Резерв</b>"
    text = f"{title} — выбери пилота ({total}, стр. {page + 1}/{pages})"
    if total == 0:
        text = f"{title} — пока никого."
    await edit_or_send(call, text, InlineKeyboardMarkup(inline_keyboard=kb_rows))


# ----------------------------------------------------------------------
# Карточка пилота
# ----------------------------------------------------------------------

async def _card(driver_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    async with connect() as db:
        cur = await db.execute(
            "SELECT d.display_name, d.game_nick, d.car_number, d.role, d.tg_id, t.name "
            "FROM drivers d LEFT JOIN teams t ON t.id=d.team_id WHERE d.id=?",
            (driver_id,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    name, nick, num, role, tg_id, team = row

    text = (
        f"👤 <b>{esc(name)}</b> (<code>{esc(nick)}</code>)\n"
        f"Номер: <b>#{num}</b>\n"
        f"Роль: <b>{'Основа' if role == 'main' else 'Резерв'}</b>"
    )
    if team:
        text += f"\nКоманда: <b>{esc(team)}</b>"
    if not tg_id:
        text += "\n<i>Telegram не привязан (добавлен вручную)</i>"

    rows = []
    if role == "main":
        rows.append([_btn("🔄 В резерв", f"ros:toreserve:{driver_id}")])
        rows.append([_btn("🏎 Сменить команду", f"ros:tomain:{driver_id}")])
    else:
        rows.append([_btn("🏎 Перевести в основу", f"ros:tomain:{driver_id}")])
    rows.append([_btn("🔢 Сменить номер", f"ros:num:{driver_id}")])
    rows.append([_btn("🗑 Удалить", f"ros:del:{driver_id}")])
    rows.append([_btn("⬅️ К списку", f"ros:l:{role}:0")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_card(call: CallbackQuery, driver_id: int):
    card = await _card(driver_id)
    if not card:
        await edit_or_send(call, "Пилот не найден.", InlineKeyboardMarkup(
            inline_keyboard=[[_btn("⬅️ Назад", "admin:menu")]]
        ))
        return
    await edit_or_send(call, *card)


@router.callback_query(F.data.startswith("ros:d:"))
async def cb_card(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await call.answer()
    await state.clear()
    await _show_card(call, int(call.data.split(":")[2]))


# ----------------------------------------------------------------------
# Основа → резерв
# ----------------------------------------------------------------------

@router.callback_query(F.data.startswith("ros:toreserve:"))
async def cb_to_reserve(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    driver_id = int(call.data.split(":")[2])

    async with connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("SELECT 1 FROM races WHERE active=1 AND reg_closed=1")
        if await cur.fetchone():
            await call.answer("Сначала заверши этап (/finish) или открой регистрацию (/reopen)", show_alert=True)
            return
        cur = await db.execute(
            "SELECT role, tg_id FROM drivers WHERE id=?", (driver_id,)
        )
        row = await cur.fetchone()
        if not row or row[0] != "main":
            await call.answer("Пилот не найден или уже в резерве", show_alert=True)
            return
        tg_id = row[1]

        await db.execute("UPDATE drivers SET role='reserve', team_id=NULL WHERE id=?", (driver_id,))
        cur = await db.execute("SELECT id FROM races WHERE active=1")
        race = await cur.fetchone()
        if race:
            # Штрафной пропуск этапа сохраняется при смене роли
            await db.execute(
                "DELETE FROM entries WHERE race_id=? AND driver_id=? AND status!='banned'",
                (race[0], driver_id),
            )
        await db.execute("UPDATE entries SET replaces=NULL WHERE replaces=?", (driver_id,))
        await db.commit()

    await call.answer("Переведён в резерв ✅")
    await _dm(call.bot, tg_id, "ℹ️ Админ перевёл тебя в резерв. Нажми /start.")
    await _show_card(call, driver_id)


# ----------------------------------------------------------------------
# Резерв → основа / смена команды (выбор команды)
# ----------------------------------------------------------------------

@router.callback_query(F.data.startswith("ros:tomain:"))
async def cb_to_main(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await call.answer()
    driver_id = int(call.data.split(":")[2])
    async with connect() as db:
        teams = await teams_with_counts(db)
    if not teams:
        await edit_or_send(call, "Сначала создай команды.", InlineKeyboardMarkup(
            inline_keyboard=[[_btn("⬅️ Назад", f"ros:d:{driver_id}")]]
        ))
        return
    await edit_or_send(
        call, "🏎 Выбери команду:",
        team_pick_kb(teams, f"ros:setteam:{driver_id}:", f"ros:d:{driver_id}"),
    )


@router.callback_query(F.data.startswith("ros:setteam:"))
async def cb_set_team(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    _, _, did, tid = call.data.split(":")
    driver_id, team_id = int(did), int(tid)

    async with connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("SELECT 1 FROM races WHERE active=1 AND reg_closed=1")
        if await cur.fetchone():
            await call.answer("Сначала заверши этап (/finish) или открой регистрацию (/reopen)", show_alert=True)
            return
        cur = await db.execute(
            "SELECT role, team_id, tg_id FROM drivers WHERE id=?", (driver_id,)
        )
        row = await cur.fetchone()
        if not row:
            await call.answer("Пилот не найден", show_alert=True)
            return
        role, old_team, tg_id = row

        if role == "main" and old_team == team_id:
            await call.answer("Он уже в этой команде")
            return

        cur = await db.execute("SELECT name FROM teams WHERE id=?", (team_id,))
        target = await cur.fetchone()
        if not target or target[0] not in DEFAULT_TEAMS:
            await call.answer("Выбери одну из 11 команд чемпионата", show_alert=True)
            return
        cur = await db.execute(
            "SELECT COUNT(*) FROM drivers WHERE role='main' AND team_id=?", (team_id,)
        )
        if (await cur.fetchone())[0] >= MAX_MAINS_PER_TEAM:
            await call.answer(
                f"В команде уже {MAX_MAINS_PER_TEAM} основных пилота", show_alert=True
            )
            return

        await db.execute(
            "UPDATE drivers SET role='main', team_id=? WHERE id=?", (team_id, driver_id)
        )

        if role == "reserve":
            # новый основной попадает в активный этап
            cur = await db.execute("SELECT id, reg_closed FROM races WHERE active=1")
            race = await cur.fetchone()
            if race:
                race_id, reg_closed = race
                await db.execute(
                    "DELETE FROM reserve_queue WHERE race_id=? AND driver_id=?",
                    (race_id, driver_id),
                )
                cur = await db.execute(
                    "SELECT status FROM entries WHERE race_id=? AND driver_id=?", (race_id, driver_id)
                )
                prev = await cur.fetchone()
                await db.execute(
                    "DELETE FROM entries WHERE race_id=? AND driver_id=?", (race_id, driver_id)
                )
                # Штрафной пропуск этапа сохраняется при смене роли
                new_status = "banned" if prev and prev[0] == "banned" else (
                    "going" if reg_closed else "pending")
                await db.execute(
                    "INSERT INTO entries (race_id, driver_id, status) VALUES (?, ?, ?)",
                    (race_id, driver_id, new_status),
                )

        cur = await db.execute("SELECT name FROM teams WHERE id=?", (team_id,))
        team_name = (await cur.fetchone())[0]
        await db.commit()

    await call.answer("Готово ✅")
    await _dm(
        call.bot, tg_id,
        f"ℹ️ Админ назначил тебя в основу команды <b>{esc(team_name)}</b>. Нажми /start.",
    )
    await _show_card(call, driver_id)


# ----------------------------------------------------------------------
# Смена номера (2–99, без повторов)
# ----------------------------------------------------------------------

@router.callback_query(F.data.startswith("ros:num:"))
async def cb_num(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await call.answer()
    driver_id = int(call.data.split(":")[2])
    await state.set_state(RosterFlow.number)
    await state.update_data(driver_id=driver_id)
    async with connect() as db:
        free = await free_car_numbers(db)
    await edit_or_send(
        call,
        "🔢 Пришли новый номер болида.\n\n" + free_numbers_text(free),
        InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Отмена", f"ros:d:{driver_id}")]]),
    )


@router.message(RosterFlow.number, F.text)
async def msg_num(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    driver_id = data.get("driver_id")
    if not driver_id:
        await state.clear()
        return

    raw = message.text.strip().lstrip("№#").strip()
    try:
        number = int(raw)
    except ValueError:
        await ui.answer(message, "⚠️ Пришли номер целым числом.")
        return
    error = car_number_error(number)
    if error:
        async with connect() as db:
            free = await free_car_numbers(db, exclude_driver_id=driver_id)
        await ui.answer(message, "⚠️ " + error + "\n\n" + free_numbers_text(free))
        return

    async with connect() as db:
        for table in ("drivers", "registration_requests"):
            q = f"SELECT game_nick FROM {table} WHERE car_number=?"
            params: tuple = (number,)
            if table == "drivers":
                q += " AND id!=?"
                params = (number, driver_id)
            cur = await db.execute(q, params)
            owner = await cur.fetchone()
            if owner:
                free = await free_car_numbers(db, exclude_driver_id=driver_id)
                await ui.answer(message, 
                    f"⚠️ Номер #{number} уже занят: {esc(owner[0])}. Пришли другой.\n\n"
                    + free_numbers_text(free)
                )
                return
        await db.execute("UPDATE drivers SET car_number=? WHERE id=?", (number, driver_id))
        await db.commit()

    await state.clear()
    card = await _card(driver_id)
    if card:
        await send_with_photo(message, f"✅ Номер изменён на #{number}\n\n" + card[0], card[1])


# ----------------------------------------------------------------------
# Удаление пилота
# ----------------------------------------------------------------------

@router.callback_query(F.data.startswith("ros:del:"))
async def cb_del(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await call.answer()
    driver_id = int(call.data.split(":")[2])
    card = await _card(driver_id)
    if not card:
        await edit_or_send(call, "Пилот не найден.", InlineKeyboardMarkup(
            inline_keyboard=[[_btn("⬅️ Назад", "admin:menu")]]
        ))
        return
    await edit_or_send(
        call,
        card[0] + "\n\n🗑 <b>Удалить пилота полностью?</b> Это нельзя отменить.",
        InlineKeyboardMarkup(inline_keyboard=[
            [_btn("✅ Да, удалить", f"ros:delyes:{driver_id}")],
            [_btn("❌ Отмена", f"ros:d:{driver_id}")],
        ]),
    )


@router.callback_query(F.data.startswith("ros:delyes:"))
async def cb_del_yes(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    driver_id = int(call.data.split(":")[2])

    async with connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("SELECT 1 FROM races WHERE active=1 AND reg_closed=1")
        if await cur.fetchone():
            await call.answer("Сначала заверши этап (/finish) или открой регистрацию (/reopen)", show_alert=True)
            return
        cur = await db.execute("SELECT role, tg_id FROM drivers WHERE id=?", (driver_id,))
        row = await cur.fetchone()
        if not row:
            await call.answer("Уже удалён", show_alert=True)
            return
        role, tg_id = row
        await db.execute("UPDATE entries SET replaces=NULL WHERE replaces=?", (driver_id,))
        await db.execute("DELETE FROM entries WHERE driver_id=?", (driver_id,))
        await db.execute("DELETE FROM reserve_queue WHERE driver_id=?", (driver_id,))
        await db.execute("DELETE FROM penalties WHERE driver_id=?", (driver_id,))
        await db.execute("DELETE FROM lobby_checks WHERE driver_id=?", (driver_id,))
        await db.execute("DELETE FROM drivers WHERE id=?", (driver_id,))
        await db.commit()

    await call.answer("Удалён 🗑")
    await _dm(call.bot, tg_id, "ℹ️ Админ удалил тебя из лиги. Если это ошибка — напиши админу.")
    await edit_or_send(
        call,
        "🗑 Пилот удалён.",
        InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ К списку", f"ros:l:{role}:0")]]),
    )