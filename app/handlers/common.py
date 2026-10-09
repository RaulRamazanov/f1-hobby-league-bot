import html

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, FSInputFile

from app.config import F1_PHOTO, ADMIN_IDS
from app.db import connect
from app.events import race_status_pages
from app import texts
from app.keyboards import main_menu_kb, profile_actions_kb, rules_kb

router = Router()


async def send_with_photo(message: Message, text: str, kb=None):
    if F1_PHOTO.exists():
        await message.answer_photo(photo=FSInputFile(F1_PHOTO), caption=text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


async def edit_or_send(call: CallbackQuery, text: str, kb=None):
    try:
        await call.message.edit_caption(caption=text, reply_markup=kb)
        return
    except Exception:
        pass
    try:
        await call.message.edit_text(text=text, reply_markup=kb)
        return
    except Exception:
        pass
    await call.message.answer(text=text, reply_markup=kb)


@router.message(CommandStart())
async def cmd_start(message: Message):
    async with connect() as db:
        cur = await db.execute("SELECT id FROM drivers WHERE tg_id=?", (message.from_user.id,))
        row = await cur.fetchone()
    if row:
        await _show_profile(message, row[0])
    else:
        await send_with_photo(message, texts.START, main_menu_kb())


@router.message(Command("help"))
async def cmd_help(message: Message):
    await send_with_photo(message, texts.HELP, main_menu_kb())


async def _driver_row(tg_id: int):
    async with connect() as db:
        cur = await db.execute("SELECT id FROM drivers WHERE tg_id=?", (tg_id,))
        return await cur.fetchone()


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery):
    await call.answer()
    row = await _driver_row(call.from_user.id)
    if row:
        await _show_profile(call.message, row[0], edit=True)
    else:
        await edit_or_send(call, texts.START, main_menu_kb())


@router.callback_query(F.data == "rules")
async def cb_rules(call: CallbackQuery):
    await call.answer()
    row = await _driver_row(call.from_user.id)
    await edit_or_send(call, texts.RULES, rules_kb("profile" if row else "menu"))


@router.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery):
    row = await _driver_row(call.from_user.id)
    if not row:
        await call.answer(
            "Ты ещё не зарегистрирован. Напиши анкету в теме регистрации.",
            show_alert=True,
        )
        return
    await call.answer()
    await _show_profile(call.message, row[0], edit=True)


_STATUS_ICON = {
    "pending":   ("⏳", "Ждём подтверждения"),
    "going":     ("✅", "Едет"),
    "not_going": ("❌", "Не едет"),
    "missed":    ("⌛", "Не подтвердил"),
    "queued":    ("🟢", "В очереди резервов"),
    "offered":   ("📨", "Предложено место"),
    "unused":    ("⚪", "Резерв, место не досталось"),
}


async def _show_profile(message: Message, driver_id: int, edit: bool = False):
    async with connect() as db:
        cur = await db.execute(
            "SELECT display_name, game_nick, car_number, role, team_id "
            "FROM drivers WHERE id=?",
            (driver_id,),
        )
        row = await cur.fetchone()
        if not row:
            await send_with_photo(message, "Профиль не найден.", main_menu_kb())
            return
        display_name, game_nick, car_number, role, team_id = row

        team_name = None
        if team_id:
            cur = await db.execute("SELECT name FROM teams WHERE id=?", (team_id,))
            tr = await cur.fetchone()
            team_name = tr[0] if tr else None

        cur = await db.execute(
            "SELECT id, name, reg_closed FROM races WHERE active=1 "
            "ORDER BY race_at DESC LIMIT 1"
        )
        race = await cur.fetchone()

        text = (
            f"👤 <b>{html.escape(display_name)}</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🏷 <code>{html.escape(game_nick)}</code>\n"
            f"🔑 Номер: <b>#{car_number}</b>\n"
            f"🎽 Роль: <b>{'Основной' if role == 'main' else 'Резерв'}</b>"
        )
        if team_name:
            text += f"\n🏎 Команда: <b>{html.escape(team_name)}</b>"

        status = None
        race_id = None
        closed = not bool(race)

        if race:
            race_id, race_name, closed = race
            text += f"\n\n\u200b\n🏁 <b>Текущий этап:</b> {html.escape(race_name)}"

            cur = await db.execute(
                "SELECT status, assigned_team, replaces, offer_deadline "
                "FROM entries WHERE race_id=? AND driver_id=?",
                (race_id, driver_id),
            )
            entry = await cur.fetchone()

            if entry:
                status, at, rep, dl = entry
                icon, label = _STATUS_ICON.get(status, ("•", status))
                text += f"\n{icon} <b>Статус:</b> {label}"

                if at:
                    cur = await db.execute("SELECT name FROM teams WHERE id=?", (at,))
                    tr = await cur.fetchone()
                    if tr:
                        text += f"\n🏎 <b>Команда:</b> {html.escape(tr[0])}"

                if rep:
                    cur = await db.execute(
                        "SELECT display_name FROM drivers WHERE id=?", (rep,)
                    )
                    rr = await cur.fetchone()
                    if rr:
                        text += f"\n🔁 <b>Заменяет:</b> {html.escape(rr[0])}"

                if dl and status == "offered":
                    text += f"\n⏰ <b>Ответить до:</b> {dl}"
            else:
                status = "queued" if await _in_queue(race_id, driver_id) else None
                if status == "queued":
                    text += "\n🟢 <b>Статус:</b> В очереди резервов"
        else:
            text += "\n\n\u200b\n😴 <b>Активного этапа сейчас нет</b>"

        kb = profile_actions_kb(
            role,
            status or ("pending" if role == "main" and race else None),
            closed,
        )

    if edit:
        try:
            await message.edit_caption(caption=text, reply_markup=kb)
            return
        except Exception:
            try:
                await message.edit_text(text=text, reply_markup=kb)
                return
            except Exception:
                pass
    await send_with_photo(message, text, kb)


async def _in_queue(race_id: int, driver_id: int) -> bool:
    async with connect() as db:
        cur = await db.execute(
            "SELECT 1 FROM reserve_queue WHERE race_id=? AND driver_id=?",
            (race_id, driver_id),
        )
        return bool(await cur.fetchone())


@router.message(Command("whoami"))
async def cmd_whoami(message: Message):
    uid = message.from_user.id
    await message.reply(
        f"tg_id: <code>{uid}</code>\n"
        f"В ADMIN_IDS: {uid in ADMIN_IDS}\n"
        f"Список: <code>{ADMIN_IDS}</code>"
    )


@router.message(Command("status"))
async def cmd_status(message: Message):
    pages = await race_status_pages()
    for page in pages:
        await message.reply(page, reply_markup=rules_kb("menu"))


@router.callback_query(F.data == "race:status")
async def cb_race_status(call: CallbackQuery):
    await call.answer()
    pages = await race_status_pages()
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить статус", callback_data="race:status")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu" if call.from_user.id in ADMIN_IDS else "menu")],
    ])
    await edit_or_send(call, pages[0], kb)
    for page in pages[1:]:
        await call.message.answer(page)