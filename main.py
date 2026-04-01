import asyncio
import json
import logging
from datetime import datetime, timedelta, time
from typing import Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Загрузка конфигурации
with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

BOT_TOKEN = CONFIG["bot_token"]
OWNER_ID = CONFIG["owner_id"]
INTERVAL_MIN = CONFIG["booking_interval_minutes"]
SERVICES = CONFIG["services"]
SCHEDULE = CONFIG["work_schedule"]
TIMEZONE = CONFIG["timezone"]
MAX_DAYS_AHEAD = CONFIG["max_days_ahead"]

# Словарь для хранения занятых слотов: ключ -> (date, time_start) -> данные записи
# В реальном проекте лучше использовать базу данных
bookings: Dict[Tuple[str, str], dict] = {}

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ----- Состояния FSM -----
class BookingState(StatesGroup):
    choosing_service = State()
    choosing_date = State()
    choosing_time = State()
    confirm = State()

# ----- Вспомогательные функции -----
def parse_time(time_str: str) -> time:
    """Преобразует строку 'HH:MM' в объект time."""
    return datetime.strptime(time_str, "%H:%M").time()

def get_working_hours(date: datetime) -> Optional[Tuple[time, time]]:
    """Возвращает (start_time, end_time) для указанной даты, если день рабочий."""
    weekday = date.strftime("%A").lower()  # monday, tuesday, ...
    day_schedule = SCHEDULE.get(weekday)
    if not day_schedule:
        return None
    start = parse_time(day_schedule["start"])
    end = parse_time(day_schedule["end"])
    return start, end

def generate_available_slots(
    date: datetime,
    service_duration: int,
    booked_slots: List[str]
) -> List[str]:
    """
    Генерирует список свободных временных слотов на заданную дату.
    :param date: дата
    :param service_duration: длительность услуги в минутах
    :param booked_slots: список занятых слотов (в формате "HH:MM")
    :return: список свободных слотов (в формате "HH:MM")
    """
    wh = get_working_hours(date)
    if not wh:
        return []
    start_time, end_time = wh

    # Преобразуем время в минуты от начала дня
    start_min = start_time.hour * 60 + start_time.minute
    end_min = end_time.hour * 60 + end_time.minute

    slots = []
    current = start_min
    while current + service_duration <= end_min:
        slot_time = datetime.combine(date, time.min) + timedelta(minutes=current)
        slot_str = slot_time.strftime("%H:%M")
        # Проверяем, не занят ли слот (учитываем длительность)
        occupied = False
        for booked in booked_slots:
            booked_time = datetime.strptime(booked, "%H:%M").time()
            booked_min = booked_time.hour * 60 + booked_time.minute
            # Если интервал пересекается с уже занятым слотом
            if current < booked_min + service_duration and current + service_duration > booked_min:
                occupied = True
                break
        if not occupied:
            slots.append(slot_str)
        current += INTERVAL_MIN
    return slots

def get_available_dates() -> List[datetime]:
    """Возвращает список доступных для записи дат (с учётом рабочих дней)."""
    today = datetime.now().date()
    dates = []
    for i in range(MAX_DAYS_AHEAD + 1):
        check_date = today + timedelta(days=i)
        if get_working_hours(check_date):
            dates.append(check_date)
    return dates

def format_service_list() -> str:
    """Формирует строку со списком услуг для вывода."""
    lines = []
    for idx, s in enumerate(SERVICES, 1):
        lines.append(f"{idx}. {s['name']} — {s['price']} руб. ({s['duration_minutes']} мин)")
    return "\n".join(lines)

def get_service_by_index(index: int) -> dict:
    """Возвращает услугу по её порядковому номеру (начиная с 1)."""
    if 1 <= index <= len(SERVICES):
        return SERVICES[index - 1]
    return None

# ----- Клавиатуры -----
def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Главная клавиатура с кнопкой 'Записаться'."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📅 Записаться")]],
        resize_keyboard=True
    )

def services_keyboard() -> InlineKeyboardMarkup:
    """Inline-клавиатура для выбора услуги."""
    builder = InlineKeyboardBuilder()
    for idx, s in enumerate(SERVICES, 1):
        builder.button(
            text=f"{s['name']} — {s['price']} руб.",
            callback_data=f"service_{idx}"
        )
    builder.adjust(1)
    return builder.as_markup()

def dates_keyboard() -> InlineKeyboardMarkup:
    """Inline-клавиатура для выбора даты."""
    dates = get_available_dates()
    builder = InlineKeyboardBuilder()
    for d in dates:
        builder.button(
            text=d.strftime("%d.%m.%Y (%A)"),
            callback_data=f"date_{d.strftime('%Y-%m-%d')}"
        )
    builder.adjust(1)
    return builder.as_markup()

def times_keyboard(slots: List[str]) -> InlineKeyboardMarkup:
    """Inline-клавиатура для выбора времени (слоты)."""
    builder = InlineKeyboardBuilder()
    for slot in slots:
        builder.button(text=slot, callback_data=f"time_{slot}")
    builder.adjust(3)  # по 3 кнопки в ряд
    return builder.as_markup()

def confirm_keyboard() -> InlineKeyboardMarkup:
    """Inline-клавиатура для подтверждения записи."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data="confirm_yes")
    builder.button(text="❌ Отмена", callback_data="confirm_no")
    builder.adjust(2)
    return builder.as_markup()

def cancel_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура с кнопкой отмены."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отменить запись")]],
        resize_keyboard=True
    )

# ----- Обработчики -----
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Обработчик команды /start."""
    await state.clear()
    await message.answer(
        f"Здравствуйте! Я бот для записи к специалисту.\n\n"
        f"Доступные услуги:\n{format_service_list()}\n\n"
        f"Нажмите кнопку «Записаться», чтобы выбрать услугу и время.",
        reply_markup=main_menu_keyboard()
    )

@dp.message(F.text == "📅 Записаться")
async def start_booking(message: Message, state: FSMContext):
    """Начало процесса записи."""
    await state.set_state(BookingState.choosing_service)
    await message.answer(
        "Выберите услугу:",
        reply_markup=services_keyboard()
    )

@dp.callback_query(StateFilter(BookingState.choosing_service), F.data.startswith("service_"))
async def process_service(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора услуги."""
    service_index = int(callback.data.split("_")[1])
    service = get_service_by_index(service_index)
    if not service:
        await callback.answer("Услуга не найдена")
        return

    await state.update_data(service=service)
    await state.set_state(BookingState.choosing_date)
    await callback.message.edit_text(
        f"Выбрана услуга: {service['name']} ({service['price']} руб., {service['duration_minutes']} мин)\n\n"
        f"Теперь выберите дату:",
        reply_markup=dates_keyboard()
    )
    await callback.answer()

@dp.callback_query(StateFilter(BookingState.choosing_date), F.data.startswith("date_"))
async def process_date(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора даты."""
    date_str = callback.data.split("_")[1]
    selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    data = await state.get_data()
    service = data["service"]

    # Получаем уже занятые слоты на эту дату
    booked = [slot.split("_")[1] for slot in bookings.keys() if slot.startswith(f"{date_str}_")]
    # Генерируем свободные слоты
    slots = generate_available_slots(selected_date, service["duration_minutes"], booked)

    if not slots:
        await callback.answer("На эту дату нет свободных слотов, выберите другую.", show_alert=True)
        return

    await state.update_data(date=selected_date, slots=slots)
    await state.set_state(BookingState.choosing_time)

    # Показываем клавиатуру со слотами
    await callback.message.edit_text(
        f"Услуга: {service['name']}\nДата: {selected_date.strftime('%d.%m.%Y')}\n\n"
        f"Выберите удобное время:",
        reply_markup=times_keyboard(slots)
    )
    await callback.answer()

@dp.callback_query(StateFilter(BookingState.choosing_time), F.data.startswith("time_"))
async def process_time(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора времени."""
    time_str = callback.data.split("_")[1]
    data = await state.get_data()
    service = data["service"]
    date = data["date"]
    slots = data["slots"]

    if time_str not in slots:
        await callback.answer("Это время больше недоступно, выберите другое.", show_alert=True)
        return

    await state.update_data(time=time_str)
    await state.set_state(BookingState.confirm)

    confirm_text = (
        f"Пожалуйста, проверьте данные записи:\n\n"
        f"Услуга: {service['name']}\n"
        f"Стоимость: {service['price']} руб.\n"
        f"Дата: {date.strftime('%d.%m.%Y')}\n"
        f"Время: {time_str}\n\n"
        f"Всё верно?"
    )
    await callback.message.edit_text(confirm_text, reply_markup=confirm_keyboard())
    await callback.answer()

@dp.callback_query(StateFilter(BookingState.confirm), F.data == "confirm_yes")
async def confirm_booking(callback: CallbackQuery, state: FSMContext):
    """Подтверждение записи."""
    data = await state.get_data()
    service = data["service"]
    date = data["date"]
    time_str = data["time"]

    # Сохраняем запись в словарь занятых слотов
    booking_key = f"{date.strftime('%Y-%m-%d')}_{time_str}"
    bookings[booking_key] = {
        "service": service,
        "user_id": callback.from_user.id,
        "username": callback.from_user.username,
        "full_name": callback.from_user.full_name
    }

    # Уведомление владельцу
    owner_msg = (
        f"🔔 Новая запись!\n\n"
        f"Клиент: @{callback.from_user.username or callback.from_user.full_name}\n"
        f"Услуга: {service['name']}\n"
        f"Дата: {date.strftime('%d.%m.%Y')}\n"
        f"Время: {time_str}\n"
        f"Стоимость: {service['price']} руб."
    )
    await bot.send_message(OWNER_ID, owner_msg)

    # Подтверждение клиенту
    await callback.message.edit_text(
        f"✅ Запись подтверждена!\n\n"
        f"Услуга: {service['name']}\n"
        f"Дата: {date.strftime('%d.%m.%Y')}\n"
        f"Время: {time_str}\n\n"
        f"Ждём вас!",
        reply_markup=None
    )

    # Возврат в главное меню
    await state.clear()
    await callback.message.answer(
        "Для новой записи нажмите «Записаться».",
        reply_markup=main_menu_keyboard()
    )
    await callback.answer()

@dp.callback_query(StateFilter(BookingState.confirm), F.data == "confirm_no")
async def cancel_booking(callback: CallbackQuery, state: FSMContext):
    """Отмена записи."""
    await state.clear()
    await callback.message.edit_text(
        "Запись отменена. Если захотите записаться снова, нажмите «Записаться».",
        reply_markup=None
    )
    await callback.message.answer(
        "Главное меню:",
        reply_markup=main_menu_keyboard()
    )
    await callback.answer()

# Обработчик кнопки "Отменить запись" (дополнительная функция)
@dp.message(F.text == "❌ Отменить запись")
async def cancel_booking_cmd(message: Message, state: FSMContext):
    """Отмена текущей записи (если пользователь уже в процессе)."""
    current_state = await state.get_state()
    if current_state:
        await state.clear()
        await message.answer("Запись отменена.", reply_markup=main_menu_keyboard())
    else:
        await message.answer("У вас нет активной записи.", reply_markup=main_menu_keyboard())

# ----- Запуск бота -----
async def main():
    logger.info("Запуск бота...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())