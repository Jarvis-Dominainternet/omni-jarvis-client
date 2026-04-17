#!/usr/bin/env python3
"""
Omni-Jarvis Client v2 — 100% manos libres, sin ventana.
Wake word "Jarvis" → graba tu voz + captura pantalla → DGX Spark → respuesta.

100% local, sin cuentas externas. Usa openwakeword para la detección de voz.

INSTALAR:
  pip install -r requirements.txt

EJECUTAR:
  python client.py
  → detecta dispositivos automáticamente (primer arranque)
  → aparece icono en la bandeja del sistema
  → di "Jarvis" y espera el pitido
"""

import asyncio
import base64
import io
import json
import logging
import os
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
import webbrowser
from pathlib import Path

import numpy as np

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jarvis-client")

# ── config ────────────────────────────────────────────────────────────────────
CONFIG_FILE = Path(__file__).parent / "config.json"

DEFAULT_CONFIG = {
    "server_ip":          "192.168.1.129",  # IP del DGX
    "server_port":        8765,
    "wakeword_model":     "hey_jarvis",     # modelo openwakeword (100% local)
    "wakeword_threshold": 0.5,              # sensibilidad 0.1–0.9 (más bajo = más sensible)
    "vad_silence_sec":    1.5,
    "vad_min_sec":        0.5,
    "beep_on_activate":   True,
    "screenshot_on_send": True,
    "webcam_on_send":     False,
    "mic_device":         None,             # None = auto-detectar
    "speaker_device":     None,             # None = auto-detectar
    "webcam_device":      None,             # None = auto-detectar (índice cv2)
    "devices_detected":   False,
}

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text()))
        except Exception as e:
            log.warning(f"Error leyendo config.json: {e}")
    if os.environ.get("OMNI_SERVER"):
        cfg["server_ip"] = os.environ["OMNI_SERVER"]
    return cfg

def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

# ── Detección automática de dispositivos ─────────────────────────────────────

def detect_devices(cfg: dict) -> dict:
    """
    Detecta micrófonos, altavoces y webcams disponibles.
    Muestra la lista, deja elegir o usa el primero de cada categoría.
    Guarda la selección en config y marca devices_detected=True.
    """
    print("\n" + "═"*56)
    print("  J.A.R.V.I.S. — Detección de dispositivos")
    print("═"*56)

    # ── Audio ─────────────────────────────────────────────────────────────────
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        mics     = [(i, d) for i, d in enumerate(devices) if d["max_input_channels"]  > 0]
        speakers = [(i, d) for i, d in enumerate(devices) if d["max_output_channels"] > 0]

        print(f"\n  MICRÓFONOS ({len(mics)} encontrados):")
        for i, (idx, d) in enumerate(mics):
            marker = "  [defecto]" if idx == sd.default.device[0] else ""
            print(f"    {i}) [{idx}] {d['name']}{marker}")

        print(f"\n  ALTAVOCES ({len(speakers)} encontrados):")
        for i, (idx, d) in enumerate(speakers):
            marker = "  [defecto]" if idx == sd.default.device[1] else ""
            print(f"    {i}) [{idx}] {d['name']}{marker}")

        if mics:
            try:
                sel = input(f"\n  Selecciona micrófono [Enter = defecto]: ").strip()
                cfg["mic_device"] = mics[int(sel)][0] if sel else None
            except Exception:
                cfg["mic_device"] = None

        if speakers:
            try:
                sel = input(f"  Selecciona altavoz   [Enter = defecto]: ").strip()
                cfg["speaker_device"] = speakers[int(sel)][0] if sel else None
            except Exception:
                cfg["speaker_device"] = None

    except Exception as e:
        print(f"  [!] sounddevice no disponible: {e}")

    # ── Webcam ────────────────────────────────────────────────────────────────
    print(f"\n  CÁMARAS / ENTRADAS DE VÍDEO:")
    webcams_found = []
    try:
        import cv2
        for idx in range(8):
            cap = cv2.VideoCapture(idx)
            if cap.isOpened():
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                webcams_found.append((idx, f"{w}x{h}"))
                cap.release()
        if webcams_found:
            for i, (idx, res) in enumerate(webcams_found):
                print(f"    {i}) Cámara {idx} — {res}")
            try:
                sel = input(f"  Selecciona cámara    [Enter = primera]: ").strip()
                cfg["webcam_device"] = webcams_found[int(sel) if sel else 0][0]
            except Exception:
                cfg["webcam_device"] = webcams_found[0][0] if webcams_found else None
        else:
            print("    (ninguna detectada)")
            cfg["webcam_device"] = None
    except ImportError:
        print("    (opencv no instalado — solo captura de pantalla disponible)")
        cfg["webcam_device"] = None

    # ── Pantalla ──────────────────────────────────────────────────────────────
    print(f"\n  CAPTURA DE PANTALLA:")
    try:
        import mss
        with mss.mss() as sct:
            monitors = sct.monitors[1:]
        for i, m in enumerate(monitors):
            print(f"    Monitor {i+1}: {m['width']}x{m['height']} en ({m['left']},{m['top']})")
        print("    ✓ Captura de pantalla disponible")
        cfg["screenshot_on_send"] = True
    except Exception as e:
        print(f"    [!] mss no disponible: {e}")
        cfg["screenshot_on_send"] = False

    # ── Webcam junto con screenshot ───────────────────────────────────────────
    if cfg.get("webcam_device") is not None:
        try:
            sel = input("\n  ¿Enviar también imagen de webcam junto con la pantalla? [s/N]: ").strip().lower()
            cfg["webcam_on_send"] = sel in ("s", "si", "sí", "y", "yes")
        except Exception:
            cfg["webcam_on_send"] = False

    cfg["devices_detected"] = True
    save_config(cfg)

    print("\n  ✓ Configuración guardada en config.json")
    print("═"*56 + "\n")
    return cfg


def take_webcam_frame(cfg: dict) -> str:
    """Captura un frame de la webcam y devuelve JPEG en base64. '' si falla."""
    idx = cfg.get("webcam_device")
    if idx is None:
        return ""
    try:
        import cv2
        cap = cv2.VideoCapture(idx)
        if not cap.isOpened():
            return ""
        ret, frame = cap.read()
        cap.release()
        if not ret:
            return ""
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        return base64.b64encode(buf.tobytes()).decode()
    except Exception as e:
        log.debug(f"Webcam error: {e}")
        return ""


# ── Auth: prompt inicial ──────────────────────────────────────────────────────
def prompt_credentials() -> tuple[str, str]:
    """Pide email y contraseña en terminal la primera vez."""
    print("\n" + "═"*50)
    print("  J.A.R.V.I.S. — Autenticación requerida")
    print("═"*50)
    import getpass
    email    = input("  Email    : ").strip()
    password = getpass.getpass("  Password : ")
    print("═"*50 + "\n")
    return email, password

def prompt_2fa_code() -> str:
    print("\n[JARVIS] Código de verificación enviado a tu correo.")
    code = input("  Introduce el código (6 dígitos): ").strip()
    return code

# ── audio constants (openwakeword requiere 16 kHz int16 mono) ────────────────
SAMPLE_RATE  = 16000
CHANNELS     = 1
DTYPE        = "int16"
OWW_FRAME    = 1280   # 80ms a 16kHz — tamaño de chunk para openwakeword

# ── estado global ─────────────────────────────────────────────────────────────
class State:
    IDLE       = "idle"       # escuchando wake word
    RECORDING  = "recording"  # grabando comando de voz
    PROCESSING = "processing" # enviando al servidor

state      = State.IDLE
paused     = False
state_lock = threading.Lock()

send_queue   : queue.Queue = queue.Queue()
action_queue : queue.Queue = queue.Queue()

# ── pitido de activación ──────────────────────────────────────────────────────
def beep(freq: int = 880, duration: float = 0.12, volume: float = 0.4):
    try:
        import sounddevice as sd
        t = np.linspace(0, duration, int(SAMPLE_RATE * duration), endpoint=False)
        tone = (volume * np.sin(2 * np.pi * freq * t)).astype(np.float32)
        sd.play(tone, SAMPLE_RATE)
        sd.wait()
    except Exception as e:
        log.debug(f"beep error: {e}")

# ── captura de pantalla ───────────────────────────────────────────────────────
def take_screenshot() -> str:
    """Captura la pantalla principal y devuelve JPEG en base64."""
    try:
        import mss
        from PIL import Image
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            img = sct.grab(monitor)
            pil = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        pil.save(buf, format="JPEG", quality=75)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        log.error(f"Screenshot error: {e}")
        return ""

# ── VAD simple: energía RMS ───────────────────────────────────────────────────
def rms(chunk: np.ndarray) -> float:
    return float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))

# ── hilo de wake word + grabación ─────────────────────────────────────────────
def audio_loop(cfg: dict):
    """
    Hilo principal de audio:
    1. openwakeword escucha continuamente la palabra 'Jarvis' (100% local).
    2. Al detectarla: pitido, graba con VAD hasta silencio.
    3. Empaqueta audio + screenshot y lo pone en send_queue.
    """
    global state, paused

    # ── cargar openwakeword ───────────────────────────────────────────────────
    try:
        from openwakeword.model import Model as OWWModel
    except ImportError:
        log.error("openwakeword no instalado. Ejecuta: pip install openwakeword")
        return

    model_name = cfg.get("wakeword_model", "hey_jarvis")
    threshold  = float(cfg.get("wakeword_threshold", 0.5))

    try:
        oww = OWWModel(wakeword_models=[model_name], inference_framework="onnx")
        log.info(f"openwakeword cargado — modelo: '{model_name}', umbral: {threshold}")
    except Exception as e:
        log.error(f"Error cargando openwakeword (modelo '{model_name}'): {e}")
        log.error("Prueba: python -c \"import openwakeword; openwakeword.utils.download_models()\"")
        return

    import sounddevice as sd

    recording_frames: list[np.ndarray] = []
    silence_start: float | None = None
    recording_start: float | None = None

    SILENCE_THRESH = 800
    SILENCE_SEC    = cfg.get("vad_silence_sec", 1.5)
    MIN_SEC        = cfg.get("vad_min_sec", 0.5)

    log.info(f"Escuchando wake word '{model_name}'... (di 'Jarvis' para activar)")

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype=DTYPE,
        device=cfg.get("mic_device"),
        blocksize=OWW_FRAME,
    ) as stream:
        while True:
            if paused:
                time.sleep(0.1)
                continue

            chunk, _ = stream.read(OWW_FRAME)
            chunk = chunk.flatten()

            with state_lock:
                current_state = state

            if current_state == State.IDLE:
                # ── detección wake word con openwakeword ──────────────────────
                # predict espera numpy int16 1-D
                prediction = oww.predict(chunk)
                score = max(prediction.get(model_name, 0),
                            prediction.get(model_name.replace("hey_", ""), 0))
                if score >= threshold:
                    log.info(f"Wake word detectado (score={score:.2f}) — grabando...")
                    if cfg.get("beep_on_activate", True):
                        threading.Thread(target=beep, args=(880, 0.12), daemon=True).start()
                    recording_frames = []
                    silence_start    = None
                    recording_start  = time.monotonic()
                    # limpiar buffer interno de openwakeword para evitar re-trigger
                    oww.reset()
                    with state_lock:
                        state = State.RECORDING

            elif current_state == State.RECORDING:
                # ── VAD: grabar hasta silencio ────────────────────────────────
                recording_frames.append(chunk)
                energy  = rms(chunk)
                elapsed = time.monotonic() - (recording_start or 0)

                if energy < SILENCE_THRESH:
                    if silence_start is None:
                        silence_start = time.monotonic()
                    elif (time.monotonic() - silence_start >= SILENCE_SEC
                          and elapsed >= MIN_SEC):
                        log.info(f"Fin de grabación ({elapsed:.1f}s, {len(recording_frames)} frames)")

                        screenshot_b64 = take_screenshot() if cfg.get("screenshot_on_send", True) else ""
                        webcam_b64     = take_webcam_frame(cfg) if cfg.get("webcam_on_send", False) else ""
                        audio_wav      = frames_to_wav(recording_frames)

                        send_queue.put({
                            "audio":      base64.b64encode(audio_wav).decode(),
                            "screenshot": screenshot_b64,
                            "webcam":     webcam_b64,
                        })
                        with state_lock:
                            state = State.PROCESSING
                else:
                    silence_start = None

            elif current_state == State.PROCESSING:
                pass  # esperando respuesta del servidor


def frames_to_wav(frames: list[np.ndarray]) -> bytes:
    """Convierte lista de numpy int16 en bytes WAV."""
    audio = np.concatenate(frames)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)   # int16 = 2 bytes
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    return buf.getvalue()

# ── hilo WebSocket ────────────────────────────────────────────────────────────
def ws_loop(cfg: dict):
    """Hilo que mantiene conexión WebSocket y pasa mensajes entre colas."""
    asyncio.run(_ws_async(cfg))

async def _ws_async(cfg: dict):
    import websockets

    url = f"ws://{cfg['server_ip']}:{cfg['server_port']}/stream"
    log.info(f"Conectando a {url}")

    while True:
        try:
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=10,
                max_size=20 * 1024 * 1024,
            ) as ws:
                log.info("WebSocket conectado")

                # ── autenticación antes del bucle normal ──────────────────────
                authenticated = await _ws_auth(ws, cfg)
                if not authenticated:
                    log.error("Autenticación fallida. Reintentando en 10s...")
                    await asyncio.sleep(10)
                    continue

                log.info("Autenticado. Sistema operativo.")
                await asyncio.gather(
                    _ws_sender(ws),
                    _ws_receiver(ws),
                )
        except Exception as e:
            log.warning(f"WebSocket desconectado: {e}. Reintentando en 5s...")
            await asyncio.sleep(5)


async def _ws_auth(ws, cfg: dict) -> bool:
    """
    Gestiona el handshake de autenticación.
    Retorna True si queda autenticado.
    """
    loop = asyncio.get_event_loop()

    # 1) Esperar auth_required del servidor
    try:
        raw  = await asyncio.wait_for(ws.recv(), timeout=10)
        msg  = json.loads(raw)
        if msg.get("type") not in ("auth_required", "auth_need_login"):
            # servidor sin auth (no debería pasar, pero por compatibilidad)
            if msg.get("type") == "status":
                return True
    except asyncio.TimeoutError:
        log.error("Timeout esperando respuesta del servidor")
        return False

    # 2) Intentar token guardado
    token = cfg.get("session_token", "")
    if token:
        await ws.send(json.dumps({"type": "auth_token", "token": token}))
        try:
            raw  = await asyncio.wait_for(ws.recv(), timeout=10)
            resp = json.loads(raw)
            if resp.get("type") == "auth_ok":
                log.info(f"Sesión restaurada para {resp.get('email')}")
                return True
            # token inválido → borrar y pedir credenciales
            cfg.pop("session_token", None)
            save_config(cfg)
        except asyncio.TimeoutError:
            pass

    # 3) Pedir credenciales en terminal (fuera del event loop)
    email, password = await loop.run_in_executor(None, prompt_credentials)
    await ws.send(json.dumps({
        "type": "auth_init", "email": email, "password": password
    }))

    try:
        raw  = await asyncio.wait_for(ws.recv(), timeout=15)
        resp = json.loads(raw)
    except asyncio.TimeoutError:
        log.error("Timeout en auth_init")
        return False

    rtype = resp.get("type", "")

    if rtype == "auth_error":
        print(f"\n[JARVIS] {resp.get('message', 'Error de autenticación')}\n")
        return False

    if rtype == "auth_locked":
        print(f"\n[JARVIS] CUENTA BLOQUEADA — {resp.get('message')}")
        print("         Revisa tu correo para el enlace de desbloqueo.\n")
        return False

    if rtype == "auth_code_required":
        # 4) Código 2FA
        code = await loop.run_in_executor(None, prompt_2fa_code)
        await ws.send(json.dumps({"type": "auth_code", "code": code}))
        try:
            raw  = await asyncio.wait_for(ws.recv(), timeout=15)
            resp = json.loads(raw)
        except asyncio.TimeoutError:
            log.error("Timeout esperando verificación 2FA")
            return False

        if resp.get("type") == "auth_ok":
            # guardar token de sesión en config
            new_token = resp.get("token", "")
            if new_token:
                cfg["session_token"] = new_token
                save_config(cfg)
                log.info("Token de sesión guardado (válido 30 días)")
            return True

        print(f"\n[JARVIS] {resp.get('message', 'Código incorrecto')}\n")
        return False

    if rtype == "auth_ok":
        token = resp.get("token", "")
        if token:
            cfg["session_token"] = token
            save_config(cfg)
        return True

    return False

async def _ws_sender(ws):
    """Lee send_queue y envía al servidor."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            payload = await loop.run_in_executor(
                None, lambda: send_queue.get(timeout=0.2)
            )
            msg = json.dumps({
                "type":       "voice_command",
                "audio":      payload["audio"],
                "screenshot": payload.get("screenshot", ""),
                "webcam":     payload.get("webcam", ""),
            })
            await ws.send(msg)
            parts = ["audio"]
            if payload.get("screenshot"): parts.append("pantalla")
            if payload.get("webcam"):     parts.append("webcam")
            log.info(f"Enviado al servidor ({' + '.join(parts)})")
        except queue.Empty:
            await asyncio.sleep(0.05)
        except Exception as e:
            log.error(f"WS sender error: {e}")
            break

async def _ws_receiver(ws):
    """Recibe respuestas del servidor y las pone en action_queue."""
    global state
    async for raw in ws:
        try:
            msg = json.loads(raw)
            mtype = msg.get("type", "")

            if mtype in ("audio", "actions", "action", "text"):
                action_queue.put(msg)

            if mtype in ("audio", "text", "actions", "action"):
                with state_lock:
                    state = State.IDLE   # listo para siguiente comando
                log.info(f"Respuesta recibida ({mtype}) → volviendo a IDLE")

        except Exception as e:
            log.error(f"WS receiver error: {e}")

# ── hilo de acciones / reproducción de audio ─────────────────────────────────
def action_loop():
    """Procesa respuestas del servidor: ejecuta clics o reproduce audio."""
    import sounddevice as sd
    import soundfile as sf
    import pyautogui
    import tempfile

    pyautogui.FAILSAFE = True

    while True:
        try:
            msg = action_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        mtype = msg.get("type", "")

        if mtype == "audio":
            # reproducir audio de Jarvis
            try:
                data = base64.b64decode(msg["data"])
                with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
                    f.write(data)
                    tmp = f.name
                arr, sr = sf.read(tmp)
                os.unlink(tmp)
                log.info("Reproduciendo respuesta de Jarvis...")
                sd.play(arr, sr)
                sd.wait()
            except Exception as e:
                log.error(f"Error reproduciendo audio: {e}")

        elif mtype == "text":
            print(f"\n[JARVIS] {msg.get('content', '')}\n")

        elif mtype in ("action", "actions"):
            acts = msg.get("actions", [msg] if mtype == "action" else [])
            for act in acts:
                _execute_action(act, pyautogui)
                time.sleep(act.get("delay", 0.1))


def _execute_action(act: dict, pyautogui):
    t  = act.get("action", "")
    x  = act.get("x")
    y  = act.get("y")
    tx = act.get("text", "")

    if t == "click" and x is not None:
        log.info(f"  → click ({x},{y})")
        pyautogui.click(x, y, duration=0.2)
    elif t == "right_click" and x is not None:
        log.info(f"  → right_click ({x},{y})")
        pyautogui.rightClick(x, y)
    elif t == "double_click" and x is not None:
        log.info(f"  → double_click ({x},{y})")
        pyautogui.doubleClick(x, y)
    elif t == "move" and x is not None:
        pyautogui.moveTo(x, y, duration=0.3)
    elif t == "type" and tx:
        log.info(f"  → type: {tx[:40]}")
        pyautogui.write(tx, interval=0.04)
    elif t == "key" and tx:
        log.info(f"  → key: {tx}")
        pyautogui.hotkey(*tx.split("+"))
    elif t == "scroll" and x is not None:
        pyautogui.scroll(act.get("clicks", 3), x=x, y=y)

    elif t == "open_url":
        url = act.get("url", "")
        if url:
            log.info(f"  → open_url: {url}")
            webbrowser.open(url)

    elif t == "show_html":
        html = act.get("html", "")
        if html:
            # guardar en fichero temporal y abrir en navegador
            tmp = tempfile.NamedTemporaryFile(
                suffix=".html", prefix="jarvis-", delete=False, mode="w", encoding="utf-8"
            )
            tmp.write(html)
            tmp.close()
            log.info(f"  → show_html: {tmp.name} ({len(html)} bytes)")
            webbrowser.open(f"file://{tmp.name}")

    elif t == "notify":
        # notificación del sistema (Linux: notify-send, macOS: osascript)
        title = act.get("title", "Jarvis")
        body  = act.get("body", tx)
        log.info(f"  → notify: {title} — {body[:60]}")
        try:
            if sys.platform == "darwin":
                subprocess.Popen(
                    ["osascript", "-e",
                     f'display notification "{body}" with title "{title}"']
                )
            else:
                subprocess.Popen(["notify-send", title, body])
        except Exception as e:
            log.debug(f"notify error: {e}")

# ── icono del system tray ─────────────────────────────────────────────────────
def build_icon():
    """Genera un icono 64×64 minimalista para la bandeja."""
    from PIL import Image, ImageDraw, ImageFont
    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # fondo circular azul oscuro
    draw.ellipse([2, 2, 62, 62], fill=(20, 30, 80, 255))
    # letra J blanca
    draw.text((22, 14), "J", fill=(255, 255, 255, 255))
    return img

def create_tray(cfg: dict):
    """Crea y arranca el icono en la bandeja del sistema."""
    try:
        import pystray
        from PIL import Image
    except ImportError:
        log.warning("pystray/Pillow no disponibles — sin icono en bandeja")
        return None

    global paused

    def on_pause(icon, item):
        global paused
        paused = not paused
        label = "▶ Reanudar" if paused else "⏸ Pausar"
        log.info("Pausado" if paused else "Reanudado")

    def on_set_ip(icon, item):
        ip = input("IP del servidor DGX: ").strip()
        if ip:
            cfg["server_ip"] = ip
            save_config(cfg)
            log.info(f"IP guardada: {ip} — reinicia para aplicar")

    def on_quit(icon, item):
        log.info("Cerrando Omni-Jarvis...")
        icon.stop()
        os._exit(0)

    icon = pystray.Icon(
        "omni-jarvis",
        build_icon(),
        "Omni-Jarvis",
        menu=pystray.Menu(
            pystray.MenuItem("⏸ Pausar / Reanudar", on_pause),
            pystray.MenuItem("🌐 Cambiar IP servidor", on_set_ip),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("❌ Salir", on_quit),
        ),
    )
    return icon

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    cfg = load_config()

    print("═══════════════════════════════════")
    print("   Omni-Jarvis v2 — J.A.R.V.I.S.  ")
    print("═══════════════════════════════════")

    # ── primer arranque: guardar config base ─────────────────────────────────
    if not CONFIG_FILE.exists():
        save_config(cfg)

    # ── detección de dispositivos (solo si nunca se ha hecho) ────────────────
    if not cfg.get("devices_detected", False):
        cfg = detect_devices(cfg)

    log.info("═══════════════════════════════")
    log.info("  Omni-Jarvis v2 — arrancando  ")
    log.info("═══════════════════════════════")
    log.info(f"Servidor : {cfg['server_ip']}:{cfg['server_port']}")
    log.info(f"Wake word: 'Jarvis'")
    log.info(f"Micrófono: {cfg.get('mic_device', 'defecto')}")
    log.info(f"Altavoz  : {cfg.get('speaker_device', 'defecto')}")
    log.info(f"Webcam   : {cfg.get('webcam_device') if cfg.get('webcam_device') is not None else 'no'}")
    log.info(f"Pantalla : {'sí' if cfg.get('screenshot_on_send') else 'no'}")
    log.info(f"Wake word: {cfg.get('wakeword_model', 'hey_jarvis')} (umbral {cfg.get('wakeword_threshold', 0.5)})")

    # arrancar hilos
    threads = [
        threading.Thread(target=audio_loop,  args=(cfg,), daemon=True, name="audio"),
        threading.Thread(target=ws_loop,     args=(cfg,), daemon=True, name="websocket"),
        threading.Thread(target=action_loop, daemon=True,               name="actions"),
    ]
    for t in threads:
        t.start()
        log.info(f"Hilo '{t.name}' iniciado")

    # pystray DEBE correr en el hilo principal (requisito de macOS y Windows)
    icon = create_tray(cfg)
    if icon:
        log.info("Icono en bandeja del sistema activo")
        icon.run()        # bloquea hasta que el usuario elija 'Salir'
    else:
        # sin tray: esperar indefinidamente (Ctrl-C para salir)
        log.info("Sin bandeja — Ctrl-C para salir")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Detenido.")

if __name__ == "__main__":
    main()
