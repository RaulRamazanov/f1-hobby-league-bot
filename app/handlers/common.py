import html
from datetime import datetime, timezone, timedelta

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, FSInputFile

from app.config import F1_PHOTO, ADMIN_IDS
from app.db import connect
from app.events import race_status_pages
from app import texts, ui
from app.keyboards import main_menu_kb, profile_actions_kb, rules_kb

router = Router()


async def send_with_photo(message: Message, text: str, kb=None):
    # В личке — один экран: предыдущее сообщение бота удаляется.
    if ui.is_private(message.chat.id):
        await ui.send_screen(message.bot, message.chat.id, text, kb, photo=True)
        return
    if F1_PHOTO.exists():
        await message.answer_photo(photo=FSInputFile(F1_PHOTO), caption=text, reply_markup=kb)
    else:
        await ui.answer(message, text, reply_markup=kb)


def _not_modified(exc: Exception) -> bool:
    return "not modified" in str(exc)


async def edit_or_send(call: CallbackQuery, text: str, kb=None):
    msg = call.message
    chat_id = msg.chat.id
    mid = getattr(msg, "message_id", None)
    for edit in (lambda: msg.edit_caption(caption=text, reply_markup=kb),
                 lambda: msg.edit_text(text=text, reply_markup=kb)):
        try:
            await edit()
            await ui.adopt(chat_id, mid, call.bot)
            return
        except Exception as exc:
            if _not_modified(exc):  # нажали «Обновить», а изменений нет
                return
    if ui.is_private(chat_id):
        # Не получилось отредактировать (фото ↔ текст / длинная подпись):
        # удаляем старое сообщение и присылаем новое — в чате остаётся одно.
        await ui.safe_delete(call.bot, chat_id, mid)
        await ui.send_screen(call.bot, chat_id, text, kb, photo=len(text) <= 1024)
        return
    await msg.answer(text=text, reply_markup=kb)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext = None):
    if state is not None:
        await state.clear()
    async with connect() as db:
        cur = await db.execute("SELECT id FROM drivers WHERE tg_id=?", (message.from_user.id,))
        row = await cur.fetchone()
    if row:
        await _show_profile(message, row[0])
        return
    payload = (message.text or "").split(maxsplit=1)[1:] or [""]
    if payload[0] == "reg" and state is not None:
        # Пришёл по кнопке «Зарегистрироваться в боте» из группы — сразу мастер
        from app.handlers.register import RegFlow, _step_nick, _reg_blocked
        blocked = await _reg_blocked(message.from_user.id)
        if not blocked:
            await state.set_state(RegFlow.nick)
            await send_with_photo(message, *_step_nick())
            return
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
            "Ты ещё не зарегистрирован — нажми «📝 Регистрация».",
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
    "unused":    ("⚪", "Резерв в листе ожидания (получит место, если кто-то откажется)"),
    "banned":    ("🚫", "Пропуск этапа (штраф)"),
}

# Крупная строка «едешь или нет» на главном экране
_HOME_STATUS = {
    "going":     "✅ <b>Ты едешь</b>",
    "not_going": "❌ <b>Ты не едешь</b>",
    "pending":   "⏳ <b>Подтверди участие</b> кнопкой ниже",
    "missed":    "⌛ <b>Не подтвердил — в этом этапе не едешь</b>",
    "queued":    "🟢 <b>Ты в очереди резервов</b> — ждёшь жеребьёвку",
    "unused":    "⚪ <b>Лист ожидания</b> — получишь место, если кто-то откажется",
    "banned":    "🚫 <b>Пропуск этапа (штраф)</b>",
    "offered":   "📨 <b>Тебе предложено место</b>",
}

MSK_TZ = timezone(timedelta(hours=3))


async def home_view(driver_id: int):
    """Главный экран пилота: текущий этап и едет ли он. Без данных профиля."""
    async with connect() as db:
        cur = await db.execute("SELECT role FROM drivers WHERE id=?", (driver_id,))
        drv = await cur.fetchone()
        if not drv:
            return None
        role = drv[0]
        cur = await db.execute(
            "SELECT id, name, reg_closed, started, race_at FROM races WHERE active=1 "
            "ORDER BY race_at DESC LIMIT 1"
        )
        race = await cur.fetchone()
        status = None
        closed = True
        started = False
        in_lobby = None
        if not race:
            text = ("🏁 <b>Текущий этап</b>\n━━━━━━━━━━━━━━━━\n"
                    "😴 <b>Активного этапа сейчас нет</b>\n\nКак только админ объявит Гран-при — придёт уведомление.")
        else:
            race_id, race_name, closed, started, race_at = race
            try:
                start_txt = datetime.fromisoformat(race_at).astimezone(MSK_TZ).strftime("%d.%m.%Y %H:%M") + " МСК"
            except (TypeError, ValueError):
                start_txt = str(race_at)
            phase = ("🚦 Гонка идёт" if started else "🔒 Регистрация закрыта" if closed
                     else "🟢 Регистрация открыта")
            text = (f"🏁 <b>{html.escape(race_name)}</b>\n━━━━━━━━━━━━━━━━\n"
                    f"📅 Старт: <b>{start_txt}</b>\n📌 {phase}\n\n")
            cur = await db.execute(
                "SELECT status, assigned_team, replaces FROM entries WHERE race_id=? AND driver_id=?",
                (race_id, driver_id),
            )
            entry = await cur.fetchone()
            if entry:
                status, at, rep = entry
            else:
                at = rep = None
                status = "queued" if await _in_queue(race_id, driver_id) else None
            if status is None and role == "main":
                status = "pending"
            if status:
                text += _HOME_STATUS.get(status, f"• {status}")
            elif not started:
                text += "🔄 <b>Ты резерв</b> — нажми «Готов заменить», если можешь поехать"
            else:
                text += "🔄 <b>Ты резерв</b> — в этом этапе не едешь"
            if status == "going":
                team_id = at
                if not team_id:
                    cur = await db.execute("SELECT team_id FROM drivers WHERE id=?", (driver_id,))
                    team_id = (await cur.fetchone())[0]
                cur = await db.execute("SELECT name FROM teams WHERE id=?", (team_id,))
                tr = await cur.fetchone()
                if tr:
                    text += f"\n🏎 Команда: <b>{html.escape(tr[0])}</b>"
                if rep:
                    cur = await db.execute("SELECT display_name FROM drivers WHERE id=?", (rep,))
                    rr = await cur.fetchone()
                    if rr:
                        text += f"\n🔁 Заменяешь: {html.escape(rr[0])}"
                if closed:
                    cur = await db.execute(
                        "SELECT 1 FROM lobby_checks WHERE race_id=? AND driver_id=?", (race_id, driver_id))
                    in_lobby = bool(await cur.fetchone())
                    text += "\n🎮 Лобби: " + ("в лобби ✅" if in_lobby else "не отмечен")
    kb = profile_actions_kb(role, status, bool(closed), started=bool(started),
                            has_race=bool(race), in_lobby=in_lobby)
    return text, kb


async def profile_view(driver_id: int):
    """Отдельный экран «Мой профиль»."""
    async with connect() as db:
        cur = await db.execute(
            "SELECT d.display_name, d.game_nick, d.car_number, d.role, t.name, d.ban_pending "
            "FROM drivers d LEFT JOIN teams t ON t.id=d.team_id WHERE d.id=?",
            (driver_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        display_name, game_nick, car_number, role, team_name, ban_pending = row
        cur = await db.execute(
            "SELECT COALESCE(SUM(points), 0) FROM penalties WHERE driver_id=?", (driver_id,)
        )
        penalty_points = (await cur.fetchone())[0]
    text = (
        f"👤 <b>{html.escape(display_name)}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🏷 <code>{html.escape(game_nick)}</code>\n"
        f"🔑 Номер: <b>#{car_number}</b>\n"
        f"🎽 Роль: <b>{'Основной' if role == 'main' else 'Резерв'}</b>"
    )
    if team_name:
        text += f"\n🏎 Команда: <b>{html.escape(team_name)}</b>"
    text += f"\n⚠️ Штрафные баллы: <b>{penalty_points}</b>"
    if ban_pending:
        text += "\n🚫 <b>Пропускаешь следующий этап (штраф)</b>"
    return text, rules_kb("profile")


async def _render(message: Message, text: str, kb, edit: bool):
    if edit:
        mid = getattr(message, "message_id", None)
        for do_edit in (lambda: message.edit_caption(caption=text, reply_markup=kb),
                        lambda: message.edit_text(text=text, reply_markup=kb)):
            try:
                await do_edit()
                await ui.adopt(message.chat.id, mid, message.bot)
                return
            except Exception as exc:
                if _not_modified(exc):
                    return
        if ui.is_private(message.chat.id):
            await ui.safe_delete(message.bot, message.chat.id, mid)
    await send_with_photo(message, text, kb)


async def _show_profile(message: Message, driver_id: int, edit: bool = False):
    """Главный экран (этап и статус). Имя оставлено для совместимости с другими модулями."""
    view = await home_view(driver_id)
    if not view:
        await send_with_photo(message, "Профиль не найден.", main_menu_kb())
        return
    await _render(message, *view, edit)


@router.callback_query(F.data == "me")
async def cb_me(call: CallbackQuery):
    row = await _driver_row(call.from_user.id)
    if not row:
        await call.answer("Ты ещё не зарегистрирован — нажми «📝 Регистрация».", show_alert=True)
        return
    await call.answer()
    view = await profile_view(row[0])
    if view:
        await _render(call.message, *view, True)


async def refresh_home_screens(bot) -> int:
    """Обновляет главный экран у всех пилотов (например, после «Завершить этап»),
    чтобы старые статусы «Едет / Готов заменить» исчезли."""
    async with connect() as db:
        cur = await db.execute(
            "SELECT d.id, u.chat_id, u.message_id FROM drivers d "
            "JOIN ui_screens u ON u.chat_id=d.tg_id"
        )
        rows = await cur.fetchall()
    updated = 0
    for driver_id, chat_id, message_id in rows:
        view = await home_view(driver_id)
        if not view:
            continue
        text, kb = view
        for edit in (lambda: bot.edit_message_caption(chat_id=chat_id, message_id=message_id,
                                                      caption=text, reply_markup=kb),
                     lambda: bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id,
                                                   reply_markup=kb)):
            try:
                await edit()
                updated += 1
                break
            except Exception as exc:
                if _not_modified(exc):
                    break
    return updated


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
    await ui.reply(message, 
        f"tg_id: <code>{uid}</code>\n"
        f"В ADMIN_IDS: {uid in ADMIN_IDS}\n"
        f"Список: <code>{ADMIN_IDS}</code>"
    )


@router.message(Command("status"))
async def cmd_status(message: Message):
    pages = await race_status_pages()
    await ui.reply(message, pages[0], reply_markup=rules_kb("menu"))
    for page in pages[1:]:
        await message.answer(page)


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