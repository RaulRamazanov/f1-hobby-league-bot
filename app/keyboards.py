from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from app.defaults import MAX_MAINS_PER_TEAM


def _btn(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb)


def _back(cb: str, text: str = "⬅️ Назад") -> list[InlineKeyboardButton]:
    return [_btn(text, cb)]


# ---------- пользователь ----------

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("👤 Мой профиль", "profile")],
        [_btn("🏁 Статус Гран-при", "race:status")],
        [_btn("📋 Правила", "rules")],
    ])


def rules_kb(back_cb: str = "menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_back(back_cb)])


def profile_actions_kb(
    role: str, status: str | None, closed: bool = False
) -> InlineKeyboardMarkup:
    rows = []
    if closed:
        pass  # регистрация завершена — кнопок действий нет
    elif role == "main":
        if status in ("pending", None):
            rows.append([_btn("✅ Подтверждаю", "main:yes")])
            rows.append([_btn("❌ Не еду", "main:no")])
        elif status == "going":
            rows.append([_btn("❌ Отказаться", "main:no")])
    else:  # reserve
        if status in (None, "not_going", "missed"):
            rows.append([_btn("🟢 Готов заменить", "reserve:ready")])
        elif status == "queued":
            rows.append([_btn("⚪ Снять готовность", "reserve:cancel")])
    rows.append([_btn("🏁 Статус Гран-при", "race:status")])
    rows.append([_btn("🔄 Обновить", "profile"), _btn("📋 Правила", "rules")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- админ-панель ----------

def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("➕ Объявить Гран-при", "admin:race:new")],
        [_btn("🏁 Статус Гран-при", "race:status")],
        [_btn("✅ Завершить регистрацию", "admin:reg:close")],
        [_btn("🔓 Открыть регистрацию заново", "admin:race:reopen")],
        [_btn("🚦 Начать гонку", "admin:race:start")],
        [_btn("🏁 Завершить этап", "admin:race:finish")],
        [_btn("➕ Добавить пилота", "admin:driver:new"), _btn("⏳ Заявки", "admin:pending")],
        [_btn("📊 Состав", "admin:roster")],
        [_btn("✏️ Изменить основу", "ros:l:main:0"), _btn("✏️ Изменить резерв", "ros:l:reserve:0")],
    ])


def confirm_close_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ Да, завершить", "admin:reg:close:yes")],
        _back("admin:menu", "❌ Отмена"),
    ])


def admin_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_back("admin:menu")])


def role_choice_kb(reserve_cb: str, main_cb: str, back_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🔄 Резерв", reserve_cb), _btn("🏎 Основа", main_cb)],
        _back(back_cb),
    ])


def team_pick_kb(
    teams: list[tuple[int, str, int]], cb_prefix: str, back_cb: str
) -> InlineKeyboardMarkup:
    """teams: (id, name, mains_count). cb_prefix + team_id."""
    rows = []
    for tid, name, cnt in teams:
        full = cnt >= MAX_MAINS_PER_TEAM
        label = f"{'⛔ ' if full else ''}{name} ({cnt}/{MAX_MAINS_PER_TEAM})"
        rows.append([_btn(label, "team:full" if full else f"{cb_prefix}{tid}")])
    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- заявки ----------

def approve_kb(request_id: int) -> InlineKeyboardMarkup:
    """Под каждой заявкой: Резерв / Основа / Отклонить."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            _btn("🔄 Резерв", f"req:reserve:{request_id}"),
            _btn("🏎 Основа", f"req:main:{request_id}"),
        ],
        [_btn("❌ Отклонить", f"req:reject:{request_id}")],
    ])