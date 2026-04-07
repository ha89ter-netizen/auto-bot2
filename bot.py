import os
from dotenv import load_dotenv
from openai import OpenAI
import urllib.parse
import requests
import json
import re
import sqlite3

from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler
)

# ------------------ ЗАГРУЗКА ------------------
load_dotenv()

TOKEN = os.getenv("TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

SYSTEM_PROMPT = """
Ты помощник по автомобилям.
Отвечай кратко и по делу.
Помогай определить проблему и при необходимости предлагай сервис.
"""

client = OpenAI(api_key=OPENAI_API_KEY)

# ------------------ БАЗА ------------------
user_states = {}

conn = sqlite3.connect("bot.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    latitude REAL,
    longitude REAL
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    role TEXT,
    content TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS cars (
    user_id INTEGER PRIMARY KEY,
    brand TEXT,
    model TEXT,
    generation TEXT,
    year INTEGER
)
""")

conn.commit()

# ------------------ КАТЕГОРИИ ------------------
CATEGORY_MAP = {
    "category_engine": ("🔧 Двигатель / СТО", ["автосервис"]),
    "category_wheel": ("🛞 Шиномонтаж", ["шиномонтаж"]),
    "category_wash": ("🚿 Автомойка", ["автомойка"]),
    "category_parts": ("🔩 Запчасти", ["автозапчасти"]),
    "category_other": ("📍 Общее", ["СТО"]),
}

# ------------------ МЕНЮ ------------------
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🚗 Указать авто", "📍 Найти сервис"],
        ],
        resize_keyboard=True
    )

# ------------------ HELPERS ------------------
def save_message(user_id, role, content):
    cursor.execute(
        "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
        (user_id, role, content)
    )
    conn.commit()

def get_last_messages(user_id, limit=10):
    cursor.execute(
        "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    rows = cursor.fetchall()
    rows.reverse()
    return [{"role": r[0], "content": r[1]} for r in rows]

def save_location(user_id, lat, lon):
    cursor.execute("INSERT OR REPLACE INTO users VALUES (?, ?, ?)", (user_id, lat, lon))
    conn.commit()

def get_user_car(user_id):
    cursor.execute("SELECT brand, model FROM cars WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    return f"{row[0]} {row[1]}" if row else None

def save_user_car(user_id, brand, model):
    cursor.execute(
        "INSERT OR REPLACE INTO cars VALUES (?, ?, ?, ?, ?)",
        (user_id, brand, model, "", None)
    )
    conn.commit()

async def search_places(lat, lng, keyword):
    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"

    params = {
        "location": f"{lat},{lng}",
        "radius": 5000,
        "keyword": keyword,
        "key": GOOGLE_MAPS_API_KEY,
        "language": "ru",
    }

    res = requests.get(url, params=params).json()
    results = res.get("results", [])[:5]

    if not results:
        return "❌ Ничего не найдено"

    text = "🔍 Найдено:\n\n"
    for place in results:
        text += f"📍 {place['name']}\n⭐ {place.get('rating', '—')}\n\n"

    return text

# ------------------ HANDLERS ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Опиши проблему или выбери действие 👇",
        reply_markup=get_main_keyboard()
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    # --- кнопки ---
    if text == "🚗 Указать авто":
        user_states[user_id] = {"waiting_car": True}
        await update.message.reply_text("Напиши: Toyota Camry 2018")
        return

    if text == "📍 Найти сервис":
        await show_categories(update, context)
        return

    # --- ввод авто ---
    if user_states.get(user_id, {}).get("waiting_car"):
        try:
            parts = text.split()
            save_user_car(user_id, parts[0], parts[1])
            user_states[user_id]["waiting_car"] = False
            await update.message.reply_text("✅ Авто сохранено", reply_markup=get_main_keyboard())
        except:
            await update.message.reply_text("❌ Напиши нормально: BMW X5")
        return

    # --- GPT ---
    history = get_last_messages(user_id)

    if not history:
        history = [{"role": "system", "content": SYSTEM_PROMPT}]
    else:
        history.insert(0, {"role": "system", "content": SYSTEM_PROMPT})

    car = get_user_car(user_id)
    if car:
        text += f"\nАвто: {car}"

    history.append({"role": "user", "content": text})
    save_message(user_id, "user", text)

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=history
        )
        reply = response.choices[0].message.content
    except Exception as e:
        print(e)
        await update.message.reply_text("❌ Ошибка GPT")
        return

    save_message(user_id, "assistant", reply)
    await update.message.reply_text(reply)

async def show_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(text, callback_data=key)]
        for key, (text, _) in CATEGORY_MAP.items()
    ]

    await update.message.reply_text(
        "Выбери категорию:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    category = query.data

    user_states[user_id] = {"category": category}

    await query.message.reply_text(
        "Отправь геолокацию 📍",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("📍 Отправить локацию", request_location=True)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
    )

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    loc = update.message.location

    state = user_states.get(user_id, {})
    category = state.get("category")

    if not category:
        await update.message.reply_text("Сначала выбери категорию")
        return

    keyword = CATEGORY_MAP[category][1][0]
    result = await search_places(loc.latitude, loc.longitude, keyword)

    await update.message.reply_text(result, reply_markup=get_main_keyboard())

# ------------------ MAIN ------------------
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(CallbackQueryHandler(category_callback))

    print("🚀 Бот запущен")
    app.run_polling()
