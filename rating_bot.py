"""
Telegram-бот для двух пользователей: оценки по датам и общий график.

Установка:
    pip install aiogram==3.* matplotlib

Перед запуском задайте переменные окружения:
    BOT_TOKEN      — токен от @BotFather
    USER_IDS       — два Telegram user ID через запятую, например 123456789,987654321

Запуск:
    python rating_bot.py

База SQLite (ratings.db) создаётся автоматически рядом с файлом.
Каждая пара user_id + дата уникальна: повторная оценка за дату обновляет старую.
"""

import asyncio
import os
import sqlite3
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# По умолчанию уже указаны ID участников. При необходимости их можно
# переопределить переменной окружения USER_IDS в том же порядке.
USER_ID_ORDER = [
    int(value.strip())
    for value in os.getenv("USER_IDS", "5944267232,762075450").split(",")
    if value.strip()
]
ALLOWED_USER_IDS = set(USER_ID_ORDER)
DB_PATH = Path(__file__).with_name("ratings.db")

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN.")
if len(ALLOWED_USER_IDS) != 2:
    raise RuntimeError("В USER_IDS нужно указать ровно два Telegram user ID.")

dp = Dispatcher()


class RatingForm(StatesGroup):
    waiting_for_score = State()


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ratings (
            user_id INTEGER NOT NULL,
            rating_date TEXT NOT NULL,
            score INTEGER NOT NULL CHECK(score > 0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, rating_date)
        )
    """)
    return conn


def save_rating(user_id: int, day: date, score: int):
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO ratings(user_id, rating_date, score, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, rating_date)
            DO UPDATE SET score=excluded.score, updated_at=excluded.updated_at
            """,
            (user_id, day.isoformat(), score, datetime.now().isoformat(timespec="seconds")),
        )


def load_ratings(start: date | None = None):
    query = "SELECT user_id, rating_date, score FROM ratings"
    params = []
    if start:
        query += " WHERE rating_date >= ?"
        params.append(start.isoformat())
    query += " ORDER BY rating_date"
    with db_connect() as conn:
        return conn.execute(query, params).fetchall()


def get_user_label(user_id: int) -> str:
    return "Владислав" if user_id == USER_ID_ORDER[0] else "Аня"


def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Поставить / изменить оценку", callback_data="rate")],
        [InlineKeyboardButton(text="📈 Общий график", callback_data="chart:all")],
        [
            InlineKeyboardButton(text="За неделю", callback_data="chart:7"),
            InlineKeyboardButton(text="За месяц", callback_data="chart:30"),
        ],
        [InlineKeyboardButton(text="📚 История оценок", callback_data="history")],
    ])


def calendar_keyboard(year: int, month: int):
    first_weekday, days_in_month = __import__("calendar").monthrange(year, month)
    rows = [[
        InlineKeyboardButton(text="Пн", callback_data="noop"),
        InlineKeyboardButton(text="Вт", callback_data="noop"),
        InlineKeyboardButton(text="Ср", callback_data="noop"),
        InlineKeyboardButton(text="Чт", callback_data="noop"),
        InlineKeyboardButton(text="Пт", callback_data="noop"),
        InlineKeyboardButton(text="Сб", callback_data="noop"),
        InlineKeyboardButton(text="Вс", callback_data="noop"),
    ]]

    cells = [InlineKeyboardButton(text=" ", callback_data="noop")] * first_weekday
    for day_num in range(1, days_in_month + 1):
        cells.append(InlineKeyboardButton(
            text=str(day_num),
            callback_data=f"date:{year:04d}-{month:02d}-{day_num:02d}",
        ))
        if len(cells) == 7:
            rows.append(cells)
            cells = []
    if cells:
        cells += [InlineKeyboardButton(text=" ", callback_data="noop")] * (7 - len(cells))
        rows.append(cells)

    prev_month = date(year, month, 1) - timedelta(days=1)
    next_month = (date(year, month, 28) + timedelta(days=4)).replace(day=1)
    rows.append([
        InlineKeyboardButton(text="◀️", callback_data=f"month:{prev_month.year}:{prev_month.month}"),
        InlineKeyboardButton(text=f"{year}-{month:02d}", callback_data="noop"),
        InlineKeyboardButton(text="▶️", callback_data=f"month:{next_month.year}:{next_month.month}"),
    ])
    rows.append([InlineKeyboardButton(text="Сегодня", callback_data=f"date:{date.today().isoformat()}")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def create_chart(days: int | None):
    start = date.today() - timedelta(days=days - 1) if days else None
    rows = load_ratings(start)
    if not rows:
        return None

    data = {uid: {} for uid in ALLOWED_USER_IDS}
    for uid, day_str, score in rows:
        data.setdefault(uid, {})[date.fromisoformat(day_str)] = score

    all_dates = sorted({date.fromisoformat(day_str) for _, day_str, _ in rows})
    fig, ax = plt.subplots(figsize=(10, 5), dpi=160)

    for uid in sorted(ALLOWED_USER_IDS):
        points = data.get(uid, {})
        xs = sorted(points)
        ys = [points[x] for x in xs]
        if xs:
            ax.plot(
                [datetime.combine(x, datetime.min.time()) for x in xs],
                ys,
                marker="o",
                linewidth=2,
                markersize=5,
                label=get_user_label(uid),
            )

    ax.set_title("Оценки дней — сравнение")
    ax.set_xlabel("Дата")
    ax.set_ylabel("Оценка")
    ax.grid(True, alpha=0.25)
    ax.legend()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    fig.autofmt_xdate()
    fig.tight_layout()

    output = BytesIO()
    fig.savefig(output, format="png", bbox_inches="tight")
    plt.close(fig)
    output.seek(0)
    return output.getvalue()


def format_history(days: int | None = 30):
    start = date.today() - timedelta(days=days - 1) if days else None
    rows = load_ratings(start)
    if not rows:
        return "Пока оценок нет."

    by_date = {}
    for uid, day_str, score in rows:
        by_date.setdefault(day_str, {})[uid] = score

    lines = ["📚 <b>История оценок</b>", ""]
    for day_str in sorted(by_date, reverse=True)[:30]:
        values = by_date[day_str]
        d = date.fromisoformat(day_str).strftime("%d.%m.%Y")
        first = values.get(USER_ID_ORDER[0])
        second = values.get(USER_ID_ORDER[1])
        lines.append(
            f"<b>{d}</b> — Владислав: {first if first is not None else '—'}"
            f" | Аня: {second if second is not None else '—'}"
        )
    return "\n".join(lines)


async def allowed_message(message: Message) -> bool:
    if message.from_user and message.from_user.id in ALLOWED_USER_IDS:
        return True
    await message.answer("Извини, этот бот доступен только двум участникам.")
    return False


async def allowed_callback(callback: CallbackQuery) -> bool:
    if callback.from_user.id in ALLOWED_USER_IDS:
        return True
    await callback.answer("Нет доступа.", show_alert=True)
    return False


@dp.message(CommandStart())
async def start(message: Message):
    if not await allowed_message(message):
        return
    await message.answer(
        "Привет! 👋\nЭто общий дневник оценок. Выберите действие:",
        reply_markup=main_menu(),
    )


@dp.message(Command("menu"))
async def menu(message: Message):
    if not await allowed_message(message):
        return
    await message.answer("Главное меню:", reply_markup=main_menu())


@dp.callback_query(F.data == "rate")
async def choose_date(callback: CallbackQuery, state: FSMContext):
    if not await allowed_callback(callback):
        return
    today = date.today()
    await callback.message.edit_text(
        "Выберите дату, за которую хотите поставить или изменить оценку:",
        reply_markup=calendar_keyboard(today.year, today.month),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("month:"))
async def change_month(callback: CallbackQuery):
    if not await allowed_callback(callback):
        return
    _, year, month = callback.data.split(":")
    await callback.message.edit_reply_markup(
        reply_markup=calendar_keyboard(int(year), int(month))
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("date:"))
async def select_date(callback: CallbackQuery, state: FSMContext):
    if not await allowed_callback(callback):
        return
    selected = date.fromisoformat(callback.data.split(":", 1)[1])
    await state.update_data(selected_date=selected.isoformat())
    await state.set_state(RatingForm.waiting_for_score)
    await callback.message.edit_text(
        f"Дата: <b>{selected.strftime('%d.%m.%Y')}</b>\n"
        "Отправьте оценку — любое целое положительное число.\n"
        "Например: 8, 15, 100.",
    )
    await callback.answer()


@dp.message(RatingForm.waiting_for_score)
async def receive_score(message: Message, state: FSMContext):
    if not await allowed_message(message):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("Нужно отправить целое положительное число, например 7 или 25.")
        return

    score = int(raw)
    data = await state.get_data()
    selected_date = date.fromisoformat(data["selected_date"])
    save_rating(message.from_user.id, selected_date, score)
    await state.clear()
    await message.answer(
        f"✅ Оценка <b>{score}</b> сохранена за {selected_date.strftime('%d.%m.%Y')}.\n"
        "Если за эту дату оценка уже была, она обновлена.",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data.startswith("chart:"))
async def show_chart(callback: CallbackQuery):
    if not await allowed_callback(callback):
        return
    period = callback.data.split(":", 1)[1]
    days = None if period == "all" else int(period)
    chart = create_chart(days)
    if chart is None:
        await callback.message.answer("Пока нет оценок для построения графика.")
    else:
        label = "за всё время" if days is None else f"за последние {days} дней"
        await callback.message.answer_photo(
            BufferedInputFile(chart, filename="ratings.png"),
            caption=f"📈 Общий график {label}",
        )
    await callback.answer()


@dp.callback_query(F.data == "history")
async def show_history(callback: CallbackQuery):
    if not await allowed_callback(callback):
        return
    await callback.message.answer(format_history(30), reply_markup=main_menu())
    await callback.answer()


@dp.callback_query(F.data == "cancel")
async def cancel(callback: CallbackQuery, state: FSMContext):
    if not await allowed_callback(callback):
        return
    await state.clear()
    await callback.message.edit_text("Действие отменено.", reply_markup=main_menu())
    await callback.answer()


@dp.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()


async def main():
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
