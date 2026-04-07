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

# ------------------ ЗАГРУЗКА ПЕРЕМЕННЫХ ------------------
load_dotenv()

TOKEN = os.getenv("TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

# ------------------ ПРОВЕРКА КЛЮЧЕЙ ------------------
print("🔍 Проверка переменных окружения:")
print(f"TOKEN: {'✅ Загружен' if TOKEN else '❌ Не найден'}")
print(f"OPENAI_API_KEY: {'✅ Загружен' if OPENAI_API_KEY else '❌ Не найден'}")
print(f"GOOGLE_MAPS_API_KEY: {'✅ Загружен' if GOOGLE_MAPS_API_KEY else '❌ Не найден'}")

if not OPENAI_API_KEY or not GOOGLE_MAPS_API_KEY:
    print("⚠️  ВНИМАНИЕ: Один из ключей отсутствует!")

client = OpenAI(api_key=OPENAI_API_KEY)

# ------------------ СОСТОЯНИЯ И БАЗА ------------------
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

# ------------------ КАТЕГОРИИ ПРОБЛЕМ ------------------
CATEGORY_MAP = {
    "category_engine":   ("🔧 Двигатель / СТО",     ["автосервис", "СТО", "car repair"]),
    "category_wheel":    ("🛞 Шиномонтаж / Колёса", ["шиномонтаж", "шины", "tires"]),
    "category_wash":     ("🚿 Автомойка",           ["автомойка", "мойка", "car wash"]),
    "category_parts":    ("🔩 Запчасти",            ["автозапчасти", "запчасти", "auto parts store"]),
    "category_detailing":("✨ Детейлинг",            ["детейлинг", "полировка", "detailing"]),
    "category_other":    ("📍 Другое / Общий сервис",["автосервис", "СТО"]),
}

# ------------------ HELPERS ------------------
def save_message(user_id, role, content):
    cursor.execute(
        "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
        (user_id, role, content)
    )
    conn.commit()

def get_last_messages(user_id, limit=20):
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

def get_location(user_id):
    cursor.execute("SELECT latitude, longitude FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    return (row[0], row[1]) if row else None

def save_user_car(user_id, brand, model, generation, year):
    cursor.execute(
        "INSERT OR REPLACE INTO cars VALUES (?, ?, ?, ?, ?)",
        (user_id, brand, model, generation, year)
    )
    conn.commit()

def get_user_car(user_id):
    cursor.execute(
        "SELECT brand, model, generation, year FROM cars WHERE user_id=?",
        (user_id,)
    )
    row = cursor.fetchone()
    return {"brand": row[0], "model": row[1], "generation": row[2], "year": row[3]} if row else None

def generate_maps_link(user_lat, user_lon, dest_lat, dest_lon):
    params = urllib.parse.urlencode({
        "api": "1",
        "origin": f"{user_lat},{user_lon}",
        "destination": f"{dest_lat},{dest_lon}"
    })
    return f"https://www.google.com/maps/dir/?{params}"

# Новый умный поиск по категории
async def search_places(lat: float, lng: float, keywords: list, radius: int = 8000):
    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    main_keyword = keywords[0]

    params = {
        "location": f"{lat},{lng}",
        "radius": radius,
        "keyword": main_keyword,
        "key": GOOGLE_MAPS_API_KEY,
        "language": "ru",
    }

    try:
        res = requests.get(url, params=params, timeout=10).json()
        results = res.get("results", [])[:5]

        if not results:
            return f"😔 Рядом не найдено мест по запросу «{main_keyword}»."

        text = f"🔍 Найдено ближайших мест ({main_keyword}):\n\n"
        for place in results:
            name = place.get("name", "Без названия")
            address = place.get("vicinity", "Адрес не указан")
            rating = place.get("rating", "—")
            text += f"📍 <b>{name}</b>\n📌 {address}\n⭐ {rating}\n\n"

        return text
    except Exception as e:
        print(e)
        return "❌ Ошибка поиска на Google Maps"

# ------------------ HANDLERS ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Опиши проблему с авто или отправь фото")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip().lower()

    state = user_states.get(user_id, {})
    car = get_user_car(user_id)

    # Обработка сохранения авто
    if state.get("waiting_for_car"):
        # ... (твой старый код сохранения авто оставляем без изменений)
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "system", "content": "Верни JSON: brand, model, generation, year"},
                          {"role": "user", "content": text}],
                temperature=0,
                max_tokens=150
            )
            parsed = response.choices[0].message.content
            parsed = re.sub(r"```json|```", "", parsed).strip()
            match = re.search(r"\{.*\}", parsed, re.DOTALL)
            if match:
                car_data = json.loads(match.group(0))
                year = int(car_data.get("year")) if str(car_data.get("year")).isdigit() else None
                save_user_car(user_id, car_data.get("brand"), car_data.get("model"),
                              car_data.get("generation"), year)
                user_states[user_id]["waiting_for_car"] = False
                await update.message.reply_text("Авто сохранено ✅")
        except:
            await update.message.reply_text("Напиши типа: Mercedes E55 2002")
        return

    # Основной чат с GPT
    history = get_last_messages(user_id)
    if not history:
        history = [{"role": "system", "content": SYSTEM_PROMPT}]

    car_text = f"\nАвто: {car['brand']} {car['model']}" if car else ""
    history.append({"role": "user", "content": text + car_text})
    save_message(user_id, "user", text)

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=history,
            max_tokens=400
        )
        reply = response.choices[0].message.content
        save_message(user_id, "assistant", reply)
    except Exception as e:
        print(e)
        await update.message.reply_text("Ошибка GPT")
        return

    # Если GPT просит марку авто
    if ("марка" in reply.lower() or "модель" in reply.lower()) and not car:
        user_states[user_id] = {"waiting_for_car": True}
        await update.message.reply_text("Напиши марку и модель авто (например: Toyota Camry 2018)")
        return

    # Если GPT предлагает найти СТО → показываем категории
    if any(word in reply.lower() for word in ["сто", "сервис", "мастер", "ремонт", "проблема", "сломал"]):
        await show_categories(update, context)
        return

    await update.message.reply_text(reply)


async def show_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показываем кнопки категорий"""
    keyboard = [
        [InlineKeyboardButton(text, callback_data=key)]
        for key, (text, _) in CATEGORY_MAP.items()
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Уточни тип проблемы, чтобы я нашёл подходящие места:",
        reply_markup=reply_markup
    )


async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    category_key = query.data

    user_states[user_id] = {"category": category_key}

    category_name = CATEGORY_MAP[category_key][0]

    await query.edit_message_text(
        f"Выбрано: <b>{category_name}</b>\n\n"
        "Теперь отправь свою локацию (нажми на кнопку 📍 ниже)",
        parse_mode='HTML',
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("📍 Отправить локацию", request_location=True)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
    )


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    loc = update.message.location

    save_location(user_id, loc.latitude, loc.longitude)

    state = user_states.get(user_id, {})
    category_key = state.get("category")

    if not category_key:
        await update.message.reply_text("Сначала выбери категорию проблемы.")
        return

    keywords = CATEGORY_MAP.get(category_key, [None])[1]
    result_text = await search_places(loc.latitude, loc.longitude, keywords)

    await update.message.reply_text(result_text, parse_mode='HTML')

    # Очищаем состояние
    if user_id in user_states:
        user_states[user_id].pop("category", None)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # твой старый код обработки фото
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Определи проблему авто"},
                    {"type": "image_url", "image_url": {"url": file.file_path}}
                ]
            }],
            max_tokens=300
        )
        await update.message.reply_text(response.choices[0].message.content)
    except Exception as e:
        print(e)
        await update.message.reply_text("Ошибка анализа фото")


# ------------------ MAIN ------------------
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.add_handler(CallbackQueryHandler(category_callback, pattern="^category_"))

    print("✅ Бот запущен с умным поиском по категориям")
    app.run_polling()
