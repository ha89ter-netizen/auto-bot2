import os
import sqlite3
from urllib.parse import urlparse
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)

# ---------------- ENV ----------------
load_dotenv()
TOKEN = os.getenv("TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
CLOUDINARY_URL = os.getenv("CLOUDINARY_URL")

# ---------- Настройка Cloudinary ----------
if CLOUDINARY_URL:
    parsed = urlparse(CLOUDINARY_URL)
    CLOUDINARY_NAME = parsed.hostname
    CLOUDINARY_KEY = parsed.username
    CLOUDINARY_SECRET = parsed.password

    import cloudinary
    import cloudinary.uploader
    cloudinary.config(
        cloud_name=CLOUDINARY_NAME,
        api_key=CLOUDINARY_KEY,
        api_secret=CLOUDINARY_SECRET
    )
else:
    raise ValueError("CLOUDINARY_URL не задана в переменных окружения")

# ---------------- OpenAI ----------------
client = OpenAI(api_key=OPENAI_API_KEY)

# ---------------- DB ----------------
conn = sqlite3.connect("bot.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER,
    vin TEXT,
    problem TEXT,
    PRIMARY KEY (user_id, vin)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    vin TEXT,
    type TEXT,
    content TEXT,
    url TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()
# ---------------- HELPERS ----------------
def save_user(user_id, vin, problem=None):
    cursor.execute("""
    INSERT OR IGNORE INTO users (user_id, vin, problem) VALUES (?, ?, ?)
    """, (user_id, vin, problem))
    cursor.execute("""
    UPDATE users SET problem=? WHERE user_id=? AND vin=?
    """, (problem, user_id, vin))
    conn.commit()

def get_user(user_id, vin=None):
    if vin:
        cursor.execute("SELECT problem FROM users WHERE user_id=? AND vin=?", (user_id, vin))
        row = cursor.fetchone()
        return {"problem": row[0]} if row else {"problem": None}
    else:
        cursor.execute("SELECT vin, problem FROM users WHERE user_id=?", (user_id,))
        rows = cursor.fetchall()
        return [{"vin": r[0], "problem": r[1]} for r in rows]

def save_history(user_id, vin, type_, content, url=None):
    cursor.execute("""
    INSERT INTO history (user_id, vin, type, content, url) VALUES (?, ?, ?, ?, ?)
    """, (user_id, vin, type_, content, url))
    # Удаляем старые записи, чтобы держать последние 25
    cursor.execute("""
    DELETE FROM history WHERE id NOT IN (
        SELECT id FROM history WHERE user_id=? AND vin=? ORDER BY timestamp DESC LIMIT 25
    ) AND user_id=? AND vin=?
    """, (user_id, vin, user_id, vin))
    conn.commit()

def get_car_by_vin(vin):
    url = f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVin/{vin}?format=json"
    res = requests.get(url).json()
    brand = model = year = None
    for item in res["Results"]:
        if item["Variable"] == "Make":
            brand = item["Value"]
        elif item["Variable"] == "Model":
            model = item["Value"]
        elif item["Variable"] == "Model Year":
            year = item["Value"]
    return f"{brand} {model} {year}" if brand else None

def analyze_step(problem, car):
    prompt = f"""
Ты автомеханик. Дано:
Проблема: {problem}
Авто: {car}

Ответь JSON:
{{
 "stage": "ask / solution / sto / tow",
 "question": "если нужно уточнить",
 "answer": "если есть решение",
 "category": "engine / wheel / battery / unknown"
}}
"""
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.75
    )
    text = response.choices[0].message.content
    text = re.sub(r"```json|```", "", text)
    return json.loads(text)

# ---------------- COMMANDS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Опиши проблему или отправь фото авто.")

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    autos = get_user(user_id)
    text = "📋 Твои авто:\n"
    for a in autos:
        text += f"- {a['vin']} | Проблемы: {a['problem'] or '-'}\n"
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("➕ Добавить авто")], [KeyboardButton("📍 Найти сервис")]],
        resize_keyboard=True
    )
    await update.message.reply_text(text, reply_markup=keyboard)

# ---------------- MESSAGES ----------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # VIN
    vin_match = re.search(r"\b[A-HJ-NPR-Z0-9]{17}\b", text.upper())
    if vin_match:
        vin = vin_match.group(0)
        car = get_car_by_vin(vin)
        save_user(user_id, vin)
        await update.message.reply_text(f"Авто добавлено: {car} ✅")
        return

    # Определяем активный VIN (последний добавленный)
    autos = get_user(user_id)
    if not autos:
        await update.message.reply_text("Сначала добавь авто через VIN")
        return
    active_vin = autos[-1]["vin"]
    problem = text if not autos[-1]["problem"] else autos[-1]["problem"] + " | " + text
    save_user(user_id, active_vin, problem)
    save_history(user_id, active_vin, "text", text)

    result = analyze_step(problem, get_car_by_vin(active_vin))

    if result["stage"] == "ask":
        await update.message.reply_text(result["question"])
    elif result["stage"] == "solution":
        await update.message.reply_text(result["answer"])
    elif result["stage"] == "tow":
        await update.message.reply_text("🚑 Похоже, лучше вызвать эвакуатор")
    elif result["stage"] == "sto":
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("📍 Отправить локацию", request_location=True)]],
            resize_keyboard=True
        )
        await update.message.reply_text("Найду СТО рядом. Отправь локацию", reply_markup=keyboard)

# ---------------- PHOTO ----------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()

    try:
        result = cloudinary.uploader.upload(
            io.BytesIO(file_bytes),
            folder="car_photos",
            public_id=f"{update.effective_user.id}_{int(datetime.now().timestamp())}",
            overwrite=True
        )
        cloudinary_url_saved = result.get("secure_url")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка загрузки фото: {e}")
        return

    user_id = update.effective_user.id
    autos = get_user(user_id)
    if not autos:
        await update.message.reply_text("Сначала добавь авто через VIN")
        return
    active_vin = autos[-1]["vin"]
    save_history(user_id, active_vin, "photo", "Фото авто", cloudinary_url_saved)
    await update.message.reply_text(f"Фото сохранено ✅")

# ---------------- LOCATION ----------------
async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lat = update.message.location.latitude
    lng = update.message.location.longitude

    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("СТО"), KeyboardButton("Мойка"), KeyboardButton("Шиномонтаж")]],
        resize_keyboard=True
    )
    await update.message.reply_text("Выберите тип сервиса:", reply_markup=keyboard)
    context.user_data["lat"] = lat
    context.user_data["lng"] = lng

async def handle_service_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "lat" not in context.user_data or "lng" not in context.user_data:
        await update.message.reply_text("❗ Сначала отправь локацию")
        return

    service_type = update.message.text
    lat, lng = context.user_data["lat"], context.user_data["lng"]
    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    params = {"location": f"{lat},{lng}", "radius": 5000, "keyword": service_type, "key": GOOGLE_MAPS_API_KEY}
    res = requests.get(url, params=params).json()
    places = res.get("results", [])[:5]

    text = f"🔧 Ближайшие {service_type}:\n"
    for p in places:
        maps_link = f"https://www.google.com/maps/search/?api=1&query={p['geometry']['location']['lat']},{p['geometry']['location']['lng']}"
        text += f"{p['name']} ⭐ {p.get('rating','-')} | [Навигатор]({maps_link})\n"

    await update.message.reply_text(text, disable_web_page_preview=False)

# ---------------- MAIN ----------------
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))  # латиница для Telegram

    # Обработчик текста на русском
    async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.lower()
        if text == "меню":
            await menu(update, context)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Обработчики сообщений, фото, локации
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))

    print("🔥 Умный бот запущен")
    app.run_polling()
