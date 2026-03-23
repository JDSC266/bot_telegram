#!/usr/bin/env python
# -*- coding: utf-8 -*-

import telebot
from telebot import types
import os
import time
import sys
import subprocess
import threading
import signal
import tempfile
from dotenv import load_dotenv

# ── Configuración ──────────────────────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    sys.exit("❌ Error: define BOT_TOKEN en las variables de entorno")

bot = telebot.TeleBot(TOKEN)

FFMPEG  = "ffmpeg"
FFPROBE = "ffprobe"

# Directorio temporal compatible con cualquier SO (Linux en Railway, Windows local)
TEMP_DIR = tempfile.gettempdir()

FORMATS = ["mp4", "mkv", "mov", "ogg", "flv", "webm", "mpg", "avi"]
TIEMPO_BORRADO = 30  # minutos antes de borrar archivos temporales

# Estado por usuario (thread-safe)
user_state:      dict[int, int] = {}
user_files:      dict[int, str] = {}
user_video:      dict[int, str] = {}
msg_files:       dict[int, str] = {}
protected_files: set[str]       = set()
state_lock = threading.Lock()


# ── Helper: borrado seguro ─────────────────────────────────────────────────────
def safe_remove(*paths, retries: int = 5, delay: float = 0.5):
    for path in paths:
        for i in range(retries):
            try:
                if os.path.exists(path):
                    os.remove(path)
                break
            except PermissionError:
                if i < retries - 1:
                    time.sleep(delay)
                else:
                    print(f"⚠️ No se pudo borrar: {path}")
            except Exception as e:
                print(f"⚠️ Error al borrar {path}: {e}")
                break


# ── Helper: ruta temporal ──────────────────────────────────────────────────────
def tmp(filename: str) -> str:
    """Devuelve la ruta completa dentro del directorio temporal del sistema."""
    return os.path.join(TEMP_DIR, filename)


# ── Limpieza automática ────────────────────────────────────────────────────────
def limpiar_archivos_antiguos():
    extensiones = (".mp4", ".mkv", ".mov", ".ogg", ".flv", ".webm",
                   ".mpg", ".avi", ".mp3", ".gif", ".png")
    while True:
        tiempo_limite = time.time() - (TIEMPO_BORRADO * 60)
        try:
            for archivo in os.listdir(TEMP_DIR):
                ruta = os.path.join(TEMP_DIR, archivo)
                if ruta in protected_files:
                    continue
                if archivo.endswith(extensiones):
                    try:
                        if os.path.getmtime(ruta) < tiempo_limite:
                            safe_remove(ruta)
                            print(f"🗑️ Eliminado: {archivo}")
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(60)

threading.Thread(target=limpiar_archivos_antiguos, daemon=True).start()


# ── Helpers de estado ──────────────────────────────────────────────────────────
def get_step(uid: int) -> int:
    with state_lock:
        return user_state.get(uid, 0)

def set_step(uid: int, step: int):
    with state_lock:
        user_state[uid] = step

def set_file(uid: int, path: str):
    with state_lock:
        user_files[uid] = path

def get_file(uid: int) -> str | None:
    with state_lock:
        return user_files.get(uid)

def clear_file(uid: int):
    with state_lock:
        user_files.pop(uid, None)

def download_file(message) -> str | None:
    """Descarga el vídeo/documento adjunto y devuelve el nombre local."""
    try:
        if message.video:
            file_info = bot.get_file(message.video.file_id)
            local_name = tmp(f"{message.chat.id}_{message.video.file_id[-8:]}.mp4")
        elif message.document:
            file_info = bot.get_file(message.document.file_id)
            fname = message.document.file_name or f"{message.chat.id}_doc"
            local_name = tmp(fname)
        else:
            return None
        data = bot.download_file(file_info.file_path)
        with open(local_name, "wb") as f:
            f.write(data)
        return local_name
    except Exception as e:
        print(f"Error descargando archivo: {e}")
        return None

def send_typing(chat_id, text, parse_mode=None):
    bot.send_chat_action(chat_id, "typing")
    return bot.send_message(chat_id, text, parse_mode=parse_mode)


# ── /start ─────────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["start"])
def send_welcome(message):
    nombre = message.from_user.first_name
    bot.send_chat_action(message.chat.id, "typing")
    bot.send_message(
        message.chat.id,
        f"Hola <strong>{nombre}</strong>, soy un bot de tratamiento de vídeos. "
        "Estoy aquí para ayudarte a trabajar con tus vídeos. "
        "Para ver todo lo que puedes hacer usa el comando /help",
        parse_mode="HTML"
    )


# ── /help ──────────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["help"])
def send_help(message):
    bot.send_message(
        message.chat.id,
        "Estas son todas las opciones disponibles:\n\n"
        "/convert — Convierte tu vídeo al formato que elijas.\n"
        "/compress — Comprime tu vídeo en calidad Baja, Media o Alta.\n"
        "/youtube <url> — Descarga un vídeo de YouTube.\n"
        "/youtubetomp3 <url> — Descarga el audio de un vídeo de YouTube en mp3.\n"
        "/gif — Crea un GIF a partir de un vídeo.\n"
        "/onlyaudio — Extrae el audio de un vídeo en mp3.\n"
        "/onlyvideo — Elimina el audio de un vídeo.\n"
        "/clip — Recorta un vídeo entre dos segundos.\n"
        "/videoaudio — Une un vídeo con un archivo de audio.\n"
        "/mejorav — Mejora la calidad de tu vídeo (nitidez, brillo, contraste).\n"
        "/cancelar — Cancela la operación actual."
    )


# ── /cancelar ─────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["cancelar"])
def cancelar(message):
    set_step(message.chat.id, 0)
    clear_file(message.chat.id)
    bot.send_message(message.chat.id, "✅ Operación cancelada.")


# ── /youtubetomp3 ──────────────────────────────────────────────────────────────
@bot.message_handler(commands=["youtubetomp3"])
def youtube_to_mp3(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        bot.send_message(message.chat.id, "Formato: /youtubetomp3 <URL>")
        return

    url = parts[1]
    send_typing(message.chat.id, "Perfecto, ¡estamos en ello! Ten un poco de paciencia...")

    output_base = tmp(f"{message.chat.id}_audio")
    mp3_file    = output_base + ".mp3"

    proceso = subprocess.run(
        ["yt-dlp", "--extract-audio", "--audio-format", "mp3",
         "-o", output_base + ".%(ext)s", url],
        capture_output=True, text=True
    )

    if proceso.returncode != 0 or not os.path.exists(mp3_file):
        bot.send_message(message.chat.id,
                         f"❌ Error:\n`{proceso.stderr[-500:]}`",
                         parse_mode="Markdown")
        return

    with open(mp3_file, "rb") as audio:
        bot.send_audio(message.chat.id, audio)
    safe_remove(mp3_file)


# ── /youtube ───────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["youtube"])
def youtube(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        bot.send_message(message.chat.id, "Formato: /youtube <URL>")
        return

    url = parts[1]
    send_typing(message.chat.id, "Perfecto, ¡estamos en ello! Ten un poco de paciencia...")

    output_base = tmp(f"{message.chat.id}_video")
    mp4_file    = output_base + ".mp4"

    proceso = subprocess.run(
        ["yt-dlp", "-f", "mp4", "-o", output_base + ".%(ext)s", url],
        capture_output=True, text=True
    )

    if proceso.returncode != 0 or not os.path.exists(mp4_file):
        bot.send_message(message.chat.id,
                         f"❌ Error:\n`{proceso.stderr[-500:]}`",
                         parse_mode="Markdown")
        return

    with open(mp4_file, "rb") as video:
        bot.send_video(message.chat.id, video)
    safe_remove(mp4_file)


# ── /gif ───────────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["gif"])
def gif_start(message):
    parts = message.text.split()
    if len(parts) == 3:
        _gif_process(message, int(parts[1]), int(parts[2]))
    else:
        set_step(message.chat.id, 2)
        bot.send_message(message.chat.id, "Mándanos el vídeo que quieres convertir a GIF.")

def _gif_process(message, inicio: int, final: int):
    cadena = get_file(message.chat.id)
    if not cadena or not os.path.exists(cadena):
        bot.send_message(message.chat.id, "Primero envíanos el vídeo con /gif")
        return

    duracion = final - inicio
    if inicio < 0 or final < 0:
        bot.send_message(message.chat.id, "Los segundos no pueden ser negativos.")
        return
    if duracion <= 0:
        bot.send_message(message.chat.id, "El segundo de fin debe ser mayor que el de inicio.")
        return

    resultado = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", cadena],
        capture_output=True, text=True
    )
    try:
        segundosvideo = int(float(resultado.stdout.strip()))
    except Exception:
        bot.send_message(message.chat.id, "❌ No se pudo leer la duración del vídeo.")
        return

    if inicio > segundosvideo or final > segundosvideo:
        bot.send_message(message.chat.id, "El vídeo es más corto que los segundos indicados.")
        return

    gif_file     = tmp(f"{message.chat.id}.gif")
    palette_file = tmp(f"{message.chat.id}_palette.png")

    subprocess.run([FFMPEG, "-y", "-ss", str(inicio), "-t", str(duracion),
                    "-i", cadena, "-vf", "fps=10,scale=320:-1:flags=lanczos,palettegen",
                    palette_file], capture_output=True)

    res = subprocess.run([FFMPEG, "-y", "-ss", str(inicio), "-t", str(duracion),
                          "-i", cadena, "-i", palette_file,
                          "-lavfi", "fps=10,scale=320:-1:flags=lanczos[x];[x][1:v]paletteuse",
                          gif_file], capture_output=True, text=True)

    safe_remove(palette_file)

    if res.returncode != 0 or not os.path.exists(gif_file):
        bot.send_message(message.chat.id,
                         f"❌ Error al crear el GIF:\n`{res.stderr[-500:]}`",
                         parse_mode="Markdown")
        return

    bot.send_message(message.chat.id, "¡Aquí tienes tu GIF recién salido del horno!")
    with open(gif_file, "rb") as gif:
        bot.send_document(message.chat.id, gif)

    safe_remove(gif_file, cadena)
    clear_file(message.chat.id)
    set_step(message.chat.id, 0)


# ── /onlyaudio ─────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["onlyaudio"])
def onlyaudio_start(message):
    set_step(message.chat.id, 3)
    bot.send_message(message.chat.id, "Mándanos el vídeo del que quieres obtener el audio.")


# ── /onlyvideo ─────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["onlyvideo"])
def onlyvideo_start(message):
    set_step(message.chat.id, 4)
    bot.send_message(message.chat.id, "Mándanos el vídeo del que quieres quitar el audio.")


# ── /clip ──────────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["clip"])
def clip_start(message):
    parts = message.text.split()
    if len(parts) == 3:
        _clip_process(message, int(parts[1]), int(parts[2]))
    else:
        set_step(message.chat.id, 5)
        bot.send_message(message.chat.id, "Mándanos el vídeo que quieres recortar.")

def _clip_process(message, inicio: int, final: int):
    cadena = get_file(message.chat.id)
    if not cadena or not os.path.exists(cadena):
        bot.send_message(message.chat.id, "Primero envíanos el vídeo con /clip")
        return

    duracion = final - inicio
    if inicio < 0 or final < 0:
        bot.send_message(message.chat.id, "Los segundos no pueden ser negativos.")
        return
    if duracion <= 0:
        bot.send_message(message.chat.id, "El segundo de fin debe ser mayor que el de inicio.")
        return

    resultado = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", cadena],
        capture_output=True, text=True
    )
    try:
        segundosvideo = int(float(resultado.stdout.strip()))
    except Exception:
        bot.send_message(message.chat.id, "❌ No se pudo leer la duración del vídeo.")
        return

    if inicio > segundosvideo or final > segundosvideo:
        bot.send_message(message.chat.id, "El vídeo es más corto que los segundos indicados.")
        return

    recortado = tmp(cadena.rsplit(".", 1)[0].split(os.sep)[-1] + "-recortado.mp4")
    subprocess.run([FFMPEG, "-ss", str(inicio), "-t", str(duracion), "-i", cadena, recortado])

    if not os.path.exists(recortado):
        bot.send_message(message.chat.id, "❌ Error al recortar el vídeo.")
        return

    bot.send_message(message.chat.id, "¡Aquí tienes tu vídeo recortado!")
    with open(recortado, "rb") as video:
        bot.send_video(message.chat.id, video)

    safe_remove(cadena, recortado)
    clear_file(message.chat.id)
    set_step(message.chat.id, 0)


# ── /videoaudio ────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["videoaudio"])
def videoaudio_start(message):
    set_step(message.chat.id, 6)
    bot.send_message(message.chat.id, "Mándanos el vídeo que quieres unir con el audio.")


# ── /mejorav ───────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["mejorav"])
def mejorav_start(message):
    set_step(message.chat.id, 10)
    bot.send_message(message.chat.id, "Mándanos el vídeo que quieres mejorar.")


# ── /convert ───────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["convert"])
def convert_start(message):
    set_step(message.chat.id, 8)
    bot.send_message(message.chat.id, "Mándanos el vídeo que quieres cambiar de formato.")


# ── /compress ──────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["compress"])
def compress_start(message):
    set_step(message.chat.id, 9)
    bot.send_message(message.chat.id, "Mándanos el vídeo que quieres comprimir.")


# ── Recepción de vídeos/documentos ────────────────────────────────────────────
@bot.message_handler(content_types=["video", "document"])
def handle_video(message):
    step = get_step(message.chat.id)
    if step == 0:
        return

    local_name = download_file(message)
    if not local_name:
        bot.send_message(message.chat.id, "❌ No pude descargar el archivo. Inténtalo de nuevo.")
        return

    if step == 2:
        set_file(message.chat.id, local_name)
        bot.send_message(message.chat.id,
                         "Perfecto. Ahora dinos el segundo de inicio y fin:\n/gif <inicio> <fin>")

    elif step == 3:
        fn = local_name.rsplit(".", 1)[0]
        audio_file = fn + "-audio.mp3"
        subprocess.run([FFMPEG, "-i", local_name,
                        "-vn", "-ar", "44100", "-ac", "2", "-ab", "192k", "-f", "mp3", audio_file])
        if not os.path.exists(audio_file):
            bot.send_message(message.chat.id, "❌ Error al extraer el audio.")
        else:
            bot.send_message(message.chat.id, "¡Aquí tienes tu audio!")
            with open(audio_file, "rb") as audio:
                bot.send_audio(message.chat.id, audio)
            safe_remove(audio_file)
        safe_remove(local_name)
        set_step(message.chat.id, 0)

    elif step == 4:
        mute_file = local_name.rsplit(".", 1)[0] + "-mute.mp4"
        subprocess.run([FFMPEG, "-i", local_name, "-an", "-vcodec", "copy", mute_file])
        if not os.path.exists(mute_file):
            bot.send_message(message.chat.id, "❌ Error al procesar el vídeo.")
        else:
            bot.send_message(message.chat.id, "¡Aquí tienes tu vídeo sin sonido!")
            with open(mute_file, "rb") as video:
                bot.send_video(message.chat.id, video)
            safe_remove(mute_file)
        safe_remove(local_name)
        set_step(message.chat.id, 0)

    elif step == 5:
        set_file(message.chat.id, local_name)
        bot.send_message(message.chat.id,
                         "Perfecto. Ahora dinos el segundo de inicio y fin:\n/clip <inicio> <fin>")

    elif step == 6:
        with state_lock:
            user_video[message.chat.id] = local_name
        set_step(message.chat.id, 7)
        bot.send_message(message.chat.id, "Perfecto. Ahora envíanos el archivo de audio.")

    elif step == 8:
        _ask_format(message, local_name)
        set_step(message.chat.id, 0)

    elif step == 9:
        _ask_compress(message, local_name)
        set_step(message.chat.id, 0)

    elif step == 10:
        _ask_enhance(message, local_name)
        set_step(message.chat.id, 0)


@bot.message_handler(content_types=["audio"])
def handle_audio(message):
    if get_step(message.chat.id) != 7:
        return

    file_info = bot.get_file(message.audio.file_id)
    audio_file = tmp(f"{message.chat.id}.mp3")
    with open(audio_file, "wb") as f:
        f.write(bot.download_file(file_info.file_path))

    with state_lock:
        videoinput = user_video.pop(message.chat.id, None)

    if not videoinput or not os.path.exists(videoinput):
        bot.send_message(message.chat.id, "❌ No encontré el vídeo. Empieza de nuevo con /videoaudio")
        safe_remove(audio_file)
        set_step(message.chat.id, 0)
        return

    output_file = tmp(f"{message.chat.id}-join.mkv")
    subprocess.run([FFMPEG, "-i", videoinput, "-i", audio_file,
                    "-map", "0:v", "-map", "1:a",
                    "-c:v", "libx264", "-c:a", "libvorbis",
                    "-shortest", output_file])

    if not os.path.exists(output_file):
        bot.send_message(message.chat.id, "❌ Error al unir vídeo y audio.")
    else:
        bot.send_message(message.chat.id, "¡Aquí tienes tu vídeo con el nuevo audio!")
        with open(output_file, "rb") as video:
            bot.send_video(message.chat.id, video)
        safe_remove(output_file)

    safe_remove(videoinput, audio_file)
    set_step(message.chat.id, 0)


# ── Helpers de teclados inline ─────────────────────────────────────────────────
FORMAT_ARGS = {
    "mp4":  ["-c:v", "libx264", "-vf", "scale=iw:ih", "-c:a", "aac", "-ar", "44100", "-crf", "18"],
    "mkv":  ["-c:v", "libx264", "-vf", "scale=iw:ih", "-c:a", "aac", "-ar", "44100", "-crf", "18"],
    "mov":  ["-c:v", "libx264", "-vf", "scale=iw:ih", "-c:a", "aac", "-ar", "44100", "-crf", "18"],
    "webm": ["-c:v", "libvpx-vp9", "-vf", "scale=iw:ih", "-c:a", "libopus", "-b:v", "0", "-crf", "18"],
    "ogg":  ["-c:v", "libvpx", "-vf", "scale=iw:ih", "-c:a", "libvorbis", "-q:v", "7"],
    "flv":  ["-c:v", "libx264", "-vf", "scale=iw:ih", "-c:a", "aac", "-ar", "44100", "-crf", "18"],
    "mpg":  ["-c:v", "mpeg2video", "-vf", "scale=iw:ih", "-b:v", "4000k",
             "-maxrate", "4000k", "-bufsize", "8000k", "-c:a", "mp2", "-b:a", "192k"],
    "avi":  ["-c:v", "libxvid", "-vf", "scale=iw:ih", "-c:a", "mp3", "-ar", "44100", "-b:v", "4000k"],
}

ENHANCE_ARGS = {
    "suave":  "unsharp=3:3:0.5:3:3:0.0,eq=contrast=1.05:brightness=0.02:saturation=1.1",
    "media":  "unsharp=5:5:1.0:5:5:0.0,eq=contrast=1.15:brightness=0.04:saturation=1.2",
    "fuerte": "unsharp=7:7:1.5:7:7:0.0,eq=contrast=1.25:brightness=0.06:saturation=1.3",
}

def _ask_format(message, filename: str):
    fmt_actual = filename.rsplit(".", 1)[-1].lower()
    markup = types.InlineKeyboardMarkup(row_width=1)
    for f in FORMATS:
        if f != fmt_actual:
            markup.add(types.InlineKeyboardButton(f.upper(), callback_data=f"convert_{f}"))
    msg = bot.send_message(
        message.chat.id,
        f"El vídeo que has enviado es {fmt_actual.upper()}. Selecciona el formato destino:",
        reply_markup=markup
    )
    msg_files[msg.message_id] = filename

def _ask_enhance(message, filename: str):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🟢 Suave",  callback_data="enhance_suave"))
    markup.add(types.InlineKeyboardButton("🟡 Media",  callback_data="enhance_media"))
    markup.add(types.InlineKeyboardButton("🔴 Fuerte", callback_data="enhance_fuerte"))
    msg = bot.send_message(message.chat.id, "¿Cuánto quieres mejorar el vídeo?", reply_markup=markup)
    msg_files[msg.message_id] = filename

def _ask_compress(message, filename: str):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("Baja",  callback_data="compress_Baja"))
    markup.add(types.InlineKeyboardButton("Media", callback_data="compress_Media"))
    markup.add(types.InlineKeyboardButton("Alta",  callback_data="compress_Alta"))
    msg = bot.send_message(message.chat.id, "Selecciona la calidad de compresión:", reply_markup=markup)
    msg_files[msg.message_id] = filename


# ── Callbacks inline ───────────────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    old_file = msg_files.get(call.message.message_id)
    if not old_file:
        bot.answer_callback_query(call.id, "⚠️ Esta acción ya no es válida.")
        return

    bot.answer_callback_query(call.id)

    if call.data.startswith("convert_"):
        fmt = call.data.split("_", 1)[1]
        msg = send_typing(call.from_user.id, f"Convirtiendo a {fmt.upper()}...")
        new_file = tmp(old_file.rsplit(os.sep, 1)[-1].rsplit(".", 1)[0] + "." + fmt)
        extra = FORMAT_ARGS.get(fmt, ["-ar", "44100"])
        res = subprocess.run([FFMPEG, "-y", "-i", old_file] + extra + [new_file],
                             capture_output=True, text=True)
        if res.returncode != 0 or not os.path.exists(new_file):
            bot.edit_message_text(f"❌ Error:\n`{res.stderr[-800:]}`",
                                  chat_id=call.from_user.id, message_id=msg.message_id,
                                  parse_mode="Markdown")
            return
        bot.edit_message_text("✅ ¡Conversión completada!",
                              chat_id=call.from_user.id, message_id=msg.message_id)
        with open(new_file, "rb") as f:
            if fmt in ("mp4", "mkv", "mov"):
                bot.send_video(call.from_user.id, f)
            else:
                bot.send_document(call.from_user.id, f)
        safe_remove(old_file, new_file)
        del msg_files[call.message.message_id]

    elif call.data.startswith("enhance_"):
        level = call.data.split("_", 1)[1]
        msg = send_typing(call.from_user.id, "Mejorando el vídeo...")
        base = old_file.rsplit(os.sep, 1)[-1].rsplit(".", 1)
        new_file = tmp(base[0] + f"_mejorado.{base[1]}")
        res = subprocess.run([FFMPEG, "-y", "-i", old_file,
                              "-vf", ENHANCE_ARGS[level],
                              "-c:v", "libx264", "-crf", "18", "-c:a", "copy", new_file],
                             capture_output=True, text=True)
        if res.returncode != 0 or not os.path.exists(new_file):
            bot.edit_message_text(f"❌ Error:\n`{res.stderr[-800:]}`",
                                  chat_id=call.from_user.id, message_id=msg.message_id,
                                  parse_mode="Markdown")
            return
        bot.edit_message_text("✅ ¡Vídeo mejorado!",
                              chat_id=call.from_user.id, message_id=msg.message_id)
        with open(new_file, "rb") as video:
            bot.send_video(call.from_user.id, video)
        safe_remove(old_file, new_file)
        del msg_files[call.message.message_id]

    elif call.data.startswith("compress_"):
        quality = call.data.split("_", 1)[1]
        msg = send_typing(call.from_user.id, f"Comprimiendo en calidad {quality}...")
        base = old_file.rsplit(os.sep, 1)[-1].rsplit(".", 1)
        new_file = tmp(base[0] + f"_Comprimido.{base[1]}")
        res_br = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                                 "-show_entries", "stream=bit_rate",
                                 "-of", "default=noprint_wrappers=1:nokey=1", old_file],
                                capture_output=True, text=True)
        try:
            bitrate = int(res_br.stdout.strip())
        except Exception:
            bot.edit_message_text("❌ Error al leer el bitrate.",
                                  chat_id=call.from_user.id, message_id=msg.message_id)
            return
        factor = {"Baja": 0.1, "Media": 0.2, "Alta": 0.5}.get(quality, 0.2)
        bitrate = int(bitrate * factor)
        subprocess.run([FFMPEG, "-y", "-i", old_file, "-b:v", str(bitrate), new_file])
        if not os.path.exists(new_file):
            bot.edit_message_text("❌ Error al comprimir.",
                                  chat_id=call.from_user.id, message_id=msg.message_id)
            return
        bot.edit_message_text("✅ ¡Compresión completada!",
                              chat_id=call.from_user.id, message_id=msg.message_id)
        with open(new_file, "rb") as video:
            bot.send_video(call.from_user.id, video)
        safe_remove(old_file, new_file)
        del msg_files[call.message.message_id]


# ── Arranque ───────────────────────────────────────────────────────────────────
import signal
import sys

def detener_bot(sig, frame):
    print("\n🛑 Bot detenido.")
    bot.stop_polling()
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, detener_bot)
    signal.signal(signal.SIGTERM, detener_bot)
    # Instalar ffmpeg si no está disponible (Railway puede no tenerlo en PATH)
    if subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0:
        print("📦 ffmpeg no encontrado, instalando...")
        try:
            subprocess.run(["apt-get", "update", "-y"], capture_output=True, check=True)
            subprocess.run(
                ["apt-get", "install", "-y", "ffmpeg"],
                capture_output=True, check=True  # capture_output silencia el output de apt-get
            )
            print("✅ ffmpeg instalado correctamente.")
        except Exception as e:
            print(f"❌ No se pudo instalar ffmpeg: {e}")
            sys.exit(1)
    time.sleep(10)  # Evita conflicto 409 en redeploys (espera a que la instancia anterior muera)
    print("🤖 Bot iniciado correctamente.")
    while True:
        try:
            bot.polling(non_stop=True, timeout=30, long_polling_timeout=20)
        except Exception as e:
            msg = str(e)
            if "Break" in msg or "interrupted" in msg.lower():
                break
            if "409" in msg or "Conflict" in msg:
                print(f"⚠️ Conflicto de instancia (409). Esperando 15s antes de reintentar...")
                time.sleep(15)
            else:
                print(f"⚠️ Error: {msg}. Reconectando en 5s...")
                time.sleep(5)