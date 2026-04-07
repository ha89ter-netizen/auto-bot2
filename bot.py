import os
from dotenv import load_dotenv
from openai import OpenAI
import requests
import re
import sqlite3
from datetime import datetime

from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# ---------------- ENV ----------------
load_dotenv()
TOKEN = os.getenv("TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CLOUDINARY_URL = os.getenv("CLOUDINARY_URL")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

# ---------------- DB ----------------
conn = sqlite3.connect("bot.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    active_vin TEXT
)")

cursor.execute("""
CREATE TABLE IF NOT EXISTS cars (
    vin TEXT PRIMARY KEY,
    user_id INTEGER,
    brand TEXT,
    model TEXT,
    year TEXT,
    body TEXT,
    engine TEXT,
    transmission TEXT
)")

cursor.execute("""
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    vin TEXT,
    message_type TEXT,
    content TEXT,
    cloudinary_url TEXT,
    timestamp DATETIME
)")
conn.commit()

# ---------------- HELPERS ----------------

def save_user(user_id, active_vin=None):
    cursor.execute("INSERT OR REPLACE INTO users VALUES (?, ?)" , (user_id, active_vin))
    conn.commit()

def get_user(user_id):
    cursor.execute("SELECT active_vin FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    return {"active_vin": row[0]} if row else {"active_vin": None}

def save_history(user_id, vin, message_type, content, cloudinary_url=None):
    cursor.execute("INSERT INTO history (user_id, vin, message_type, content, cloudinary_url, timestamp) VALUES (?, ?, ?, ?, ?, ?)" ,
                   (user_id, vin, message_type, content, cloudinary_url, datetime.now()))
    conn.commit()

# ---------------- IDEAL VIN ----------------
def process_vin(user_id, vin):
    vin = vin.upper().strip()
    if not re.match(r"^[A-HJ-NPR-Z0-9]{17}$", vin):
        return None, "❌ Неверный VIN. Попробуй ещё раз."

    url = f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVin/{vin}?format=json"
    try:
        res = requests.get(url, timeout=5).json()
    except Exception:
        return None, "⚠️ Не удалось получить данные по VIN. Попробуй позже."

    brand = model = year = body = engine = transmission = None
    for item in res["Results"]:
        if item["Variable"] == "Make": brand = item["Value"]
        elif item["Variable"] == "Model": model = item["Value"]
        elif item["Variable"] == "Model Year": year = item["Value"]
        elif item["Variable"] == "Body Class": body = item["Value"]
        elif item["Variable"] == "Engine Model": engine = item["Value"]
        elif item["Variable"] == "Transmission Style": transmission = item["Value"]

    if not brand and not model:
        return None, "⚠️ По этому VIN авто не найдено. Можно ввести данные вручную."

    cursor.execute("INSERT OR REPLACE INTO cars VALUES (?, ?, ?, ?, ?, ?, ?, ?)" ,
                   (vin, user_id, brand, model, year, body, engine, transmission))
    cursor.execute("INSERT OR REPLACE INTO users VALUES (?, ?)" , (user_id, vin))
    conn.commit()

    return {
        "vin": vin,
        "brand": brand,
        "model": model,
        "year": year,
        "body": body,
        "engine": engine,
        "transmission": transmission
    }, f"✅ Авто добавлено: {brand} {model} {year}"

# ---------------- AI LOGIC ----------------
def analyze_step(problem, car_info, history):
    prompt = f"""
Ты автомеханик.
Авто: {car_info}
Проблема: {problem}
История последних сообщений: {history}

Ответь JSON с полями:
{{
"stage": "ask / solution / sto / tow",
"question": "если нужно уточнить",
"answer": "если есть решение",
"category": "engine / wheel / battery / other"
}}
"""
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.75
    )
    import json, re
    text = re.sub(r"```json|```", "", response.choices[0].message.content)
    return json.loads(text)

# ---------------- START ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Опиши проблему или отправь VIN / фото автомобиля")

# ---------------- MENU ----------------
async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup(
        [["➕ Добавить авто", "🚗 Мои авто"], ["🛠 Найти сервис"]],
        resize_keyboard=True
    )
    await update.message.reply_text("Меню:", reply_markup=keyboard)

# ---------------- MESSAGE ----------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # Обработка меню действий
    if text == "➕ Добавить авто":
        await update.message.reply_text("Отправь VIN нового авто")
        return

    if text == "🚗 Мои авто":
        cursor.execute("SELECT vin, brand, model, year FROM cars WHERE user_id=?", (user_id,))
        rows = cursor.fetchall()
        if not rows:
            await update.message.reply_text("У тебя пока нет добавленных авто")
            return
        msg = "Твои авто:\n"
        for idx, r in enumerate(rows):
            msg += f"{idx+1}. {r[1]} {r[2]} {r[3]} (VIN: {r[0]})\n"
        await update.message.reply_text(msg)
        return

    if text == "🛠 Найти сервис":
        keyboard = ReplyKeyboardMarkup(
            [["СТО", "Мойка"], ["Шиномонтаж", "Детейлинг"]],
            resize_keyboard=True
        )
        await update.message.reply_text("Выбери категорию сервиса:", reply_markup=keyboard)
        return

    # VIN
    vin_match = re.search(r"\b[A-HJ-NPR-Z0-9]{17}\b", text.upper())
    if vin_match:
        car_info, msg = process_vin(user_id, vin_match.group(0))
        await update.message.reply_text(msg)
        return

    # Если есть активный VIN
    user = get_user(user_id)
    if user["active_vin"]:
        cursor.execute("SELECT brand, model, year FROM cars WHERE vin=?", (user["active_vin"],))
        row = cursor.fetchone()
        car_info = f"{row[0]} {row[1]} {row[2]}" if row else "Неизвестное авто"
        cursor.execute("SELECT content FROM history WHERE vin=? ORDER BY timestamp DESC LIMIT 25", (user["active_vin"],))
        history_msgs = [r[0] for r in cursor.fetchall()]
        result = analyze_step(text, car_info, history_msgs)

        save_history(user_id, user["active_vin"], "text", text)

        if result["stage"] == "ask":
            await update.message.reply_text(result["question"])
        elif result["stage"] == "solution":
            await update.message.reply_text(result["answer"])
        elif result["stage"] == "tow":
            await update.message.reply_text("🚑 Лучше вызвать эвакуатор")
        elif result["stage"] == "sto":
            keyboard = ReplyKeyboardMarkup(
                [[KeyboardButton("📍 Отправить локацию", request_location=True)]],
                resize_keyboard=True
            )
            await update.message.reply_text("Найду СТО рядом. Отправь локацию", reply_markup=keyboard)
        return

    await update.message.reply_text("❗ Укажи VIN или добавь авто через меню /меню")

# ---------------- PHOTO ----------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()

    # TODO: загрузка в Cloudinary и получение cloudinary_url
    cloudinary_url = "https://res.cloudinary.com/your_account/sample.jpg"  # пример

    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user["active_vin"]:
        await update.message.reply_text("❗ Сначала добавь авто через VIN")
        return

    save_history(user_id, user["active_vin"], "photo", "Фото авто", cloudinary_url)

    await update.message.reply_text(f"Фото сохранено ✅")

# ---------------- LOCATION ----------------
async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lat = update.message.location.latitude
    lng = update.message.location.longitude

    params = {
        "location": f"{lat},{lng}",
        "radius": 5000,
        "keyword": "автосервис",
        "key": GOOGLE_MAPS_API_KEY
    }

    res = requests.get("https://maps.googleapis.com/maps/api/place/nearbysearch/json", params=params).json()
    places = res.get("results", [])[:5]

    text = "🔧 Ближайшие СТО:\n\n"
    for p in places:
        link = f"https://www.google.com/maps/dir/?api=1&destination={p['geometry']['location']['lat']},{p['geometry']['location']['lng']}"
        text += f"{p['name']} ⭐ {p.get('rating','-')} [Маршрут]({link})\n"

    await update.message.reply_text(text, parse_mode="Markdown")

# ---------------- MAIN ----------------
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("меню", menu))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))

    print("🔥 Умный бот запущен")
    app.run_polling()

