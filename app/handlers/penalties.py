"""Админка: штрафные баллы и ручной пропуск гонки.

Админ выбирает пилота → указывает сколько баллов (можно отрицательное число,
чтобы снять) → причину → пропустит ли пилот следующую гонку.
Пропуск можно поставить/снять и отдельной кнопкой в карточке пилота.
"""
import html
from datetime import datetime, timezone, timedelta

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import ADMIN_IDS
from app.db import connect
from app.events import safe_send, message_pages
from app.handlers.common import edit_or_send, send_with_photo
from app.race_ops import apply_ban, lift_ban, ban_info

from app import ui

router = Router()

PAGE_SIZE = 8
QUICK_POINTS = (1, 2, 3, 5, 10)
MSK = timezone(timedelta(hours=3))


class PenFlow(StatesGroup):
    points = State()
    reason = State()


def esc(v) -> str:
    return html.escape(str(v), quote=False)


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def _btn(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb)


def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _fmt_points(n: int) -> str:
    return f"+{n}" if n > 0 else str(n)


def _fmt_date(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:  # CURRENT_TIMESTAMP в SQLite — UTC
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(MSK).strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return str(value)


async def _guard(call: CallbackQuery) -> bool:
    if is_admin(call.from_user.id):
        return True
    await call.answer("Только для админов", show_alert=True)
    return False


# ----------------------------------------------------------------------
# Список пилотов
# ----------------------------------------------------------------------

async def _list_view(page: int) -> tuple[str, InlineKeyboardMarkup]:
    async with connect() as db:
        cur = await db.execute("SELECT COUNT(*) FROM drivers")
        total = (await cur.fetchone())[0]
        pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
        page = min(max(page, 0), pages - 1)
        cur = await db.execute(
            "SELECT d.id, d.display_name, d.car_number, d.role, d.ban_pending, "
            "COALESCE((SELECT SUM(points) FROM penalties p WHERE p.driver_id=d.id), 0) AS pts, "
            "EXISTS(SELECT 1 FROM entries e JOIN races r ON r.id=e.race_id "
            "       WHERE r.active=1 AND e.driver_id=d.id AND e.status='banned') "
            "FROM drivers d ORDER BY pts DESC, d.role, d.display_name LIMIT ? OFFSET ?",
            (PAGE_SIZE, page * PAGE_SIZE),
        )
        rows = await cur.fetchall()

    kb = []
    for did, name, num, role, pending, pts, banned_now in rows:
        label = f"#{num} {name}" + (" · резерв" if role == "reserve" else "")
        if pts:
            label += f" · ⚠️{pts}"
        if pending or banned_now:
            label += " · 🚫"
        kb.append([_btn(label, f"pen:d:{did}")])
    nav = []
    if page > 0:
        nav.append(_btn("⬅️", f"pen:l:{page - 1}"))
    if page < pages - 1:
        nav.append(_btn("➡️", f"pen:l:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([_btn("📋 Таблица штрафов", "pen:t")])
    kb.append([_btn("⬅️ Назад", "admin:menu")])
    text = (
        "⚠️ <b>Штрафы</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        "Выбери пилота: начислить штрафные баллы и/или поставить пропуск следующей гонки.\n"
        f"Пилотов: {total}, стр. {page + 1}/{pages}\n\n"
        "⚠️N — сумма баллов · 🚫 — пропуск гонки"
    )
    if total == 0:
        text = "⚠️ <b>Штрафы</b>\n\nПилотов пока нет."
    return text, _kb(kb)


@router.callback_query(F.data.startswith("pen:l:"))
async def cb_list(call: CallbackQuery, state: FSMContext):
    if not await _guard(call):
        return
    await call.answer()
    await state.clear()
    try:
        page = int(call.data.split(":")[2])
    except ValueError:
        page = 0
    await edit_or_send(call, *await _list_view(page))


@router.message(Command("penalties"))
async def cmd_penalties(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await send_with_photo(message, *await _list_view(0))


# ----------------------------------------------------------------------
# Таблица штрафов
# ----------------------------------------------------------------------

@router.callback_query(F.data == "pen:t")
async def cb_table(call: CallbackQuery):
    if not await _guard(call):
        return
    await call.answer()
    async with connect() as db:
        cur = await db.execute(
            "SELECT d.id, d.display_name, d.car_number, d.ban_pending, SUM(p.points) AS pts "
            "FROM drivers d JOIN penalties p ON p.driver_id=d.id "
            "GROUP BY d.id HAVING pts != 0 ORDER BY pts DESC, d.display_name"
        )
        rows = await cur.fetchall()
        cur = await db.execute(
            "SELECT d.display_name, d.car_number, d.ban_pending, "
            "EXISTS(SELECT 1 FROM entries e JOIN races r ON r.id=e.race_id "
            "       WHERE r.active=1 AND e.driver_id=d.id AND e.status='banned') AS now_banned "
            "FROM drivers d WHERE d.ban_pending>0 OR now_banned"
        )
        bans = await cur.fetchall()

    lines = ["📋 <b>Таблица штрафов</b>", "━━━━━━━━━━━━━━━━"]
    if rows:
        for i, (_, name, num, _, pts) in enumerate(rows, 1):
            lines.append(f"{i}. #{num} {esc(name)} — <b>{pts}</b>")
    else:
        lines.append("Штрафных баллов ни у кого нет.")
    if bans:
        lines += ["", "🚫 <b>Пропуск гонки</b>"]
        for name, num, pending, now_banned in bans:
            what = "текущий этап" if now_banned else "следующий этап"
            lines.append(f"• #{num} {esc(name)} — {what}")
    pages = message_pages("\n".join(lines))
    await edit_or_send(call, pages[0], _kb([[_btn("⬅️ К списку", "pen:l:0")]]))
    for page in pages[1:]:
        await call.message.answer(page)


# ----------------------------------------------------------------------
# Карточка пилота
# ----------------------------------------------------------------------

async def _card(driver_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    async with connect() as db:
        cur = await db.execute(
            "SELECT d.display_name, d.game_nick, d.car_number, d.role, t.name "
            "FROM drivers d LEFT JOIN teams t ON t.id=d.team_id WHERE d.id=?",
            (driver_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        name, nick, num, role, team = row
        cur = await db.execute(
            "SELECT COALESCE(SUM(points), 0) FROM penalties WHERE driver_id=?", (driver_id,)
        )
        total = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT points, reason, race_ban, created_at FROM penalties "
            "WHERE driver_id=? ORDER BY id DESC LIMIT 5",
            (driver_id,),
        )
        history = await cur.fetchall()
        ban = await ban_info(db, driver_id)

    text = (
        f"⚠️ <b>Штрафы · {esc(name)}</b> (<code>{esc(nick)}</code>)\n"
        "━━━━━━━━━━━━━━━━\n"
        f"🔑 Номер: <b>#{num}</b> · {'Основа' if role == 'main' else 'Резерв'}"
        + (f" · {esc(team)}" if team else "")
        + f"\n⚠️ Штрафные баллы: <b>{total}</b>"
        + f"\n🚫 Пропуск гонки: <b>{ban or 'нет'}</b>"
    )
    if history:
        text += "\n\n🗂 <b>Последние решения:</b>"
        for pts, reason, race_ban, created in history:
            item = f"\n• {_fmt_date(created)} — <b>{_fmt_points(pts)}</b>"
            if reason:
                item += f" · {esc(reason)}"
            if race_ban:
                item += " · 🚫 пропуск"
            text += item

    rows = [[_btn("➕ Начислить штрафные баллы", f"pen:add:{driver_id}")]]
    if ban:
        rows.append([_btn("✅ Снять пропуск гонки", f"pen:unban:{driver_id}")])
    else:
        rows.append([_btn("🚫 Пропуск следующей гонки", f"pen:ban:{driver_id}")])
    rows.append([_btn("⬅️ К списку", "pen:l:0")])
    return text, _kb(rows)


async def _show_card(call: CallbackQuery, driver_id: int, prefix: str = ""):
    card = await _card(driver_id)
    if not card:
        await edit_or_send(call, "Пилот не найден.", _kb([[_btn("⬅️ К списку", "pen:l:0")]]))
        return
    await edit_or_send(call, (prefix + "\n\n" if prefix else "") + card[0], card[1])


@router.callback_query(F.data.startswith("pen:d:"))
async def cb_card(call: CallbackQuery, state: FSMContext):
    if not await _guard(call):
        return
    await call.answer()
    await state.clear()
    await _show_card(call, int(call.data.split(":")[2]))


# ----------------------------------------------------------------------
# Пропуск гонки отдельной кнопкой
# ----------------------------------------------------------------------

@router.callback_query(F.data.startswith("pen:ban:"))
async def cb_ban(call: CallbackQuery):
    if not await _guard(call):
        return
    await call.answer()
    driver_id = int(call.data.split(":")[2])
    card = await _card(driver_id)
    if not card:
        await _show_card(call, driver_id)
        return
    await edit_or_send(
        call,
        card[0] + "\n\n🚫 <b>Заблокировать участие в ближайшей гонке?</b>\n"
        "• Если этап объявлен и гонка не началась — пилот пропускает его "
        "(если он уже в составе, место сразу уйдёт резерву из листа ожидания).\n"
        "• Если этапа нет или гонка уже идёт — пропуск применится к следующему Гран-при.",
        _kb([[_btn("🚫 Да, пропуск", f"pen:banyes:{driver_id}")],
             [_btn("❌ Отмена", f"pen:d:{driver_id}")]]),
    )


@router.callback_query(F.data.startswith("pen:banyes:"))
async def cb_ban_yes(call: CallbackQuery):
    if not await _guard(call):
        return
    await call.answer("Выполняю…")
    driver_id = int(call.data.split(":")[2])
    report = await apply_ban(call.bot, driver_id)
    await _show_card(call, driver_id, report)


@router.callback_query(F.data.startswith("pen:unban:"))
async def cb_unban(call: CallbackQuery):
    if not await _guard(call):
        return
    await call.answer("Выполняю…")
    driver_id = int(call.data.split(":")[2])
    report = await lift_ban(call.bot, driver_id)
    await _show_card(call, driver_id, report)


# ----------------------------------------------------------------------
# Начисление баллов: сколько → причина → пропуск гонки?
# ----------------------------------------------------------------------

@router.callback_query(F.data.startswith("pen:add:"))
async def cb_add(call: CallbackQuery, state: FSMContext):
    if not await _guard(call):
        return
    await call.answer()
    await state.clear()
    driver_id = int(call.data.split(":")[2])
    card = await _card(driver_id)
    if not card:
        await _show_card(call, driver_id)
        return
    quick = [_btn(f"+{n}", f"pen:pts:{driver_id}:{n}") for n in QUICK_POINTS]
    await edit_or_send(
        call,
        card[0] + "\n\n➕ <b>Сколько штрафных баллов начислить?</b>\n"
        "Выбери кнопку или «Другое число». Чтобы снять баллы — введи отрицательное число, например <code>-2</code>.",
        _kb([quick,
             [_btn("✏️ Другое число", f"pen:custom:{driver_id}")],
             [_btn("❌ Отмена", f"pen:d:{driver_id}")]]),
    )


@router.callback_query(F.data.startswith("pen:custom:"))
async def cb_custom(call: CallbackQuery, state: FSMContext):
    if not await _guard(call):
        return
    await call.answer()
    driver_id = int(call.data.split(":")[2])
    await state.set_state(PenFlow.points)
    await state.update_data(pen_driver=driver_id)
    await edit_or_send(
        call,
        "✏️ Пришли количество баллов целым числом (от -100 до 100, не 0).\n"
        "Например: <code>4</code> — начислить, <code>-2</code> — снять.",
        _kb([[_btn("❌ Отмена", f"pen:d:{driver_id}")]]),
    )


@router.message(PenFlow.points, F.text, ~F.text.startswith("/"))
async def msg_points(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    driver_id = data.get("pen_driver")
    if not driver_id:
        await state.clear()
        return
    try:
        points = int(message.text.strip().replace("+", "", 1).replace(" ", ""))
        if points == 0 or not -100 <= points <= 100:
            raise ValueError
    except ValueError:
        await ui.answer(message, "⚠️ Нужно целое число от -100 до 100, не 0. Попробуй ещё раз.")
        return
    await _ask_reason(message, state, driver_id, points)


@router.callback_query(F.data.startswith("pen:pts:"))
async def cb_points(call: CallbackQuery, state: FSMContext):
    if not await _guard(call):
        return
    try:
        _, _, did, pts = call.data.split(":")
        driver_id, points = int(did), int(pts)
    except ValueError:
        await call.answer("Кнопка устарела", show_alert=True)
        return
    await call.answer()
    await _ask_reason(call.message, state, driver_id, points, edit_call=call)


async def _ask_reason(message: Message, state: FSMContext, driver_id: int, points: int,
                      edit_call: CallbackQuery | None = None):
    await state.set_state(PenFlow.reason)
    await state.update_data(pen_driver=driver_id, pen_points=points, pen_reason=None)
    text = (f"📝 Баллы: <b>{_fmt_points(points)}</b>\n\n"
            "Напиши <b>причину</b> одним сообщением (например: «срезка в 1 повороте»)\n"
            "или нажми «Без причины».")
    kb = _kb([[_btn("➡️ Без причины", f"pen:noreason:{driver_id}")],
              [_btn("❌ Отмена", f"pen:d:{driver_id}")]])
    if edit_call:
        await edit_or_send(edit_call, text, kb)
    else:
        await ui.answer(message, text, reply_markup=kb)


@router.message(PenFlow.reason, F.text, ~F.text.startswith("/"))
async def msg_reason(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    if not data.get("pen_driver") or data.get("pen_points") is None:
        await state.clear()
        return
    reason = message.text.strip()[:300]
    await state.update_data(pen_reason=reason)
    await state.set_state(None)
    text, kb = _ban_question(data["pen_driver"], data["pen_points"], reason)
    await ui.answer(message, text, reply_markup=kb)


@router.callback_query(F.data.startswith("pen:noreason:"))
async def cb_no_reason(call: CallbackQuery, state: FSMContext):
    if not await _guard(call):
        return
    data = await state.get_data()
    if not data.get("pen_driver") or data.get("pen_points") is None:
        await call.answer("Форма устарела — начни заново", show_alert=True)
        return
    await call.answer()
    await state.update_data(pen_reason=None)
    await state.set_state(None)
    await edit_or_send(call, *_ban_question(data["pen_driver"], data["pen_points"], None))


def _ban_question(driver_id: int, points: int, reason: str | None):
    text = (f"Баллы: <b>{_fmt_points(points)}</b>\n"
            f"Причина: <b>{esc(reason) if reason else '—'}</b>\n\n"
            "🚫 <b>Пилот пропускает следующую гонку?</b>")
    kb = _kb([[_btn("🚫 Да, пропуск гонки", f"pen:fin:{driver_id}:1")],
              [_btn("✅ Нет, только баллы", f"pen:fin:{driver_id}:0")],
              [_btn("❌ Отмена", f"pen:d:{driver_id}")]])
    return text, kb


@router.callback_query(F.data.startswith("pen:fin:"))
async def cb_finish(call: CallbackQuery, state: FSMContext):
    if not await _guard(call):
        return
    try:
        _, _, did, ban_s = call.data.split(":")
        driver_id, race_ban = int(did), ban_s == "1"
    except ValueError:
        await call.answer("Кнопка устарела", show_alert=True)
        return
    data = await state.get_data()
    if data.get("pen_driver") != driver_id or data.get("pen_points") is None:
        await call.answer("Форма устарела — начни заново из карточки пилота", show_alert=True)
        return
    points, reason = data["pen_points"], data.get("pen_reason")
    # Сбрасываем форму до записи: повторное нажатие не задвоит штраф.
    await state.clear()
    await call.answer("Сохраняю…")

    async with connect() as db:
        cur = await db.execute("SELECT display_name, tg_id FROM drivers WHERE id=?", (driver_id,))
        drv = await cur.fetchone()
        if not drv:
            await _show_card(call, driver_id)
            return
        name, tg_id = drv
        await db.execute(
            "INSERT INTO penalties (driver_id, points, reason, race_ban, admin_id) VALUES (?, ?, ?, ?, ?)",
            (driver_id, points, reason, int(race_ban), call.from_user.id),
        )
        await db.commit()
        cur = await db.execute(
            "SELECT COALESCE(SUM(points), 0) FROM penalties WHERE driver_id=?", (driver_id,)
        )
        total = (await cur.fetchone())[0]

    if tg_id:
        if points > 0:
            msg = f"⚠️ <b>Тебе начислено штрафных баллов: {points}</b>"
        else:
            msg = f"✅ <b>С тебя снято штрафных баллов: {-points}</b>"
        if reason:
            msg += f"\nПричина: {esc(reason)}"
        msg += f"\nВсего штрафных баллов: <b>{total}</b>"
        await safe_send(call.bot, tg_id, msg)

    report = f"✅ {esc(name)}: {_fmt_points(points)} балл(ов), всего <b>{total}</b>."
    if race_ban:
        report += "\n" + await apply_ban(call.bot, driver_id)
    await _show_card(call, driver_id, report)
