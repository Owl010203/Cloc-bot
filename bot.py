import asyncio
import json
import os
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATA_FILE = "data.json"
TZ = ZoneInfo("Europe/Moscow")

# ─────────────────────────────────────────────
# БАЗА ДАННЫХ
# ─────────────────────────────────────────────
def load_db():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"periods": [], "staff": []}

def save_db(db):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def gen_id():
    import random, string
    ts = format(int(datetime.now().timestamp() * 1000), 'x')
    return ts + ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

def get_dates(start: str, end: str):
    d = datetime.strptime(start, "%Y-%m-%d").date()
    ed = datetime.strptime(end, "%Y-%m-%d").date()
    result = []
    while d <= ed:
        result.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return result

def is_weekend(ds: str):
    return datetime.strptime(ds, "%Y-%m-%d").date().weekday() >= 5

def get_active_period(db):
    return db["periods"][0] if db["periods"] else None

def find_employee_by_tg(db, tg_id: int):
    for s in db["staff"]:
        if s.get("telegram_id") == tg_id:
            return s
    return None

def find_emp_in_period(period, name: str):
    for e in period["employees"]:
        if e["name"] == name:
            return e
    return None

def calc_hours(s: str, e: str):
    if not s or not e:
        return 0
    sh, sm = map(int, s.split(":"))
    eh, em = map(int, e.split(":"))
    minutes = (eh * 60 + em) - (sh * 60 + sm)
    if minutes < 0:
        minutes += 1440
    return round(minutes / 60 * 100) / 100

MONTHS = ['янв','фев','мар','апр','май','июн','июл','авг','сен','окт','ноя','дек']
WEEKDAYS = ['Пн','Вт','Ср','Чт','Пт','Сб','Вс']

def fmt_date(ds: str):
    d = datetime.strptime(ds, "%Y-%m-%d").date()
    return f"{d.day} {MONTHS[d.month-1]} ({WEEKDAYS[d.weekday()]})"

def fmt_date_short(ds: str):
    d = datetime.strptime(ds, "%Y-%m-%d").date()
    return f"{d.day} {MONTHS[d.month-1]}"

def now_msk():
    return datetime.now(TZ)

def today_msk():
    return now_msk().date().strftime("%Y-%m-%d")

def yesterday_msk():
    return (now_msk().date() - timedelta(days=1)).strftime("%Y-%m-%d")

def log_message(db, tg_id: int, name: str, action: str, detail: str):
    if "message_log" not in db:
        db["message_log"] = []
    db["message_log"].append({
        "tg_id": tg_id, "name": name, "action": action, "detail": detail,
        "ts": now_msk().strftime("%Y-%m-%d %H:%M:%S")
    })
    if len(db["message_log"]) > 2000:
        db["message_log"] = db["message_log"][-2000:]

# ─────────────────────────────────────────────
# ЛОГИКА ПЕРИОДОВ
# Периоды: 6–20 и 21–5 следующего месяца
# ─────────────────────────────────────────────
def calc_next_period_dates(after_date: str = None):
    """
    Вычисляет даты следующего периода после given date.
    Периоды: 6-е – 20-е и 21-е – 5-е следующего месяца.
    """
    if after_date:
        ref = datetime.strptime(after_date, "%Y-%m-%d").date()
    else:
        ref = now_msk().date()

    y, m = ref.year, ref.month

    # Определяем какой следующий период
    if ref.day <= 5:
        # Сейчас начало месяца (1–5), следующий: 6–20 этого месяца
        start = date(y, m, 6)
        end = date(y, m, 20)
    elif ref.day <= 20:
        # Сейчас 6–20, следующий: 21–5 следующего месяца
        start = date(y, m, 21)
        if m == 12:
            end = date(y + 1, 1, 5)
        else:
            end = date(y, m + 1, 5)
    else:
        # Сейчас 21–конец месяца, следующий: 6–20 следующего месяца
        if m == 12:
            start = date(y + 1, 1, 6)
            end = date(y + 1, 1, 20)
        else:
            start = date(y, m + 1, 6)
            end = date(y, m + 1, 20)

    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

def period_name_auto(start: str, end: str):
    s = datetime.strptime(start, "%Y-%m-%d").date()
    e = datetime.strptime(end, "%Y-%m-%d").date()
    if s.month == e.month:
        return f"{s.day}–{e.day} {MONTHS[s.month-1]}"
    else:
        return f"{s.day} {MONTHS[s.month-1]} – {e.day} {MONTHS[e.month-1]}"

def create_period_from_dates(db, start: str, end: str, staff_list: list, custom_name: str = None):
    all_dates = get_dates(start, end)
    off_dates = [d for d in all_dates if is_weekend(d)]
    name = custom_name or period_name_auto(start, end)
    new_period = {
        "id": gen_id(), "name": name, "start": start, "end": end,
        "dates": all_dates, "off": off_dates, "employees": []
    }
    for s in staff_list:
        new_period["employees"].append({
            "id": gen_id(), "name": s["name"], "rate": s.get("rate", 300),
            "days": {d: {"s": "", "e": ""} for d in all_dates},
            "adv": 0, "debtPaid": False, "paidOut": 0, "salaryPaid": False, "purchases": []
        })
    db["periods"].insert(0, new_period)
    return new_period

# ─────────────────────────────────────────────
# СОСТОЯНИЯ
# ─────────────────────────────────────────────
class RegState(StatesGroup):
    waiting_name = State()

class CheckinState(StatesGroup):
    choose_date = State()
    waiting_time = State()

class CheckoutState(StatesGroup):
    choose_date = State()
    waiting_time = State()

class AdminState(StatesGroup):
    add_staff_name = State()
    add_staff_rate = State()
    change_rate_value = State()
    new_period_name = State()
    new_period_start = State()
    new_period_end = State()
    new_period_staff = State()

# ─────────────────────────────────────────────
# КЛАВИАТУРЫ
# ─────────────────────────────────────────────
def main_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🟢 Пришёл"), KeyboardButton(text="🔴 Ушёл")],
        [KeyboardButton(text="📊 Мои часы"), KeyboardButton(text="❓ Помощь")],
    ], resize_keyboard=True)

def admin_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🟢 Пришёл"), KeyboardButton(text="🔴 Ушёл")],
        [KeyboardButton(text="📊 Мои часы"), KeyboardButton(text="👑 Админ")],
    ], resize_keyboard=True)

def admin_panel_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Скачать JSON", callback_data="admin_export")],
        [InlineKeyboardButton(text="👥 Сводка по сотрудникам", callback_data="admin_staff_list")],
        [InlineKeyboardButton(text="📅 Создать период вручную", callback_data="admin_new_period")],
        [InlineKeyboardButton(text="📋 Итоги текущего периода", callback_data="admin_current_period")],
        [InlineKeyboardButton(text="📆 Сводка за вчера", callback_data="admin_day_yesterday")],
    ])

def time_keyboard(action: str, chosen_date: str):
    now = now_msk().strftime("%H:%M")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⏰ Сейчас ({now})", callback_data=f"time_now|{action}|{chosen_date}")],
        [InlineKeyboardButton(text="✏️ Ввести вручную", callback_data=f"time_manual|{action}|{chosen_date}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="time_cancel")],
    ])

def date_keyboard(action: str, period):
    today = today_msk()
    dates = [d for d in period["dates"] if d <= today and d not in period.get("off", [])]
    dates = dates[-8:]
    buttons = []
    for d in reversed(dates):
        label = ("📅 Сегодня" if d == today else
                 ("📅 Вчера" if d == yesterday_msk() else f"📅 {fmt_date_short(d)}"))
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"date_pick|{action}|{d}")])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="time_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def is_admin(user_id: int):
    return user_id == ADMIN_ID

# ─────────────────────────────────────────────
# BOT + DISPATCHER
# ─────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ─────────────────────────────────────────────
# РЕГИСТРАЦИЯ
# ─────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    db = load_db()
    emp = find_employee_by_tg(db, message.from_user.id)
    if emp:
        kb = admin_keyboard() if is_admin(message.from_user.id) else main_keyboard()
        await message.answer(f"👋 С возвращением, *{emp['name']}*!", parse_mode="Markdown", reply_markup=kb)
    else:
        await message.answer(
            "👋 Привет! Я бот учёта рабочего времени.\n\nНапиши своё *имя и фамилию*:",
            parse_mode="Markdown"
        )
        await state.set_state(RegState.waiting_name)

@dp.message(RegState.waiting_name)
async def reg_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 2:
        await message.answer("❌ Слишком короткое имя, попробуй ещё раз:")
        return
    db = load_db()
    staff_match = next((s for s in db["staff"] if s["name"].lower() == name.lower()), None)
    if not staff_match:
        db["staff"].append({"id": gen_id(), "name": name, "rate": 300, "telegram_id": message.from_user.id})
    else:
        staff_match["telegram_id"] = message.from_user.id
        name = staff_match["name"]
    save_db(db)
    kb = admin_keyboard() if is_admin(message.from_user.id) else main_keyboard()
    await message.answer(f"✅ Готово! Зарегистрирован как *{name}*. Можешь отмечаться 👇", parse_mode="Markdown", reply_markup=kb)
    await state.clear()

# ─────────────────────────────────────────────
# ПРИХОД / УХОД
# ─────────────────────────────────────────────
async def start_checkin_checkout(message: Message, state: FSMContext, action: str):
    db = load_db()
    emp = find_employee_by_tg(db, message.from_user.id)
    if not emp:
        await message.answer("👋 Напиши своё имя для регистрации:")
        await state.set_state(RegState.waiting_name)
        return
    period = get_active_period(db)
    if not period:
        await message.answer("❌ Нет активного периода. Обратись к администратору.")
        return
    emoji = "🟢" if action == "checkin" else "🔴"
    word = "Приход" if action == "checkin" else "Уход"
    await message.answer(f"{emoji} *{word}* — выбери дату:", parse_mode="Markdown", reply_markup=date_keyboard(action, period))
    await state.set_state(CheckinState.choose_date if action == "checkin" else CheckoutState.choose_date)

@dp.message(F.text == "🟢 Пришёл")
async def checkin_start(message: Message, state: FSMContext):
    await start_checkin_checkout(message, state, "checkin")

@dp.message(F.text == "🔴 Ушёл")
async def checkout_start(message: Message, state: FSMContext):
    await start_checkin_checkout(message, state, "checkout")

@dp.callback_query(F.data.startswith("date_pick|"))
async def date_picked(callback: CallbackQuery, state: FSMContext):
    _, action, chosen_date = callback.data.split("|")
    emoji = "🟢" if action == "checkin" else "🔴"
    word = "Приход" if action == "checkin" else "Уход"
    await callback.message.edit_text(
        f"{emoji} *{word}* — {fmt_date(chosen_date)}\n\nВыбери время:",
        parse_mode="Markdown", reply_markup=time_keyboard(action, chosen_date)
    )
    await state.set_state(CheckinState.waiting_time if action == "checkin" else CheckoutState.waiting_time)
    await state.update_data(action=action, chosen_date=chosen_date)
    await callback.answer()

@dp.callback_query(F.data.startswith("time_now|"))
async def time_now_cb(callback: CallbackQuery, state: FSMContext):
    _, action, chosen_date = callback.data.split("|")
    now = now_msk().strftime("%H:%M")
    await process_time_entry(callback.message, state, now, callback.from_user.id, action, chosen_date)
    await callback.answer()

@dp.callback_query(F.data.startswith("time_manual|"))
async def time_manual_cb(callback: CallbackQuery, state: FSMContext):
    _, action, chosen_date = callback.data.split("|")
    await state.update_data(action=action, chosen_date=chosen_date)
    await callback.message.edit_text("✏️ Введи время *ЧЧ:ММ* (например `09:30`):", parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "time_cancel")
async def time_cancel_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Отменено.")
    await callback.answer()

@dp.message(CheckinState.waiting_time)
@dp.message(CheckoutState.waiting_time)
async def time_text_input(message: Message, state: FSMContext):
    try:
        parts = message.text.strip().split(":")
        h, m = int(parts[0]), int(parts[1])
        assert 0 <= h <= 23 and 0 <= m <= 59
        time_str = f"{h:02d}:{m:02d}"
    except:
        await message.answer("❌ Неверный формат. Введи как *ЧЧ:ММ*, например `09:00`:", parse_mode="Markdown")
        return
    data = await state.get_data()
    await process_time_entry(message, state, time_str, message.from_user.id,
                             data.get("action", "checkin"), data.get("chosen_date", today_msk()))

async def process_time_entry(message: Message, state: FSMContext, time_str: str,
                              user_id: int, action: str, chosen_date: str):
    is_checkin = action == "checkin"
    db = load_db()
    emp_staff = find_employee_by_tg(db, user_id)
    if not emp_staff:
        await message.answer("❌ Сотрудник не найден.")
        await state.clear()
        return
    period = get_active_period(db)
    if not period or chosen_date not in period["dates"]:
        await message.answer("❌ Дата не входит в активный период.")
        await state.clear()
        return

    emp = find_emp_in_period(period, emp_staff["name"])
    if not emp:
        emp = {
            "id": gen_id(), "name": emp_staff["name"], "rate": emp_staff.get("rate", 300),
            "days": {d: {"s": "", "e": ""} for d in period["dates"]},
            "adv": 0, "debtPaid": False, "paidOut": 0, "salaryPaid": False, "purchases": []
        }
        period["employees"].append(emp)

    if chosen_date not in emp["days"]:
        emp["days"][chosen_date] = {"s": "", "e": ""}
    emp["days"][chosen_date]["s" if is_checkin else "e"] = time_str

    log_message(db, user_id, emp_staff["name"],
                "Приход" if is_checkin else "Уход",
                f"{fmt_date(chosen_date)} {time_str}")
    save_db(db)

    day = emp["days"][chosen_date]
    hours = calc_hours(day.get("s", ""), day.get("e", ""))
    hours_text = f"\n⏱ Часов за день: *{hours} ч*" if hours > 0 else ""
    retro = "" if chosen_date == today_msk() else f"\n_(задним числом за {fmt_date(chosen_date)})_"
    emoji = "🟢" if is_checkin else "🔴"

    await message.answer(
        f"{emoji} *{'Приход' if is_checkin else 'Уход'} отмечен!*\n\n"
        f"👤 {emp_staff['name']}\n📅 {fmt_date(chosen_date)}\n🕐 *{time_str}*"
        f"{hours_text}{retro}",
        parse_mode="Markdown"
    )
    await state.clear()

# ─────────────────────────────────────────────
# МОИ ЧАСЫ
# ─────────────────────────────────────────────
@dp.message(F.text == "📊 Мои часы")
async def my_hours(message: Message, state: FSMContext):
    db = load_db()
    emp_staff = find_employee_by_tg(db, message.from_user.id)
    if not emp_staff:
        await message.answer("👋 Напиши своё имя для регистрации:")
        await state.set_state(RegState.waiting_name)
        return
    period = get_active_period(db)
    if not period:
        await message.answer("❌ Нет активного периода.")
        return
    emp = find_emp_in_period(period, emp_staff["name"])
    if not emp:
        await message.answer("ℹ️ Ты ещё не отмечался в текущем периоде.")
        return

    total_hours = 0
    lines = []
    for d in period["dates"]:
        if d in period.get("off", []):
            continue
        day = emp["days"].get(d, {})
        h = calc_hours(day.get("s", ""), day.get("e", ""))
        if h > 0:
            total_hours += h
            lines.append(f"  {fmt_date_short(d)}: {day.get('s','—')}–{day.get('e','—')} ({h}ч)")

    earned = round(total_hours * emp.get("rate", 0))
    adv = emp.get("adv", 0)
    left = max(0, earned - adv - emp.get("paidOut", 0))
    detail = "\n".join(lines[-7:]) if lines else "  Нет данных"
    if len(lines) > 7:
        detail = f"  ...ещё {len(lines)-7} дней раньше\n" + detail

    adv_line = f"📤 Аванс: {int(adv)}₽\n" if adv else ""
    left_line = f"✅ К выдаче: {left}₽" if left > 0 else "✅ Полностью выплачено"
    await message.answer(
        f"📊 *{emp_staff['name']}* — {period['name']}\n\n"
        f"*Последние отметки:*\n{detail}\n\n"
        f"⏱ Итого: *{round(total_hours*100)/100} ч*" + "\n"
        f"💰 Заработано: *{earned:,}₽*\n"
        f"{adv_line}{left_line}",
        parse_mode="Markdown"
    )
@dp.message(F.text == "❓ Помощь")
async def help_cmd(message: Message):
    await message.answer(
        "ℹ️ *Как пользоваться:*\n\n"
        "🟢 *Пришёл* — отметить начало дня\n"
        "🔴 *Ушёл* — отметить конец дня\n"
        "📊 *Мои часы* — твоя статистика\n\n"
        "💡 Можно выбрать *другую дату* — если забыл отметиться раньше.",
        parse_mode="Markdown"
    )

# ─────────────────────────────────────────────
# АДМИН ПАНЕЛЬ
# ─────────────────────────────────────────────
@dp.message(F.text == "👑 Админ")
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    db = load_db()
    period = get_active_period(db)
    period_info = f"📅 Активный период: *{period['name']}*" if period else "❌ Нет активного периода"
    next_start, next_end = calc_next_period_dates(period["end"] if period else None)
    next_name = period_name_auto(next_start, next_end)
    await message.answer(
        f"👑 *Панель администратора*\n\n"
        f"{period_info}\n"
        f"👥 Сотрудников: {len(db['staff'])}\n\n"
        f"_Следующий автопериод: {next_name}_",
        parse_mode="Markdown", reply_markup=admin_panel_inline()
    )

# ЭКСПОРТ
@dp.callback_query(F.data == "admin_export")
async def admin_export(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    db = load_db()
    filename = f"зарплата_{now_msk().strftime('%Y%m%d_%H%M')}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    await callback.message.answer_document(
        FSInputFile(filename),
        caption="📥 *Данные экспортированы!*\n\nЗагрузи в HTML-приложение кнопкой «📂 Загрузить данные».",
        parse_mode="Markdown"
    )
    os.remove(filename)
    await callback.answer("✅ Готово!")

@dp.message(Command("export"))
async def cmd_export(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    db = load_db()
    filename = f"зарплата_{now_msk().strftime('%Y%m%d_%H%M')}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    await message.answer_document(FSInputFile(filename), caption="📥 Данные для HTML-приложения.")
    os.remove(filename)

# СПИСОК СОТРУДНИКОВ
@dp.callback_query(F.data == "admin_staff_list")
async def admin_staff_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    db = load_db()
    if not db["staff"]:
        await callback.message.edit_text(
            "👥 Список пуст.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить", callback_data="admin_add_staff")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
            ])
        )
        await callback.answer()
        return
    buttons = [[InlineKeyboardButton(text=f"👤 {s['name']} — {s.get('rate',300)}₽/ч",
                                     callback_data=f"admin_emp|{s['name']}")] for s in db["staff"]]
    buttons.append([InlineKeyboardButton(text="➕ Добавить сотрудника", callback_data="admin_add_staff")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")])
    await callback.message.edit_text(
        f"👥 *Сотрудники ({len(db['staff'])})* — нажми для сводки или смены ставки:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()

# КАРТОЧКА СОТРУДНИКА
@dp.callback_query(F.data.startswith("admin_emp|"))
async def admin_emp_detail(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    name = callback.data.split("|", 1)[1]
    db = load_db()
    period = get_active_period(db)
    staff = next((s for s in db["staff"] if s["name"] == name), None)
    if not staff:
        await callback.answer("Сотрудник не найден.", show_alert=True)
        return

    tg_status = "✅ привязан" if staff.get("telegram_id") else "⚠️ не привязан к Telegram"
    lines = [f"👤 *{name}*", f"💰 Ставка: {staff.get('rate', 300)}₽/ч", f"Telegram: {tg_status}", ""]

    if period:
        emp = find_emp_in_period(period, name)
        if emp:
            total_h = 0
            worked_days = []
            for d in period["dates"]:
                if d in period.get("off", []):
                    continue
                day = emp["days"].get(d, {})
                h = calc_hours(day.get("s", ""), day.get("e", ""))
                if h > 0:
                    total_h += h
                    worked_days.append(f"  {fmt_date_short(d)}: {day.get('s','—')}–{day.get('e','—')} ({h}ч)")
            earned = round(total_h * emp.get("rate", 0))
            left = max(0, earned - emp.get("adv", 0) - emp.get("paidOut", 0))
            lines.append(f"📋 *Период: {period['name']}*")
            lines.append(f"⏱ {round(total_h*100)/100}ч | 💰 {earned:,}₽ | К выдаче: {left:,}₽")
            if worked_days:
                lines.append("*Рабочие дни:*")
                lines += worked_days[-10:]
        else:
            lines.append(f"📋 В периоде «{period['name']}» не отмечался.")

    # История отметок
    logs = [l for l in db.get("message_log", []) if l.get("name") == name]
    if logs:
        lines.append("\n📨 *Последние отметки:*")
        for l in logs[-8:]:
            lines.append(f"  {l['ts']} — {l['action']}: {l['detail']}")
    else:
        lines.append("\n📨 История отметок пуста.")

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить ставку", callback_data=f"admin_change_rate|{name}")],
            [InlineKeyboardButton(text="◀️ К списку", callback_data="admin_staff_list")],
        ])
    )
    await callback.answer()

# ИЗМЕНЕНИЕ СТАВКИ
@dp.callback_query(F.data.startswith("admin_change_rate|"))
async def admin_change_rate_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    name = callback.data.split("|", 1)[1]
    db = load_db()
    staff = next((s for s in db["staff"] if s["name"] == name), None)
    current_rate = staff.get("rate", 300) if staff else 300
    await state.update_data(change_rate_name=name)
    await state.set_state(AdminState.change_rate_value)
    await callback.message.answer(
        f"✏️ Изменение ставки для *{name}*\n"
        f"Текущая ставка: *{current_rate}₽/ч*\n\n"
        f"Введи новую ставку (₽/час):",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(AdminState.change_rate_value)
async def admin_change_rate_value(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        new_rate = float(message.text.strip())
        assert new_rate > 0
    except:
        await message.answer("❌ Введи положительное число, например `350`:", parse_mode="Markdown")
        return
    data = await state.get_data()
    name = data["change_rate_name"]
    db = load_db()

    # Меняем в базе сотрудников
    staff = next((s for s in db["staff"] if s["name"] == name), None)
    if staff:
        staff["rate"] = new_rate

    # Меняем в активном периоде тоже
    period = get_active_period(db)
    if period:
        emp = find_emp_in_period(period, name)
        if emp:
            emp["rate"] = new_rate

    save_db(db)
    await message.answer(
        f"✅ Ставка *{name}* обновлена: *{new_rate}₽/ч*\n\n"
        f"_Изменение применено и в текущем периоде._",
        parse_mode="Markdown"
    )
    await state.clear()

# ИТОГИ ПЕРИОДА
@dp.callback_query(F.data == "admin_current_period")
async def admin_current_period(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    db = load_db()
    period = get_active_period(db)
    if not period:
        await callback.message.edit_text("❌ Нет активного периода.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]]))
        await callback.answer()
        return
    lines = [f"📋 *{period['name']}*", f"🗓 {period['start']} — {period['end']}", ""]
    total_h = 0
    for emp in period["employees"]:
        h = sum(calc_hours(emp["days"].get(d, {}).get("s", ""), emp["days"].get(d, {}).get("e", ""))
                for d in period["dates"] if d not in period.get("off", []))
        h = round(h * 100) / 100
        total_h += h
        earned = round(h * emp.get("rate", 0))
        left = max(0, earned - emp.get("adv", 0) - emp.get("paidOut", 0))
        lines.append(f"• *{emp['name']}*: {h}ч → {earned:,}₽ (осталось: {left:,}₽)")
    lines.append(f"\n⏱ Всего: *{round(total_h*100)/100}ч*")
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 Скачать JSON", callback_data="admin_export")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
        ])
    )
    await callback.answer()

# СВОДКА ЗА ДЕНЬ
async def build_day_summary(target_date: str, db) -> str:
    period = get_active_period(db)
    if not period:
        return "❌ Нет активного периода."
    if target_date not in period["dates"]:
        return f"📅 {fmt_date(target_date)}\n\nЭта дата не входит в текущий период."

    is_off = target_date in period.get("off", [])
    lines = [f"📆 *Сводка за {fmt_date(target_date)}*"]
    if is_off:
        lines.append("_(выходной день)_")
    lines.append("")

    full, partial, missing = [], [], []
    for emp in period["employees"]:
        day = emp["days"].get(target_date, {})
        s, e = day.get("s", ""), day.get("e", "")
        h = calc_hours(s, e)
        if s and e:
            full.append(f"✅ *{emp['name']}*: {s}–{e} ({h}ч)")
        elif s:
            partial.append(f"🟡 *{emp['name']}*: пришёл в {s}, уход не отмечен")
        else:
            missing.append(f"❌ *{emp['name']}*")

    if full:
        lines.append("*Отработали полностью:*")
        lines += full
        lines.append("")
    if partial:
        lines.append("*Пришли, уход не отмечен:*")
        lines += partial
        lines.append("")
    if missing and not is_off:
        lines.append("*Не отметились:*")
        lines += missing
    if not period["employees"]:
        lines.append("Нет сотрудников в периоде.")

    return "\n".join(lines)

@dp.callback_query(F.data == "admin_day_yesterday")
async def admin_day_yesterday(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    db = load_db()
    text = await build_day_summary(yesterday_msk(), db)
    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()

@dp.message(Command("day"))
async def cmd_day(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    parts = message.text.strip().split()
    if len(parts) >= 2:
        try:
            datetime.strptime(parts[1], "%Y-%m-%d")
            target = parts[1]
        except:
            await message.answer("❌ Формат: `/day 2025-05-19`", parse_mode="Markdown")
            return
    else:
        target = yesterday_msk()
    db = load_db()
    text = await build_day_summary(target, db)
    await message.answer(text, parse_mode="Markdown")

# СОЗДАТЬ ПЕРИОД ВРУЧНУЮ
@dp.callback_query(F.data == "admin_new_period")
async def admin_new_period_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    db = load_db()
    period = get_active_period(db)
    next_start, next_end = calc_next_period_dates(period["end"] if period else None)
    next_name = period_name_auto(next_start, next_end)
    await callback.message.answer(
        f"📅 *Создание периода вручную*\n\n"
        f"Следующий автопериод был бы: *{next_name}* ({next_start} – {next_end})\n\n"
        f"Название (или `-` для авто):",
        parse_mode="Markdown"
    )
    await state.set_state(AdminState.new_period_name)
    await callback.answer()

@dp.message(AdminState.new_period_name)
async def admin_period_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.update_data(period_name=None if message.text.strip() == "-" else message.text.strip())
    await message.answer("📅 Дата начала (*ГГГГ-ММ-ДД*):", parse_mode="Markdown")
    await state.set_state(AdminState.new_period_start)

@dp.message(AdminState.new_period_start)
async def admin_period_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try:
        datetime.strptime(message.text.strip(), "%Y-%m-%d")
    except:
        await message.answer("❌ Формат: `2025-06-06`", parse_mode="Markdown")
        return
    await state.update_data(period_start=message.text.strip())
    await message.answer("📅 Дата конца:")
    await state.set_state(AdminState.new_period_end)

@dp.message(AdminState.new_period_end)
async def admin_period_end(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try:
        datetime.strptime(message.text.strip(), "%Y-%m-%d")
    except:
        await message.answer("❌ Формат: `2025-06-20`", parse_mode="Markdown")
        return
    data = await state.get_data()
    if message.text.strip() < data["period_start"]:
        await message.answer("❌ Конец должен быть после начала!")
        return
    await state.update_data(period_end=message.text.strip())
    db = load_db()
    if not db["staff"]:
        await message.answer("⚠️ Нет сотрудников. Добавь через 👑 Админ → Сотрудники.")
        await state.clear()
        return
    lines = "\n".join([f"  {i+1}. {s['name']}" for i, s in enumerate(db["staff"])])
    await message.answer(f"👥 Кого добавить?\nНомера через запятую или `все`:\n\n{lines}", parse_mode="Markdown")
    await state.set_state(AdminState.new_period_staff)

@dp.message(AdminState.new_period_staff)
async def admin_period_staff(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data()
    db = load_db()
    staff = db["staff"]
    text = message.text.strip().lower()
    if text == "все":
        selected = staff
    else:
        try:
            indices = [int(x.strip()) - 1 for x in text.split(",")]
            selected = [staff[i] for i in indices if 0 <= i < len(staff)]
        except:
            await message.answer("❌ Введи номера через запятую, например `1,2,3` или `все`:")
            return
    period = create_period_from_dates(db, data["period_start"], data["period_end"],
                                      selected, data.get("period_name"))
    save_db(db)
    names = ", ".join([s["name"] for s in selected])
    await message.answer(
        f"✅ *Период создан!*\n📅 {period['name']}\n🗓 {period['start']} — {period['end']}\n👥 {names}",
        parse_mode="Markdown"
    )
    await state.clear()

# ДОБАВИТЬ СОТРУДНИКА
@dp.callback_query(F.data == "admin_add_staff")
async def admin_add_staff_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    await callback.message.answer("👤 Введи имя нового сотрудника:")
    await state.set_state(AdminState.add_staff_name)
    await callback.answer()

@dp.message(AdminState.add_staff_name)
async def admin_add_staff_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.update_data(name=message.text.strip())
    await message.answer(f"💰 Ставка для *{message.text.strip()}* (₽/час):", parse_mode="Markdown")
    await state.set_state(AdminState.add_staff_rate)

@dp.message(AdminState.add_staff_rate)
async def admin_add_staff_rate(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try:
        rate = float(message.text.strip())
    except:
        await message.answer("❌ Введи число, например `300`:", parse_mode="Markdown")
        return
    data = await state.get_data()
    db = load_db()
    db["staff"].append({"id": gen_id(), "name": data["name"], "rate": rate})
    save_db(db)
    await message.answer(f"✅ *{data['name']}* добавлен, ставка {rate}₽/ч.", parse_mode="Markdown")
    await state.clear()

@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery):
    db = load_db()
    period = get_active_period(db)
    period_info = f"📅 Активный период: *{period['name']}*" if period else "❌ Нет активного периода"
    next_start, next_end = calc_next_period_dates(period["end"] if period else None)
    await callback.message.edit_text(
        f"👑 *Панель администратора*\n\n{period_info}\n👥 Сотрудников: {len(db['staff'])}\n\n"
        f"_Следующий автопериод: {period_name_auto(next_start, next_end)}_",
        parse_mode="Markdown", reply_markup=admin_panel_inline()
    )
    await callback.answer()

# ─────────────────────────────────────────────
# АВТО-РАССЫЛКА В 7:00 МСК + АВТОСОЗДАНИЕ ПЕРИОДА
# ─────────────────────────────────────────────
async def auto_create_next_period(db):
    """
    Создаёт следующий период автоматически, если текущий закончился.
    Берёт тех же сотрудников что и в предыдущем периоде.
    """
    period = get_active_period(db)
    if not period:
        return None

    today = today_msk()
    # Создаём новый период на следующий день после окончания текущего
    if today > period["end"]:
        next_start, next_end = calc_next_period_dates(period["end"])
        # Проверяем что такого периода ещё нет
        exists = any(p["start"] == next_start for p in db["periods"])
        if not exists:
            staff_names = [e["name"] for e in period["employees"]]
            staff_list = [s for s in db["staff"] if s["name"] in staff_names]
            if not staff_list:
                staff_list = db["staff"]  # если не нашли — берём всех
            new_period = create_period_from_dates(db, next_start, next_end, staff_list)
            save_db(db)
            return new_period
    return None

async def daily_jobs():
    """Каждый день в 7:00 МСК: авторассылка + автосоздание периода."""
    while True:
        now = now_msk()
        next_run = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        wait_seconds = (next_run - now).total_seconds()
        logger.info(f"Следующая авторассылка через {wait_seconds/3600:.1f} ч ({next_run.strftime('%Y-%m-%d %H:%M')} МСК)")
        await asyncio.sleep(wait_seconds)

        if not ADMIN_ID:
            continue
        try:
            db = load_db()

            # 1. Сводка за вчера
            text = await build_day_summary(yesterday_msk(), db)
            await bot.send_message(ADMIN_ID, f"🌅 *Автоотчёт — итоги вчерашнего дня*\n\n{text}", parse_mode="Markdown")

            # 2. Автосоздание нового периода если старый закончился
            new_period = await auto_create_next_period(db)
            if new_period:
                names = ", ".join([e["name"] for e in new_period["employees"]])
                await bot.send_message(
                    ADMIN_ID,
                    f"📅 *Автоматически создан новый период!*\n\n"
                    f"*{new_period['name']}*\n"
                    f"🗓 {new_period['start']} — {new_period['end']}\n"
                    f"👥 {names}\n\n"
                    f"_Сотрудники перенесены из предыдущего периода._",
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error(f"Ошибка в daily_jobs: {e}")

# ─────────────────────────────────────────────
# ЗАПУСК
# ─────────────────────────────────────────────
async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не задан!")
        return
    if not ADMIN_ID:
        logger.warning("ADMIN_ID не задан!")
    await start_web()
    asyncio.create_task(daily_jobs())
    logger.info("Бот запущен (авторассылка и автопериоды в 7:00 МСК)...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
