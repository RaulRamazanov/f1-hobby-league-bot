"""Маршрутизация сообщений этапа, уведомления и общий статус Гран-при."""
import asyncio
import html
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter, TelegramBadRequest
from aiogram.types import FSInputFile, InputMediaPhoto

from app.config import GROUP_ID, EVENTS_THREAD_ID, REGISTRATION_THREAD_ID
from app.db import connect
from app.defaults import DEFAULT_TEAMS, GRID_CAPACITY, MAX_MAINS_PER_TEAM
from app.keyboards import main_menu_kb

log = logging.getLogger(__name__)

# ---------- пути к обучающим картинкам ----------
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
TUTOR1_PATH = ASSETS_DIR / "tutor1.jpg"
TUTOR2_PATH = ASSETS_DIR / "tutor2.jpg"

# ---------- текст рассылки "ты едешь" ----------
RACE_READY_TEXT = (
    "🏁 <b>Ты в составе на этот Гран-при!</b>\n"
    "━━━━━━━━━━━━━━━━\n"
    "📍 Этап: <b>{race_name}</b>\n"
    "🔑 Номер болида: <b>#{car_number}</b>\n\n"
    "⚠️ <b>Обязательно перед гонкой:</b>\n"
    "1️⃣ Телеметрия — включить и сделать публичной\n"
    "2️⃣ Игровой номер — сверить с анкетой: <b>#{car_number}</b>\n\n"
    "👆 Настройки — на картинках выше\n"
    "💬 Не понял, как это сделать — спроси в общем чате"
)


def message_pages(text: str, limit: int = 3500) -> list[str]:
    """Разбивает статус по строкам, не разрывая HTML-теги."""
    pages, lines, length = [], [], 0
    for line in text.splitlines():
        if lines and length + len(line) + 1 > limit:
            pages.append("\n".join(lines))
            lines, length = [], 0
        lines.append(line)
        length += len(line) + 1
    if lines:
        pages.append("\n".join(lines))
    return pages


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    for attempt in range(3):
        try:
            await bot.send_message(chat_id, text, **kwargs)
            return True
        except TelegramRetryAfter as exc:
            if attempt == 2:
                break
            await asyncio.sleep(exc.retry_after)
        except TelegramAPIError as exc:
            log.warning("Не удалось доставить уведомление в %s: %s", chat_id, exc)
            return False
    log.warning("Лимит Telegram: уведомление в %s не доставлено", chat_id)
    return False


async def send_race_message(bot: Bot, race_id: int, text: str) -> bool:
    if not GROUP_ID:
        return False
    async with connect() as db:
        cur = await db.execute("SELECT thread_id FROM races WHERE id=?", (race_id,))
        row = await cur.fetchone()
    thread_id = (row[0] if row else None) or EVENTS_THREAD_ID or REGISTRATION_THREAD_ID
    if not thread_id:
        try:
            chat = await bot.get_chat(GROUP_ID)
        except TelegramAPIError as exc:
            log.warning("Не удалось определить тему этапа: %s", exc)
            return False
        if chat.is_forum:
            log.warning("Тема этапа не задана. Откройте этап в нужной теме или задайте EVENTS_THREAD_ID.")
            return False
    delivered = True
    for page in message_pages(text):
        ok = await safe_send(bot, GROUP_ID, page, message_thread_id=thread_id or None)
        delivered = delivered and ok
    return delivered


async def notify_all_drivers(bot: Bot, text: str) -> tuple[int, int, int]:
    """Все зарегистрированные пилоты: основа и резерв, независимо от ответа на этап."""
    async with connect() as db:
        cur = await db.execute("SELECT DISTINCT tg_id FROM drivers WHERE tg_id IS NOT NULL")
        recipients = [r[0] for r in await cur.fetchall()]
        cur = await db.execute("SELECT COUNT(*) FROM drivers WHERE tg_id IS NULL")
        unlinked = (await cur.fetchone())[0]
    sent = failed = 0
    for tg_id in recipients:
        if await safe_send(bot, tg_id, text, reply_markup=main_menu_kb()):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)
    return sent, failed, unlinked


async def notify_going_drivers(bot: Bot, race_id: int) -> tuple[int, int, int]:
    """Рассылка пилотам, у кого на этот этап статус 'going':
    - media group из tutor1.jpg + tutor2.jpg с подписью-инструкцией
    - отдельное сообщение с кнопкой «Ознакомился»

    Возвращает (sent, failed, no_tg).
    """
    async with connect() as db:
        cur = await db.execute("SELECT name FROM races WHERE id=?", (race_id,))
        row = await cur.fetchone()
        if not row:
            return 0, 0, 0
        race_name = row[0]

        cur = await db.execute(
            "SELECT d.id, d.tg_id, d.display_name, d.car_number "
            "FROM entries e JOIN drivers d ON d.id = e.driver_id "
            "WHERE e.race_id=? AND e.status='going'",
            (race_id,),
        )
        rows = await cur.fetchall()

    sent = failed = no_tg = 0
    has_p1 = TUTOR1_PATH.exists()
    has_p2 = TUTOR2_PATH.exists()

    for driver_id, tg_id, display_name, car_number in rows:
        if not tg_id:
            no_tg += 1
            continue

        caption = RACE_READY_TEXT.format(
            race_name=html.escape(race_name),
            car_number=car_number,
        )

        ok = True

        # ---- 1) media group с двумя фото (или одиночное фото/текст как fallback) ----
        try:
            if has_p1 and has_p2:
                media = [
                    InputMediaPhoto(media=FSInputFile(TUTOR1_PATH), caption=caption),
                    InputMediaPhoto(media=FSInputFile(TUTOR2_PATH)),
                ]
                await bot.send_media_group(tg_id, media=media)
            elif has_p1:
                await bot.send_photo(tg_id, photo=FSInputFile(TUTOR1_PATH), caption=caption)
            else:
                await bot.send_message(tg_id, caption)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            try:
                if has_p1 and has_p2:
                    media = [
                        InputMediaPhoto(media=FSInputFile(TUTOR1_PATH), caption=caption),
                        InputMediaPhoto(media=FSInputFile(TUTOR2_PATH)),
                    ]
                    await bot.send_media_group(tg_id, media=media)
                elif has_p1:
                    await bot.send_photo(tg_id, photo=FSInputFile(TUTOR1_PATH), caption=caption)
                else:
                    await bot.send_message(tg_id, caption)
            except (TelegramAPIError, TelegramBadRequest) as exc:
                log.warning("Медиагруппа не ушла %s: %s", tg_id, exc)
                ok = False
        except (TelegramAPIError, TelegramBadRequest) as exc:
            log.warning("Медиагруппа не ушла %s: %s", tg_id, exc)
            ok = False

        # ---- 2) отдельное сообщение с кнопкой «Ознакомился» ----
        if ok:
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="✅ Ознакомился",
                    callback_data=f"ack:{race_id}:{driver_id}",
                )],
            ])
            try:
                await bot.send_message(
                    tg_id,
                    "👇 <b>Подтверди, что ознакомился с инструкцией.</b>\n"
                    "Это увидят админы — они поймут, что ты в курсе насчёт телеметрии и номера.",
                    reply_markup=kb,
                )
            except (TelegramAPIError, TelegramBadRequest) as exc:
                log.warning("Кнопка не ушла %s: %s", tg_id, exc)

        if ok:
            sent += 1
        else:
            failed += 1

        await asyncio.sleep(0.05)

    return sent, failed, no_tg


def delivery_report(sent: int, failed: int, unlinked: int, group_sent: bool) -> str:
    return (
        f"Личные уведомления: доставлено {sent}, не доставлено {failed}.\n"
        f"Без привязанного Telegram: {unlinked}.\n"
        f"Объявление в теме: {'отправлено' if group_sent else 'не отправлено (проверь тему и права бота)'}."
    )


async def race_status_pages() -> list[str]:
    MSK = timezone(timedelta(hours=3))
    now = datetime.now(MSK)

    async with connect() as db:
        cur = await db.execute(
            "SELECT id, name, race_at, main_deadline, active, reg_closed, started "
            "FROM races ORDER BY active DESC, id DESC LIMIT 1"
        )
        race = await cur.fetchone()
        if not race:
            return [
                "📊 <b>Статус Гран-при</b>\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "Гран-при пока не создан."
            ]
        rid, name, race_at_iso, deadline_iso, active, closed, started = race

        cur = await db.execute(
            "SELECT d.display_name, d.game_nick, d.car_number, d.role, e.status, "
            "COALESCE(e.assigned_team, d.team_id), t.name, replaced.display_name "
            "FROM entries e JOIN drivers d ON d.id=e.driver_id "
            "LEFT JOIN teams t ON t.id=COALESCE(e.assigned_team, d.team_id) "
            "LEFT JOIN drivers replaced ON replaced.id=e.replaces "
            "WHERE e.race_id=? ORDER BY t.id, d.car_number",
            (rid,),
        )
        entries = await cur.fetchall()

    esc = html.escape
    race_at = datetime.fromisoformat(race_at_iso).astimezone(MSK)
    deadline = datetime.fromisoformat(deadline_iso).astimezone(MSK)

    if not active:
        phase_icon, phase_txt = "🏁", "Этап завершён"
    elif started:
        phase_icon, phase_txt = "🚦", "Гонка началась"
    elif closed:
        phase_icon, phase_txt = "🔒", "Регистрация закрыта"
    else:
        phase_icon, phase_txt = "🟢", "Регистрация открыта"

    def humanize(delta_seconds: int) -> str:
        if delta_seconds <= 0:
            return "срок истёк"
        days, rem = divmod(delta_seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days:
            return f"{days} д {hours} ч"
        if hours:
            return f"{hours} ч {minutes} мин"
        return f"{minutes} мин"

    to_race = humanize(int((race_at - now).total_seconds()))
    to_deadline = humanize(int((deadline - now).total_seconds()))

    occupied = sum(e[4] == "going" for e in entries)
    queued = sum(e[4] == "queued" for e in entries)
    pending = sum(e[4] == "pending" for e in entries)
    offered = sum(e[4] == "offered" for e in entries)
    missed = sum(e[4] in ("missed", "not_going") for e in entries)
    unused = sum(e[4] == "unused" for e in entries)
    free_seats = max(0, GRID_CAPACITY - occupied)

    lines = [
        f"{phase_icon} <b>Статус Гран-при</b>",
        "━━━━━━━━━━━━━━━━",
        f"🏁 <b>{esc(name)}</b>",
        f"📅 Старт: <b>{race_at:%d.%m.%Y %H:%M} МСК</b>  ·  {to_race}",
        f"⏳ Подтверждение: {deadline:%d.%m.%Y %H:%M} МСК  ·  {to_deadline}",
        f"📌 Фаза: <b>{phase_txt}</b>",
        "",
        "📈 <b>Сводка</b>",
        f"✅ Едут: <b>{occupied}/{GRID_CAPACITY}</b>",
        f"🪑 Свободно мест: <b>{free_seats}</b>",
        f"⏳ Ждут ответа: <b>{pending}</b>",
        f"🟢 Резервов в очереди: <b>{queued}</b>"
        + (f"\n📨 Ждут оффер: <b>{offered}</b>" if offered else "")
        + (f"\n❌ Отказались / пропустили: <b>{missed}</b>" if missed else "")
        + (f"\n💤 Резервов без места: <b>{unused}</b>" if unused else ""),
    ]

    if not closed and active:
        lines += ["", "ℹ️ <i>Неподтверждённые места сохраняются за основой до закрытия регистрации.</i>"]

    lines += ["", "🏎 <b>Состав по командам</b>"]
    for team in DEFAULT_TEAMS:
        going = [e for e in entries if e[4] == "going" and e[6] == team]
        lines.append(f"{esc(team)} — <b>{len(going)}/{MAX_MAINS_PER_TEAM}</b>")
        if not going:
            lines.append("  └ <i>никого не подтверждено</i>")
            continue
        for dname, nick, number, role, st, team_id, tname, replaces in going:
            extra = ""
            if role == "reserve" and replaces:
                extra = f" — вместо <b>{esc(replaces)}</b>"
            elif role == "reserve" and not replaces:
                extra = " — <i>свободное место</i>"
            lines.append(f"  ✅ #{number} {esc(dname)} <code>{esc(nick)}</code>{extra}")

    sections = [
        ("pending",   "⏳ <b>Ждут ответа</b>"),
        ("queued",    "🟢 <b>Резервы в очереди</b>"),
        ("offered",   "📨 <b>Ждут оффер</b>"),
        ("not_going", "❌ <b>Отказались</b>"),
        ("missed",    "⌛ <b>Не подтвердили</b>"),
        ("unused",    "💤 <b>Резерв: мест не хватило</b>"),
    ]
    for status, title in sections:
        selected = [e for e in entries if e[4] == status]
        if not selected:
            continue
        lines += ["", title]
        for dname, nick, number, role, st, team_id, tname, replaces in selected:
            team_txt = f" · {esc(tname)}" if tname else ""
            repl_txt = f" — за {esc(replaces)}" if replaces else ""
            lines.append(f"• #{number} {esc(dname)} <code>{esc(nick)}</code>{team_txt}{repl_txt}")

    lines += ["", f"🕐 <i>Обновлено: {now:%d.%m %H:%M} МСК</i>"]

    return message_pages("\n".join(lines))