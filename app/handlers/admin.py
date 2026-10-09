import html
import calendar
import secrets
from datetime import datetime, timedelta, timezone, date, time

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

from app.config import ADMIN_IDS, GROUP_ID, EVENTS_THREAD_ID, REGISTRATION_THREAD_ID
from app.db import connect
from app.events import send_race_message, notify_all_drivers, delivery_report, message_pages
from app.handlers.common import send_with_photo, edit_or_send
from app.handlers.reserve import finish_registration, registration_stats
from app.handlers.register import send_pending
from app.keyboards import (
    admin_menu_kb, admin_back_kb, role_choice_kb, team_pick_kb, confirm_close_kb,
)
from app.defaults import DEFAULT_TEAMS, car_number_error, ALLOWED_CAR_NUMBERS_TEXT
from app.parser import parse_profile
from app.services import create_driver, teams_with_counts
from app import texts

router = Router()

PROFILE_HINT = (
    "📝 Пришли анкету пилота:\n\n"
    "<code>1. Игровой ник\n"
    "2. Имя Фамилия (или Name Nick)\n"
    "3. Номер болида</code>"
)


def esc(v) -> str:
    return html.escape(str(v), quote=False)


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


class Flow(StatesGroup):
    driver_profile = State()
    race_name = State()
    race_date = State()
    race_hour = State()
    race_minute = State()
    race_deadline = State()
    race_review = State()


# ----------------------- меню -----------------------

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await send_with_photo(message, texts.ADMIN_MENU, admin_menu_kb())


@router.callback_query(F.data == "admin:menu")
async def cb_admin_menu(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await call.answer()
    await state.clear()
    await edit_or_send(call, texts.ADMIN_MENU, admin_menu_kb())


@router.callback_query(F.data == "admin:menu")
async def cb_admin_menu(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await call.answer()
    await state.clear()
    await edit_or_send(call, "🛠 <b>Админ-панель · Гран-при по шагам</b>", admin_menu_kb())


# ----------------------- /seed_teams -----------------------

@router.message(Command("seed_teams"))
async def cmd_seed_teams(message: Message):
    if not is_admin(message.from_user.id):
        return
    async with connect() as db:
        added = 0
        for name in DEFAULT_TEAMS:
            cur = await db.execute("INSERT OR IGNORE INTO teams (name) VALUES (?)", (name,))
            if cur.rowcount:
                added += 1
        await db.commit()
        cur = await db.execute("SELECT id, name FROM teams ORDER BY id")
        teams = await cur.fetchall()

    lines = [f"✅ Добавлено команд: <b>{added}</b>", "", "📋 <b>Все команды:</b>"]
    for tid, name in teams:
        lines.append(f"{tid}. {esc(name)}")
    await send_with_photo(message, "\n".join(lines), admin_menu_kb())


# ----------------------- Гран-при: мастер и управление -----------------------

MSK = timezone(timedelta(hours=3))
MONTHS = ("", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
          "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь")


def now_msk():
    return datetime.now(MSK)


def buttons(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in rows
    ])


async def allowed(event):
    if event.from_user and is_admin(event.from_user.id):
        return True
    text = "Действие доступно только администратору. В группе отправляй команды от своего аккаунта."
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
    else:
        await event.reply(text)
    return False


async def current_race():
    async with connect() as db:
        cur = await db.execute("SELECT id, name, reg_closed, started FROM races WHERE active=1 ORDER BY id DESC LIMIT 1")
        return await cur.fetchone()


async def _active_race_id():
    race = await current_race()
    return race[0] if race else None


async def choose_thread(message):
    if EVENTS_THREAD_ID:
        return EVENTS_THREAD_ID
    if message.chat.id == GROUP_ID and message.message_thread_id:
        return message.message_thread_id
    async with connect() as db:
        cur = await db.execute("SELECT thread_id FROM races WHERE thread_id IS NOT NULL ORDER BY id DESC LIMIT 1")
        row = await cur.fetchone()
    return (row[0] if row else None) or REGISTRATION_THREAD_ID or None


async def publish_race(bot, name, race_at, hours, thread_id):
    """Одна транзакция для команд и мастера. Возвращает (текст, race_id)."""
    if race_at <= now_msk():
        return "Выбранное время уже прошло. Выбери новую дату и время.", None
    if not 1 <= len(name) <= 120:
        return "Название должно содержать от 1 до 120 символов.", None
    if not 0 <= hours <= 336:
        return "Ориентир подтверждения должен быть от 0 до 336 часов.", None
    deadline = race_at - timedelta(hours=hours)
    async with connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("SELECT name FROM races WHERE active=1")
        old = await cur.fetchone()
        if old:
            return f"Уже открыт этап «{esc(old[0])}». Сначала заверши его кнопкой «Завершить этап».", None
        cur = await db.execute(
            "INSERT INTO races(name,race_at,main_deadline,thread_id) VALUES(?,?,?,?)",
            (name, race_at.isoformat(), deadline.isoformat(), thread_id),
        )
        rid = cur.lastrowid
        await db.execute("INSERT INTO entries(race_id,driver_id,status) SELECT ?,id,'pending' FROM drivers WHERE role='main'", (rid,))
        await db.commit()
    announcement = texts.RACE_OPENED.format(
        name=esc(name), race_at=race_at.strftime("%d.%m.%Y %H:%M") + " (МСК)",
        deadline=deadline.strftime("%d.%m.%Y %H:%M") + " (МСК)",
    )
    group_sent = await send_race_message(bot, rid, announcement)
    sent, failed, unlinked = await notify_all_drivers(bot, announcement)
    return f"✅ <b>{esc(name)}</b> — регистрация открыта.\n" + delivery_report(sent, failed, unlinked, group_sent), rid


async def start_wizard(message, state, actor_id):
    await state.clear()
    await state.set_state(Flow.race_name)
    await state.update_data(gp_token=secrets.token_hex(4), gp_thread=await choose_thread(message))
    await message.answer(
        f"🏁 <b>Новый Гран-при · шаг 1/5</b>\n\n<a href='tg://user?id={actor_id}'>Администратор</a>, "
        "Напиши название этапа, например: <b>Гран-при Бахрейн</b>.\n"
        "В группе ответь именно на это сообщение. Для отмены — /cancel или кнопка «Назад» в админке.",
        reply_markup=ForceReply(selective=True, input_field_placeholder="Гран-при Бахрейн"),
    )


@router.callback_query(F.data == "admin:race:new")
async def cb_new_race(call: CallbackQuery, state: FSMContext):
    if not await allowed(call):
        return
    await call.answer()
    await start_wizard(call.message, state, call.from_user.id)


@router.message(Command("open", ignore_case=True))
async def cmd_open(message: Message, state: FSMContext):
    if not await allowed(message):
        return
    args = message.text.split(maxsplit=1)
    if len(args) == 1:
        await start_wizard(message, state, message.from_user.id)
        return
    parts = [part.strip() for part in args[1].split("|")]
    try:
        if len(parts) != 3:
            raise ValueError
        name, date_string, hours_string = parts
        race_at = datetime.strptime(date_string, "%d.%m.%Y %H:%M").replace(tzinfo=MSK)
        hours = int(hours_string)
    except ValueError:
        await message.reply("Не удалось прочитать дату. Нажми «➕ Объявить Гран-при» в /admin — там всё выбирается по шагам.")
        return
    await state.clear()
    await message.reply("⏳ Создаю этап и отправляю уведомления…")
    result, _ = await publish_race(message.bot, name, race_at, hours, await choose_thread(message))
    await message.reply(result, reply_markup=admin_menu_kb())


@router.message(Command("cancel", ignore_case=True))
async def cmd_cancel(message: Message, state: FSMContext):
    if not await allowed(message):
        return
    await state.clear()
    await message.reply("Создание отменено.", reply_markup=admin_menu_kb())


def calendar_kb(year, month, token):
    prefix = f"gpw:{token}:"
    today = now_msk().date()
    rows = [[(f"{MONTHS[month]} {year}", prefix + "noop")],
            [(name, prefix + "noop") for name in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")]]
    for week in calendar.monthcalendar(year, month):
        rows.append([
            (str(day), prefix + "date:" + date(year, month, day).isoformat())
            if day and date(year, month, day) >= today else ("·", prefix + "noop")
            for day in week
        ])
    idx = year * 12 + month - 1
    current = today.year * 12 + today.month - 1
    nav = []
    if idx > current:
        y, m = divmod(idx - 1, 12)
        nav.append(("◀️", prefix + f"month:{y}-{m+1:02d}"))
    if idx < current + 12:
        y, m = divmod(idx + 1, 12)
        nav.append(("▶️", prefix + f"month:{y}-{m+1:02d}"))
    rows += [nav, [("❌ Отмена", "admin:menu")]]
    return buttons(rows)


async def wizard_view(state, stage):
    data = await state.get_data()
    token = data['gp_token']
    p = f"gpw:{token}:"
    name = esc(data['gp_name'])
    if stage == "date":
        await state.set_state(Flow.race_date)
        year, month = data.get('gp_month', (now_msk().year, now_msk().month))
        return f"📅 <b>{name} · шаг 2/5</b>\nВыбери дату гонки. Время — московское.", calendar_kb(year, month, token)
    if stage == "hour":
        await state.set_state(Flow.race_hour)
        rows = [[(f"{h:02d}", p + f"hour:{h}") for h in range(start, start+6)] for start in range(0,24,6)]
        rows += [[("⬅️ Дата", p + "back:date"), ("❌ Отмена", "admin:menu")]]
        return f"🕒 <b>{name} · шаг 3/5</b>\nДата: {date.fromisoformat(data['gp_date']):%d.%m.%Y}\nВыбери час старта (МСК).", buttons(rows)
    if stage == "minute":
        await state.set_state(Flow.race_minute)
        rows = [[(f"{m:02d}", p + f"minute:{m}") for m in range(start,start+20,5)] for start in range(0,60,20)]
        rows += [[("⬅️ Час", p + "back:hour"), ("❌ Отмена", "admin:menu")]]
        return f"🕒 <b>{name} · шаг 3/5</b>\nЧас: {data['gp_hour']:02d}\nВыбери минуты старта (МСК).", buttons(rows)
    if stage == "deadline":
        await state.set_state(Flow.race_deadline)
        rows = [[("К старту", p + "deadline:0"), ("За 1 час", p + "deadline:1"), ("За 2 часа", p + "deadline:2")],
                [("За 6 часов", p + "deadline:6"), ("За 12 часов", p + "deadline:12"), ("За сутки", p + "deadline:24")],
                [("⬅️ Время", p + "back:hour"), ("❌ Отмена", "admin:menu")]]
        return f"⏳ <b>{name} · шаг 4/5</b>\nК какому моменту попросить пилотов подтвердить участие?\nРегистрацию ты закрываешь вручную кнопкой.", buttons(rows)
    await state.set_state(Flow.race_review)
    race_at = datetime.fromisoformat(data['gp_at'])
    deadline = race_at - timedelta(hours=data['gp_hours'])
    return (
        f"🏁 <b>Проверь Гран-при · шаг 5/5</b>\n\nНазвание: <b>{name}</b>\n"
        f"📅 Старт: <b>{race_at:%d.%m.%Y %H:%M} МСК</b>\n"
        f"⏳ Подтверждение до: {deadline:%d.%m.%Y %H:%M} МСК\n\n"
        "После нажатия откроется регистрация, объявление появится в теме мероприятий, "
        "основа и резерв получат личные уведомления. Фактический старт гонки объявляется отдельно.",
        buttons([[("✅ Объявить и открыть регистрацию", p + "publish")],
                 [("✏️ Дата и время", p + "back:date"), ("❌ Отмена", "admin:menu")]]),
    )


@router.message(Flow.race_name, F.text, ~F.text.startswith("/"))
async def wizard_name(message: Message, state: FSMContext):
    if not await allowed(message):
        return
    name = message.text.strip()
    if not 1 <= len(name) <= 120 or '\n' in name:
        await message.reply("Пришли название одной строкой, не длиннее 120 символов.")
        return
    await state.update_data(gp_name=name)
    text, kb = await wizard_view(state, "date")
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("gpw:"))
async def wizard_callback(call: CallbackQuery, state: FSMContext):
    if not await allowed(call):
        return
    parts = call.data.split(":")
    data = await state.get_data()
    if len(parts) < 3 or parts[1] != data.get('gp_token') or not data.get('gp_name'):
        await call.answer("Эта форма устарела. Открой /admin и создай новую.", show_alert=True)
        return
    action = parts[2]
    value = parts[3] if len(parts) == 4 else None
    current = await state.get_state()
    expected = {"month": Flow.race_date.state, "date": Flow.race_date.state,
                "hour": Flow.race_hour.state, "minute": Flow.race_minute.state,
                "deadline": Flow.race_deadline.state, "publish": Flow.race_review.state}
    if action == 'noop':
        await call.answer()
        return
    if action in expected and current != expected[action]:
        await call.answer("Этот шаг уже пройден. Используй актуальные кнопки.", show_alert=True)
        return
    try:
        if action == 'month':
            year, month = map(int, value.split('-'))
            today = now_msk().date()
            index = year * 12 + month - 1
            base = today.year * 12 + today.month - 1
            if not 1 <= month <= 12 or not base <= index <= base + 12:
                raise ValueError
            await state.update_data(gp_month=(year, month))
            stage = 'date'
        elif action == 'date':
            selected = date.fromisoformat(value)
            if selected < now_msk().date():
                raise ValueError
            await state.update_data(gp_date=value)
            stage = 'hour'
        elif action == 'hour':
            hour = int(value)
            if not 0 <= hour <= 23:
                raise ValueError
            await state.update_data(gp_hour=hour)
            stage = 'minute'
        elif action == 'minute':
            minute = int(value)
            if minute not in range(0,60,5):
                raise ValueError
            race_at = datetime.combine(date.fromisoformat(data['gp_date']), time(data['gp_hour'],minute), tzinfo=MSK)
            if race_at <= now_msk():
                await call.answer("Это время уже прошло. Вернись к выбору часа или даты.", show_alert=True)
                return
            await state.update_data(gp_at=race_at.isoformat())
            stage = 'deadline'
        elif action == 'deadline':
            hours = int(value)
            if hours not in (0,1,2,6,12,24):
                raise ValueError
            await state.update_data(gp_hours=hours)
            stage = 'review'
        elif action == 'back':
            if value not in ('date','hour') or (value == 'hour' and 'gp_date' not in data):
                raise ValueError
            stage = value
        elif action == 'publish':
            await call.answer("Создаю этап…")
            # Сбрасываем токен до отправки: старая кнопка не запускает повторную рассылку.
            await state.clear()
            await edit_or_send(call, "⏳ Создаю Гран-при и отправляю уведомления…")
            result, _ = await publish_race(call.bot, data['gp_name'], datetime.fromisoformat(data['gp_at']),
                                         data['gp_hours'], data.get('gp_thread'))
            await edit_or_send(call, result, admin_menu_kb())
            return
        else:
            raise ValueError
    except (ValueError, TypeError, KeyError):
        await call.answer("Не удалось выбрать значение. Открой мастер заново через /admin.", show_alert=True)
        return
    await call.answer()
    text, kb = await wizard_view(state, stage)
    await edit_or_send(call, text, kb)


async def reopen_registration(bot, rid, thread_id=None):
    async with connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("SELECT name,reg_closed,started FROM races WHERE id=? AND active=1", (rid,))
        race = await cur.fetchone()
        if not race:
            return "Этот этап уже завершён. Открой актуальное меню."
        name, closed, started = race
        if started:
            return "Гонка уже началась. Повторное открытие регистрации недоступно."
        if not closed:
            return "Регистрация уже открыта."
        await db.execute("INSERT OR IGNORE INTO reserve_queue(race_id,driver_id) SELECT e.race_id,e.driver_id "
                         "FROM entries e JOIN drivers d ON d.id=e.driver_id WHERE e.race_id=? "
                         "AND d.role='reserve' AND e.status IN ('going','unused','queued')", (rid,))
        await db.execute("UPDATE entries SET status='queued',assigned_team=NULL,replaces=NULL,marked_at=NULL "
                         "WHERE race_id=? AND driver_id IN (SELECT driver_id FROM reserve_queue WHERE race_id=?)", (rid,rid))
        await db.execute("UPDATE entries SET status='pending' WHERE race_id=? AND status='missed'", (rid,))
        await db.execute("UPDATE races SET reg_closed=0,thread_id=COALESCE(?,thread_id) WHERE id=?", (thread_id,rid))
        await db.commit()
    text = f"🟢 Регистрация на <b>{esc(name)}</b> открыта повторно. Резервы возвращены в очередь, назначения отменены."
    group_sent = await send_race_message(bot,rid,text)
    sent, failed, unlinked = await notify_all_drivers(bot,text)
    return "✅ Регистрация открыта повторно.\n" + delivery_report(sent,failed,unlinked,group_sent)


async def announce_start(bot, rid=None):
    rid = rid or await _active_race_id()
    if not rid:
        return "Активного этапа нет. Нажми «➕ Объявить Гран-при»."
    async with connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("SELECT name,reg_closed,started FROM races WHERE id=? AND active=1", (rid,))
        race = await cur.fetchone()
        if not race:
            return "Этот этап уже завершён."
        name, closed, started = race
        if started:
            return "Старт этого Гран-при уже объявлен."
        if not closed:
            return "Сначала нажми «Завершить регистрацию» — бот распределит резервов."
        await db.execute("UPDATE races SET started=1 WHERE id=?", (rid,))
        await db.commit()
    text = f"🚦 <b>Гран-при начался: {esc(name)}</b>\nПилоты, собирайтесь в лобби! Состав — в разделе «Статус Гран-при»."
    group_sent = await send_race_message(bot,rid,text)
    sent,failed,unlinked = await notify_all_drivers(bot,text)
    return "✅ Старт объявлен.\n" + delivery_report(sent,failed,unlinked,group_sent)


async def run_race_action(bot, action, rid, thread_id=None):
    if action == 'close':
        return await finish_registration(bot,rid) or "Регистрация уже закрыта или этап завершён."
    if action == 'reopen':
        return await reopen_registration(bot,rid,thread_id)
    if action == 'start':
        return await announce_start(bot,rid)
    if action == 'finish':
        async with connect() as db:
            cur = await db.execute("UPDATE races SET active=0 WHERE id=? AND active=1", (rid,))
            await db.commit()
        return "🏁 Этап завершён. Теперь можно объявить следующий Гран-при." if cur.rowcount else "Этот этап уже завершён."
    return "Неизвестное действие."


async def action_prompt(call, action):
    if not await allowed(call):
        return
    await call.answer()
    race = await current_race()
    if not race:
        await edit_or_send(call,"Активного этапа нет. Нажми «➕ Объявить Гран-при».",admin_menu_kb())
        return
    rid,name,closed,started = race
    labels = {'close':'Завершить регистрацию и распределить резервов',
              'reopen':'Открыть регистрацию заново и сбросить назначения резервов',
              'start':'Объявить фактический старт гонки и уведомить всех пилотов',
              'finish':'Завершить этап'}
    text = f"🏁 <b>{esc(name)}</b>\n\n{labels[action]}?"
    if action == 'close':
        st = await registration_stats(rid)
        text += f"\n\nПодтвердили: {st['going']}. Резервов готово: {st['ready']}. Мест для распределения: {st['free']}."
    await edit_or_send(call,text,buttons([[("✅ Подтвердить",f"gpa:{action}:{rid}")],[("⬅️ Отмена","admin:menu")]]))


@router.callback_query(F.data.in_({"admin:reg:close","admin:reg:close:yes","admin:race:reopen","admin:race:start","admin:race:finish"}))
async def cb_race_action(call: CallbackQuery, state: FSMContext):
    if not await allowed(call):
        return
    await state.clear()
    action = {'admin:reg:close':'close','admin:reg:close:yes':'close','admin:race:reopen':'reopen',
              'admin:race:start':'start','admin:race:finish':'finish'}[call.data]
    await action_prompt(call,action)


@router.callback_query(F.data.startswith("gpa:"))
async def cb_execute_race_action(call: CallbackQuery):
    if not await allowed(call):
        return
    try:
        _,action,raw_id = call.data.split(':')
        rid=int(raw_id)
        if action not in ('close','reopen','start','finish'):
            raise ValueError
    except ValueError:
        await call.answer("Кнопка устарела. Открой /admin.",show_alert=True)
        return
    await call.answer("Выполняю…")
    await edit_or_send(call,"⏳ Выполняю действие, дождись результата…")
    # Обновлять тему следует лишь при явном повторном открытии из темы группы.
    thread = EVENTS_THREAD_ID or (call.message.message_thread_id if call.message.chat.id==GROUP_ID else None)
    result = await run_race_action(call.bot,action,rid,thread)
    pages=message_pages(result)
    await edit_or_send(call,pages[0],admin_menu_kb())
    for page in pages[1:]:
        await call.message.answer(page)


@router.message(Command("close","reopen","race_start","finish",ignore_case=True))
async def cmd_race_action(message: Message, state: FSMContext):
    if not await allowed(message):
        return
    await state.clear()
    command=message.text.split()[0].split('@')[0].lower().lstrip('/')
    action={'close':'close','reopen':'reopen','race_start':'start','finish':'finish'}[command]
    rid=await _active_race_id()
    if not rid:
        await message.reply("Активного этапа нет. Нажми «➕ Объявить Гран-при».",reply_markup=admin_menu_kb())
        return
    await message.reply("⏳ Выполняю действие…")
    thread=EVENTS_THREAD_ID or (message.message_thread_id if message.chat.id==GROUP_ID else None)
    result=await run_race_action(message.bot,action,rid,thread)
    for page in message_pages(result):
        await message.reply(page,reply_markup=admin_menu_kb())


@router.callback_query(F.data == "admin:team:new")
async def cb_team_new(call: CallbackQuery):
    await call.answer("В чемпионате фиксированные 11 команд. Создание команд отключено.",show_alert=True)


# ----------------------- добавить пилота (основа/резерв — одним потоком) -----------------------

@router.callback_query(F.data == "admin:driver:new")
async def cb_driver_new(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await call.answer()
    await state.set_state(Flow.driver_profile)
    await edit_or_send(call, PROFILE_HINT, admin_back_kb())


@router.message(Flow.driver_profile, F.text, ~F.text.startswith("/"))
async def driver_profile(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    parsed = parse_profile(message.text)
    if not parsed:
        await send_with_photo(
            message,
            "⚠️ Не удалось разобрать анкету. Три строки с <b>1.</b> <b>2.</b> <b>3.</b>",
            admin_back_kb(),
        )
        return
    game_nick, display_name, car_number = parsed
    error = car_number_error(car_number)
    if error:
        await send_with_photo(message, "⚠️ " + error + "\n\nДопустимые номера: " + ALLOWED_CAR_NUMBERS_TEXT, admin_back_kb())
        return
    await state.update_data(game_nick=game_nick, display_name=display_name, car_number=car_number)
    await state.set_state(None)
    await send_with_photo(
        message,
        f"👤 <b>{esc(display_name)}</b> (<code>{esc(game_nick)}</code>, #{car_number})\n\n"
        "Кем добавить пилота?",
        role_choice_kb("admin:add:reserve", "admin:add:main", "admin:menu"),
    )


@router.callback_query(F.data == "admin:add:back")
async def cb_add_back(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await call.answer()
    data = await state.get_data()
    if not data.get("game_nick"):
        await edit_or_send(call, "🛠 <b>Админ-панель · Гран-при по шагам</b>", admin_menu_kb())
        return
    await edit_or_send(
        call,
        f"👤 <b>{esc(data['display_name'])}</b> (<code>{esc(data['game_nick'])}</code>, "
        f"#{data['car_number']})\n\nКем добавить пилота?",
        role_choice_kb("admin:add:reserve", "admin:add:main", "admin:menu"),
    )


@router.callback_query(F.data == "admin:add:reserve")
async def cb_add_reserve(call: CallbackQuery, state: FSMContext):
    await _finish_add(call, state, "reserve", None)


@router.callback_query(F.data == "admin:add:main")
async def cb_add_main(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await call.answer()
    async with connect() as db:
        teams = await teams_with_counts(db)
    if not teams:
        await edit_or_send(call, "Выполни /seed_teams, чтобы восстановить 11 команд.", admin_back_kb())
        return
    await edit_or_send(
        call, "🏎 Выбери команду:", team_pick_kb(teams, "admin:add:team:", "admin:add:back")
    )


@router.callback_query(F.data.startswith("admin:add:team:"))
async def cb_add_team(call: CallbackQuery, state: FSMContext):
    await _finish_add(call, state, "main", int(call.data.split(":")[3]))


async def _finish_add(call: CallbackQuery, state: FSMContext, role: str, team_id: int | None):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    data = await state.get_data()
    if not data.get("game_nick"):
        await call.answer("Анкета потерялась, начни заново", show_alert=True)
        return

    async with connect() as db:
        ok, err = await create_driver(
            db,
            tg_id=None, tg_username=None,
            game_nick=data["game_nick"], display_name=data["display_name"],
            car_number=data["car_number"], role=role, team_id=team_id,
        )
        if not ok:
            await call.answer(err, show_alert=True)
            return
        await db.commit()
        team_name = None
        if team_id:
            cur = await db.execute("SELECT name FROM teams WHERE id=?", (team_id,))
            team_name = (await cur.fetchone())[0]

    await call.answer("Добавлен ✅")
    await state.clear()
    where = f"в <b>{esc(team_name)}</b> (основа)" if role == "main" else "в резерв"
    await edit_or_send(
        call,
        f"✅ <b>{esc(data['display_name'])}</b> (<code>{esc(data['game_nick'])}</code>, "
        f"#{data['car_number']}) добавлен {where}.",
        admin_menu_kb(),
    )


# ----------------------- заявки -----------------------

@router.callback_query(F.data == "admin:pending")
async def cb_pending(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    count = await send_pending(call.bot, call.from_user.id)
    await call.answer("Заявок нет" if count == 0 else f"Заявок: {count}")


# ----------------------- состав -----------------------

@router.callback_query(F.data == "admin:roster")
async def cb_roster(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await call.answer()
    async with connect() as db:
        cur = await db.execute("SELECT id, name FROM teams ORDER BY id")
        teams = await cur.fetchall()

        lines = ["📊 <b>Состав</b>"]
        for team_id, team_name in teams:
            cur = await db.execute(
                "SELECT display_name, game_nick FROM drivers "
                "WHERE role='main' AND team_id=? ORDER BY id",
                (team_id,),
            )
            mains = await cur.fetchall()
            text = ", ".join(f"{esc(n)} (<code>{esc(g)}</code>)" for n, g in mains) or "—"
            lines.append(f"🏎 <b>{esc(team_name)}</b>: {text}")

        cur = await db.execute(
            "SELECT display_name, game_nick FROM drivers WHERE role='reserve' ORDER BY id"
        )
        reserves = await cur.fetchall()
        lines.append("")
        text = ", ".join(f"{esc(n)} (<code>{esc(g)}</code>)" for n, g in reserves) or "—"
        lines.append(f"🔄 <b>Резервы</b>: {text}")

    await edit_or_send(call, "\n".join(lines), admin_back_kb())