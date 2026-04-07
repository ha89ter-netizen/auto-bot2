from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from openai import OpenAI
import sqlite3
import time
import urllib.parse
import requests

# ------------------ НАСТРОЙКИ ------------------
import os

TOKEN = os.getenv("TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

# ------------------ СОСТОЯНИЯ ------------------
user_states = {}

# ------------------ БАЗА ------------------
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

# ------------------ PROMPT ------------------
SYSTEM_PROMPT = """Ты авто-механик.
Отвечай кратко, по делу, шагами.
Если не хватает данных — спроси.
Если нужен СТО — предложи найти ближайшие."""

# ------------------ HELPERS ------------------
def save_message(user_id, role, content):
    cursor.execute("INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content))
    conn.commit()

def get_last_messages(user_id, limit=20):
    cursor.execute("SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))
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
    cursor.execute("INSERT OR REPLACE INTO cars VALUES (?, ?, ?, ?, ?)",
                   (user_id, brand, model, generation, year))
    conn.commit()

def get_user_car(user_id):
    cursor.execute("SELECT brand, model, generation, year FROM cars WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    return {"brand": row[0], "model": row[1], "generation": row[2], "year": row[3]} if row else None

def generate_maps_link(user_lat, user_lon, dest_lat, dest_lon):
    params = urllib.parse.urlencode({
        "api": "1",
        "origin": f"{user_lat},{user_lon}",
        "destination": f"{dest_lat},{dest_lon}"
    })
    return f"https://www.google.com/maps/dir/?{params}"

def find_nearby_services(lat, lon):
    url = f"https://maps.googleapis.com/maps/api/place/nearbysearch/json?location={lat},{lon}&radius=5000&type=car_repair&key={GOOGLE_MAPS_API_KEY}"
    res = requests.get(url).json()
    results = res.get("results", [])[:5]

    services = []
    for r in results:
        services.append({
            "name": r.get("name"),
            "rating": r.get("rating", "N/A"),
            "lat": r["geometry"]["location"]["lat"],
            "lon": r["geometry"]["location"]["lng"]
        })
    return services

# ------------------ START ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Опиши проблему или отправь фото")

# ------------------ MESSAGE ------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    state = user_states.get(user_id, {"waiting_for_car": False})
    car = get_user_car(user_id)

    # --- если ждем авто ---
    if state["waiting_for_car"]:
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Верни JSON: brand, model, generation, year"},
                    {"role": "user", "content": text}
                ],
                temperature=0,
                max_tokens=150
            )

            parsed = response.choices[0].message.content
            parsed = parsed.replace("```json", "").replace("```", "").strip()

            match = re.search(r"\{.*\}", parsed, re.DOTALL)
            if match:
                parsed = match.group(0)

            car_data = json.loads(parsed)

            year = car_data.get("year")
            year = int(year) if year and str(year).isdigit() else None

            save_user_car(user_id,
                          car_data.get("brand"),
                          car_data.get("model"),
                          car_data.get("generation"),
                          year)

            user_states[user_id]["waiting_for_car"] = False

            await update.message.reply_text("Авто сохранено ✅")

        except Exception as e:
            print(e)
            await update.message.reply_text("Напиши таким или схожим образом: Mercedes E55 2002")

        return

    # --- GPT ---
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

    # --- запрос авто ---
    if "марка" in reply.lower() and not car:
        user_states[user_id] = {"waiting_for_car": True}
        await update.message.reply_text("Напиши марку и модель авто")
        return

    # --- СТО ---
    if "сто" in reply.lower() and not get_location(user_id):
        markup = ReplyKeyboardMarkup([[KeyboardButton("📍 Локация", request_location=True)]], resize_keyboard=True)
        await update.message.reply_text(reply + "\nОтправь локацию", reply_markup=markup)
    else:
        await update.message.reply_text(reply)

# ------------------ LOCATION ------------------
async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    loc = update.message.location
    save_location(user_id, loc.latitude, loc.longitude)

    keyboard = [[InlineKeyboardButton("Да", callback_data="yes"),
                 InlineKeyboardButton("Нет", callback_data="no")]]

    await update.message.reply_text("Показать СТО?", reply_markup=InlineKeyboardMarkup(keyboard))

# ------------------ YES/NO ------------------
async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if query.data == "no":
        await query.edit_message_text("Ок")
        return

    loc = get_location(user_id)
    services = find_nearby_services(loc[0], loc[1])

    context.user_data["services"] = services

    buttons = [
        [InlineKeyboardButton(f"{s['name']} ⭐ {s['rating']}", callback_data=f"sto_{i}")]
        for i, s in enumerate(services)
    ]

    await query.edit_message_text("Выбери СТО:", reply_markup=InlineKeyboardMarkup(buttons))

# ------------------ STO ------------------
async def handle_sto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    index = int(query.data.split("_")[1])

    services = context.user_data.get("services", [])
    loc = get_location(user_id)

    if not services:
        await query.edit_message_text("Ошибка")
        return

    s = services[index]
    link = generate_maps_link(loc[0], loc[1], s["lat"], s["lon"])

    await query.edit_message_text(f"{s['name']} ⭐ {s['rating']}\n{link}")

# ------------------ PHOTO ------------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    app.add_handler(CallbackQueryHandler(handle_choice, pattern="^(yes|no)$"))
    app.add_handler(CallbackQueryHandler(handle_sto, pattern="^sto_"))

    print("Бот запущен")
    app.run_polling()
