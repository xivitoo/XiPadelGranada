import os
import psycopg2
import asyncio
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

# ------------------- CONEXIÓN A LA BASE DE DATOS -------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
conn = psycopg2.connect(DATABASE_URL, sslmode="require")
cursor = conn.cursor()

def fetchone(query, params=None):
    cursor.execute(query, params or ())
    return cursor.fetchone()

def fetchall(query, params=None):
    cursor.execute(query, params or ())
    return cursor.fetchall()

# ------------------- FUNCIONES DE NIVELES -------------------
def nivel_a_num(nivel: str) -> float:
    try:
        return float(nivel.split()[0].replace(",", "."))
    except:
        return 0.0

def es_nivel_compatible(nivel_jugador, nivel_partido):
    return abs(nivel_jugador - nivel_partido) <= 0.5

# ------------------- BOT -------------------
TOKEN = os.environ.get("BOT_TOKEN")
scheduler = AsyncIOScheduler()
scheduler.start()

# ------------------- REGISTRO -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    nombre = update.message.from_user.first_name
    jugador = fetchone("SELECT * FROM jugadores WHERE id_telegram=%s", (user_id,))
    if jugador:
        await update.message.reply_text("Ya estás registrado ✅")
    else:
        cursor.execute(
            "INSERT INTO jugadores (id_telegram, nombre, nivel_num) VALUES (%s,%s,%s)",
            (user_id, nombre, 3.0),
        )
        conn.commit()
        await update.message.reply_text("Te has registrado con nivel 3.0 ✅")

# ------------------- CREAR PARTIDOS -------------------
partido_temporal = {}

async def crear_partido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    partido_temporal[user_id] = {"paso": "nivel"}
    await update.message.reply_text("Indica el nivel del partido (ej: 4.0):")

async def mensaje_partido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    texto = update.message.text

    if user_id not in partido_temporal:
        return

    paso = partido_temporal[user_id]["paso"]

    if paso == "nivel":
        partido_temporal[user_id]["nivel"] = texto
        partido_temporal[user_id]["paso"] = "hora_inicio"
        await update.message.reply_text("Indica la hora de inicio (YYYY-MM-DD HH:MM):")

    elif paso == "hora_inicio":
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

        # Guardar en DB
        nivel_num = nivel_a_num(partido_temporal[user_id]["nivel"])
        cursor.execute(
            """
            INSERT INTO partidos (creador_id, nivel_num, hora_inicio, hora_fin, lugar, precio, jugadores, reserva_confirmada, cancelado)
            VALUES (%s,%s,%s,%s,%s,%s,%s,FALSE,FALSE) RETURNING id_partido
            """,
            (
                user_id,
                nivel_num,
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
            [InlineKeyboardButton("Cancelar (creador)", callback_data=f"cancelar_{id_partido}")],
            [InlineKeyboardButton("Confirmar reserva", callback_data=f"confirmar_{id_partido}")]
        ]
        markup = InlineKeyboardMarkup(botones)

        await update.message.reply_text(
            f"🎾 Partido creado!\n"
            f"Nivel: {partido_temporal[user_id]['nivel']}\n"
            f"Hora: {partido_temporal[user_id]['hora_inicio']} - {partido_temporal[user_id]['hora_fin']}\n"
            f"Lugar: {partido_temporal[user_id]['lugar']}\n"
            f"Precio por persona: {partido_temporal[user_id]['precio']}€",
            reply_markup=markup,
        )

        # Programar evaluación
        hora_fin_dt = datetime.strptime(partido_temporal[user_id]['hora_fin'], "%Y-%m-%d %H:%M")
        scheduler.add_job(enviar_evaluacion, 'date', run_date=hora_fin_dt + timedelta(hours=1), args=[id_partido])

        del partido_temporal[user_id]

# ------------------- CONSULTAR PARTIDOS -------------------
async def consultar_partidos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    jugador = fetchone("SELECT nivel_num FROM jugadores WHERE id_telegram=%s", (user_id,))
    if not jugador:
        await update.message.reply_text("Debes registrarte primero con /start")
        return
    nivel_jugador = jugador[0]

    hoy = datetime.now().date()
    partidos = fetchall(
        "SELECT id_partido, nivel_num, hora_inicio, hora_fin, lugar FROM partidos WHERE hora_inicio::date = %s AND cancelado=FALSE",
        (hoy,),
    )

    if not partidos:
        await update.message.reply_text("Hoy no hay partidos disponibles 🙁")
        return

    texto = "🎾 Partidos disponibles hoy compatibles con tu nivel:\n"
    for p in partidos:
        id_partido, nivel_partido, hora_inicio, hora_fin, lugar = p
        if es_nivel_compatible(nivel_jugador, nivel_partido):
            texto += f"- Partido {id_partido}: {hora_inicio.strftime('%H:%M')} - {hora_fin.strftime('%H:%M')} en {lugar}\n"
    await update.message.reply_text(texto)

# ------------------- EVALUACIÓN -------------------
async def enviar_evaluacion(id_partido):
    jugadores = fetchall("SELECT unnest(jugadores) FROM partidos WHERE id_partido=%s", (id_partido,))
    for (jugador,) in jugadores:
        # Aquí se podría enviar mensaje de evaluación al jugador
        print(f"Enviar evaluación a {jugador} del partido {id_partido}")

# ------------------- MAIN -------------------
async def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("Registro", start))
    app.add_handler(CommandHandler("Crear", crear_partido))
    app.add_handler(CommandHandler("Buscar", consultar_partidos))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje_partido))

    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())