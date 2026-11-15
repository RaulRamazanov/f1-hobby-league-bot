"""Админ: замены в составе после закрытия регистрации.

Пример: за 30 минут до гонки резерв, получивший место, не отвечает —
админ выбирает его и запускает новый розыгрыш на это место
или ставит конкретного резерва из листа ожидания.
"""
import html

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.config import ADMIN_IDS
from app.db import connect
from app.handlers.common import edit_or_send
from app.race_ops import active_race, standby_list, admin_replace, pickable_reserves, final_checks_map

router = Router()
PAGE_SIZE = 8


def esc(v) -> str:
    return html.escape(str(v), quote=False)


def _btn(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb)


def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


BACK = [_btn("⬅️ Назад", "admin:menu")]


async def _guard(call: CallbackQuery) -> bool:
    if call.from_user.id in ADMIN_IDS:
        return True
    await call.answer("Только для админов", show_alert=True)
    return False


async def _list_view():
    async with connect() as db:
        race = await active_race(db)
        if not race:
            return "🔀 <b>Замены в составе</b>\n\nАктивного этапа нет.", _kb([BACK])
        race_id, race_name, closed, started = race
        if not closed:
            return ("🔀 <b>Замены в составе</b>\n\nРегистрация ещё открыта. "
                    "Замены доступны после «Завершить регистрацию»."), _kb([BACK])
        cur = await db.execute(
            "SELECT d.id, d.display_name, d.car_number, t.name FROM entries e "
            "JOIN drivers d ON d.id=e.driver_id "
            "LEFT JOIN teams t ON t.id=COALESCE(e.assigned_team, d.team_id) "
            "WHERE e.race_id=? AND e.status='going' ORDER BY t.id, d.car_number",
            (race_id,),
        )
        going = await cur.fetchall()
        standby = await standby_list(db, race_id)
        cur = await db.execute("SELECT reminder_sent FROM races WHERE id=?", (race_id,))
        reminder = (await cur.fetchone())[0]
        answers = await final_checks_map(db, race_id)
    marks = {"yes": "✅ ", "no": "❌ "}
    rows = [[_btn(f"{marks.get(answers.get(did), '⏳ ' if reminder else '')}#{num} {name} · {team or '—'}",
                  f"sw:d:{did}")] for did, name, num, team in going]
    rows.append([_btn("🔄 Обновить", "sw:l")])
    rows.append(BACK)
    text = (f"🔀 <b>Замены в составе · {esc(race_name)}</b>\n"
            "━━━━━━━━━━━━━━━━\n"
            f"В составе: <b>{len(going)}</b> · в листе ожидания: <b>{len(standby)}</b>\n\n"
            "Выбери участника, чтобы снять его с этапа и отдать место "
            "конкретному резерву или разыграть."
            + ("\n\n✅ подтвердил за час · ❌ не сможет · ⏳ не ответил" if reminder else ""))
    return text, _kb(rows)


@router.callback_query(F.data == "sw:l")
async def cb_list(call: CallbackQuery):
    if not await _guard(call):
        return
    await call.answer()
    await edit_or_send(call, *await _list_view())


@router.callback_query(F.data.startswith("sw:d:"))
async def cb_card(call: CallbackQuery):
    if not await _guard(call):
        return
    await call.answer()
    driver_id = int(call.data.split(":")[2])
    async with connect() as db:
        race = await active_race(db)
        if not race:
            await edit_or_send(call, *await _list_view())
            return
        cur = await db.execute(
            "SELECT d.display_name, d.game_nick, d.car_number, t.name, r.display_name, e.status, d.tg_id "
            "FROM entries e JOIN drivers d ON d.id=e.driver_id "
            "LEFT JOIN teams t ON t.id=COALESCE(e.assigned_team, d.team_id) "
            "LEFT JOIN drivers r ON r.id=e.replaces "
            "WHERE e.race_id=? AND e.driver_id=?",
            (race[0], driver_id),
        )
        row = await cur.fetchone()
        pool = await standby_list(db, race[0])
    if not row or row[5] != "going":
        await edit_or_send(call, "Этот пилот уже не в составе.", _kb([[_btn("⬅️ К списку", "sw:l")]]))
        return
    name, nick, num, team, replaces, _, tg_id = row
    who = f"<a href='tg://user?id={tg_id}'>{esc(name)}</a>" if tg_id else esc(name)
    text = (f"🔀 <b>#{num} {who}</b> (<code>{esc(nick)}</code>)\n"
            f"🏎 Команда: <b>{esc(team or '—')}</b>"
            + (f"\n🔁 Заменяет: {esc(replaces)}" if replaces else "")
            + "\n\nЧто сделать с этим местом?")
    rows = [
        [_btn(f"🎲 Разыграть место ({len(pool)} в ожидании)", f"sw:rnd:{driver_id}")],
        [_btn("👤 Отдать конкретному резерву", f"sw:pick:{driver_id}:0")],
        [_btn("🚫 Снять без замены", f"sw:rm:{driver_id}")],
        [_btn("⬅️ К списку", "sw:l")],
    ]
    await edit_or_send(call, text, _kb(rows))


@router.callback_query(F.data.startswith("sw:rnd:"))
async def cb_random(call: CallbackQuery):
    if not await _guard(call):
        return
    await call.answer()
    driver_id = int(call.data.split(":")[2])
    await edit_or_send(
        call,
        "🎲 <b>Снять участника и разыграть его место заново?</b>\n\n"
        "Место случайно получит резерв из листа ожидания, ему придёт сообщение "
        "с фото и напоминанием про телеметрию. Снятый участник получит уведомление.",
        _kb([[_btn("✅ Да, новый розыгрыш", f"sw:rndy:{driver_id}")],
             [_btn("❌ Отмена", f"sw:d:{driver_id}")]]),
    )


@router.callback_query(F.data.startswith("sw:rndy:"))
async def cb_random_yes(call: CallbackQuery):
    if not await _guard(call):
        return
    await call.answer("Разыгрываю…")
    report = await admin_replace(call.bot, int(call.data.split(":")[2]), mode="random")
    await edit_or_send(call, report, _kb([[_btn("⬅️ К списку", "sw:l")], BACK]))


@router.callback_query(F.data.startswith("sw:pick:"))
async def cb_pick(call: CallbackQuery):
    if not await _guard(call):
        return
    await call.answer()
    _, _, did, page_s = call.data.split(":")
    driver_id, page = int(did), int(page_s)
    async with connect() as db:
        race = await active_race(db)
        pool = await pickable_reserves(db, race[0]) if race else []
    pages = max((len(pool) + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(max(page, 0), pages - 1)
    chunk = pool[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    rows = [[_btn(f"{'🟢 ' if ready else ''}#{num} {name}", f"sw:set:{driver_id}:{rid}")]
            for rid, name, num, ready in chunk]
    nav = []
    if page > 0:
        nav.append(_btn("⬅️", f"sw:pick:{driver_id}:{page - 1}"))
    if page < pages - 1:
        nav.append(_btn("➡️", f"sw:pick:{driver_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_btn("❌ Отмена", f"sw:d:{driver_id}")])
    text = ("👤 <b>Кого поставить на это место?</b>\n🟢 — готов (в листе ожидания), "
            "остальные — все свободные резервы."
            if pool else "👤 Свободных резервов нет.")
    await edit_or_send(call, text, _kb(rows))


@router.callback_query(F.data.startswith("sw:set:"))
async def cb_set(call: CallbackQuery):
    if not await _guard(call):
        return
    await call.answer()
    _, _, did, rid = call.data.split(":")
    async with connect() as db:
        cur = await db.execute("SELECT id, display_name FROM drivers WHERE id IN (?, ?)", (int(did), int(rid)))
        names = dict(await cur.fetchall())
    await edit_or_send(
        call,
        f"👤 Заменить <b>{esc(names.get(int(did), '?'))}</b> на <b>{esc(names.get(int(rid), '?'))}</b>?",
        _kb([[_btn("✅ Да, заменить", f"sw:sety:{did}:{rid}")],
             [_btn("❌ Отмена", f"sw:d:{did}")]]),
    )


@router.callback_query(F.data.startswith("sw:sety:"))
async def cb_set_yes(call: CallbackQuery):
    if not await _guard(call):
        return
    await call.answer("Заменяю…")
    _, _, did, rid = call.data.split(":")
    report = await admin_replace(call.bot, int(did), int(rid), mode="pick")
    await edit_or_send(call, report, _kb([[_btn("⬅️ К списку", "sw:l")], BACK]))


@router.callback_query(F.data.startswith("sw:rm:"))
async def cb_remove(call: CallbackQuery):
    if not await _guard(call):
        return
    await call.answer()
    driver_id = int(call.data.split(":")[2])
    await edit_or_send(
        call,
        "🚫 <b>Снять участника с этапа без замены?</b>\n\nМесто останется свободным. "
        "Если позже резерв нажмёт «Готов заменить» — получит его автоматически.",
        _kb([[_btn("✅ Да, снять", f"sw:rmy:{driver_id}")],
             [_btn("❌ Отмена", f"sw:d:{driver_id}")]]),
    )


@router.callback_query(F.data.startswith("sw:rmy:"))
async def cb_remove_yes(call: CallbackQuery):
    if not await _guard(call):
        return
    await call.answer("Снимаю…")
    report = await admin_replace(call.bot, int(call.data.split(":")[2]), mode="remove")
    await edit_or_send(call, report, _kb([[_btn("⬅️ К списку", "sw:l")], BACK]))
