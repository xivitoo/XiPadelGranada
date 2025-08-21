import os
import psycopg2
import asyncio
import threading
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from flask import Flask

# ------------------- CONFIG -------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
TOKEN = os.environ.get("TELEGRAM_TOKEN")

NIVELES = [
    "Iniciación",
    "5 baja", "5 media", "5 alta",
    "4 baja", "4 media", "4 alta",
    "3 baja", "3 media", "3 alta",
    "Profesional"
]

# ------------------- DB -------------------
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
def es_nivel_compatible(nivel_jugador, nivel_partido):
    idx_jugador = NIVELES.index(nivel_jugador)
    idx_partido = NIVELES.index(nivel_partido)
    return abs(idx_jugador - idx_partido) <= 1

# ------------------- BOT -------------------
scheduler = AsyncIOScheduler()
scheduler.start()

registro_temporal = {}
partido_temporal = {}

# ------------------- REGISTRO -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    jugador = fetchone("SELECT * FROM jugadores WHERE id_telegram=%s", (user_id,))
    if jugador:
        await update.message.reply_text("Ya estás registrado ✅")
        return
    registro_temporal[user_id] = {"paso": "nivel"}
    botones = [[InlineKeyboardButton(n, callback_data=f"nivel_{n}")] for n in NIVELES]
    markup = InlineKeyboardMarkup(botones)
    await update.message.reply_text("Selecciona tu nivel:", reply_markup=markup)

async def boton_registro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    if user_id not in registro_temporal:
        return
    if data.startswith("nivel_"):
        nivel = data.split("_", 1)[1]
        ejecutar(
            "INSERT INTO jugadores (id_telegram, nombre, division) VALUES (%s,%s,%s)",
            (user_id, query.from_user.first_name, nivel)
        )
        await query.edit_message_text(f"Registro completado ✅ Nivel: {nivel}")
        del registro_temporal[user_id]

# ------------------- CREAR PARTIDO -------------------

async def mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    texto = update.message.text

    if user_id in registro_temporal:
        await mensaje_registro(update, context)
    elif user_id in partido_temporal:
        await mensaje_partido(update, context)
    else:
        await update.message.reply_text("Usa /start para registrarte o /crear_partido para crear un partido.")

async def crear_partido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    partido_temporal[user_id] = {"paso": "nivel"}
    botones = [[InlineKeyboardButton(n, callback_data=f"partido_{n}")] for n in NIVELES]
    markup = InlineKeyboardMarkup(botones)
    await update.message.reply_text("Selecciona el nivel del partido:", reply_markup=markup)

async def boton_partido_nivel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id not in partido_temporal:
        return
    data = query.data
    if data.startswith("partido_"):
        nivel = data.split("_", 1)[1]
        partido_temporal[user_id]["nivel"] = nivel
        partido_temporal[user_id]["paso"] = "hora_inicio"
        await query.edit_message_text(f"Nivel del partido: {nivel}\nIndica la hora de inicio (YYYY-MM-DD HH:MM):")

async def mensaje_partido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    texto = update.message.text
    if user_id not in partido_temporal:
        return
    paso = partido_temporal[user_id]["paso"]

    if paso == "hora_inicio":
        partido_temporal[user_id]["hora_inicio"] = texto
        partido_temporal[user_id]["paso"] = "hora_fin"
        await update.message.reply_text("Indica la hora de fin (YYYY-MM-DD HH:MM):")
    elif paso == "hora_fin":
        partido_temporal[user_id]["hora_fin"] = texto
        partido_temporal[user_id]["paso"] = "lugar"
        await update.message.reply_text("Indica el lugar del partido:")
    elif paso == "lugar":
        partido_temporal[user_id]["lugar"] = texto
        partido_temporal[user_id]["paso"] = "precio"
        await update.message.reply_text("Indica el precio por persona:")
    elif paso == "precio":
        partido_temporal[user_id]["precio"] = texto
        nivel = partido_temporal[user_id]["nivel"]
        cursor.execute(
            """
            INSERT INTO partidos (creador_id, nivel, hora_inicio, hora_fin, lugar, precio, jugadores, reserva_confirmada, cancelado)
            VALUES (%s,%s,%s,%s,%s,%s,%s,FALSE,FALSE) RETURNING id_partido
            """,
            (
                user_id,
                nivel,
                partido_temporal[user_id]["hora_inicio"],
                partido_temporal[user_id]["hora_fin"],
                partido_temporal[user_id]["lugar"],
                float(partido_temporal[user_id]["precio"]),
                [user_id],
            ),
        )
        id_partido = cursor.fetchone()[0]
        conn.commit()
        botones = [
            [InlineKeyboardButton("Unirse", callback_data=f"unirse_{id_partido}")],
            [InlineKeyboardButton("Salir", callback_data=f"salir_{id_partido}")],
            [InlineKeyboardButton("Cancelar", callback_data=f"cancelar_{id_partido}")],
        ]
        markup = InlineKeyboardMarkup(botones)
        await update.message.reply_text(
            f"🎾 Partido creado!\nNivel: {nivel}\nHora: {partido_temporal[user_id]['hora_inicio']} - {partido_temporal[user_id]['hora_fin']}\nLugar: {partido_temporal[user_id]['lugar']}\nPrecio: {partido_temporal[user_id]['precio']}€",
            reply_markup=markup,
        )
        hora_fin_dt = datetime.strptime(partido_temporal[user_id]['hora_fin'], "%Y-%m-%d %H:%M")
        scheduler.add_job(enviar_evaluacion, 'date', run_date=hora_fin_dt + timedelta(hours=1), args=[id_partido])
        del partido_temporal[user_id]

# ------------------- CONSULTAR PARTIDOS -------------------
async def consultar_partidos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    jugador = fetchone("SELECT division FROM jugadores WHERE id_telegram=%s", (user_id,))
    if not jugador:
        await update.message.reply_text("Debes registrarte primero con /start")
        return
    nivel_jugador = jugador[0]
    hoy = datetime.now().date()
    partidos = fetchall("SELECT id_partido, nivel, hora_inicio, hora_fin, lugar FROM partidos WHERE hora_inicio::date = %s AND cancelado=FALSE", (hoy,))
    texto = "🎾 Partidos disponibles hoy compatibles:\n"
    for p in partidos:
        id_partido, nivel_partido, hora_inicio, hora_fin, lugar = p
        if es_nivel_compatible(nivel_jugador, nivel_partido):
            texto += f"- Partido {id_partido}: {hora_inicio} - {hora_fin} en {lugar}\n"
    await update.message.reply_text(texto if texto != "" else "No hay partidos compatibles hoy.")

# ------------------- EVALUACIÓN -------------------
async def enviar_evaluacion(id_partido):
    jugadores = fetchall("SELECT unnest(jugadores) FROM partidos WHERE id_partido=%s", (id_partido,))
    for (jugador,) in jugadores:
        print(f"Enviar evaluación a {jugador} del partido {id_partido}")

# ------------------- FLASK KEEP-ALIVE -------------------
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Bot running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)

# ------------------- MAIN -------------------
async def main():
    threading.Thread(target=run_flask).start()
    app = Application.builder().token(TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(boton_registro, pattern=r"^nivel_"))
    app.add_handler(CommandHandler("crear_partido", crear_partido))
    app.add_handler(CallbackQueryHandler(boton_partido_nivel, pattern=r"^partido_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje_partido))
    app.add_handler(CommandHandler("partidos", consultar_partidos))

    await app.run_polling()

if __name__ == "__main__":
    # Arrancar Flask en hilo aparte
    import threading
    threading.Thread(target=run_flask, daemon=True).start()

    # Crear bot
    app = Application.builder().token(TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("crear_partido", crear_partido))
    app.add_handler(CommandHandler("partidos", consultar_partidos))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje_partido))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje))

    # Ejecutar polling sin asyncio.run()
    import asyncio
    loop = asyncio.get_event_loop()
    loop.create_task(app.run_polling())
    loop.run_forever()