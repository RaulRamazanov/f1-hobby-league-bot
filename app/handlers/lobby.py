"""Проверка лобби: админ рассылает участникам этапа (status='going') сообщение
с кнопкой «Я в лобби ✅» и видит, кто отметился.

Админка: кнопка «📣 Проверка лобби» или команда /lobby.
"""
import asyncio
import html
from datetime import datetime, timezone, timedelta

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import ADMIN_IDS
from app.db import connect
from app.events import safe_send, message_pages, service_to_admins
from app.handlers.common import edit_or_send

from app import ui

router = Router()
MSK = timezone(timedelta(hours=3))

LOBBY_TEXT = (
    "🏁 <b>{race_name}</b>\n"
    "━━━━━━━━━━━━━━━━\n"
    "🎮 Если ты сейчас <b>в лобби и ждёшь гонку</b> — нажми кнопку ниже 👇\n\n"
    "Так админы увидят, что ты на месте."
)


def esc(v) -> str:
    return html.escape(str(v), quote=False)


def _btn(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb)


def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _race_and_participants():
    """(race_id, name, lobby_sent_at), [(driver_id, name, nick, number, tg_id, checked)]"""
    async with connect() as db:
        cur = await db.execute(
            "SELECT id, name, lobby_sent_at FROM races WHERE active=1 ORDER BY id DESC LIMIT 1"
        )
        race = await cur.fetchone()
        if not race:
            return None, []
        cur = await db.execute(
            "SELECT d.id, d.display_name, d.game_nick, d.car_number, d.tg_id, "
            "EXISTS(SELECT 1 FROM lobby_checks lc WHERE lc.race_id=e.race_id AND lc.driver_id=d.id) "
            "FROM entries e JOIN drivers d ON d.id=e.driver_id "
            "WHERE e.race_id=? AND e.status='going' ORDER BY d.car_number",
            (race[0],),
        )
        return race, await cur.fetchall()


async def _admin_view() -> tuple[str, InlineKeyboardMarkup]:
    race, people = await _race_and_participants()
    back = [_btn("⬅️ Назад", "admin:menu")]
    if not race:
        return "📣 <b>Проверка лобби</b>\n\nАктивного этапа нет.", _kb([back])
    race_id, race_name, sent_at = race
    if not people:
        return (
            f"📣 <b>Проверка лобби · {esc(race_name)}</b>\n\n"
            "В составе пока никого нет. Сначала заверши регистрацию — "
            "рассылка уходит только участникам этапа.",
            _kb([[_btn("🔄 Обновить", "lobby:admin")], back]),
        )
    checked = [p for p in people if p[5]]
    waiting = [p for p in people if not p[5]]
    lines = [
        f"📣 <b>Проверка лобби · {esc(race_name)}</b>",
        "━━━━━━━━━━━━━━━━",
        f"✅ В лобби: <b>{len(checked)}/{len(people)}</b>",
    ]
    if sent_at:
        try:
            when = datetime.fromisoformat(sent_at).astimezone(MSK).strftime("%H:%M")
        except ValueError:
            when = sent_at
        lines.append(f"📨 Рассылка отправлена в {when} МСК")
    else:
        lines.append("📨 Рассылка ещё не отправлялась")
    if waiting:
        lines += ["", f"❌ <b>Ещё НЕ в лобби ({len(waiting)}):</b>"]
        for p in waiting:
            who = f"<a href='tg://user?id={p[4]}'>{esc(p[1])}</a>" if p[4] else f"{esc(p[1])} (без Telegram)"
            lines.append(f"• #{p[3]} {who} <code>{esc(p[2])}</code>")
    else:
        lines += ["", "🎉 <b>Все участники в лобби!</b>"]
    if checked:
        lines += ["", f"✅ <b>В лобби ({len(checked)}):</b>"]
        lines += [f"• #{p[3]} {esc(p[1])} <code>{esc(p[2])}</code>" for p in checked]
    lines += ["", f"🕐 <i>Обновлено: {datetime.now(MSK):%H:%M:%S} МСК</i>"]

    rows = []
    if not sent_at:
        rows.append([_btn(f"📣 Разослать участникам ({len(people)})", "lobby:send")])
    else:
        if waiting:
            rows.append([_btn(f"🔔 Напомнить неотметившимся ({len(waiting)})", "lobby:remind")])
        rows.append([_btn("📣 Новая проверка (сбросить отметки)", "lobby:reset")])
    rows.append([_btn("🔄 Обновить", "lobby:admin")])
    rows.append(back)
    return "\n".join(lines), _kb(rows)


async def _send_lobby(bot, only_unchecked: bool) -> tuple[int, int, int]:
    race, people = await _race_and_participants()
    if not race:
        return 0, 0, 0
    race_id, race_name, _ = race
    async with connect() as db:
        if not only_unchecked:
            await db.execute("DELETE FROM lobby_checks WHERE race_id=?", (race_id,))
        await db.execute("UPDATE races SET lobby_sent_at=? WHERE id=?",
                         (datetime.now(timezone.utc).isoformat(), race_id))
        await db.commit()
    targets = [p for p in people if not (only_unchecked and p[5])]
    sent = failed = no_tg = 0
    text = LOBBY_TEXT.format(race_name=esc(race_name))
    for driver_id, _, _, _, tg_id, _ in targets:
        if not tg_id:
            no_tg += 1
            continue
        kb = _kb([[_btn("Я в лобби ✅", f"lobby:in:{race_id}:{driver_id}")]])
        if await safe_send(bot, tg_id, text, reply_markup=kb):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)
    return sent, failed, no_tg


def _report(sent: int, failed: int, no_tg: int) -> str:
    text = f"📨 Отправлено: <b>{sent}</b>"
    if failed:
        text += f", не доставлено: <b>{failed}</b> (пилот не нажимал /start у бота)"
    if no_tg:
        text += f", без Telegram: <b>{no_tg}</b>"
    return text


async def _show_in_place(call: CallbackQuery, text: str, kb) -> None:
    """Обновляет экран лобби в том же сообщении. Если исходное сообщение — фото
    админ-меню (подпись до 1024 символов), один раз присылает текстовое сообщение,
    дальше «Обновить» правит уже его."""
    text = message_pages(text)[0]
    try:
        await call.message.edit_text(text, reply_markup=kb)
        return
    except Exception as exc:
        if "not modified" in str(exc):
            return
    if len(text) <= 1024:
        try:
            await call.message.edit_caption(caption=text, reply_markup=kb)
            return
        except Exception as exc:
            if "not modified" in str(exc):
                return
    if ui.is_private(call.message.chat.id):
        await ui.safe_delete(call.bot, call.message.chat.id, getattr(call.message, "message_id", None))
        await ui.send_screen(call.bot, call.message.chat.id, text, kb)
        return
    await call.message.answer(text, reply_markup=kb)


# ----------------------------------------------------------------------
# Админ
# ----------------------------------------------------------------------

@router.callback_query(F.data == "lobby:admin")
async def cb_lobby_admin(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Только для админов", show_alert=True)
        return
    await call.answer("Обновлено")
    await _show_in_place(call, *await _admin_view())


@router.message(Command("lobby"))
async def cmd_lobby(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    text, kb = await _admin_view()
    await ui.answer(message, message_pages(text)[0], reply_markup=kb)


@router.callback_query(F.data == "lobby:reset")
async def cb_lobby_reset(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Только для админов", show_alert=True)
        return
    await call.answer()
    await _show_in_place(
        call,
        "📣 <b>Новая проверка лобби?</b>\n\nВсе отметки «Я в лобби» сбросятся, "
        "и сообщение с кнопкой снова уйдёт всем участникам этапа.",
        _kb([[_btn("✅ Да, разослать заново", "lobby:send")],
             [_btn("⬅️ Отмена", "lobby:admin")]]),
    )


@router.callback_query(F.data.in_({"lobby:send", "lobby:remind"}))
async def cb_lobby_send(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Только для админов", show_alert=True)
        return
    await call.answer("Рассылаю…")
    sent, failed, no_tg = await _send_lobby(call.bot, only_unchecked=call.data == "lobby:remind")
    text, kb = await _admin_view()
    await _show_in_place(call, _report(sent, failed, no_tg) + "\n\n" + text, kb)


# ----------------------------------------------------------------------
# Пилот: «Я в лобби ✅» / «Я вылетел ❌» — можно нажимать сколько угодно
# ----------------------------------------------------------------------

def lobby_toggle_kb(race_id: int, driver_id: int, inside: bool) -> InlineKeyboardMarkup:
    if inside:
        return _kb([[_btn("❌ Я вылетел / не в лобби", f"lobby:out:{race_id}:{driver_id}")]])
    return _kb([[_btn("Я в лобби ✅", f"lobby:in:{race_id}:{driver_id}")]])


async def set_lobby(bot, tg_id: int, inside: bool, race_id: int | None = None,
                    driver_id: int | None = None) -> tuple[str, dict | None]:
    """Отмечает/снимает отметку. Возвращает (текст всплывашки, info|None)."""
    async with connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        if race_id is None:
            cur = await db.execute(
                "SELECT d.id, r.id FROM drivers d JOIN races r ON r.active=1 WHERE d.tg_id=? "
                "ORDER BY r.id DESC LIMIT 1", (tg_id,))
            row = await cur.fetchone()
            if not row:
                return "Нет активного этапа", None
            driver_id, race_id = row
        cur = await db.execute(
            "SELECT d.tg_id, d.display_name, d.car_number, r.active, r.name, e.status FROM drivers d "
            "JOIN races r ON r.id=? "
            "LEFT JOIN entries e ON e.race_id=r.id AND e.driver_id=d.id WHERE d.id=?",
            (race_id, driver_id),
        )
        row = await cur.fetchone()
        if not row or row[0] != tg_id:
            return "Эта кнопка не для тебя", None
        _, name, number, active, race_name, status = row
        if not active:
            return "Этот этап уже завершён", None
        if status != "going":
            return "Ты сейчас не в составе этого этапа", None
        if inside:
            cur = await db.execute(
                "INSERT OR IGNORE INTO lobby_checks (race_id, driver_id) VALUES (?, ?)", (race_id, driver_id))
        else:
            cur = await db.execute(
                "DELETE FROM lobby_checks WHERE race_id=? AND driver_id=?", (race_id, driver_id))
        changed = cur.rowcount > 0
        cur = await db.execute(
            "SELECT COUNT(*), SUM(EXISTS(SELECT 1 FROM lobby_checks lc "
            "WHERE lc.race_id=e.race_id AND lc.driver_id=e.driver_id)) "
            "FROM entries e WHERE e.race_id=? AND e.status='going'",
            (race_id,),
        )
        total, done = await cur.fetchone()
        await db.commit()
    info = {"race_id": race_id, "driver_id": driver_id, "name": name, "number": number,
            "race_name": race_name, "changed": changed, "total": total or 0, "done": done or 0}
    return ("Отмечено: ты в лобби ✅" if inside else "Отмечено: ты не в лобби ❌"), info


async def _after_toggle(bot, info: dict, inside: bool) -> None:
    """Сообщения админам — служебные: удаляются после «Завершить этап»."""
    if not info["changed"]:
        return
    if not inside:
        await service_to_admins(bot, info["race_id"],
                                f"❌ <b>#{info['number']} {esc(info['name'])}</b> вылетел / не в лобби "
                                f"({esc(info['race_name'])}: в лобби {info['done']}/{info['total']}).")
    elif info["total"] and info["done"] == info["total"]:
        await service_to_admins(bot, info["race_id"],
                                f"✅ <b>{esc(info['race_name'])}</b>: все участники в лобби "
                                f"({info['done']}/{info['total']}).")


def _pilot_text(race_name: str, inside: bool) -> str:
    if inside:
        return (f"🏁 <b>{esc(race_name)}</b>\n✅ Ты отмечен: <b>в лобби</b>. Удачной гонки!\n\n"
                "Если вылетел из лобби — нажми кнопку ниже, админы увидят.")
    return (f"🏁 <b>{esc(race_name)}</b>\n❌ Ты отмечен: <b>не в лобби</b>.\n\n"
            "Как зайдёшь обратно — нажми «Я в лобби ✅».")


@router.callback_query(F.data.startswith("lobby:in:") | F.data.startswith("lobby:out:"))
async def cb_lobby_toggle(call: CallbackQuery):
    try:
        _, action, race_s, driver_s = call.data.split(":")
        race_id, driver_id = int(race_s), int(driver_s)
    except ValueError:
        await call.answer("Кнопка устарела", show_alert=True)
        return
    inside = action == "in"
    text, info = await set_lobby(call.bot, call.from_user.id, inside, race_id, driver_id)
    await call.answer(text, show_alert=info is None)
    if not info:
        return
    try:
        await call.message.edit_text(_pilot_text(info["race_name"], inside),
                                     reply_markup=lobby_toggle_kb(race_id, driver_id, inside))
    except Exception:
        pass
    await _after_toggle(call.bot, info, inside)


@router.callback_query(F.data.in_({"lobby:me:in", "lobby:me:out"}))
async def cb_lobby_from_profile(call: CallbackQuery):
    """Кнопка в профиле участника — работает и без рассылки админа."""
    from app.handlers.common import _show_profile
    inside = call.data == "lobby:me:in"
    text, info = await set_lobby(call.bot, call.from_user.id, inside)
    await call.answer(text, show_alert=info is None)
    if info:
        await _show_profile(call.message, info["driver_id"], edit=True)
        await _after_toggle(call.bot, info, inside)
