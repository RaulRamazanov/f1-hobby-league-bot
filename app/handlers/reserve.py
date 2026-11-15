import html
import logging
import random
from datetime import datetime, timezone

from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery

from app.config import ADMIN_IDS
from app.db import connect
from app.defaults import DEFAULT_TEAMS, GRID_CAPACITY, MAX_MAINS_PER_TEAM
from app.events import send_race_message, safe_send, notify_going_drivers, answer_or_message, service_to_admins
from app.handlers.common import _show_profile
from app.race_ops import join_standby

log = logging.getLogger(__name__)

router = Router()


def esc(v) -> str:
    return html.escape(str(v), quote=False)


@router.callback_query(F.data == "reserve:ready")
async def cb_reserve_ready(call: CallbackQuery):
    async with connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT d.id, d.display_name, r.id, r.name, r.reg_closed, r.started FROM drivers d "
            "JOIN races r ON r.active=1 WHERE d.tg_id=? AND d.role='reserve'",
            (call.from_user.id,),
        )
        row = await cur.fetchone()
        if not row:
            await call.answer("Нет активного этапа или ты не резерв", show_alert=True)
            return
        driver_id, name, race_id, race_name, closed, started = row
        cur = await db.execute(
            "SELECT status FROM entries WHERE race_id=? AND driver_id=?", (race_id, driver_id)
        )
        entry = await cur.fetchone()
        status = entry[0] if entry else None
        if status == "banned":
            await call.answer("Ты пропускаешь этот этап по решению админа (штраф)", show_alert=True)
            return
        if closed:
            await db.rollback()
            if started:
                await call.answer("Гонка уже началась", show_alert=True)
                return
            if status == "going":
                await call.answer("Ты уже в составе этого этапа ✅", show_alert=True)
                return
            if status == "unused":
                await call.answer("Ты уже в листе ожидания 🟢")
                await _show_profile(call.message, driver_id, edit=True)
                return
            # Регистрация закрыта, гонка не началась: встаём в лист ожидания.
            assignments = await join_standby(call.bot, driver_id, race_id, race_name, name)
            got = any(a["driver_id"] == driver_id for a in assignments)
            await answer_or_message(
                call,
                "Тебе досталось свободное место! 🏎" if got
                else "Ты в листе ожидания: если кто-то откажется — место может стать твоим 🟢",
                show_alert=got,
            )
            await _show_profile(call.message, driver_id, edit=True)
            return
        cur = await db.execute(
            "INSERT OR IGNORE INTO reserve_queue (race_id, driver_id) VALUES (?, ?)",
            (race_id, driver_id),
        )
        added = cur.rowcount > 0
        await db.execute(
            "INSERT INTO entries (race_id, driver_id, status) VALUES (?, ?, 'queued') "
            "ON CONFLICT(race_id, driver_id) DO UPDATE SET status='queued', "
            "assigned_team=NULL, replaces=NULL, offer_deadline=NULL",
            (race_id, driver_id),
        )
        await db.commit()
    await call.answer("Ты в списке готовых заменить 🟢")
    if added:
        await send_race_message(call.bot, race_id,
                                f"🟢 Резерв <b>{esc(name)}</b> готов заменить на <b>{esc(race_name)}</b>.", service=True)
    await _show_profile(call.message, driver_id, edit=True)


@router.callback_query(F.data == "reserve:cancel")
async def cb_reserve_cancel(call: CallbackQuery):
    async with connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT d.id, d.display_name, r.id, r.name, r.reg_closed, r.started FROM drivers d "
            "JOIN races r ON r.active=1 WHERE d.tg_id=? AND d.role='reserve'",
            (call.from_user.id,),
        )
        row = await cur.fetchone()
        if not row:
            await call.answer("Нет активного этапа", show_alert=True)
            return
        driver_id, name, race_id, race_name, closed, started = row
        if closed:
            cur = await db.execute(
                "SELECT status FROM entries WHERE race_id=? AND driver_id=?", (race_id, driver_id)
            )
            entry = await cur.fetchone()
            status = entry[0] if entry else None
            if started or status != "unused":
                await call.answer(
                    "Ты уже в составе — чтобы отказаться, нажми «Отказаться от участия» в профиле"
                    if status == "going" and not started else "Регистрация завершена",
                    show_alert=True,
                )
                return
            # Выход из листа ожидания после закрытия регистрации
            await db.execute(
                "UPDATE entries SET status='not_going' WHERE race_id=? AND driver_id=? AND status='unused'",
                (race_id, driver_id),
            )
            await db.commit()
            await call.answer("Ты вышел из листа ожидания")
            await send_race_message(call.bot, race_id,
                                    f"⚪ Резерв <b>{esc(name)}</b> вышел из листа ожидания на <b>{esc(race_name)}</b>.", service=True)
            await _show_profile(call.message, driver_id, edit=True)
            return
        cur = await db.execute(
            "DELETE FROM reserve_queue WHERE race_id=? AND driver_id=?", (race_id, driver_id)
        )
        removed = cur.rowcount > 0
        await db.execute(
            "UPDATE entries SET status='not_going' WHERE race_id=? AND driver_id=? AND status='queued'",
            (race_id, driver_id),
        )
        await db.commit()
    await call.answer("Готовность снята")
    if removed:
        await send_race_message(call.bot, race_id,
                                f"⚪ Резерв <b>{esc(name)}</b> снял готовность на <b>{esc(race_name)}</b>.", service=True)
    await _show_profile(call.message, driver_id, edit=True)


async def registration_stats(race_id: int) -> dict:
    async with connect() as db:
        async def count(q: str) -> int:
            cur = await db.execute(q, (race_id,))
            return (await cur.fetchone())[0]
        base = ("SELECT COUNT(*) FROM entries e JOIN drivers d ON d.id=e.driver_id "
                "WHERE e.race_id=? AND d.role='main' AND e.status")
        going = await count(base + "='going'")
        return {
            "going": going,
            "pending": await count(base + "='pending'"),
            "not_going": await count(base + "='not_going'"),
            "ready": await count("SELECT COUNT(*) FROM reserve_queue WHERE race_id=?"),
            "free": max(0, GRID_CAPACITY - going),
        }


async def finish_registration(bot: Bot, race_id: int) -> str | None:
    """Распределяет все места сетки: замены и незанятые кресла в 11 командах.

    BEGIN IMMEDIATE защищает жеребьёвку от двойного закрытия и одновременных ответов.
    """
    async with connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("SELECT name, reg_closed FROM races WHERE id=? AND active=1", (race_id,))
        race = await cur.fetchone()
        if not race or race[1]:
            return None
        race_name = race[0]
        # Для старых баз: основной без entry тоже считается не подтвердившим.
        await db.execute(
            "INSERT OR IGNORE INTO entries (race_id, driver_id, status) "
            "SELECT ?, id, 'pending' FROM drivers WHERE role='main'", (race_id,)
        )
        await db.execute(
            "UPDATE entries SET status='missed' WHERE race_id=? AND status='pending' "
            "AND driver_id IN (SELECT id FROM drivers WHERE role='main')", (race_id,)
        )
        cur = await db.execute("SELECT id, name FROM teams ORDER BY id")
        teams = [t for t in await cur.fetchall() if t[1] in DEFAULT_TEAMS]
        if len(teams) != len(DEFAULT_TEAMS):
            return "⚠️ В базе должны быть все 11 команд. Выполни /seed_teams и повтори закрытие."
        cur = await db.execute(
            "SELECT d.id, d.display_name, d.tg_id, COALESCE(e.assigned_team,d.team_id), d.role, e.status "
            "FROM entries e JOIN drivers d ON d.id=e.driver_id WHERE e.race_id=?", (race_id,)
        )
        entries = await cur.fetchall()
        team_ids = {t[0] for t in teams}
        if any(e[5] == 'going' and e[3] not in team_ids for e in entries):
            return "⚠️ У участвующего пилота не выбрана одна из 11 команд. Исправь состав."

        # slot = (replaced_id, replaced_name, replaced_tg, team_id, team_name)
        slots = []
        for team_id, team_name in teams:
            occupants = [e for e in entries if e[3] == team_id and e[5] == 'going']
            if len(occupants) > MAX_MAINS_PER_TEAM:
                return f"⚠️ В {esc(team_name)} больше двух участников. Исправь состав."
            free = MAX_MAINS_PER_TEAM - len(occupants)
            absent = [e for e in entries if e[3] == team_id and e[4] == 'main'
                      and e[5] in ('missed', 'not_going', 'banned')]
            slots.extend((e[0], e[1], e[2], team_id, team_name) for e in absent[:free])
            # Места, за которыми вообще ещё не закреплён основной пилот.
            slots.extend((None, None, None, team_id, team_name)
                         for _ in range(max(0, free - len(absent))))
        cur = await db.execute(
            "SELECT d.id, d.display_name, d.tg_id FROM reserve_queue rq "
            "JOIN drivers d ON d.id=rq.driver_id "
            "LEFT JOIN entries e ON e.race_id=rq.race_id AND e.driver_id=rq.driver_id "
            "WHERE rq.race_id=? AND d.role='reserve' AND (e.status IS NULL OR e.status='queued')",
            (race_id,),
        )
        ready = list(await cur.fetchall())
        random.shuffle(slots)
        random.shuffle(ready)
        pairs = list(zip(slots, ready))
        now = datetime.now(timezone.utc).isoformat()
        for slot, res in pairs:
            await db.execute(
                "INSERT INTO entries (race_id, driver_id, status, assigned_team, replaces, marked_at) "
                "VALUES (?, ?, 'going', ?, ?, ?) ON CONFLICT(race_id, driver_id) DO UPDATE SET "
                "status='going', assigned_team=excluded.assigned_team, replaces=excluded.replaces, "
                "marked_at=excluded.marked_at, offer_deadline=NULL",
                (race_id, res[0], slot[3], slot[0], now),
            )
        unused = ready[len(pairs):]
        for res in unused:
            await db.execute(
                "INSERT INTO entries (race_id, driver_id, status) VALUES (?, ?, 'unused') "
                "ON CONFLICT(race_id, driver_id) DO UPDATE SET status='unused', "
                "assigned_team=NULL, replaces=NULL, offer_deadline=NULL", (race_id, res[0]),
            )
        await db.execute("DELETE FROM reserve_queue WHERE race_id=?", (race_id,))
        await db.execute("UPDATE races SET reg_closed=1 WHERE id=?", (race_id,))
        cur = await db.execute("SELECT COUNT(*) FROM entries WHERE race_id=? AND status='going'", (race_id,))
        occupied = (await cur.fetchone())[0]
        await db.commit()

    async def dm(tg_id, text):
        if tg_id:
            await safe_send(bot, tg_id, text)

    # --- персональные уведомления о судьбе каждого ---
    for slot, res in pairs:
        extra = f"Заменяешь: <b>{esc(slot[1])}</b>" if slot[0] else "Занимаешь свободное место в команде."
        await dm(res[2], f"🏎 <b>Ты едешь на {esc(race_name)}!</b>\nКоманда: <b>{esc(slot[4])}</b>\n{extra}")
        await dm(slot[2], f"ℹ️ Ты не участвуешь в <b>{esc(race_name)}</b>. Твоё место займёт <b>{esc(res[1])}</b>.")
    for slot in slots[len(pairs):]:
        await dm(slot[2], f"ℹ️ Ты не участвуешь в <b>{esc(race_name)}</b> — регистрация завершена.")
    for res in unused:
        await dm(res[2], f"ℹ️ Регистрация на <b>{esc(race_name)}</b> завершена. "
                        "Свободных мест на всех не хватило — ты в <b>листе ожидания</b>. "
                        "Если кто-то из состава откажется до старта, его место случайно "
                        "достанется одному из резервов в ожидании.")

    # --- рассылка "ты едешь: включи телеметрию, проверь номер" (с фото и кнопкой) ---
    try:
        sent, failed, no_tg = await notify_going_drivers(bot, race_id)
        log.info("Рассылка 'ты едешь': доставлено %s, ошибок %s, без tg %s", sent, failed, no_tg)
    except Exception as exc:
        log.exception("Ошибка рассылки 'ты едешь': %s", exc)

    # --- сводка в группу ---
    lines = [f"🏁 <b>Регистрация на {esc(race_name)} завершена</b>",
             f"Едут: <b>{occupied}/{GRID_CAPACITY}</b>. Свободно: <b>{GRID_CAPACITY - occupied}</b>.", ""]
    if pairs:
        lines.append("🔀 <b>Распределение резервов (жеребьёвка):</b>")
        for slot, res in pairs:
            extra = f"вместо {esc(slot[1])}" if slot[0] else "свободное место"
            lines.append(f"• {esc(res[1])} → <b>{esc(slot[4])}</b> ({extra})")
    else:
        lines.append("Назначений резервов нет.")
    if unused:
        lines += ["", "💤 <b>Лист ожидания (заменят отказавшихся до старта):</b>"]
        lines.extend(f"• {esc(res[1])}" for res in unused)
    summary = "\n".join(lines)
    await send_race_message(bot, race_id, summary)
    return summary


# ---------- кнопка "Ознакомился" ----------

@router.callback_query(F.data.startswith("ack:"))
async def cb_ack(call: CallbackQuery):
    try:
        _, race_id_s, driver_id_s = call.data.split(":")
        race_id = int(race_id_s)
        driver_id = int(driver_id_s)
    except ValueError:
        await call.answer("Кнопка устарела", show_alert=True)
        return

    async with connect() as db:
        cur = await db.execute(
            "SELECT d.tg_id, d.display_name, d.game_nick, d.car_number, "
            "       t.name, r.name "
            "FROM drivers d "
            "LEFT JOIN teams t ON t.id = COALESCE("
            "    (SELECT assigned_team FROM entries WHERE race_id=? AND driver_id=d.id), d.team_id) "
            "JOIN races r ON r.id=? "
            "WHERE d.id=?",
            (race_id, race_id, driver_id),
        )
        row = await cur.fetchone()
        if not row:
            await call.answer("Пилот не найден", show_alert=True)
            return
        tg_id, display_name, game_nick, car_number, team_name, race_name = row

    if call.from_user.id != tg_id:
        await call.answer("Эта кнопка не для тебя", show_alert=True)
        return

    await call.answer("Спасибо! Админы увидят ✅")

    text = (
        "✅ <b>Ознакомился</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"🏁 Этап: <b>{esc(race_name)}</b>\n"
        f"👤 Пилот: <b>{esc(display_name)}</b> (<code>{esc(game_nick)}</code>)\n"
        f"🔑 Номер болида: <b>#{car_number}</b>\n"
        f"🏎 Команда: <b>{esc(team_name) if team_name else '—'}</b>\n"
        f"💬 TG: <a href='tg://user?id={tg_id}'>профиль</a> · <code>{tg_id}</code>"
    )
    await service_to_admins(call.bot, race_id, text)

    try:
        await call.message.edit_text("✅ <b>Ознакомился</b> · спасибо!")
    except Exception:
        pass
