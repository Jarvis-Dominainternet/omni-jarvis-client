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

state      = State.IDLE
paused     = False
state_lock = threading.Lock()

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

        if rtype in ("auth_locked",):
            return False

        # auth_error o auth_code_required → la GUI mostrará el siguiente campo
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

    # ── detección de dispositivos: GUI (solo si nunca se ha hecho) ──────────
    if not cfg.get("devices_detected", False):
        cfg = detect_devices(cfg)

    log.info(f"Servidor : {cfg['server_ip']}:{cfg['server_port']}")
    log.info(f"Modelo   : {cfg.get('wakeword_model', 'hey_jarvis')} (umbral {cfg.get('wakeword_threshold', 0.5)})")

    # ── arrancar hilos de audio, WS y acciones ────────────────────────────────
    threads = [
        threading.Thread(target=audio_loop,  args=(cfg,), daemon=True, name="audio"),
        threading.Thread(target=ws_loop,     args=(cfg,), daemon=True, name="websocket"),
        threading.Thread(target=action_loop, daemon=True,               name="actions"),
    ]
    for t in threads:
        t.start()

    # ── gestionar GUI de auth si el WS la solicita (hilo principal) ──────────
    # Antes de lanzar pystray, atender posibles peticiones de GUI de login
    auth_shown = False
    deadline = time.monotonic() + 15   # esperar max 15s a que WS inicie
    while time.monotonic() < deadline:
        try:
            req = _gui_request_queue.get(timeout=0.3)
            if req == "show_auth" and not auth_shown:
                auth_shown = True
                show_auth_gui(cfg)
            elif req == "auth_done":
                break
        except queue.Empty:
            # si ya hay token en config, no hay GUI que esperar
            if cfg.get("session_token"):
                break

    # ── pystray en hilo principal ─────────────────────────────────────────────
    icon = create_tray(cfg)
    if icon:
        log.info("Sistema activo — di 'Jarvis' para activar")
        # mientras pystray corre, seguir atendiendo peticiones de GUI de re-login
        def tray_runner():
            icon.run()
        tray_t = threading.Thread(target=tray_runner, daemon=True)
        tray_t.start()
        # bucle principal: atiende re-autenticaciones si el token expira
        while tray_t.is_alive():
            try:
                req = _gui_request_queue.get(timeout=1)
                if req == "show_auth":
                    show_auth_gui(cfg)
            except queue.Empty:
                pass
    else:
        log.info("Sin bandeja — Ctrl-C para salir")
        try:
            while True:
                try:
                    req = _gui_request_queue.get(timeout=1)
                    if req == "show_auth":
                        show_auth_gui(cfg)
                except queue.Empty:
                    pass
        except KeyboardInterrupt:
            log.info("Detenido.")

if __name__ == "__main__":
    main()
