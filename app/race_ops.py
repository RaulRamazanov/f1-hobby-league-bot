"""Изменения состава после закрытия регистрации.

* Отказ пилота из состава (до старта гонки) → место по жеребьёвке получает
  резерв из листа ожидания (status='unused'), ему уходит рассылка «ты едешь»
  с двумя фото и напоминанием про телеметрию.
* Резерв, вставший в лист ожидания после закрытия, сразу получает свободное
  место, если оно есть.
* Штрафной пропуск гонки (назначает админ) и его снятие.

Все изменения базы делаются внутри BEGIN IMMEDIATE, уведомления — после commit.
"""
import html
import logging
import random
from datetime import datetime, timezone, timedelta

from aiogram import Bot

from app.config import ADMIN_IDS
from app.db import connect
from app.defaults import DEFAULT_TEAMS, MAX_MAINS_PER_TEAM
from app.events import send_race_message, safe_send, notify_going_drivers, service_to_admins, message_pages

log = logging.getLogger(__name__)

ABSENT_STATUSES = ("missed", "not_going", "banned")


def esc(v) -> str:
    return html.escape(str(v), quote=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def active_race(db):
    """(id, name, reg_closed, started) активного этапа или None."""
    cur = await db.execute(
        "SELECT id, name, reg_closed, started FROM races WHERE active=1 ORDER BY id DESC LIMIT 1"
    )
    return await cur.fetchone()


async def fill_free_seats(db, race_id: int, priority: tuple[int | None, int | None] | None = None) -> list[dict]:
    """Раздаёт свободные места сетки резервам из листа ожидания (status='unused').

    priority = (team_id, replaced_driver_id) — место, которое заполняется первым
    (например, место только что отказавшегося пилота). Не делает commit.
    """
    cur = await db.execute("SELECT id, name FROM teams ORDER BY id")
    teams = [t for t in await cur.fetchall() if t[1] in DEFAULT_TEAMS]
    cur = await db.execute(
        "SELECT d.id, d.display_name, d.tg_id, d.role, e.status, "
        "COALESCE(e.assigned_team, d.team_id), e.replaces "
        "FROM entries e JOIN drivers d ON d.id=e.driver_id WHERE e.race_id=?",
        (race_id,),
    )
    entries = await cur.fetchall()
    replaced = {e[6] for e in entries if e[4] == "going" and e[6]}

    # slot = (replaced_id, replaced_name, team_id, team_name)
    slots = []
    for team_id, team_name in teams:
        going = [e for e in entries if e[5] == team_id and e[4] == "going"]
        free = MAX_MAINS_PER_TEAM - len(going)
        if free <= 0:
            continue
        absent = [e for e in entries if e[5] == team_id and e[3] == "main"
                  and e[4] in ABSENT_STATUSES and e[0] not in replaced][:free]
        slots.extend((e[0], e[1], team_id, team_name) for e in absent)
        slots.extend((None, None, team_id, team_name) for _ in range(free - len(absent)))

    standby = [e for e in entries if e[3] == "reserve" and e[4] == "unused"]
    if not slots or not standby:
        return []

    random.shuffle(slots)
    random.shuffle(standby)
    if priority:
        p_team, p_replaced = priority
        slots.sort(key=lambda s: 0 if (s[2] == p_team and s[0] == p_replaced)
                   else 1 if s[2] == p_team else 2)

    assignments = []
    now = _now()
    for slot, res in zip(slots, standby):
        await db.execute(
            "UPDATE entries SET status='going', assigned_team=?, replaces=?, marked_at=?, "
            "offer_deadline=NULL WHERE race_id=? AND driver_id=?",
            (slot[2], slot[0], now, race_id, res[0]),
        )
        assignments.append({
            "driver_id": res[0], "name": res[1], "tg_id": res[2],
            "team_id": slot[2], "team_name": slot[3],
            "replaced_id": slot[0], "replaced_name": slot[1],
        })
    return assignments


async def _release_seat(db, race_id: int, driver_id: int, role: str,
                        team_id: int | None, replaces: int | None, new_status: str) -> list[dict]:
    """Снимает пилота со статуса going и сразу отдаёт место резерву. Не делает commit."""
    if role == "reserve":
        await db.execute(
            "UPDATE entries SET status=?, assigned_team=NULL, replaces=NULL, marked_at=? "
            "WHERE race_id=? AND driver_id=?",
            (new_status, _now(), race_id, driver_id),
        )
        target = replaces
    else:
        await db.execute(
            "UPDATE entries SET status=?, marked_at=? WHERE race_id=? AND driver_id=?",
            (new_status, _now(), race_id, driver_id),
        )
        target = driver_id
    await db.execute("DELETE FROM lobby_checks WHERE race_id=? AND driver_id=?", (race_id, driver_id))
    return await fill_free_seats(db, race_id, priority=(team_id, target))


async def announce_assignments(bot: Bot, race_id: int, race_name: str, assignments: list[dict]) -> None:
    """Личные сообщения новым участникам + рассылка с фото/телеметрией + строчка в тему."""
    if not assignments:
        return
    for a in assignments:
        extra = (f"Заменяешь: <b>{esc(a['replaced_name'])}</b>" if a["replaced_name"]
                 else "Занимаешь свободное место в команде.")
        if a["tg_id"]:
            await safe_send(
                bot, a["tg_id"],
                f"🏎 <b>Ты едешь на {esc(race_name)}!</b>\n"
                "Освободилось место — жеребьёвка среди резервов выбрала тебя.\n"
                f"Команда: <b>{esc(a['team_name'])}</b>\n{extra}",
            )
    try:
        await notify_going_drivers(bot, race_id, [a["driver_id"] for a in assignments])
    except Exception as exc:  # рассылка не должна ломать само назначение
        log.exception("Ошибка рассылки 'ты едешь' новому участнику: %s", exc)



def assignments_lines(assignments: list[dict]) -> list[str]:
    lines = []
    for a in assignments:
        extra = f"вместо {esc(a['replaced_name'])}" if a["replaced_name"] else "свободное место"
        lines.append(f"🔀 <b>{esc(a['name'])}</b> → <b>{esc(a['team_name'])}</b> ({extra})")
    return lines


async def notify_admins(bot: Bot, text: str, race_id: int | None = None) -> None:
    """Сообщение админам; с race_id — служебное (удалится после «Завершить этап»)."""
    await service_to_admins(bot, race_id, text)


# ---------------------------------------------------------------------------
# Отказ пилота после закрытия регистрации
# ---------------------------------------------------------------------------

async def withdraw_after_close(bot: Bot, tg_id: int) -> tuple[str, int | None]:
    """Возвращает (текст для всплывашки, driver_id)."""
    async with connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT d.id, d.display_name, d.role, r.id, r.name, r.reg_closed, r.started, "
            "e.status, COALESCE(e.assigned_team, d.team_id), e.replaces "
            "FROM drivers d JOIN races r ON r.active=1 "
            "LEFT JOIN entries e ON e.race_id=r.id AND e.driver_id=d.id "
            "WHERE d.tg_id=? ORDER BY r.id DESC LIMIT 1",
            (tg_id,),
        )
        row = await cur.fetchone()
        if not row:
            return "Нет активного этапа", None
        driver_id, name, role, race_id, race_name, closed, started, status, team_id, replaces = row
        if not closed:
            return "Регистрация ещё открыта — используй кнопку «Не еду» в профиле", driver_id
        if started:
            return "Гонка уже началась — отказаться через бота нельзя. Напиши админу.", driver_id
        if status != "going":
            return "Ты не в составе этого этапа", driver_id
        cur = await db.execute("SELECT name FROM teams WHERE id=?", (team_id,))
        team_row = await cur.fetchone()
        team_name = team_row[0] if team_row else "—"
        assignments = await _release_seat(db, race_id, driver_id, role, team_id, replaces, "not_going")
        await db.commit()

    lines = [f"❌ <b>{esc(name)}</b> отказался от участия в <b>{esc(race_name)}</b> "
             f"после закрытия регистрации ({esc(team_name)})."]
    if assignments:
        lines.append("Место разыграно среди резервов из листа ожидания:")
        lines += assignments_lines(assignments)
    else:
        lines.append("⚠️ В листе ожидания нет резервов — место пока свободно.")
    summary = "\n".join(lines)
    await send_race_message(bot, race_id, summary, service=True)
    await notify_admins(bot, summary, race_id)
    await announce_assignments(bot, race_id, race_name, assignments)
    return "Отказ принят. Место передано резерву." if assignments else "Отказ принят.", driver_id


# ---------------------------------------------------------------------------
# Резерв встаёт в лист ожидания после закрытия регистрации
# ---------------------------------------------------------------------------

async def join_standby(bot: Bot, driver_id: int, race_id: int, race_name: str, name: str) -> list[dict]:
    """Ставит резерва в лист ожидания (status='unused') и сразу раздаёт свободные места."""
    async with connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT reg_closed, started, active FROM races WHERE id=?", (race_id,)
        )
        race = await cur.fetchone()
        if not race or not race[0] or race[1] or not race[2]:
            return []
        await db.execute(
            "INSERT INTO entries (race_id, driver_id, status) VALUES (?, ?, 'unused') "
            "ON CONFLICT(race_id, driver_id) DO UPDATE SET status='unused', "
            "assigned_team=NULL, replaces=NULL, offer_deadline=NULL",
            (race_id, driver_id),
        )
        assignments = await fill_free_seats(db, race_id)
        await db.commit()

    text = f"🟢 Резерв <b>{esc(name)}</b> встал в лист ожидания на <b>{esc(race_name)}</b>."
    if assignments:
        text += "\nСвободные места разыграны:\n" + "\n".join(assignments_lines(assignments))
    await send_race_message(bot, race_id, text, service=True)
    if assignments:
        await notify_admins(bot, text, race_id)
    await announce_assignments(bot, race_id, race_name, assignments)
    return assignments


# ---------------------------------------------------------------------------
# Штрафной пропуск гонки
# ---------------------------------------------------------------------------

async def ban_info(db, driver_id: int) -> str | None:
    """Человекочитаемый статус пропуска или None."""
    cur = await db.execute("SELECT ban_pending FROM drivers WHERE id=?", (driver_id,))
    row = await cur.fetchone()
    parts = []
    race = await active_race(db)
    if race:
        cur = await db.execute(
            "SELECT status FROM entries WHERE race_id=? AND driver_id=?", (race[0], driver_id)
        )
        entry = await cur.fetchone()
        if entry and entry[0] == "banned":
            parts.append(f"пропускает текущий этап «{esc(race[1])}»")
    if row and row[0] > 0:
        parts.append("пропустит следующий этап")
    return ", ".join(parts) if parts else None


async def apply_ban(bot: Bot, driver_id: int) -> str:
    """Блокирует участие в ближайшем этапе. Возвращает отчёт для админа."""
    async with connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT display_name, tg_id, role, team_id FROM drivers WHERE id=?", (driver_id,)
        )
        drv = await cur.fetchone()
        if not drv:
            return "Пилот не найден."
        name, tg_id, role, base_team = drv
        race = await active_race(db)
        assignments: list[dict] = []
        race_name = None
        if race and not race[3]:
            race_id, race_name, closed, _ = race
            cur = await db.execute(
                "SELECT status, COALESCE(assigned_team, ?), replaces FROM entries "
                "WHERE race_id=? AND driver_id=?",
                (base_team, race_id, driver_id),
            )
            entry = await cur.fetchone()
            if entry and entry[0] == "banned":
                return f"{esc(name)} уже пропускает «{esc(race_name)}»."
            if closed and entry and entry[0] == "going":
                assignments = await _release_seat(db, race_id, driver_id, role, entry[1], entry[2], "banned")
            else:
                await db.execute("DELETE FROM reserve_queue WHERE race_id=? AND driver_id=?",
                                 (race_id, driver_id))
                await db.execute(
                    "INSERT INTO entries (race_id, driver_id, status) VALUES (?, ?, 'banned') "
                    "ON CONFLICT(race_id, driver_id) DO UPDATE SET status='banned', "
                    "assigned_team=NULL, replaces=NULL, offer_deadline=NULL",
                    (race_id, driver_id),
                )
            await db.commit()
            target = f"текущий этап «{esc(race_name)}»"
        else:
            race_id = None
            await db.execute("UPDATE drivers SET ban_pending=1 WHERE id=? AND ban_pending<1", (driver_id,))
            await db.commit()
            target = "следующий этап (применится при объявлении Гран-при)"

    if tg_id:
        await safe_send(bot, tg_id, "🚫 <b>Штраф:</b> решением админа ты пропускаешь "
                        + (f"этап <b>{esc(race_name)}</b>." if race_id else "следующий этап."))
    report = f"🚫 {esc(name)} пропускает {target}."
    if race_id and assignments:
        text = (f"🔀 <b>Изменение состава {esc(race_name)}</b>: {esc(name)} не участвует.\n"
                + "\n".join(assignments_lines(assignments)))
        await send_race_message(bot, race_id, text, service=True)
        await announce_assignments(bot, race_id, race_name, assignments)
        report += "\n" + "\n".join(assignments_lines(assignments))
    elif race_id and closed and entry and entry[0] == "going":
        await send_race_message(bot, race_id, f"🔀 <b>Изменение состава {esc(race_name)}</b>: "
                                              f"{esc(name)} не участвует. Резервов в ожидании нет — место свободно.",
                                service=True)
        report += "\nРезервов в листе ожидания нет — место свободно."
    return report


async def lift_ban(bot: Bot, driver_id: int) -> str:
    """Снимает пропуск (и отложенный, и на текущий этап, если гонка ещё не началась)."""
    restored_going = False
    async with connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT display_name, tg_id, role, team_id, ban_pending FROM drivers WHERE id=?", (driver_id,)
        )
        drv = await cur.fetchone()
        if not drv:
            return "Пилот не найден."
        name, tg_id, role, team_id, pending = drv
        changed = []
        if pending > 0:
            await db.execute("UPDATE drivers SET ban_pending=0 WHERE id=?", (driver_id,))
            changed.append("отложенный пропуск следующего этапа снят")
        race = await active_race(db)
        race_id = race_name = None
        if race:
            race_id, race_name, closed, started = race
            cur = await db.execute(
                "SELECT status FROM entries WHERE race_id=? AND driver_id=?", (race_id, driver_id)
            )
            entry = await cur.fetchone()
            if entry and entry[0] == "banned":
                if started:
                    changed.append("гонка уже идёт — на текущий этап вернуть нельзя")
                elif role == "reserve":
                    await db.execute("DELETE FROM entries WHERE race_id=? AND driver_id=?", (race_id, driver_id))
                    changed.append("резерв снова может нажать «Готов заменить»")
                elif not closed:
                    await db.execute("UPDATE entries SET status='pending' WHERE race_id=? AND driver_id=?",
                                     (race_id, driver_id))
                    changed.append("снова может подтвердить участие")
                else:
                    cur = await db.execute(
                        "SELECT COUNT(*) FROM entries e JOIN drivers d ON d.id=e.driver_id "
                        "WHERE e.race_id=? AND e.status='going' AND COALESCE(e.assigned_team, d.team_id)=?",
                        (race_id, team_id),
                    )
                    if (await cur.fetchone())[0] < MAX_MAINS_PER_TEAM:
                        await db.execute(
                            "UPDATE entries SET status='going', marked_at=? WHERE race_id=? AND driver_id=?",
                            (_now(), race_id, driver_id),
                        )
                        restored_going = True
                        changed.append("возвращён в состав (место в команде было свободно)")
                    else:
                        await db.execute("UPDATE entries SET status='missed' WHERE race_id=? AND driver_id=?",
                                         (race_id, driver_id))
                        changed.append("место в команде уже занято — в этом этапе не едет")
        await db.commit()

    if not changed:
        return f"У {esc(name)} нет активного пропуска."
    if tg_id:
        await safe_send(bot, tg_id, "✅ Админ снял с тебя пропуск гонки. Нажми /start, чтобы обновить профиль.")
    if restored_going and race_id:
        await send_race_message(bot, race_id, f"✅ <b>{esc(name)}</b> возвращён в состав <b>{esc(race_name)}</b>.", service=True)
        try:
            await notify_going_drivers(bot, race_id, [driver_id])
        except Exception as exc:
            log.exception("Ошибка рассылки 'ты едешь': %s", exc)
    return f"✅ {esc(name)}: " + "; ".join(changed) + "."


async def apply_pending_bans(db, race_id: int) -> list[tuple[int, str, int | None]]:
    """При объявлении этапа: пилоты с отложенным пропуском получают status='banned'.
    Не делает commit. Возвращает [(driver_id, name, tg_id)]."""
    cur = await db.execute("SELECT id, display_name, tg_id FROM drivers WHERE ban_pending>0")
    banned = await cur.fetchall()
    for driver_id, _, _ in banned:
        await db.execute(
            "INSERT INTO entries (race_id, driver_id, status) VALUES (?, ?, 'banned') "
            "ON CONFLICT(race_id, driver_id) DO UPDATE SET status='banned'",
            (race_id, driver_id),
        )
    await db.execute("UPDATE drivers SET ban_pending=ban_pending-1 WHERE ban_pending>0")
    return banned


# ---------------------------------------------------------------------------
# Админ меняет участника: новый розыгрыш на его место или конкретный резерв
# ---------------------------------------------------------------------------

async def standby_list(db, race_id: int) -> list[tuple[int, str, int]]:
    """Готовые резервы в листе ожидания: [(driver_id, name, car_number)]."""
    cur = await db.execute(
        "SELECT d.id, d.display_name, d.car_number FROM entries e JOIN drivers d ON d.id=e.driver_id "
        "WHERE e.race_id=? AND e.status='unused' AND d.role='reserve' ORDER BY d.display_name",
        (race_id,),
    )
    return await cur.fetchall()


async def pickable_reserves(db, race_id: int) -> list[tuple[int, str, int, bool]]:
    """Резервы, которых админ может поставить вручную: все, кто не едет и не на штрафе.
    [(driver_id, name, number, в_листе_ожидания)] — сначала лист ожидания."""
    cur = await db.execute(
        "SELECT d.id, d.display_name, d.car_number, COALESCE(e.status, '') FROM drivers d "
        "LEFT JOIN entries e ON e.driver_id=d.id AND e.race_id=? "
        "WHERE d.role='reserve' AND COALESCE(e.status, '') NOT IN ('going', 'banned') "
        "ORDER BY COALESCE(e.status, '')='unused' DESC, d.display_name",
        (race_id,),
    )
    return [(r[0], r[1], r[2], r[3] == "unused") for r in await cur.fetchall()]


async def admin_replace(bot: Bot, driver_id: int, new_driver_id: int | None = None,
                        mode: str = "random") -> str:
    """Админ снимает участника с этапа и решает, что делать с местом:
    mode='random' — разыграть среди листа ожидания;
    mode='pick'   — отдать конкретному резерву (new_driver_id, любой свободный резерв);
    mode='remove' — просто снять, место остаётся свободным.
    Работает, пока этап активен и регистрация закрыта (в том числе после старта)."""
    async with connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        race = await active_race(db)
        if not race:
            return "Активного этапа нет."
        race_id, race_name, closed, _ = race
        if not closed:
            return ("Регистрация ещё открыта — состав ещё не сформирован. "
                    "Нажми «Завершить регистрацию», после этого можно снимать и заменять.")
        cur = await db.execute(
            "SELECT d.display_name, d.tg_id, d.role, e.status, COALESCE(e.assigned_team, d.team_id), e.replaces "
            "FROM entries e JOIN drivers d ON d.id=e.driver_id WHERE e.race_id=? AND e.driver_id=?",
            (race_id, driver_id),
        )
        row = await cur.fetchone()
        if not row or row[3] != "going":
            return "Этот пилот уже не в составе — обнови список."
        name, tg_id, role, _, team_id, replaces = row
        target = driver_id if role == "main" else replaces
        cur = await db.execute("SELECT name FROM teams WHERE id=?", (team_id,))
        team_row = await cur.fetchone()
        team_name = team_row[0] if team_row else "—"

        new = None
        if mode == "pick":
            pickable = {r[0]: r for r in await pickable_reserves(db, race_id)}
            if new_driver_id not in pickable:
                return "Этого резерва нельзя поставить (уже едет или на штрафе) — обнови список."
            cur = await db.execute("SELECT display_name, tg_id FROM drivers WHERE id=?", (new_driver_id,))
            new = await cur.fetchone()
        elif mode == "random":
            pool = await standby_list(db, race_id)
            if not pool:
                return ("В листе ожидания нет резервов для розыгрыша. "
                        "Выбери резерва вручную или сними без замены.")

        # 1) снимаем текущего участника
        if role == "reserve":
            await db.execute(
                "UPDATE entries SET status='not_going', assigned_team=NULL, replaces=NULL, marked_at=? "
                "WHERE race_id=? AND driver_id=?", (_now(), race_id, driver_id))
        else:
            await db.execute("UPDATE entries SET status='not_going', marked_at=? WHERE race_id=? AND driver_id=?",
                             (_now(), race_id, driver_id))
        await db.execute("DELETE FROM lobby_checks WHERE race_id=? AND driver_id=?", (race_id, driver_id))
        await db.execute("DELETE FROM final_checks WHERE race_id=? AND driver_id=?", (race_id, driver_id))

        # 2) место
        assignments: list[dict] = []
        if mode == "pick":
            await db.execute("DELETE FROM reserve_queue WHERE race_id=? AND driver_id=?", (race_id, new_driver_id))
            await db.execute(
                "INSERT INTO entries (race_id, driver_id, status, assigned_team, replaces, marked_at) "
                "VALUES (?, ?, 'going', ?, ?, ?) ON CONFLICT(race_id, driver_id) DO UPDATE SET "
                "status='going', assigned_team=excluded.assigned_team, replaces=excluded.replaces, "
                "marked_at=excluded.marked_at, offer_deadline=NULL",
                (race_id, new_driver_id, team_id, target, _now()))
            target_name = None
            if target:
                cur = await db.execute("SELECT display_name FROM drivers WHERE id=?", (target,))
                target_name = (await cur.fetchone())[0]
            assignments = [{"driver_id": new_driver_id, "name": new[0], "tg_id": new[1],
                            "team_id": team_id, "team_name": team_name,
                            "replaced_id": target, "replaced_name": target_name}]
        elif mode == "random":
            assignments = await fill_free_seats(db, race_id, priority=(team_id, target))
        await db.commit()

    how = {"pick": "выбор админа", "random": "новый розыгрыш", "remove": "без замены"}[mode]
    text = (f"🔀 <b>Изменение состава {esc(race_name)}</b> ({how})\n"
            f"<b>{esc(name)}</b> ({esc(team_name)}) снят с этапа.")
    if assignments:
        text += "\n" + "\n".join(assignments_lines(assignments))
    else:
        text += "\n🪑 Место свободно."
    if tg_id:
        await safe_send(bot, tg_id, f"ℹ️ Админ снял тебя с этапа <b>{esc(race_name)}</b>"
                                    + (" и передал место другому пилоту." if assignments else ".")
                                    + " Если это ошибка — напиши админу.")
    await send_race_message(bot, race_id, text, service=True)
    await announce_assignments(bot, race_id, race_name, assignments)
    return "✅ " + text


# ---------------------------------------------------------------------------
# Подтверждение за час до гонки
# ---------------------------------------------------------------------------

FINAL_CHECK_TEXT = (
    "⏰ <b>{race_name}</b> — старт через час!\n"
    "━━━━━━━━━━━━━━━━\n"
    "Подтверди, что будешь на гонке 👇\n"
    "Не сможешь — тоже нажми, админ найдёт замену."
)


def final_check_kb(race_id: int, driver_id: int):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Буду на гонке", callback_data=f"fc:yes:{race_id}:{driver_id}")],
        [InlineKeyboardButton(text="❌ Не смогу", callback_data=f"fc:no:{race_id}:{driver_id}")],
    ])


async def send_final_checks(bot: Bot, race_id: int, driver_ids: list[int] | None = None) -> int:
    async with connect() as db:
        cur = await db.execute("SELECT name FROM races WHERE id=?", (race_id,))
        race_name = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT d.id, d.tg_id FROM entries e JOIN drivers d ON d.id=e.driver_id "
            "WHERE e.race_id=? AND e.status='going' AND d.tg_id IS NOT NULL", (race_id,))
        rows = await cur.fetchall()
    sent = 0
    for driver_id, tg_id in rows:
        if driver_ids is not None and driver_id not in driver_ids:
            continue
        if await safe_send(bot, tg_id, FINAL_CHECK_TEXT.format(race_name=esc(race_name)),
                           reply_markup=final_check_kb(race_id, driver_id)):
            sent += 1
    return sent


async def send_due_final_checks(bot: Bot) -> None:
    """Вызывается раз в минуту: за час до старта рассылает подтверждение участникам.
    Если регистрацию закрыли позже — рассылка уходит сразу после закрытия (до старта)."""
    now = datetime.now(timezone.utc)
    async with connect() as db:
        cur = await db.execute(
            "SELECT id, race_at FROM races WHERE active=1 AND reg_closed=1 AND reminder_sent=0")
        races = await cur.fetchall()
    for race_id, race_at in races:
        try:
            start = datetime.fromisoformat(race_at)
        except (TypeError, ValueError):
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if not (start - timedelta(hours=1) <= now <= start + timedelta(minutes=30)):
            continue
        async with connect() as db:
            cur = await db.execute("UPDATE races SET reminder_sent=1 WHERE id=? AND reminder_sent=0", (race_id,))
            await db.commit()
            if not cur.rowcount:
                continue
        sent = await send_final_checks(bot, race_id)
        log.info("Подтверждение за час: этап %s, отправлено %s", race_id, sent)


async def final_checks_map(db, race_id: int) -> dict[int, str]:
    cur = await db.execute("SELECT driver_id, answer FROM final_checks WHERE race_id=?", (race_id,))
    return dict(await cur.fetchall())


# ---------------------------------------------------------------------------
# Автоматическое закрытие регистрации (время выбирается в мастере Гран-при)
# ---------------------------------------------------------------------------

async def auto_close_due(bot: Bot) -> None:
    """Вызывается раз в минуту. Если наступило время закрытия — завершает регистрацию
    так же, как кнопка «Завершить регистрацию» (жеребьёвка, рассылки, сводка в теме).
    Ручное закрытие продолжает работать: закрытый этап повторно не трогается."""
    from app.handlers.reserve import finish_registration  # локально: избегаем циклического импорта
    now = datetime.now(timezone.utc)
    async with connect() as db:
        cur = await db.execute(
            "SELECT id, name, main_deadline FROM races "
            "WHERE active=1 AND reg_closed=0 AND started=0 AND auto_close=1")
        races = await cur.fetchall()
    for race_id, race_name, deadline in races:
        try:
            due = datetime.fromisoformat(deadline)
        except (TypeError, ValueError):
            continue
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        if now < due:
            continue
        summary = await finish_registration(bot, race_id)  # None — уже закрыли вручную
        if summary:
            log.info("Регистрация на этап %s закрыта автоматически", race_id)
            for admin_id in ADMIN_IDS:
                await safe_send(bot, admin_id, "🤖 <b>Регистрация закрыта автоматически</b> по времени.\n\n"
                                + message_pages(summary)[0])
