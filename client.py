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
import math
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

def _gui_style(root):
    """Aplica tema oscuro Iron Man a una ventana tkinter."""
    import tkinter.ttk as ttk
    BG   = "#0d0d1a"
    CARD = "#14142b"
    CYAN = "#00d4ff"
    root.configure(bg=BG)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".",             background=BG, foreground="#cccccc", font=("Consolas", 10))
    style.configure("TFrame",        background=BG)
    style.configure("Card.TFrame",   background=CARD)
    style.configure("TLabel",        background=BG, foreground="#cccccc", font=("Consolas", 10))
    style.configure("Title.TLabel",  background=BG, foreground=CYAN,     font=("Consolas", 13, "bold"))
    style.configure("Sub.TLabel",    background=BG, foreground="#888888", font=("Consolas", 9))
    style.configure("Card.TLabel",   background=CARD, foreground="#cccccc", font=("Consolas", 10))
    style.configure("TCombobox",     fieldbackground=CARD, background=CARD,
                    foreground="#cccccc", selectbackground="#1e1e3a", font=("Consolas", 10))
    style.configure("TCheckbutton",  background=BG, foreground="#cccccc", font=("Consolas", 10))
    style.configure("Cyan.TButton",  background=CYAN, foreground="#000000",
                    font=("Consolas", 11, "bold"), padding=8)
    style.map("Cyan.TButton", background=[("active", "#00aacc")])
    style.configure("TEntry",        fieldbackground=CARD, foreground="#cccccc",
                    insertcolor=CYAN, font=("Consolas", 11))


def detect_devices(cfg: dict) -> dict:
    """Ventana GUI para seleccionar micrófono, altavoz y webcam."""
    import tkinter as tk
    import tkinter.ttk as ttk

    # ── recopilar dispositivos ────────────────────────────────────────────────
    mics, speakers, webcams = [], [], []
    screen_info = ""
    default_mic = default_spk = None

    try:
        import sounddevice as sd
        devs = sd.query_devices()
        default_mic = sd.default.device[0]
        default_spk = sd.default.device[1]
        mics     = [(i, d["name"]) for i, d in enumerate(devs) if d["max_input_channels"]  > 0]
        speakers = [(i, d["name"]) for i, d in enumerate(devs) if d["max_output_channels"] > 0]
    except Exception:
        pass

    try:
        import cv2
        for idx in range(6):
            cap = cv2.VideoCapture(idx)
            if cap.isOpened():
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                webcams.append((idx, f"Cámara {idx}  ({w}x{h})"))
                cap.release()
    except ImportError:
        pass

    try:
        import mss
        with mss.mss() as sct:
            mons = sct.monitors[1:]
        screen_info = "  ".join(f"Monitor {i+1}: {m['width']}x{m['height']}" for i, m in enumerate(mons))
        cfg["screenshot_on_send"] = True
    except Exception:
        screen_info = "No disponible"
        cfg["screenshot_on_send"] = False

    # ── construir ventana ─────────────────────────────────────────────────────
    root = tk.Tk()
    root.title("J.A.R.V.I.S. — Configuración")
    root.resizable(False, False)
    _gui_style(root)

    # centrar en pantalla
    root.update_idletasks()
    W, H = 480, 480
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")

    pad = {"padx": 20, "pady": 6}

    ttk.Label(root, text="🤖  J.A.R.V.I.S.", style="Title.TLabel").pack(pady=(20, 2))
    ttk.Label(root, text="Configuración de dispositivos", style="Sub.TLabel").pack(pady=(0, 16))

    def row(label, items, default_idx):
        """Crea una fila con etiqueta y combobox."""
        ttk.Label(root, text=label).pack(anchor="w", padx=24, pady=(8, 1))
        names   = ["— predeterminado del sistema —"] + [n for _, n in items]
        indexes = [None] + [i for i, _ in items]
        var = tk.StringVar()
        cb  = ttk.Combobox(root, textvariable=var, values=names,
                           state="readonly", width=52)
        # preseleccionar defecto
        try:
            pre = next((j+1 for j, (i,_) in enumerate(items) if i == default_idx), 0)
            cb.current(pre)
        except Exception:
            cb.current(0)
        cb.pack(padx=24, pady=(0, 2))
        return var, indexes, names

    mic_var,  mic_idxs,  mic_names  = row("🎙️  Micrófono",  mics,     default_mic)
    spk_var,  spk_idxs,  spk_names  = row("🔊  Altavoz",    speakers, default_spk)

    if webcams:
        cam_var, cam_idxs, cam_names = row("📷  Cámara",    webcams, None)
        cam_names[0] = "— sin cámara —"
    else:
        cam_var = cam_idxs = None
        ttk.Label(root, text="📷  Cámara: no detectada", style="Sub.TLabel").pack(
            anchor="w", padx=24, pady=(8, 2))

    ttk.Label(root, text=f"🖥️  Pantalla: {screen_info}", style="Sub.TLabel").pack(
        anchor="w", padx=24, pady=(10, 2))

    webcam_send_var = tk.BooleanVar(value=False)
    if webcams:
        ttk.Checkbutton(root, text="Enviar imagen de webcam junto con la pantalla",
                        variable=webcam_send_var).pack(anchor="w", padx=24, pady=(4, 8))

    status_var = tk.StringVar(value="")
    ttk.Label(root, textvariable=status_var, style="Sub.TLabel").pack(pady=2)

    def on_save():
        # leer selecciones
        try:
            mic_sel = mic_var.get()
            cfg["mic_device"] = mic_idxs[mic_names.index(mic_sel)] if mic_sel in mic_names else None
        except Exception:
            cfg["mic_device"] = None
        try:
            spk_sel = spk_var.get()
            cfg["speaker_device"] = spk_idxs[spk_names.index(spk_sel)] if spk_sel in spk_names else None
        except Exception:
            cfg["speaker_device"] = None
        if cam_var and cam_idxs:
            try:
                cam_sel = cam_var.get()
                cfg["webcam_device"] = cam_idxs[cam_names.index(cam_sel)]
            except Exception:
                cfg["webcam_device"] = None
        else:
            cfg["webcam_device"] = None

        cfg["webcam_on_send"]   = webcam_send_var.get()
        cfg["devices_detected"] = True
        save_config(cfg)
        status_var.set("✓ Guardado")
        root.after(600, root.destroy)

    ttk.Button(root, text="Guardar y continuar →",
               style="Cyan.TButton", command=on_save).pack(pady=(12, 20))

    root.lift()
    root.attributes("-topmost", True)
    root.mainloop()
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


# ── Auth GUI ──────────────────────────────────────────────────────────────────
# Cola para comunicar resultado de auth desde hilo WS al hilo GUI
_auth_result_queue: queue.Queue = queue.Queue()
# Cola para enviar inputs del usuario al hilo WS
_auth_input_queue:  queue.Queue = queue.Queue()


def show_auth_gui(cfg: dict) -> bool:
    """
    Ventana GUI de login. Lanza el handshake WS en un hilo y muestra
    los campos necesarios (email/pass → 2FA). Devuelve True si OK.
    """
    import tkinter as tk
    import tkinter.ttk as ttk

    result = {"ok": False}

    root = tk.Tk()
    root.title("J.A.R.V.I.S. — Acceso")
    root.resizable(False, False)
    _gui_style(root)

    W, H = 400, 340
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")

    ttk.Label(root, text="🤖  J.A.R.V.I.S.", style="Title.TLabel").pack(pady=(22, 2))
    ttk.Label(root, text="Autenticación requerida", style="Sub.TLabel").pack(pady=(0, 16))

    # ── campos email / password ───────────────────────────────────────────────
    frame = ttk.Frame(root)
    frame.pack(padx=30, fill="x")

    ttk.Label(frame, text="Email").grid(row=0, column=0, sticky="w", pady=4)
    email_var = tk.StringVar(value=cfg.get("last_email", "sistema@dominainternet.com"))
    email_entry = ttk.Entry(frame, textvariable=email_var, width=34)
    email_entry.grid(row=0, column=1, padx=(10, 0), pady=4)

    ttk.Label(frame, text="Clave").grid(row=1, column=0, sticky="w", pady=4)
    pass_var = tk.StringVar()
    pass_entry = ttk.Entry(frame, textvariable=pass_var, show="●", width=34)
    pass_entry.grid(row=1, column=1, padx=(10, 0), pady=4)

    # ── campo código 2FA (oculto inicialmente) ────────────────────────────────
    code_frame = ttk.Frame(root)
    ttk.Label(code_frame, text="Código 2FA").grid(row=0, column=0, sticky="w", pady=4)
    code_var = tk.StringVar()
    code_entry = ttk.Entry(code_frame, textvariable=code_var, width=20,
                            font=("Consolas", 16, "bold"))
    code_entry.grid(row=0, column=1, padx=(10, 0), pady=4)

    status_var = tk.StringVar(value="")
    status_lbl = ttk.Label(root, textvariable=status_var, style="Sub.TLabel")
    status_lbl.pack(pady=4)

    btn = ttk.Button(root, text="Iniciar sesión →", style="Cyan.TButton")
    btn.pack(pady=(6, 16))

    phase = {"current": "login"}  # login → code → done

    def check_queue():
        """Polling cada 100ms para procesar respuestas del hilo WS."""
        try:
            msg = _auth_result_queue.get_nowait()
        except queue.Empty:
            if root.winfo_exists():
                root.after(100, check_queue)
            return

        kind = msg.get("type", "")

        if kind == "auth_code_required":
            status_var.set("📧  Código enviado a tu correo")
            frame.pack_forget()
            code_frame.pack(padx=30, fill="x")
            code_entry.focus()
            btn.config(text="Verificar →")
            phase["current"] = "code"

        elif kind == "auth_ok":
            token = msg.get("token", "")
            if token:
                cfg["session_token"] = token
                cfg["last_email"] = email_var.get().strip()
                save_config(cfg)
            status_var.set("✓  Acceso concedido")
            result["ok"] = True
            root.after(700, root.destroy)
            return

        elif kind in ("auth_error", "auth_locked"):
            status_var.set(f"✗  {msg.get('message', 'Error')}")
            btn.config(state="normal")

        elif kind == "connecting":
            status_var.set("Conectando…")

        if root.winfo_exists():
            root.after(100, check_queue)

    def on_action():
        btn.config(state="disabled")
        if phase["current"] == "login":
            status_var.set("Verificando…")
            _auth_input_queue.put({
                "type":     "auth_init",
                "email":    email_var.get().strip(),
                "password": pass_var.get(),
            })
        else:
            status_var.set("Verificando código…")
            _auth_input_queue.put({"type": "auth_code", "code": code_var.get().strip()})

    btn.config(command=on_action)
    pass_entry.bind("<Return>", lambda e: on_action())
    code_entry.bind("<Return>", lambda e: on_action())
    email_entry.focus()

    root.after(100, check_queue)
    root.lift()
    root.attributes("-topmost", True)
    root.mainloop()
    return result["ok"]


def prompt_credentials() -> tuple[str, str]:
    """Fallback terminal (no debería usarse si hay GUI)."""
    import getpass
    email    = input("  Email    : ").strip()
    password = getpass.getpass("  Clave    : ")
    return email, password

def prompt_2fa_code() -> str:
    return input("  Código 2FA: ").strip()

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

state         = State.IDLE
paused        = False
state_lock    = threading.Lock()
JARVIS_VOLUME: float = 1.0   # 0.0–1.0, controlado desde el panel de control

send_queue        : queue.Queue = queue.Queue()
action_queue      : queue.Queue = queue.Queue()
_gui_request_queue: queue.Queue = queue.Queue()   # ws → main: solicitudes de GUI

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
                # Si no hay token, notificar al hilo principal para abrir GUI
                needs_gui = not cfg.get("session_token")
                if needs_gui:
                    _gui_request_queue.put("show_auth")

                authenticated = await _ws_auth(ws, cfg)
                if needs_gui:
                    _gui_request_queue.put("auth_done")

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
    Handshake de autenticación.
    Si hay token válido → OK silencioso.
    Si no → comunica con la GUI via colas (_auth_result_queue / _auth_input_queue).
    """
    # 1) Esperar auth_required del servidor
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw)
        if msg.get("type") == "status":
            return True   # servidor sin auth
    except asyncio.TimeoutError:
        log.error("Timeout esperando respuesta del servidor")
        return False

    # 2) Intentar token guardado (silencioso, sin GUI)
    token = cfg.get("session_token", "")
    if token:
        await ws.send(json.dumps({"type": "auth_token", "token": token}))
        try:
            raw  = await asyncio.wait_for(ws.recv(), timeout=10)
            resp = json.loads(raw)
            if resp.get("type") == "auth_ok":
                log.info(f"Sesión restaurada para {resp.get('email')}")
                return True
            cfg.pop("session_token", None)
            save_config(cfg)
        except asyncio.TimeoutError:
            pass

    # 3) Sin token válido → señalar a la GUI que muestre el login
    _auth_result_queue.put({"type": "connecting"})

    # bucle: recibir respuesta del servidor → enviar a GUI → GUI responde → reenviar al servidor
    while True:
        # esperar input del usuario desde la GUI (con timeout por si cierra la ventana)
        loop = asyncio.get_event_loop()
        try:
            user_msg = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _auth_input_queue.get(timeout=120)),
                timeout=125,
            )
        except (asyncio.TimeoutError, Exception):
            return False

        await ws.send(json.dumps(user_msg))

        try:
            raw  = await asyncio.wait_for(ws.recv(), timeout=20)
            resp = json.loads(raw)
        except asyncio.TimeoutError:
            _auth_result_queue.put({"type": "auth_error", "message": "Timeout del servidor"})
            return False

        rtype = resp.get("type", "")
        _auth_result_queue.put(resp)

        if rtype == "auth_ok":
            new_token = resp.get("token", "")
            if new_token:
                cfg["session_token"] = new_token
                save_config(cfg)
                log.info("Token de sesión guardado (válido 30 días)")
            return True

        if rtype in ("auth_locked", "auth_error"):
            return False

        # auth_code_required → continuar el bucle para recibir el código 2FA de la GUI
        # (no hacer return aquí — la GUI enviará auth_code a _auth_input_queue)

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
                sd.play(arr * float(JARVIS_VOLUME), sr)
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

# ── Panel de control flotante ─────────────────────────────────────────────────

class SettingsPanel:
    """Ventana modal de configuración avanzada (Audio, Cámara, Servidor)."""

    BG   = "#0a0a1a"
    CYAN = "#00d4ff"

    def __init__(self, parent, cfg: dict):
        import tkinter as tk
        import tkinter.ttk as ttk

        self.cfg = cfg
        self.win = tk.Toplevel(parent)
        self.win.title("Configuración — J.A.R.V.I.S.")
        self.win.resizable(False, False)
        self.win.configure(bg=self.BG)
        self.win.grab_set()

        _gui_style(self.win)

        W, H = 460, 560
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        self.win.geometry(f"{W}x{H}+{max(0, px-W-10)}+{py}")

        self._build(tk, ttk)

    def _build(self, tk, ttk):
        win = self.win
        cfg = self.cfg
        pad = {"padx": 20, "pady": 4}

        ttk.Label(win, text="⚙  Configuración", style="Title.TLabel").pack(pady=(14, 2))
        ttk.Label(win, text="Los cambios se aplican al reiniciar", style="Sub.TLabel").pack(pady=(0, 8))

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=10, pady=2)

        # ───── Pestaña Audio ─────────────────────────────────────────────────
        t_audio = ttk.Frame(nb)
        nb.add(t_audio, text="🎙️  Audio")

        ttk.Label(t_audio, text="Modelo wake word").pack(anchor="w", **pad)
        self._ww_model = tk.StringVar(value=cfg.get("wakeword_model", "hey_jarvis"))
        ttk.Entry(t_audio, textvariable=self._ww_model, width=36).pack(padx=20, anchor="w")
        ttk.Label(t_audio, text="(ej: hey_jarvis, alexa, hey_mycroft)",
                  style="Sub.TLabel").pack(anchor="w", padx=20)

        self._ww_thresh_lbl_var = tk.StringVar(
            value=f"Umbral wake word  ({cfg.get('wakeword_threshold', 0.5):.2f})")
        ttk.Label(t_audio, textvariable=self._ww_thresh_lbl_var).pack(anchor="w", **pad)
        self._ww_thresh = tk.DoubleVar(value=float(cfg.get("wakeword_threshold", 0.5)))

        def _thresh_trace(*_):
            self._ww_thresh_lbl_var.set(
                f"Umbral wake word  ({self._ww_thresh.get():.2f})")
        self._ww_thresh.trace_add("write", _thresh_trace)

        ttk.Scale(t_audio, from_=0.1, to=0.9, variable=self._ww_thresh,
                  orient="horizontal", length=220).pack(padx=20, anchor="w")

        ttk.Label(t_audio, text="Micrófonos activos").pack(anchor="w", **pad)
        mic_card = ttk.Frame(t_audio, style="Card.TFrame")
        mic_card.pack(fill="x", padx=20, pady=2)

        self._mic_vars: dict[int, tk.BooleanVar] = {}
        sel_mics = set(cfg.get("mic_devices",
                               [cfg["mic_device"]] if cfg.get("mic_device") is not None else []))
        try:
            import sounddevice as sd
            mics = [(i, d["name"]) for i, d in enumerate(sd.query_devices())
                    if d["max_input_channels"] > 0]
        except Exception:
            mics = []
        for idx, name in mics:
            v = tk.BooleanVar(value=(idx in sel_mics))
            ttk.Checkbutton(mic_card, text=f"{name[:40]}",
                            variable=v).pack(anchor="w", padx=8, pady=1)
            self._mic_vars[idx] = v

        ttk.Label(t_audio, text="Altavoz").pack(anchor="w", **pad)
        try:
            spks = [(i, d["name"]) for i, d in enumerate(sd.query_devices())
                    if d["max_output_channels"] > 0]
        except Exception:
            spks = []
        self._spk_names   = ["— predeterminado —"] + [n for _, n in spks]
        self._spk_indices = [None] + [i for i, _ in spks]
        self._spk_var = tk.StringVar()
        cur_spk = cfg.get("speaker_device")
        pre = next((j + 1 for j, (i, _) in enumerate(spks) if i == cur_spk), 0)
        spk_cb = ttk.Combobox(t_audio, textvariable=self._spk_var,
                              values=self._spk_names, state="readonly", width=40)
        spk_cb.current(pre)
        spk_cb.pack(padx=20, anchor="w", pady=(0, 4))

        # ───── Pestaña Cámara ────────────────────────────────────────────────
        t_cam = ttk.Frame(nb)
        nb.add(t_cam, text="📷  Cámara")

        ttk.Label(t_cam, text="Cámaras activas").pack(anchor="w", **pad)
        cam_card = ttk.Frame(t_cam, style="Card.TFrame")
        cam_card.pack(fill="x", padx=20, pady=2)

        self._cam_vars: dict[int, tk.BooleanVar] = {}
        sel_cams = set(cfg.get("webcam_devices",
                               [cfg["webcam_device"]] if cfg.get("webcam_device") is not None else []))
        try:
            import cv2
            webcams = []
            for idx in range(6):
                cap = cv2.VideoCapture(idx)
                if cap.isOpened():
                    w_ = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    h_ = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    webcams.append((idx, f"Cámara {idx}  ({w_}x{h_})"))
                    cap.release()
        except ImportError:
            webcams = []
        for idx, name in webcams:
            v = tk.BooleanVar(value=(idx in sel_cams))
            ttk.Checkbutton(cam_card, text=name, variable=v).pack(anchor="w", padx=8, pady=1)
            self._cam_vars[idx] = v

        if not webcams:
            ttk.Label(cam_card, text="No se detectaron cámaras",
                      style="Sub.TLabel").pack(padx=8, pady=4)

        self._webcam_send = tk.BooleanVar(value=cfg.get("webcam_on_send", False))
        ttk.Checkbutton(t_cam,
                        text="Enviar imagen de webcam con cada comando",
                        variable=self._webcam_send).pack(anchor="w", padx=20, pady=8)

        # ───── Pestaña Servidor ──────────────────────────────────────────────
        t_srv = ttk.Frame(nb)
        nb.add(t_srv, text="🌐  Servidor")

        ttk.Label(t_srv, text="IP del servidor DGX").pack(anchor="w", **pad)
        self._srv_ip = tk.StringVar(value=cfg.get("server_ip", "192.168.1.129"))
        ttk.Entry(t_srv, textvariable=self._srv_ip, width=26).pack(padx=20, anchor="w")

        ttk.Label(t_srv, text="Puerto WebSocket").pack(anchor="w", **pad)
        self._srv_port = tk.StringVar(value=str(cfg.get("server_port", 8765)))
        ttk.Entry(t_srv, textvariable=self._srv_port, width=12).pack(padx=20, anchor="w")

        # ── guardar ──────────────────────────────────────────────────────────
        self._status = tk.StringVar(value="")
        ttk.Label(win, textvariable=self._status, style="Sub.TLabel").pack(pady=2)
        ttk.Button(win, text="  Guardar  ", style="Cyan.TButton",
                   command=self._save).pack(pady=(4, 14))

    def _save(self):
        cfg = self.cfg
        cfg["wakeword_model"]     = self._ww_model.get().strip()
        cfg["wakeword_threshold"] = round(float(self._ww_thresh.get()), 2)

        sel_mics = [i for i, v in self._mic_vars.items() if v.get()]
        cfg["mic_devices"] = sel_mics
        cfg["mic_device"]  = sel_mics[0] if sel_mics else None

        sel_cams = [i for i, v in self._cam_vars.items() if v.get()]
        cfg["webcam_devices"] = sel_cams
        cfg["webcam_device"]  = sel_cams[0] if sel_cams else None
        cfg["webcam_on_send"] = self._webcam_send.get()

        try:
            sel = self._spk_var.get()
            cfg["speaker_device"] = self._spk_indices[self._spk_names.index(sel)]
        except Exception:
            pass
        cfg["server_ip"] = self._srv_ip.get().strip()
        try:
            cfg["server_port"] = int(self._srv_port.get().strip())
        except ValueError:
            pass

        save_config(cfg)
        self._status.set("✓  Guardado — reinicia para aplicar")
        log.info("Configuración guardada desde el panel")
        self.win.after(1200, self.win.destroy)


class JarvisControlPanel:
    """
    Panel de control flotante (esquina inferior derecha).
    - Arc reactor animado: cyan=idle, rojo=grabando, dorado=procesando
    - Pausar / Parar
    - Slider de volumen
    - Botón Configuración → SettingsPanel
    - Minimizar a bandeja del sistema
    - Arrastrable (sin bordes del SO)
    """

    BG   = "#0a0a1a"
    CARD = "#14142b"
    CYAN = "#00d4ff"
    RED  = "#ff3355"
    GOLD = "#ffd700"
    W    = 290
    H    = 370

    def __init__(self, cfg: dict):
        import tkinter as tk
        import tkinter.ttk as ttk

        self.tk   = tk
        self.ttk  = ttk
        self.cfg  = cfg
        self._tick      = 0
        self._tray_icon = None
        self._drag_x = self._drag_y = 0

        root = tk.Tk()
        self.root = root
        root.title("J.A.R.V.I.S.")
        root.resizable(False, False)
        root.configure(bg=self.BG)
        root.overrideredirect(True)   # sin chrome del SO

        # esquina inferior derecha
        root.update_idletasks()
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        x  = sw - self.W - 18
        y  = sh - self.H - 56
        root.geometry(f"{self.W}x{self.H}+{x}+{y}")

        # siempre encima
        root.attributes("-topmost", True)

        self._build_ui()
        self._animate()
        self._poll_queue()

        # arrastre con ratón
        for w in (root,):
            w.bind("<ButtonPress-1>",   self._drag_start)
            w.bind("<B1-Motion>",       self._drag_move)

    # ── construcción de widgets ───────────────────────────────────────────────

    def _build_ui(self):
        tk  = self.tk
        root = self.root
        BG   = self.BG
        CYAN = self.CYAN

        # barra de título
        bar = tk.Frame(root, bg="#0d0d28", height=26)
        bar.pack(fill="x")
        bar.bind("<ButtonPress-1>", self._drag_start)
        bar.bind("<B1-Motion>",     self._drag_move)
        bar.pack_propagate(False)

        tk.Label(bar, text="  ◈  J.A.R.V.I.S.", bg="#0d0d28",
                 fg=CYAN, font=("Consolas", 10, "bold"),
                 cursor="fleur").pack(side="left", pady=3)
        tk.Label(bar, text="", bg="#0d0d28").pack(side="left", expand=True)
        tk.Button(bar, text="  ─  ", bg="#0d0d28", fg="#888888",
                  font=("Consolas", 9), bd=0,
                  activebackground="#1a1a3a", activeforeground=CYAN,
                  cursor="hand2",
                  command=self._minimize_to_tray).pack(side="right", pady=1, padx=2)

        # ── arc reactor ──────────────────────────────────────────────────────
        self.cv = tk.Canvas(root, width=130, height=130,
                            bg=BG, highlightthickness=0)
        self.cv.pack(pady=(10, 2))

        cx = cy = 65
        # anillos (exterior → interior)
        self._r3 = self.cv.create_oval(cx-58, cy-58, cx+58, cy+58,
                                       outline=CYAN, width=1, fill="")
        self._r2 = self.cv.create_oval(cx-42, cy-42, cx+42, cy+42,
                                       outline=CYAN, width=2, fill="")
        self._r1 = self.cv.create_oval(cx-26, cy-26, cx+26, cy+26,
                                       outline=CYAN, width=2, fill=BG)
        self._core = self.cv.create_oval(cx-14, cy-14, cx+14, cy+14,
                                         outline="", fill=CYAN)
        # líneas radiales estilo reactor
        for deg in range(0, 360, 60):
            rad = math.radians(deg)
            x1 = cx + 18 * math.cos(rad); y1 = cy + 18 * math.sin(rad)
            x2 = cx + 38 * math.cos(rad); y2 = cy + 38 * math.sin(rad)
            self.cv.create_line(x1, y1, x2, y2, fill=CYAN, width=1, tags="spoke")

        # ── estado texto ──────────────────────────────────────────────────────
        self._state_var = tk.StringVar(value="IDLE")
        tk.Label(root, textvariable=self._state_var,
                 bg=BG, fg=CYAN, font=("Consolas", 9)).pack(pady=(2, 4))

        # ── volumen ───────────────────────────────────────────────────────────
        vf = tk.Frame(root, bg=BG)
        vf.pack(fill="x", padx=18, pady=(0, 4))
        tk.Label(vf, text="VOL", bg=BG, fg="#555577",
                 font=("Consolas", 8)).pack(side="left")
        self._vol = tk.DoubleVar(value=1.0)
        tk.Scale(vf, from_=0.0, to=1.0, resolution=0.05,
                 orient="horizontal", variable=self._vol,
                 bg=BG, fg=CYAN, troughcolor="#14142b",
                 highlightthickness=0, sliderrelief="flat",
                 showvalue=False, length=210,
                 command=self._on_volume).pack(side="right")

        # ── botones principales ───────────────────────────────────────────────
        bf = tk.Frame(root, bg=BG)
        bf.pack(fill="x", padx=14, pady=(2, 2))

        self._pause_btn = tk.Button(
            bf, text="⏸  PAUSAR",
            bg=self.CARD, fg=CYAN, font=("Consolas", 9, "bold"),
            bd=0, padx=8, pady=7,
            activebackground="#1e1e3a", activeforeground=CYAN,
            cursor="hand2", command=self._on_pause)
        self._pause_btn.pack(side="left", expand=True, fill="x", padx=(0, 3))

        tk.Button(
            bf, text="⏹  PARAR",
            bg=self.CARD, fg=self.RED, font=("Consolas", 9, "bold"),
            bd=0, padx=8, pady=7,
            activebackground="#2a0a14", activeforeground=self.RED,
            cursor="hand2", command=self._on_stop
        ).pack(side="right", expand=True, fill="x")

        # ── botón configuración ───────────────────────────────────────────────
        tk.Button(
            root, text="⚙  CONFIGURACIÓN",
            bg="#0d0d28", fg="#7777aa", font=("Consolas", 9),
            bd=0, pady=7,
            activebackground=self.CARD, activeforeground=CYAN,
            cursor="hand2", command=self._on_settings
        ).pack(fill="x", padx=14, pady=(2, 10))

    # ── animación del reactor ─────────────────────────────────────────────────

    def _animate(self):
        self._tick += 1
        t = self._tick

        pulse = 0.4 + 0.6 * abs(math.sin(t * 0.06))

        with state_lock:
            s = state

        if s == State.RECORDING:
            color      = self.RED
            lbl        = "🔴  GRABANDO..."
        elif s == State.PROCESSING:
            color      = self.GOLD
            lbl        = "⚡  PROCESANDO..."
        else:
            # IDLE: pulso suave en el anillo exterior
            v = int(pulse * 0xff)
            color = f"#00{v:02x}ff"
            lbl   = "⏸  PAUSADO" if paused else "🔵  ESCUCHANDO"

        self.cv.itemconfig(self._core, fill=color)
        self.cv.itemconfig(self._r1,  outline=color)
        self.cv.itemconfig(self._r2,  outline=color)
        self.cv.itemconfig(self._r3,  outline=color)
        self.cv.itemconfig("spoke",   fill=color)
        self._state_var.set(lbl)

        if paused:
            self._pause_btn.config(text="▶  REANUDAR", fg=self.GOLD)
        else:
            self._pause_btn.config(text="⏸  PAUSAR",   fg=self.CYAN)

        self.root.after(50, self._animate)

    # ── sondeo de cola para auth ──────────────────────────────────────────────

    def _poll_queue(self):
        try:
            req = _gui_request_queue.get_nowait()
            if req == "show_auth":
                show_auth_gui(self.cfg)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_queue)

    # ── callbacks ─────────────────────────────────────────────────────────────

    def _on_volume(self, val):
        global JARVIS_VOLUME
        JARVIS_VOLUME = float(val)

    def _on_pause(self):
        global paused
        paused = not paused
        log.info("Jarvis pausado" if paused else "Jarvis reanudado")

    def _on_stop(self):
        log.info("Cerrando Omni-Jarvis...")
        if self._tray_icon:
            try: self._tray_icon.stop()
            except Exception: pass
        os._exit(0)

    def _on_settings(self):
        SettingsPanel(self.root, self.cfg)

    # ── minimizar a bandeja ───────────────────────────────────────────────────

    def _minimize_to_tray(self):
        self.root.withdraw()
        self._start_tray()

    def _start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            self.root.deiconify()
            return

        def _mk_icon():
            img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse([4,  4,  60, 60], fill=(10, 10, 40, 255))
            draw.ellipse([14, 14, 50, 50], outline=(0, 212, 255, 230), width=2)
            draw.ellipse([26, 26, 38, 38], fill=(0, 212, 255, 255))
            return img

        def _on_show(icon, item):
            icon.stop()
            self._tray_icon = None
            self.root.deiconify()
            self.root.lift()

        def _on_quit(icon, item):
            icon.stop()
            os._exit(0)

        icon = pystray.Icon(
            "omni-jarvis", _mk_icon(), "J.A.R.V.I.S.",
            menu=pystray.Menu(
                pystray.MenuItem("Mostrar panel", _on_show, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Salir", _on_quit),
            )
        )
        self._tray_icon = icon
        threading.Thread(target=icon.run, daemon=True, name="tray").start()

    # ── arrastre ──────────────────────────────────────────────────────────────

    def _drag_start(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _drag_move(self, event):
        x = self.root.winfo_x() + (event.x - self._drag_x)
        y = self.root.winfo_y() + (event.y - self._drag_y)
        self.root.geometry(f"+{x}+{y}")

    def run(self):
        self.root.mainloop()


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    cfg = load_config()

    print("═══════════════════════════════════")
    print("   Omni-Jarvis v2 — J.A.R.V.I.S.  ")
    print("═══════════════════════════════════")

    if not CONFIG_FILE.exists():
        save_config(cfg)

    # primer arranque: seleccionar dispositivos
    if not cfg.get("devices_detected", False):
        cfg = detect_devices(cfg)

    log.info(f"Servidor : {cfg['server_ip']}:{cfg['server_port']}")
    log.info(f"Modelo   : {cfg.get('wakeword_model', 'hey_jarvis')} (umbral {cfg.get('wakeword_threshold', 0.5)})")

    # arrancar hilos de audio, WS y acciones
    for name, target, args in [
        ("audio",     audio_loop,  (cfg,)),
        ("websocket", ws_loop,     (cfg,)),
        ("actions",   action_loop, ()),
    ]:
        threading.Thread(target=target, args=args, daemon=True, name=name).start()

    # esperar a que el hilo WS solicite (o no) la GUI de auth
    auth_shown = False
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            req = _gui_request_queue.get(timeout=0.3)
            if req == "show_auth" and not auth_shown:
                auth_shown = True
                show_auth_gui(cfg)
            elif req == "auth_done":
                break
        except queue.Empty:
            if cfg.get("session_token"):
                break

    # panel de control — corre en el hilo principal (tkinter)
    panel = JarvisControlPanel(cfg)
    log.info("Panel de control activo — di 'Jarvis' para activar")
    panel.run()


if __name__ == "__main__":
    main()
