import html
import logging

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

from app.config import GROUP_ID, REGISTRATION_THREAD_ID, ADMIN_IDS
from app.db import connect
from app.defaults import car_number_error, ALLOWED_CAR_NUMBERS_TEXT
from app.parser import parse_profile, looks_like_profile
from app.keyboards import approve_kb, team_pick_kb
from app.services import create_driver, teams_with_counts
from app import texts

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


# Фильтр-функция: обработчик срабатывает ТОЛЬКО на сообщения в теме регистрации,
# остальные сообщения группы (и команды) проходят дальше к другим роутерам.
@router.message(is_registration_message)
async def handle_profile(message: Message):
    parsed = parse_profile(message.text)
    if not parsed:
        if looks_like_profile(message.text):
            await message.reply(texts.REGISTER_PARSE_ERROR)
        return

    game_nick, display_name, car_number = parsed
    error = car_number_error(car_number)
    if error:
        await message.reply("⚠️ " + error + "\n\nДопустимые номера: " + ALLOWED_CAR_NUMBERS_TEXT)
        return
    tg_id = message.from_user.id
    tg_username = message.from_user.username

    async with connect() as db:
        cur = await db.execute("SELECT game_nick FROM drivers WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        if row:
            await message.reply(texts.REGISTER_ALREADY.format(game_nick=esc(row[0])))
            return

        cur = await db.execute("SELECT 1 FROM registration_requests WHERE tg_id=?", (tg_id,))
        if await cur.fetchone():
            await message.reply(texts.REGISTRATION_PENDING)
            return

        for table in ("drivers", "registration_requests"):
            cur = await db.execute(
                f"SELECT game_nick FROM {table} WHERE car_number=?", (car_number,)
            )
            row = await cur.fetchone()
            if row:
                await message.reply(
                    texts.REGISTER_NUMBER_TAKEN.format(car_number=car_number, owner=esc(row[0]))
                )
                return

        for table in ("drivers", "registration_requests"):
            cur = await db.execute(f"SELECT 1 FROM {table} WHERE game_nick=?", (game_nick,))
            if await cur.fetchone():
                await message.reply(texts.REGISTER_NICK_TAKEN.format(game_nick=esc(game_nick)))
                return

        await db.execute(
            "INSERT INTO registration_requests "
            "(tg_id, tg_username, game_nick, display_name, car_number) "
            "VALUES (?, ?, ?, ?, ?)",
            (tg_id, tg_username, game_nick, display_name, car_number),
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        request_id = (await cur.fetchone())[0]

    await message.reply(
        "⏳ Заявка отправлена на подтверждение админу.\n"
        f"Ник: <code>{esc(game_nick)}</code>, имя: {esc(display_name)}, номер: #{car_number}"
    )

    text = request_text(tg_id, tg_username, game_nick, display_name, car_number)
    sent = await notify_admins(message.bot, request_id, text)

    if sent == 0:
        # Никому из админов не дошло — не оставляем заявку «в тишине»
        log.error("Заявка #%s не доставлена ни одному админу", request_id)
        try:
            await message.bot.send_message(
                GROUP_ID,
                f"⚠️ Заявка #{request_id} ({esc(game_nick)}) не доставлена админам в ЛС. "
                "Админы, нажмите /start у бота, затем /pending.",
                message_thread_id=REGISTRATION_THREAD_ID or None,
            )
        except Exception as e:
            log.warning("Не удалось отправить предупреждение в группу: %s", e)


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
        await message.reply("Заявок нет.")


@router.message(Command("roster_reserves"))
async def cmd_roster_reserves(message: Message):
    if message.chat.id != GROUP_ID:
        return
    async with connect() as db:
        cur = await db.execute(
            "SELECT game_nick, display_name, car_number FROM drivers "
            "WHERE role='reserve' ORDER BY id DESC LIMIT 30"
        )
        rows = await cur.fetchall()
    if not rows:
        await message.reply("Резервов пока нет.")
        return
    lines = ["🔄 <b>Резервы (последние 30)</b>"]
    for nick, name, num in rows:
        lines.append(f"• <code>{esc(nick)}</code> — {esc(name)} (#{num})")
    await message.reply("\n".join(lines))