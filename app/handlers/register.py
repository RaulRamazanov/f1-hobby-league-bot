import html
import logging

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from app.config import GROUP_ID, REGISTRATION_THREAD_ID, ADMIN_IDS
from app.db import connect
from app.defaults import car_number_error
from app.parser import parse_profile, looks_like_profile
from app.keyboards import approve_kb, team_pick_kb, main_menu_kb
from app.handlers.common import edit_or_send
from app.services import create_driver, teams_with_counts, free_car_numbers, free_numbers_text
from app import texts

from app import ui

log = logging.getLogger(__name__)
router = Router()


def esc(value) -> str:
    return html.escape(str(value), quote=False)


def request_text(tg_id, tg_username, game_nick, display_name, car_number, suffix: str = "") -> str:
    text = (
        "🆕 <b>Новая заявка</b>\n\n"
        f"Ник: <code>{esc(game_nick)}</code>\n"
        f"Имя: <b>{esc(display_name)}</b>\n"
        f"Номер: <b>#{car_number}</b>\n"
        f"TG: <a href='tg://user?id={tg_id}'>{esc(display_name)}</a>"
        + (f" (@{esc(tg_username)})" if tg_username else "")
    )
    return text + suffix


async def notify_admins(bot: Bot, request_id: int, text: str) -> int:
    """Шлёт заявку всем админам. Возвращает, скольким дошло. Ошибки логируются."""
    sent = 0
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=approve_kb(request_id))
            sent += 1
        except Exception as e:
            log.warning(
                "Не удалось отправить заявку #%s админу %s: %s "
                "(админ должен хотя бы раз нажать /start у бота)",
                request_id, admin_id, e,
            )
    return sent


# ---------------------------------------------------------------
# Приём анкеты в теме регистрации
# ---------------------------------------------------------------

async def submit_request(bot: Bot, tg_id: int, tg_username: str | None,
                         game_nick: str, display_name: str, car_number: int) -> tuple[bool, str, str | None]:
    """Все проверки анкеты + создание заявки + рассылка админам.
    Возвращает (ok, текст для пользователя, шаг для исправления: 'nick' / 'number' / None)."""
    async with connect() as db:
        cur = await db.execute("SELECT game_nick FROM drivers WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        if row:
            return False, texts.REGISTER_ALREADY.format(game_nick=esc(row[0])), None
        cur = await db.execute("SELECT 1 FROM registration_requests WHERE tg_id=?", (tg_id,))
        if await cur.fetchone():
            return False, texts.REGISTRATION_PENDING, None

        # Номер закреплён игрой (реальный пилот Ф1) — показываем, что свободно
        error = car_number_error(car_number)
        if error:
            free = await free_car_numbers(db)
            return False, "⚠️ " + error + "\n\n" + free_numbers_text(free), "number"
        for table in ("drivers", "registration_requests"):
            cur = await db.execute(f"SELECT game_nick FROM {table} WHERE car_number=?", (car_number,))
            row = await cur.fetchone()
            if row:
                free = await free_car_numbers(db)
                return False, (texts.REGISTER_NUMBER_TAKEN.format(car_number=car_number, owner=esc(row[0]))
                               + "\n\n" + free_numbers_text(free)), "number"
        for table in ("drivers", "registration_requests"):
            cur = await db.execute(f"SELECT 1 FROM {table} WHERE game_nick=?", (game_nick,))
            if await cur.fetchone():
                return False, texts.REGISTER_NICK_TAKEN.format(game_nick=esc(game_nick)), "nick"

        await db.execute(
            "INSERT INTO registration_requests "
            "(tg_id, tg_username, game_nick, display_name, car_number) "
            "VALUES (?, ?, ?, ?, ?)",
            (tg_id, tg_username, game_nick, display_name, car_number),
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        request_id = (await cur.fetchone())[0]

    text = request_text(tg_id, tg_username, game_nick, display_name, car_number)
    sent = await notify_admins(bot, request_id, text)
    if sent == 0:
        # Никому из админов не дошло — не оставляем заявку «в тишине»
        log.error("Заявка #%s не доставлена ни одному админу", request_id)
        try:
            await bot.send_message(
                GROUP_ID,
                f"⚠️ Заявка #{request_id} ({esc(game_nick)}) не доставлена админам в ЛС. "
                "Админы, нажмите /start у бота, затем /pending.",
                message_thread_id=REGISTRATION_THREAD_ID or None,
            )
        except Exception as e:
            log.warning("Не удалось отправить предупреждение в группу: %s", e)
    return True, (
        "⏳ <b>Заявка отправлена админам!</b>\n\n"
        f"Ник: <code>{esc(game_nick)}</code>\nИмя: <b>{esc(display_name)}</b>\nНомер: <b>#{car_number}</b>\n\n"
        "Когда админ одобрит — придёт сообщение."
    ), None


# ---------------------------------------------------------------
# Регистрация в личке с ботом: ник → имя → номер → отправить
# ---------------------------------------------------------------

class RegFlow(StatesGroup):
    nick = State()
    name = State()
    number = State()
    confirm = State()


def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=c) for t, c in row] for row in rows])


CANCEL = [("❌ Отмена", "reg:cancel")]


async def _reg_blocked(tg_id: int) -> str | None:
    async with connect() as db:
        cur = await db.execute("SELECT game_nick FROM drivers WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        if row:
            return texts.REGISTER_ALREADY.format(game_nick=esc(row[0])) + " Нажми /start."
        cur = await db.execute("SELECT 1 FROM registration_requests WHERE tg_id=?", (tg_id,))
        if await cur.fetchone():
            return texts.REGISTRATION_PENDING
    return None


async def _nick_taken(nick: str) -> bool:
    async with connect() as db:
        for table in ("drivers", "registration_requests"):
            cur = await db.execute(f"SELECT 1 FROM {table} WHERE game_nick=?", (nick,))
            if await cur.fetchone():
                return True
    return False


def _step_nick(error: str = ""):
    return ((error + "\n\n" if error else "")
            + "📝 <b>Регистрация · шаг 1/3</b>\n\n"
            "Напиши свой <b>игровой ник</b> — точно как в игре.\n\n"
            "<i>Можно сразу прислать анкету целиком тремя строками:</i>\n"
            "<code>1. Игровой ник\n2. Имя Фамилия\n3. Номер болида</code>",
            _kb([CANCEL]))


def _step_name(nick: str, error: str = ""):
    return ((error + "\n\n" if error else "")
            + f"📝 <b>Регистрация · шаг 2/3</b>\nНик: <code>{esc(nick)}</code>\n\n"
            "Как тебя подписать? Напиши <b>Имя Фамилия</b> (или Name Nick).",
            _kb([[("⬅️ Ник", "reg:back:nick")], CANCEL]))


async def _step_number(data: dict, error: str = ""):
    async with connect() as db:
        free = await free_car_numbers(db)
    text = ((error + "\n\n" if error else "")
            + f"📝 <b>Регистрация · шаг 3/3</b>\nНик: <code>{esc(data['reg_nick'])}</code> · "
            f"{esc(data['reg_name'])}\n\n")
    rows = []
    if free:
        text += "🔢 Выбери <b>номер болида</b> из свободных (или напиши число):"
        row = []
        for n in free:
            row.append((str(n), f"reg:num:{n}"))
            if len(row) == 8:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
    else:
        text += free_numbers_text(free)
    rows += [[("⬅️ Имя", "reg:back:name")], CANCEL]
    return text, _kb(rows)


def _step_confirm(data: dict):
    return ("📝 <b>Проверь анкету</b>\n━━━━━━━━━━━━━━━━\n"
            f"🏷 Ник: <code>{esc(data['reg_nick'])}</code>\n"
            f"👤 Имя: <b>{esc(data['reg_name'])}</b>\n"
            f"🔑 Номер: <b>#{data['reg_num']}</b>",
            _kb([[("✅ Отправить заявку", "reg:send")],
                 [("✏️ Ник", "reg:back:nick"), ("✏️ Имя", "reg:back:name"), ("✏️ Номер", "reg:back:number")],
                 CANCEL]))


@router.callback_query(F.data == "reg:start")
async def cb_reg_start(call: CallbackQuery, state: FSMContext):
    if not ui.is_private(call.message.chat.id):
        await call.answer("Регистрация — в личке с ботом", show_alert=True)
        return
    blocked = await _reg_blocked(call.from_user.id)
    if blocked:
        await call.answer(html.unescape(blocked.replace("<b>", "").replace("</b>", "")), show_alert=True)
        return
    await call.answer()
    await state.clear()
    await state.set_state(RegFlow.nick)
    await edit_or_send(call, *_step_nick())


@router.callback_query(F.data == "reg:cancel")
async def cb_reg_cancel(call: CallbackQuery, state: FSMContext):
    await call.answer("Регистрация отменена")
    await state.clear()
    await edit_or_send(call, texts.START, main_menu_kb())


@router.callback_query(F.data.startswith("reg:back:"))
async def cb_reg_back(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    step = call.data.split(":")[2]
    await call.answer()
    if step == "name" and data.get("reg_nick"):
        await state.set_state(RegFlow.name)
        await edit_or_send(call, *_step_name(data["reg_nick"]))
    elif step == "number" and data.get("reg_name"):
        await state.set_state(RegFlow.number)
        await edit_or_send(call, *await _step_number(data))
    else:
        await state.set_state(RegFlow.nick)
        await edit_or_send(call, *_step_nick())


async def _go_confirm_or_number(message_or_call, state: FSMContext, number: int, is_call: bool):
    data = await state.get_data()
    error = car_number_error(number)
    if not error:
        async with connect() as db:
            if number not in await free_car_numbers(db):
                error = f"Номер #{number} уже занят. Выбери свободный."
    if error:
        await state.set_state(RegFlow.number)
        view = await _step_number(data, "⚠️ " + error)
    else:
        await state.update_data(reg_num=number)
        await state.set_state(RegFlow.confirm)
        view = _step_confirm(dict(data, reg_num=number))
    if is_call:
        await edit_or_send(message_or_call, *view)
    else:
        await ui.answer(message_or_call, *view)


@router.message(RegFlow.nick, F.text, ~F.text.startswith("/"))
async def msg_reg_nick(message: Message, state: FSMContext):
    parsed = parse_profile(message.text)
    if parsed:  # прислали анкету целиком
        nick, name, number = parsed
        if await _nick_taken(nick):
            await ui.answer(message, *_step_nick(texts.REGISTER_NICK_TAKEN.format(game_nick=esc(nick))))
            return
        await state.update_data(reg_nick=nick, reg_name=name)
        await _go_confirm_or_number(message, state, number, is_call=False)
        return
    nick = message.text.strip()
    if not 1 <= len(nick) <= 32 or "\n" in nick:
        await ui.answer(message, *_step_nick("⚠️ Ник — одной строкой, до 32 символов."))
        return
    if await _nick_taken(nick):
        await ui.answer(message, *_step_nick(texts.REGISTER_NICK_TAKEN.format(game_nick=esc(nick))))
        return
    await state.update_data(reg_nick=nick)
    data = await state.get_data()
    if data.get("reg_num") and data.get("reg_name"):  # правка ника с экрана проверки
        await state.set_state(RegFlow.confirm)
        await ui.answer(message, *_step_confirm(data))
        return
    await state.set_state(RegFlow.name)
    await ui.answer(message, *_step_name(nick))


@router.message(RegFlow.name, F.text, ~F.text.startswith("/"))
async def msg_reg_name(message: Message, state: FSMContext):
    name = message.text.strip()
    data = await state.get_data()
    if not 1 <= len(name) <= 64 or "\n" in name:
        await ui.answer(message, *_step_name(data.get("reg_nick", ""), "⚠️ Имя — одной строкой, до 64 символов."))
        return
    await state.update_data(reg_name=name)
    data = await state.get_data()
    if data.get("reg_num"):
        await state.set_state(RegFlow.confirm)
        await ui.answer(message, *_step_confirm(data))
        return
    await state.set_state(RegFlow.number)
    await ui.answer(message, *await _step_number(data))


@router.message(RegFlow.number, F.text, ~F.text.startswith("/"))
async def msg_reg_number(message: Message, state: FSMContext):
    try:
        number = int(message.text.strip().lstrip("№#"))
    except ValueError:
        await ui.answer(message, *await _step_number(await state.get_data(), "⚠️ Номер — целое число."))
        return
    await _go_confirm_or_number(message, state, number, is_call=False)


@router.callback_query(F.data.startswith("reg:num:"))
async def cb_reg_number(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("reg_name"):
        await call.answer("Форма устарела — нажми /start", show_alert=True)
        return
    await call.answer()
    await _go_confirm_or_number(call, state, int(call.data.split(":")[2]), is_call=True)


@router.callback_query(F.data == "reg:send")
async def cb_reg_send(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not all(data.get(k) for k in ("reg_nick", "reg_name", "reg_num")):
        await call.answer("Форма устарела — нажми /start", show_alert=True)
        return
    await call.answer("Отправляю…")
    ok, text, step = await submit_request(call.bot, call.from_user.id, call.from_user.username,
                                          data["reg_nick"], data["reg_name"], data["reg_num"])
    if ok:
        await state.clear()
        await edit_or_send(call, text, _kb([[("🏁 Статус Гран-при", "race:status")]]))
        return
    if step == "number":
        await state.set_state(RegFlow.number)
        await edit_or_send(call, *await _step_number(data, text))
    elif step == "nick":
        await state.set_state(RegFlow.nick)
        await edit_or_send(call, *_step_nick(text))
    else:
        await state.clear()
        await edit_or_send(call, text, main_menu_kb())


# ---------------------------------------------------------------
# Тема регистрации в группе: анкеты больше не принимаются — ведём в бота
# ---------------------------------------------------------------

def is_registration_message(message: Message) -> bool:
    if message.chat.id != GROUP_ID:
        return False
    if REGISTRATION_THREAD_ID and message.message_thread_id != REGISTRATION_THREAD_ID:
        return False
    if not message.text or message.text.startswith("/"):
        return False
    if message.from_user is None or message.from_user.is_bot:
        return False
    return True


_bot_username: str | None = None


@router.message(is_registration_message)
async def handle_profile(message: Message):
    if not (parse_profile(message.text) or looks_like_profile(message.text)):
        return
    global _bot_username
    if _bot_username is None:
        try:
            _bot_username = (await message.bot.get_me()).username
        except Exception:
            _bot_username = ""
    kb = None
    if _bot_username:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="📝 Зарегистрироваться в боте", url=f"https://t.me/{_bot_username}?start=reg")]])
    await message.reply("📝 Регистрация теперь проходит в личке с ботом — нажми кнопку ниже, "
                        "бот проведёт по шагам.", reply_markup=kb)


# ---------------------------------------------------------------
# Обработка заявки админом: Резерв / Основа / Отклонить
# ---------------------------------------------------------------

async def _load_request(db, request_id: int):
    cur = await db.execute(
        "SELECT tg_id, tg_username, game_nick, display_name, car_number "
        "FROM registration_requests WHERE id=?",
        (request_id,),
    )
    return await cur.fetchone()


async def _edit(call: CallbackQuery, text: str, kb=None):
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        log.debug("edit_text failed: %s", e)


def _only_admin(call: CallbackQuery) -> bool:
    return call.from_user.id in ADMIN_IDS


# Совместимость со старыми сообщениями с кнопкой «Одобрить»
@router.callback_query(F.data.startswith("approve:"))
async def cb_legacy_approve(call: CallbackQuery):
    if not _only_admin(call):
        await call.answer("Только для админов", show_alert=True)
        return
    request_id = int(call.data.split(":")[1])
    async with connect() as db:
        row = await _load_request(db, request_id)
    if not row:
        await call.answer("Заявка уже обработана", show_alert=True)
        return
    await call.answer()
    await _edit(call, request_text(*row), approve_kb(request_id))


@router.callback_query(F.data.startswith("reject:"))
async def cb_legacy_reject(call: CallbackQuery):
    await _reject(call, int(call.data.split(":")[1]))


@router.callback_query(F.data.startswith("req:back:"))
async def cb_req_back(call: CallbackQuery):
    if not _only_admin(call):
        await call.answer("Только для админов", show_alert=True)
        return
    request_id = int(call.data.split(":")[2])
    async with connect() as db:
        row = await _load_request(db, request_id)
    if not row:
        await call.answer("Заявка уже обработана", show_alert=True)
        return
    await call.answer()
    await _edit(call, request_text(*row), approve_kb(request_id))


@router.callback_query(F.data.startswith("req:reserve:"))
async def cb_req_reserve(call: CallbackQuery):
    await _approve(call, int(call.data.split(":")[2]), "reserve", None)


@router.callback_query(F.data.startswith("req:main:"))
async def cb_req_main(call: CallbackQuery):
    """Основа: сначала выбираем команду."""
    if not _only_admin(call):
        await call.answer("Только для админов", show_alert=True)
        return
    request_id = int(call.data.split(":")[2])
    async with connect() as db:
        row = await _load_request(db, request_id)
        teams = await teams_with_counts(db)
    if not row:
        await call.answer("Заявка уже обработана", show_alert=True)
        return
    await call.answer()
    await _edit(
        call,
        request_text(*row) + "\n\n🏎 <b>Выбери команду для основного пилота:</b>",
        team_pick_kb(teams, f"req:team:{request_id}:", f"req:back:{request_id}"),
    )


@router.callback_query(F.data.startswith("req:team:"))
async def cb_req_team(call: CallbackQuery):
    _, _, rid, tid = call.data.split(":")
    await _approve(call, int(rid), "main", int(tid))


@router.callback_query(F.data == "team:full")
async def cb_team_full(call: CallbackQuery):
    await call.answer("В этой команде уже 2 основных пилота", show_alert=True)


async def _approve(call: CallbackQuery, request_id: int, role: str, team_id: int | None):
    if not _only_admin(call):
        await call.answer("Только для админов", show_alert=True)
        return

    async with connect() as db:
        row = await _load_request(db, request_id)
        if not row:
            await call.answer("Заявка уже обработана", show_alert=True)
            return
        tg_id, tg_username, game_nick, display_name, car_number = row

        ok, err = await create_driver(
            db,
            tg_id=tg_id, tg_username=tg_username, game_nick=game_nick,
            display_name=display_name, car_number=car_number,
            role=role, team_id=team_id,
        )
        if not ok:
            await call.answer(err, show_alert=True)
            # Если дубль по нику/номеру/пользователю — заявка безнадёжна, убираем её
            if "уже занят" in err or "уже зарегистрирован" in err:
                await db.execute("DELETE FROM registration_requests WHERE id=?", (request_id,))
                await db.commit()
            return

        team_name = None
        if team_id:
            cur = await db.execute("SELECT name FROM teams WHERE id=?", (team_id,))
            tr = await cur.fetchone()
            team_name = tr[0] if tr else None

        await db.execute("DELETE FROM registration_requests WHERE id=?", (request_id,))
        await db.commit()

    if role == "main":
        verdict = f"🏎 <b>Основа</b> — {esc(team_name)}"
        user_text = (
            "✅ <b>Заявка одобрена!</b>\n\n"
            f"Ты — основной пилот команды <b>{esc(team_name)}</b>.\n"
            f"Ник: <code>{esc(game_nick)}</code>, номер: #{car_number}\n"
            "Нажми /start, чтобы подтверждать участие в этапах."
        )
    else:
        verdict = "🔄 <b>Резерв</b>"
        user_text = (
            "✅ <b>Заявка одобрена!</b>\n\n"
            "Ты добавлен в резерв.\n"
            f"Ник: <code>{esc(game_nick)}</code>, номер: #{car_number}\n"
            "Нажми /start, чтобы вставать в очередь на замену."
        )

    await call.answer("Одобрено ✅")
    await _edit(call, request_text(*row) + f"\n\n✅ <b>Одобрено:</b> {verdict}")
    try:
        await call.bot.send_message(tg_id, user_text)
    except Exception as e:
        log.warning("Не удалось уведомить пилота %s: %s", tg_id, e)


@router.callback_query(F.data.startswith("req:reject:"))
async def cb_req_reject(call: CallbackQuery):
    await _reject(call, int(call.data.split(":")[2]))


async def _reject(call: CallbackQuery, request_id: int):
    if not _only_admin(call):
        await call.answer("Только для админов", show_alert=True)
        return

    async with connect() as db:
        row = await _load_request(db, request_id)
        if not row:
            await call.answer("Заявка уже обработана", show_alert=True)
            return
        await db.execute("DELETE FROM registration_requests WHERE id=?", (request_id,))
        await db.commit()
    tg_id = row[0]

    await call.answer("Отклонено ❌")
    await _edit(call, request_text(*row) + "\n\n❌ <b>Отклонено</b>")
    try:
        await call.bot.send_message(tg_id, "❌ Заявка отклонена. Напиши админу, если это ошибка.")
    except Exception as e:
        log.warning("Не удалось уведомить пилота %s: %s", tg_id, e)


# ---------------------------------------------------------------
# Списки
# ---------------------------------------------------------------

async def send_pending(bot: Bot, chat_id: int) -> int:
    """Присылает каждую висящую заявку отдельным сообщением с кнопками."""
    async with connect() as db:
        cur = await db.execute(
            "SELECT id, tg_id, tg_username, game_nick, display_name, car_number "
            "FROM registration_requests ORDER BY id"
        )
        rows = await cur.fetchall()
    for rid, tg_id, uname, nick, name, num in rows:
        await bot.send_message(
            chat_id, request_text(tg_id, uname, nick, name, num), reply_markup=approve_kb(rid)
        )
    return len(rows)


@router.message(Command("pending"))
async def cmd_pending(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    count = await send_pending(message.bot, message.from_user.id)
    if count == 0:
        await ui.reply(message, "Заявок нет.")


@router.message(Command("roster_reserves"))
async def cmd_roster_reserves(message: Message):
    # Пользовательская команда: работает в личке (в группе её перехватывает group_guard)
    if not ui.is_private(message.chat.id):
        return
    async with connect() as db:
        cur = await db.execute(
            "SELECT game_nick, display_name, car_number FROM drivers "
            "WHERE role='reserve' ORDER BY id DESC LIMIT 30"
        )
        rows = await cur.fetchall()
    if not rows:
        await ui.reply(message, "Резервов пока нет.")
        return
    lines = ["🔄 <b>Резервы (последние 30)</b>"]
    for nick, name, num in rows:
        lines.append(f"• <code>{esc(nick)}</code> — {esc(name)} (#{num})")
    await ui.reply(message, "\n".join(lines))