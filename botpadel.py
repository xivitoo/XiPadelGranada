import os
import psycopg2
import asyncio
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# ------------------- DB -------------------
DATABASE_URL = os.getenv("DATABASE_URL")

conn = psycopg2.connect(DATABASE_URL)
cursor = conn.cursor()

def fetchone(query, params=None):
    cursor.execute(query, params or ())
    return cursor.fetchone()

def fetchall(query, params=None):
    cursor.execute(query, params or ())
    return cursor.fetchall()

# ------------------- UTILS -------------------
def nivel_a_num(nivel_texto: str) -> float:
    try:
        return float(nivel_texto.split()[0])
    except:
        return 0

def es_nivel_compatible(nivel_jugador: float, nivel_partido: float) -> bool:
    return abs(nivel_jugador - nivel_partido) <= 0.5

# ------------------- VARIABLES TEMPORALES -------------------
registro_temporal = {}
partido_temporal = {}

# ------------------- REGISTRO -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    registro_temporal[user_id] = {"paso": "nombre"}
    await update.message.reply_text("Bienvenido! Indica tu nombre:")

async def mensaje_registro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    texto = update.message.text

    if user_id in registro_temporal:
        paso = registro_temporal[user_id]["paso"]

        if paso == "nombre":
            registro_temporal[user_id]["nombre"] = texto
            registro_temporal[user_id]["paso"] = "nivel"
            await update.message.reply_text("Indica tu nivel (ej: 4.0):")

        elif paso == "nivel":
            nivel_num = nivel_a_num(texto)
            cursor.execute("INSERT INTO jugadores (id_telegram, nombre, nivel_num) VALUES (%s, %s, %s) ON CONFLICT (id_telegram) DO UPDATE SET nombre=%s, nivel_num=%s",
                           (user_id, registro_temporal[user_id]["nombre"], nivel_num, registro_temporal[user_id]["nombre"], nivel_num))
            conn.commit()
            await update.message.reply_text("Registro completado ✅")
            del registro_temporal[user_id]

# ------------------- PARTIDOS -------------------
async def crear_partido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    partido_temporal[user_id] = {"paso": "nivel"}
    await update.message.reply_text("Indica el nivel del partido (ej: 4 media):")

async def mensaje_partido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    texto = update.message.text

    if user_id in partido_temporal:
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
            nivel_num = nivel_a_num(partido_temporal[user_id]["nivel"])

            cursor.execute(
                "INSERT INTO partidos (creador_id, nivel_num, hora_inicio, hora_fin, lugar, precio, jugadores, reserva_confirmada, cancelado) VALUES (%s,%s,%s,%s,%s,%s,%s,FALSE,FALSE) RETURNING id_partido",
                (
                    user_id,
                    nivel_num,
                    partido_temporal[user_id]["hora_inicio"],
                    partido_temporal[user_id]["hora_fin"],
                    partido_temporal[user_id]["lugar"],
                    float(partido_temporal[user_id]["precio"]),
                    [user_id]
                )
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
                f"🎾 Partido creado!\n\n"
                f"Nivel: {partido_temporal[user_id]['nivel']}\n"
                f"Hora: {partido_temporal[user_id]['hora_inicio']} - {partido_temporal[user_id]['hora_fin']}\n"
                f"Lugar: {partido_temporal[user_id]['lugar']}\n"
                f"Precio por persona: {partido_temporal[user_id]['precio']}€",
                reply_markup=markup
            )

            hora_fin_dt = datetime.strptime(partido_temporal[user_id]['hora_fin'], "%Y-%m-%d %H:%M")
            scheduler.add_job(enviar_evaluacion, 'date', run_date=hora_fin_dt + timedelta(hours=1), args=[id_partido])

            del partido_temporal[user_id]

async def consultar_partidos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    jugador = fetchone("SELECT nivel_num FROM jugadores WHERE id_telegram=%s", (user_id,))
    if not jugador:
        await update.message.reply_text("Debes registrarte primero con /start.")
        return

    nivel_jugador = jugador[0]
    hoy = datetime.now().date()

    partidos = fetchall("SELECT id_partido, nivel_num, hora_inicio, hora_fin, lugar FROM partidos WHERE hora_inicio::date = %s AND cancelado=FALSE", (hoy,))
    
    texto = "🎾 Partidos disponibles hoy compatibles con tu nivel:\n"
    for p in partidos:
        id_partido, nivel_partido, hora_inicio, hora_fin, lugar = p
        if es_nivel_compatible(nivel_jugador, nivel_partido):
            texto += f"- Hora: {hora_inicio} - {hora_fin} | Lugar: {lugar}\n"
    await update.message.reply_text(texto)

# ------------------- CALLBACKS -------------------
async def boton_partido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(f"Botón pulsado: {query.data}")

async def enviar_evaluacion(id_partido):
    # Aquí se mandaría encuesta de evaluación a los jugadores
    print(f"Enviar evaluación a jugadores del partido {id_partido}")

# ------------------- MANEJO DE MENSAJES -------------------
async def mensajes_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_id in registro_temporal:
        await mensaje_registro(update, context)
        return

    if user_id in partido_temporal:
        await mensaje_partido(update, context)
        return

    await update.message.reply_text("Comando o texto no reconocido. Usa /start para registrarte o /Crear para crear un partido.")

# ------------------- BOT PRINCIPAL -------------------
TOKEN = os.getenv("BOT_TOKEN")
scheduler = AsyncIOScheduler()

app = ApplicationBuilder().token(TOKEN).build()

# Registro
app.add_handler(CommandHandler("start", start))

# Partidos
app.add_handler(CommandHandler("Crear", crear_partido))
app.add_handler(CommandHandler("PartidosHoy", consultar_partidos))
app.add_handler(CallbackQueryHandler(boton_partido, pattern=r"^(unirse|salir|cancelar|confirmar)_"))

# Mensajes (registro + partidos)
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensajes_texto))

# ------------------- MAIN -------------------
if __name__ == "__main__":
    scheduler.start()
    app.run_polling()