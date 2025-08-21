import os
import psycopg2
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from flask import Flask, request, jsonify

# ------------------- BASE DE DATOS -------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
conn = psycopg2.connect(DATABASE_URL, sslmode="require")
cursor = conn.cursor()

def fetchone(query, params=None):
    cursor.execute(query, params or ())
    return cursor.fetchone()

def fetchall(query, params=None):
    cursor.execute(query, params or ())
    return cursor.fetchall()

def ejecutar(query, params=None):
    cursor.execute(query, params or ())
    conn.commit()

# ------------------- NIVELES -------------------
NIVELES = [
    "Iniciacion",
    "5 baja", "5 media", "5 alta",
    "4 baja", "4 media", "4 alta",
    "3 baja", "3 media", "3 alta",
    "Profesional"
]

def nivel_index(nivel):
    try:
        return NIVELES.index(nivel)
    except ValueError:
        return -1

def es_nivel_compatible(nivel_jugador, nivel_partido):
    return abs(nivel_index(nivel_jugador) - nivel_index(nivel_partido)) <= 1

# ------------------- BOT -------------------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
scheduler = AsyncIOScheduler()
scheduler.start()

# ------------------- REGISTRO -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    jugador = fetchone("SELECT * FROM jugadores WHERE id_telegram=%s", (user_id,))
    if jugador:
        await update.message.reply_text("Ya estás registrado ✅")
        return

    keyboard = [[InlineKeyboardButton(n, callback_data=f"nivel_{n}")] for n in NIVELES]
    markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Selecciona tu nivel:", reply_markup=markup)

async def seleccionar_nivel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    nivel = query.data.split("_", 1)[1]

    ejecutar(
        "INSERT INTO jugadores (id_telegram, nombre, nivel) VALUES (%s,%s,%s)",
        (user_id, query.from_user.first_name, nivel)
    )
    await query.edit_message_text(f"Te has registrado con nivel {nivel} ✅")

# ------------------- CREAR PARTIDOS -------------------
partido_temporal = {}

HORAS = [f"{h:02d}:00" for h in range(8, 24)]  # Ej: 08:00, 09:00...
LUGARES = ["Pista 1", "Pista 2", "Pista 3"]

async def crear_partido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    partido_temporal[user_id] = {"paso": "nivel"}
    keyboard = [[InlineKeyboardButton(n, callback_data=f"partido_nivel_{n}")] for n in NIVELES]
    markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Selecciona el nivel del partido:", reply_markup=markup)

async def seleccionar_nivel_partido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    partido_temporal[user_id]["nivel"] = query.data.split("_", 2)[2]
    partido_temporal[user_id]["paso"] = "hora_inicio"

    keyboard = [[InlineKeyboardButton(h, callback_data=f"hora_inicio_{h}")] for h in HORAS]
    markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("Selecciona la hora de inicio:", reply_markup=markup)

async def seleccionar_hora_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    partido_temporal[user_id]["hora_inicio"] = query.data.split("_", 2)[2]
    partido_temporal[user_id]["paso"] = "hora_fin"

    keyboard = [[InlineKeyboardButton(h, callback_data=f"hora_fin_{h}")] for h in HORAS]
    markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("Selecciona la hora de fin:", reply_markup=markup)

async def seleccionar_hora_fin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    partido_temporal[user_id]["hora_fin"] = query.data.split("_", 2)[2]
    partido_temporal[user_id]["paso"] = "lugar"

    keyboard = [[InlineKeyboardButton(l, callback_data=f"lugar_{l}")] for l in LUGARES]
    markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("Selecciona el lugar del partido:", reply_markup=markup)

async def seleccionar_lugar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    partido_temporal[user_id]["lugar"] = query.data.split("_", 1)[1]
    partido_temporal[user_id]["paso"] = "precio"
    await query.edit_message_text("Escribe el precio por persona (solo número):")

async def mensaje_precio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in partido_temporal or partido_temporal[user_id]["paso"] != "precio":
        return

    precio = update.message.text
    partido_temporal[user_id]["precio"] = precio

    cursor.execute(
        """
        INSERT INTO partidos (creador_id, nivel, hora_inicio, hora_fin, lugar, precio, jugadores, reserva_confirmada, cancelado)
        VALUES (%s,%s,%s,%s,%s,%s,%s,FALSE,FALSE) RETURNING id_partido
        """,
        (
            user_id,
            partido_temporal[user_id]["nivel"],
            partido_temporal[user_id]["hora_inicio"],
            partido_temporal[user_id]["hora_fin"],
            partido_temporal[user_id]["lugar"],
            float(precio),
            [user_id],
        ),
    )
    id_partido = cursor.fetchone()[0]
    conn.commit()

    botones = [
        [InlineKeyboardButton("Unirse", callback_data=f"unirse_{id_partido}")],
        [InlineKeyboardButton("Salir", callback_data=f"salir_{id_partido}")],
        [InlineKeyboardButton("Cancelar (creador)", callback_data=f"cancelar_{id_partido}")],
        [InlineKeyboardButton("Confirmar reserva", callback_data=f"confirmar_{id_partido}")]
    ]
    markup = InlineKeyboardMarkup(botones)

    await update.message.reply_text(
        f"🎾 Partido creado!\nNivel: {partido_temporal[user_id]['nivel']}\n"
        f"Hora: {partido_temporal[user_id]['hora_inicio']} - {partido_temporal[user_id]['hora_fin']}\n"
        f"Lugar: {partido_temporal[user_id]['lugar']}\n"
        f"Precio por persona: {partido_temporal[user_id]['precio']}€",
        reply_markup=markup,
    )

    del partido_temporal[user_id]

# ------------------- CONSULTAR PARTIDOS -------------------
async def consultar_partidos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    jugador = fetchone("SELECT nivel FROM jugadores WHERE id_telegram=%s", (user_id,))
    if not jugador:
        await update.message.reply_text("Debes registrarte primero con /start")
        return
    nivel_jugador = jugador[0]

    hoy = datetime.now().date()
    partidos = fetchall(
        "SELECT id_partido, nivel, hora_inicio, hora_fin, lugar FROM partidos WHERE hora_inicio::date = %s AND cancelado=FALSE",
        (hoy,),
    )

    texto = "🎾 Partidos disponibles hoy compatibles con tu nivel:\n"
    for p in partidos:
        id_partido, nivel_partido, hora_inicio, hora_fin, lugar = p
        if es_nivel_compatible(nivel_jugador, nivel_partido):
            texto += f"- Partido {id_partido}: {hora_inicio} - {hora_fin} en {lugar}\n"
    await update.message.reply_text(texto)

# ------------------- EVALUACIÓN -------------------
async def enviar_evaluacion(id_partido):
    jugadores = fetchall("SELECT unnest(jugadores) FROM partidos WHERE id_partido=%s", (id_partido,))
    for (jugador,) in jugadores:
        print(f"Enviar evaluación a {jugador} del partido {id_partido}")

# ------------------- FLASK / WEBHOOK -------------------
flask_app = Flask(__name__)
bot_app = Application.builder().token(TOKEN).build()

bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CallbackQueryHandler(seleccionar_nivel, pattern=r"^nivel_"))
bot_app.add_handler(CommandHandler("crear_partido", crear_partido))
bot_app.add_handler(CallbackQueryHandler(seleccionar_nivel_partido, pattern=r"^partido_nivel_"))
bot_app.add_handler(CallbackQueryHandler(seleccionar_hora_inicio, pattern=r"^hora_inicio_"))
bot_app.add_handler(CallbackQueryHandler(seleccionar_hora_fin, pattern=r"^hora_fin_"))
bot_app.add_handler(CallbackQueryHandler(seleccionar_lugar, pattern=r"^lugar_"))
bot_app.add_handler(CommandHandler("partidos", consultar_partidos))
bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje_precio))

@flask_app.route("/")
def home():
    return "Bot running!"

@flask_app.route("/webhook", methods=["POST"])
async def webhook():
    data = await request.get_json()
    update = Update.de_json(data, bot_app.bot)
    await bot_app.process_update(update)
    return jsonify({"ok": True})

if __name__ == "__main__":
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_until_complete(bot_app.bot.set_webhook(WEBHOOK_URL))
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))