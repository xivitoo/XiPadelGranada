import os
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import psycopg
DATABASE_URL = os.getenv("DATABASE_URL")
conn = psycopg.connect(DATABASE_URL)
cursor = conn.cursor()
import asyncio

# ------------------- CONFIG -------------------
TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

logging.basicConfig(level=logging.INFO)

conn = psycopg.connect(DATABASE_URL)
cursor = conn.cursor()
scheduler = AsyncIOScheduler()  # Creamos el scheduler pero no lo iniciamos aún

# ------------------- UTILIDADES -------------------
niveles = {
    "Iniciación": 0,
    "5 baja": 1, "5 media": 2, "5 alta": 3,
    "4 baja": 4, "4 media": 5, "4 alta": 6,
    "3 baja": 7, "3 media": 8, "3 alta": 9,
    "Profesional": 10
}

divisiones = [
    "Iniciación",
    "5 baja", "5 media", "5 alta",
    "4 baja", "4 media", "4 alta",
    "3 baja", "3 media", "3 alta",
    "Profesional"
]

def ejecutar(query, params=None):
    cursor.execute(query, params)
    conn.commit()

def fetchone(query, params=None):
    cursor.execute(query, params)
    return cursor.fetchone()

def fetchall(query, params=None):
    cursor.execute(query, params)
    return cursor.fetchall()

def nivel_a_num(nivel_texto: str) -> int:
    return niveles.get(nivel_texto, 0)

def es_nivel_compatible(nivel_jugador, nivel_partido) -> bool:
    return abs(nivel_jugador - nivel_partido) <= 1

# ------------------- REGISTRO -------------------
registro_temporal = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await update.message.reply_text("Hola! Bienvenido al bot de pádel. ¿Cuál es tu nombre completo?")
    registro_temporal[user_id] = {"paso": "nombre"}

async def mensaje_registro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    texto = update.message.text

    if user_id in registro_temporal:
        paso = registro_temporal[user_id]["paso"]
        if paso == "nombre":
            registro_temporal[user_id]["nombre"] = texto
            registro_temporal[user_id]["paso"] = "nivel"
            await update.message.reply_text("Indica tu nivel aproximado (ej: 4 media):")
        elif paso == "nivel":
            registro_temporal[user_id]["nivel"] = texto
            registro_temporal[user_id]["paso"] = "preferencia"
            await update.message.reply_text("Indica tu preferencia de juego: Reves / Drive / Indiferente")
        elif paso == "preferencia":
            registro_temporal[user_id]["preferencia"] = texto
            ejecutar(
                "INSERT INTO jugadores (id_telegram, nombre, nivel_num, division, preferencia, marcas_superior, marcas_inferior) VALUES (%s,%s,%s,%s,%s,0,0) ON CONFLICT (id_telegram) DO NOTHING",
                (user_id, registro_temporal[user_id]["nombre"], nivel_a_num(registro_temporal[user_id]["nivel"]), registro_temporal[user_id]["nivel"], registro_temporal[user_id]["preferencia"])
            )
            await update.message.reply_text("Registro completado! Ya puedes usar el bot para crear o unirte a partidos.")
            del registro_temporal[user_id]

# ------------------- PARTIDOS -------------------
partido_temporal = {}

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
            # Guardar en DB y obtener ID
            nivel_num = nivel_a_num(partido_temporal[user_id]["nivel"])
            cursor.execute(
                "INSERT INTO partidos (creador_id, nivel_num, hora_inicio, hora_fin, lugar, precio, jugadores, reserva_confirmada, cancelado) VALUES (%s,%s,%s,%s,%s,%s,%s,FALSE,FALSE) RETURNING id_partido",
                (
                    user_id, nivel_num, partido_temporal[user_id]["hora_inicio"], partido_temporal[user_id]["hora_fin"],
                    partido_temporal[user_id]["lugar"], float(partido_temporal[user_id]["precio"]), [user_id]
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
                f"Partido creado!\n"
                f"Nivel: {partido_temporal[user_id]['nivel']}\n"
                f"Hora: {partido_temporal[user_id]['hora_inicio']} - {partido_temporal[user_id]['hora_fin']}\n"
                f"Lugar: {partido_temporal[user_id]['lugar']}\n"
                f"Precio por persona: {partido_temporal[user_id]['precio']}",
                reply_markup=markup
            )

            # Programar evaluación post-partido 1h después
            hora_fin_dt = datetime.strptime(partido_temporal[user_id]['hora_fin'], "%Y-%m-%d %H:%M")
            scheduler.add_job(enviar_evaluacion, 'date', run_date=hora_fin_dt + timedelta(hours=1), args=[id_partido])

            del partido_temporal[user_id]

async def consultar_partidos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    jugador = fetchone("SELECT nivel_num FROM jugadores WHERE id_telegram=%s", (user_id,))
    if not jugador:
        await update.message.reply_text("Debes registrarte primero.")
        return
    nivel_jugador = jugador[0]

    hoy = datetime.now().date()
    partidos = fetchall("SELECT id_partido, nivel_num, hora_inicio, hora_fin, lugar FROM partidos WHERE hora_inicio::date = %s AND cancelado=FALSE", (hoy,))
    
    texto = "Partidos disponibles hoy compatibles con tu nivel:\n"
    for p in partidos:
        id_partido, nivel_partido, hora_inicio, hora_fin, lugar = p
        if es_nivel_compatible(nivel_jugador, nivel_partido):
            texto += f"- Hora: {hora_inicio} - {hora_fin} | Lugar: {lugar}\n"
    await update.message.reply_text(texto)

# ------------------- CALLBACKS -------------------
async def boton_partido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    accion, id_partido = data.split("_")
    id_partido = int(id_partido)
    user_id = query.from_user.id

    partido = fetchone("SELECT jugadores, creador_id, nivel_num FROM partidos WHERE id_partido=%s", (id_partido,))
    if not partido:
        await query.edit_message_text("Este partido ya no existe o fue cancelado.")
        return
    jugadores, creador_id, nivel_partido = partido

    if accion == "unirse":
        if user_id in jugadores:
            await query.answer("Ya estás en este partido.", show_alert=True)
            return
        jugador = fetchone("SELECT nivel_num FROM jugadores WHERE id_telegram=%s", (user_id,))
        if not jugador:
            await query.answer("Debes registrarte primero.", show_alert=True)
            return
        nivel_jugador = jugador[0]
        if not es_nivel_compatible(nivel_jugador, nivel_partido):
            await query.answer("No cumples el requisito de nivel para este partido.", show_alert=True)
            return
        jugadores.append(user_id)
        ejecutar("UPDATE partidos SET jugadores=%s WHERE id_partido=%s", (jugadores, id_partido))
        await query.answer("Te has unido al partido!", show_alert=True)

    elif accion == "salir":
        if user_id not in jugadores:
            await query.answer("No estás en este partido.", show_alert=True)
            return
        if user_id == creador_id:
            await query.answer("El creador no puede salir, puede cancelar el partido.", show_alert=True)
            return
        jugadores.remove(user_id)
        ejecutar("UPDATE partidos SET jugadores=%s WHERE id_partido=%s", (jugadores, id_partido))
        await query.answer("Has salido del partido.", show_alert=True)

    elif accion == "cancelar":
        if user_id != creador_id:
            await query.answer("Solo el creador puede cancelar el partido.", show_alert=True)
            return
        ejecutar("UPDATE partidos SET cancelado=TRUE WHERE id_partido=%s", (id_partido,))
        await query.edit_message_text("El partido ha sido cancelado por el creador.")

    elif accion == "confirmar":
        if user_id != creador_id:
            await query.answer("Solo el creador puede confirmar la reserva.", show_alert=True)
            return
        ejecutar("UPDATE partidos SET reserva_confirmada=TRUE WHERE id_partido=%s", (id_partido,))
        await query.answer("Reserva confirmada!", show_alert=True)

# ------------------- EVALUACION POST-PARTIDO -------------------
async def enviar_evaluacion(id_partido):
    partido = fetchone("SELECT jugadores FROM partidos WHERE id_partido=%s", (id_partido,))
    if not partido:
        return
    jugadores = partido[0]
    for jugador_id in jugadores:
        try:
            botones = [
                [InlineKeyboardButton("Sí", callback_data=f"eval_si_{id_partido}_{jugador_id}")],
                [InlineKeyboardButton("No", callback_data=f"eval_no_{id_partido}_{jugador_id}")]
            ]
            markup = InlineKeyboardMarkup(botones)
            await app.bot.send_message(chat_id=jugador_id, text="¿El nivel del partido coincidió con lo indicado?", reply_markup=markup)
        except:
            continue

async def boton_evaluacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if "eval" in data:
        subaccion, id_partido, jugador_id = data.split("_")[1], int(data.split("_")[2]), int(data.split("_")[3])
        if subaccion == "si":
            ejecutar("INSERT INTO evaluaciones (id_partido,jugador_id,marca_conforme) VALUES (%s,%s,TRUE)", (id_partido, jugador_id))
            await query.edit_message_text("Gracias! Se ha registrado que el nivel coincidió.")
        elif subaccion == "no":
            partido = fetchone("SELECT jugadores FROM partidos WHERE id_partido=%s", (id_partido,))
            jugadores = partido[0]
            opciones = []
            for j in jugadores:
                if j != jugador_id:
                    opciones.append([InlineKeyboardButton(f"Jugador {j} Inferior", callback_data=f"marcar_inferior_{id_partido}_{j}")])
                    opciones.append([InlineKeyboardButton(f"Jugador {j} Superior", callback_data=f"marcar_superior_{id_partido}_{j}")])
            markup = InlineKeyboardMarkup(opciones)
            await query.edit_message_text("Selecciona los jugadores que no cumplieron el nivel y su categoría:", reply_markup=markup)

async def boton_marca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, tipo, id_partido, evaluado_id = query.data.split("_")
    id_partido = int(id_partido)
    evaluado_id = int(evaluado_id)

    ejecutar("INSERT INTO evaluaciones (id_partido,jugador_id,evaluado_id,resultado,marca_conforme) VALUES (%s,%s,%s,%s,FALSE)",
             (id_partido, query.from_user.id, evaluado_id, tipo.capitalize()))
    await query.edit_message_text(f"Se ha registrado que el jugador {evaluado_id} fue marcado como {tipo.capitalize()}.")

    # Revisar marcas consecutivas
    inferior = fetchone("SELECT COUNT(*) FROM evaluaciones WHERE evaluado_id=%s AND resultado='Inferior' ORDER BY fecha DESC LIMIT 3", (evaluado_id,))
    superior = fetchone("SELECT COUNT(*) FROM evaluaciones WHERE evaluado_id=%s AND resultado='Superior' ORDER BY fecha DESC LIMIT 3", (evaluado_id,))
    if inferior[0] >= 3:
        jugador = fetchone("SELECT division FROM jugadores WHERE id_telegram=%s", (evaluado_id,))
        div_index = divisiones.index(jugador[0])
        nuevo_index = max(0, div_index - 1)
        ejecutar("UPDATE jugadores SET division=%s WHERE id_telegram=%s", (divisiones[nuevo_index], evaluado_id))
    if superior[0] >= 3:
        jugador = fetchone("SELECT division FROM jugadores WHERE id_telegram=%s", (evaluado_id,))
        div_index = divisiones.index(jugador[0])
        nuevo_index = min(len(divisiones)-1, div_index + 1)
        ejecutar("UPDATE jugadores SET division=%s WHERE id_telegram=%s", (divisiones[nuevo_index], evaluado_id))

# ------------------- BOT PRINCIPAL -------------------
app = ApplicationBuilder().token(TOKEN).build()

# Registro
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje_registro))

# Partidos
app.add_handler(CommandHandler("Crear", crear_partido))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje_partido))
app.add_handler(CommandHandler("PartidosHoy", consultar_partidos))
app.add_handler(CallbackQueryHandler(boton_partido, pattern=r"^(unirse|salir|cancelar|confirmar)_"))
app.add_handler(CallbackQueryHandler(boton_evaluacion, pattern=r"^eval_"))
app.add_handler(CallbackQueryHandler(boton_marca, pattern=r"^marcar_"))

# ------------------- MAIN -------------------
async def main():
    scheduler.start()  # Se inicia el scheduler aquí, con loop activo
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())