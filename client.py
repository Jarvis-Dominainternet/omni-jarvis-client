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

# ── logging — a consola + siempre a archivo (pythonw.exe no tiene consola) ────
_LOG_FILE = Path(__file__).parent / "jarvis.log"
_log_handlers: list[logging.Handler] = [
    logging.FileHandler(_LOG_FILE, encoding="utf-8", mode="a"),
]
if sys.stdout is not None:          # python normal (con consola)
    _log_handlers.append(logging.StreamHandler())
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
    handlers=_log_handlers,
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
    style.configure(".",              background=BG,   foreground="#cccccc", font=("Consolas", 10),
                                      borderwidth=0,   relief="flat")
    style.configure("TFrame",         background=BG)
    style.configure("Card.TFrame",    background=CARD)
    style.configure("TLabel",         background=BG,   foreground="#cccccc", font=("Consolas", 10))
    style.configure("Title.TLabel",   background=BG,   foreground=CYAN,     font=("Consolas", 13, "bold"))
    style.configure("Sub.TLabel",     background=BG,   foreground="#666688", font=("Consolas", 9))
    style.configure("Card.TLabel",    background=CARD, foreground="#cccccc", font=("Consolas", 10))
    style.configure("TCombobox",      fieldbackground=CARD, background=CARD,
                    foreground="#cccccc", selectbackground="#1e1e3a", font=("Consolas", 10),
                    arrowcolor=CYAN, bordercolor="#1e1e3a")
    style.map("TCombobox",            fieldbackground=[("readonly", CARD)],
              foreground=[("readonly", "#cccccc")])
    style.configure("TCheckbutton",   background=BG,   foreground="#aaaacc", font=("Consolas", 10),
                    indicatorbackground=CARD, indicatorforeground=CYAN)
    style.map("TCheckbutton",         background=[("active", BG)],
              foreground=[("active", CYAN)], indicatorforeground=[("selected", CYAN)])
    style.configure("Cyan.TButton",   background=CYAN, foreground="#000000",
                    font=("Consolas", 11, "bold"), padding=8, borderwidth=0)
    style.map("Cyan.TButton",         background=[("active", "#00aacc"), ("pressed", "#0088aa")])
    style.configure("TEntry",         fieldbackground=CARD, foreground="#cccccc",
                    insertcolor=CYAN, font=("Consolas", 11), bordercolor="#1e1e3a")
    style.configure("TScale",         background=BG,   troughcolor=CARD,
                    sliderlength=14,  sliderrelief="flat")
    style.map("TScale",               background=[("active", BG)])
    # notebook con pestañas oscuras (crítico para Windows)
    style.configure("TNotebook",      background=BG,   borderwidth=0, tabmargins=[2, 2, 0, 0])
    style.configure("TNotebook.Tab",  background="#12122a", foreground="#7777aa",
                    padding=[14, 7],  font=("Consolas", 9),  borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", "#1c1c3e"), ("active", "#16163a")],
              foreground=[("selected", CYAN),      ("active", "#aaaaff")])


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
state_lock    = threading.Lock()

# Usamos threading.Event para paused: thread-safe sin GIL-dependency
_paused_event = threading.Event()   # set() = pausado, clear() = activo

def is_paused() -> bool:
    return _paused_event.is_set()

def set_paused(v: bool):
    if v: _paused_event.set()
    else: _paused_event.clear()

# Volumen: float protegido con lock
_volume_lock  = threading.Lock()
_jarvis_volume: float = 1.0

def get_volume() -> float:
    with _volume_lock:
        return _jarvis_volume

def set_volume(v: float):
    global _jarvis_volume
    with _volume_lock:
        _jarvis_volume = max(0.0, min(1.0, float(v)))

# Modo observación — acumula screenshots para pregunta final
_watch_mode    = False
_watch_lock    = threading.Lock()
_watch_shots:  list[str] = []          # lista de b64 JPEG
_watch_max     = 8                     # máximo de capturas acumuladas
_watch_interval = 4.0                  # segundos entre capturas

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
            # monitors[0] = escritorio virtual completo; monitors[1..n] = monitores reales
            monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            img = sct.grab(monitor)
            pil = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        pil.save(buf, format="JPEG", quality=75)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        log.error(f"Screenshot error: {e}")
        return ""

# ── Modo observación — collage de screenshots ─────────────────────────────────

def _make_collage(shots: list[str]) -> str:
    """Une varias capturas en una cuadrícula 2-columnas y devuelve b64 JPEG."""
    try:
        from PIL import Image
        import math as _math
        images = []
        for b64 in shots:
            try:
                data = base64.b64decode(b64)
                img  = Image.open(io.BytesIO(data)).convert("RGB")
                img.thumbnail((640, 360))
                images.append(img)
            except Exception:
                pass
        if not images:
            return take_screenshot()
        cols  = 2
        rows  = _math.ceil(len(images) / cols)
        W, H  = 640, 360
        grid  = Image.new("RGB", (cols * W, rows * H), (10, 10, 26))
        for i, img in enumerate(images):
            x = (i % cols) * W
            y = (i // cols) * H
            grid.paste(img, (x, y))
        buf = io.BytesIO()
        grid.save(buf, format="JPEG", quality=70)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        log.error(f"collage error: {e}")
        return take_screenshot()


def _watch_loop():
    """Hilo de modo observación: captura pantalla cada N segundos."""
    global _watch_mode
    log.info(f"Modo observación iniciado (cada {_watch_interval}s, máx {_watch_max})")
    while True:
        time.sleep(_watch_interval)
        with _watch_lock:
            if not _watch_mode:
                break
            if len(_watch_shots) < _watch_max:
                shot = take_screenshot()
                if shot:
                    _watch_shots.append(shot)
                    log.debug(f"Observación: {len(_watch_shots)}/{_watch_max} capturas")
            else:
                log.info("Modo observación: buffer lleno, deteniendo capturas")
                break
    with _watch_lock:
        _watch_mode = False
    log.info("Modo observación terminado")


def start_watch_mode():
    global _watch_mode
    with _watch_lock:
        if _watch_mode:
            return
        _watch_mode = True
        _watch_shots.clear()
    threading.Thread(target=_watch_loop, daemon=True, name="watch").start()
    log.info("Modo observación activado — di 'Jarvis' cuando quieras preguntar")


def stop_watch_mode():
    global _watch_mode
    with _watch_lock:
        _watch_mode = False
        # no borramos _watch_shots aquí — el usuario puede preguntar tras stop
    log.info("Modo observación desactivado")


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
    global state

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
            if is_paused():
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

                        # si hay shots acumulados en modo observación, crear collage
                        with _watch_lock:
                            watch_shots = list(_watch_shots)
                            _watch_shots.clear()
                            was_watching = _watch_mode

                        if was_watching and watch_shots:
                            screenshot_b64 = _make_collage(watch_shots)
                            log.info(f"Modo observación: {len(watch_shots)} capturas → collage")
                        else:
                            screenshot_b64 = take_screenshot() if cfg.get("screenshot_on_send", True) else ""

                        webcam_b64 = take_webcam_frame(cfg) if cfg.get("webcam_on_send", False) else ""
                        audio_wav  = frames_to_wav(recording_frames)
                        if not audio_wav:
                            log.warning("frames_to_wav vacío — ignorando grabación")
                            with state_lock:
                                state = State.IDLE
                            continue

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
    if not frames:
        return b""
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
            # vaciar colas para no ejecutar acciones/grabaciones antiguas tras reconectar
            for q in (send_queue, action_queue):
                while True:
                    try: q.get_nowait()
                    except queue.Empty: break
            with state_lock:
                global state
                state = State.IDLE
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
            tmp = None
            try:
                raw_b64 = msg.get("data") or msg.get("content") or ""
                if not raw_b64 or not isinstance(raw_b64, str):
                    log.warning("Mensaje audio vacío o inválido")
                    continue
                try:
                    data = base64.b64decode(raw_b64)
                except Exception:
                    log.warning("Mensaje audio con base64 inválido")
                    continue
                with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
                    f.write(data)
                    tmp = f.name
                arr, sr = sf.read(tmp)
                log.info("Reproduciendo respuesta de Jarvis...")
                sd.play(arr * get_volume(), sr)
                sd.wait()
            except Exception as e:
                log.error(f"Error reproduciendo audio: {e}")
            finally:
                if tmp:
                    try: os.unlink(tmp)
                    except Exception: pass

        elif mtype == "text":
            print(f"\n[JARVIS] {msg.get('content', '')}\n")

        elif mtype in ("action", "actions"):
            acts = msg.get("actions", [msg] if mtype == "action" else [])
            for act in acts:
                try:
                    _execute_action(act, pyautogui)
                except pyautogui.FailSafeException:
                    log.warning("FailSafe activado (ratón en esquina) — acciones detenidas")
                    break
                except Exception as e:
                    log.warning(f"_execute_action error: {e}")
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
        # pyautogui.write no soporta acentos/emojis en Windows → usar clipboard
        try:
            import pyperclip
            pyperclip.copy(tx)
            pyautogui.hotkey("ctrl", "v")
        except ImportError:
            pyautogui.write(tx, interval=0.04)
    elif t == "key" and tx:
        log.info(f"  → key: {tx}")
        try:
            pyautogui.hotkey(*tx.split("+"))
        except Exception as e:
            log.warning(f"  hotkey error '{tx}': {e}")
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
            # asegurar charset UTF-8 en el HTML para que Windows lo muestre bien
            if "<meta charset" not in html.lower():
                html = html.replace("<head>", '<head><meta charset="UTF-8">', 1)
                if "<head>" not in html:
                    html = '<meta charset="UTF-8">' + html
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    suffix=".html", prefix="jarvis-", delete=False,
                    mode="w", encoding="utf-8"
                ) as f:
                    f.write(html)
                    tmp_path = f.name
                log.info(f"  → show_html: {tmp_path} ({len(html)} bytes)")
                # En Windows la ruta necesita format file:///C:/...
                uri = tmp_path if sys.platform != "win32" else tmp_path.replace("\\", "/")
                webbrowser.open(f"file:///{uri}")
                # limpiar tras 30s (navegador ya habrá cargado el archivo)
                def _cleanup(p):
                    time.sleep(30)
                    try: os.unlink(p)
                    except Exception: pass
                threading.Thread(target=_cleanup, args=(tmp_path,), daemon=True).start()
            except Exception as e:
                log.error(f"show_html error: {e}")

    elif t == "watch_screen":
        # activar modo observación: Jarvis captura pantalla cada N segundos
        start_watch_mode()

    elif t == "stop_watch":
        stop_watch_mode()

    elif t == "notify":
        # notificación del sistema (Linux: notify-send, macOS: osascript)
        title = act.get("title", "Jarvis")
        body  = act.get("body", tx)
        log.info(f"  → notify: {title} — {body[:60]}")
        try:
            if sys.platform == "win32":
                # Windows 10/11: usar win10toast si está instalado, si no ignorar
                try:
                    from win10toast import ToastNotifier
                    ToastNotifier().show_toast(title, body, duration=5, threaded=True)
                except ImportError:
                    # fallback: mensaje en consola
                    log.info(f"[NOTIFY] {title}: {body}")
            elif sys.platform == "darwin":
                subprocess.Popen(
                    ["osascript", "-e",
                     f'display notification "{body}" with title "{title}"'])
            else:
                subprocess.Popen(["notify-send", title, body])
        except Exception as e:
            log.debug(f"notify error: {e}")

# ── helpers de UI ────────────────────────────────────────────────────────────

def _btn(parent, text, fg, cmd, bg="#12122a", font=("Consolas", 9, "bold"),
         padx=8, pady=6, hover_bg="#1c1c3e"):
    """Botón plano con hover."""
    import tkinter as tk
    b = tk.Button(parent, text=text, fg=fg, bg=bg, font=font,
                  bd=0, padx=padx, pady=pady, cursor="hand2",
                  activebackground=hover_bg, activeforeground=fg,
                  relief="flat", command=cmd)
    b.bind("<Enter>", lambda _: b.config(bg=hover_bg))
    b.bind("<Leave>", lambda _: b.config(bg=bg))
    return b


def _scrollable(parent, bg, height=120):
    """Frame con scrollbar vertical."""
    import tkinter as tk
    outer = tk.Frame(parent, bg=bg, bd=1, relief="flat",
                     highlightthickness=1, highlightbackground="#1e1e3a")
    cv    = tk.Canvas(outer, bg=bg, highlightthickness=0, height=height)
    sb    = tk.Scrollbar(outer, orient="vertical", command=cv.yview,
                         bg=bg, troughcolor="#0d0d1a", width=10)
    inner = tk.Frame(cv, bg=bg)
    cv.create_window((0, 0), window=inner, anchor="nw")
    cv.configure(yscrollcommand=sb.set)
    inner.bind("<Configure>",
               lambda e: cv.configure(scrollregion=cv.bbox("all")))
    cv.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    return outer, inner


# ── Panel de control flotante ─────────────────────────────────────────────────

class SettingsPanel:
    """Ventana modal de configuración — Audio, Cámara, Servidor."""

    BG   = "#0d0d1a"
    CARD = "#14142b"
    CYAN = "#00d4ff"

    def __init__(self, parent, cfg: dict):
        import tkinter as tk
        import tkinter.ttk as ttk

        self.cfg = cfg
        win = tk.Toplevel(parent)
        self.win = win
        win.title("J.A.R.V.I.S. — Configuración")
        win.resizable(False, False)
        win.configure(bg=self.BG)
        win.grab_set()
        _gui_style(win)

        W, H = 480, 580
        # posicionar a la izquierda del panel principal
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        wx = max(0, px - W - 8)
        wy = max(0, py)
        win.geometry(f"{W}x{H}+{wx}+{wy}")

        self._build(tk, ttk)

    def _build(self, tk, ttk):
        win  = self.win
        cfg  = self.cfg
        BG   = self.BG
        CARD = self.CARD
        CYAN = self.CYAN
        pad  = {"padx": 22, "pady": 5}

        # cabecera
        hdr = tk.Frame(win, bg="#0d0d28", height=42)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="  ⚙  CONFIGURACIÓN", bg="#0d0d28",
                 fg=CYAN, font=("Consolas", 11, "bold")).pack(side="left", pady=8)
        tk.Label(hdr, text="Los cambios se aplican al reiniciar",
                 bg="#0d0d28", fg="#555577",
                 font=("Consolas", 8)).pack(side="right", padx=14)

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=0, pady=0)

        # ─── helper: marco de pestaña con fondo correcto ─────────────────────
        def tab_frame():
            f = tk.Frame(nb, bg=BG)
            return f

        # ─── label de sección ────────────────────────────────────────────────
        def section(parent, text):
            tk.Label(parent, text=text, bg=BG, fg=CYAN,
                     font=("Consolas", 9, "bold")).pack(anchor="w", **pad)

        # ═══ AUDIO ═══════════════════════════════════════════════════════════
        t_audio = tab_frame()
        nb.add(t_audio, text="  🎙  Audio  ")

        section(t_audio, "Wake Word")
        tk.Label(t_audio, text="Modelo", bg=BG, fg="#8888aa",
                 font=("Consolas", 9)).pack(anchor="w", padx=22)
        self._ww_model = tk.StringVar(value=cfg.get("wakeword_model", "hey_jarvis"))
        e = tk.Entry(t_audio, textvariable=self._ww_model,
                     bg=CARD, fg="#cccccc", insertbackground=CYAN,
                     font=("Consolas", 11), bd=0, relief="flat", width=30)
        e.pack(anchor="w", padx=22, ipady=4)
        tk.Label(t_audio, text="hey_jarvis · alexa · hey_mycroft",
                 bg=BG, fg="#444466", font=("Consolas", 8)).pack(anchor="w", padx=22)

        self._ww_lv = tk.StringVar(
            value=f"Umbral  {cfg.get('wakeword_threshold', 0.5):.2f}  "
                  f"← más sensible   menos sensible →")
        tk.Label(t_audio, textvariable=self._ww_lv, bg=BG, fg="#8888aa",
                 font=("Consolas", 8)).pack(anchor="w", padx=22, pady=(8, 0))
        self._ww_thresh = tk.DoubleVar(value=float(cfg.get("wakeword_threshold", 0.5)))
        def _thr(*_):
            v = self._ww_thresh.get()
            self._ww_lv.set(f"Umbral  {v:.2f}  ← más sensible   menos sensible →")
        self._ww_thresh.trace_add("write", _thr)
        tk.Scale(t_audio, from_=0.1, to=0.9, resolution=0.02,
                 orient="horizontal", variable=self._ww_thresh,
                 bg=BG, fg=CYAN, troughcolor=CARD,
                 highlightthickness=0, sliderrelief="flat",
                 showvalue=False, length=300,
                 activebackground=CYAN).pack(anchor="w", padx=20)

        section(t_audio, "Micrófonos")
        scroll_out, mic_inner = _scrollable(t_audio, CARD, height=100)
        scroll_out.pack(fill="x", padx=22, pady=(0, 6))
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
            tk.Checkbutton(mic_inner, text=f"  {name[:42]}", variable=v,
                           bg=CARD, fg="#aaaacc", selectcolor="#1e1e3a",
                           activebackground=CARD, activeforeground=CYAN,
                           font=("Consolas", 9), bd=0).pack(anchor="w", pady=1)
            self._mic_vars[idx] = v

        section(t_audio, "Altavoz")
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
                              values=self._spk_names, state="readonly", width=46)
        spk_cb.current(pre)
        spk_cb.pack(padx=22, anchor="w", pady=(0, 8))

        # ═══ CÁMARA ══════════════════════════════════════════════════════════
        t_cam = tab_frame()
        nb.add(t_cam, text="  📷  Cámara  ")

        section(t_cam, "Cámaras activas")
        scroll_out2, cam_inner = _scrollable(t_cam, CARD, height=100)
        scroll_out2.pack(fill="x", padx=22, pady=(0, 6))
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
                    webcams.append((idx, f"Cámara {idx}  ({w_}×{h_})"))
                    cap.release()
        except ImportError:
            webcams = []
        if webcams:
            for idx, name in webcams:
                v = tk.BooleanVar(value=(idx in sel_cams))
                tk.Checkbutton(cam_inner, text=f"  {name}", variable=v,
                               bg=CARD, fg="#aaaacc", selectcolor="#1e1e3a",
                               activebackground=CARD, activeforeground=CYAN,
                               font=("Consolas", 9), bd=0).pack(anchor="w", pady=1)
                self._cam_vars[idx] = v
        else:
            tk.Label(cam_inner, text="  No se detectaron cámaras",
                     bg=CARD, fg="#444466", font=("Consolas", 9)).pack(padx=8, pady=6)

        self._webcam_send = tk.BooleanVar(value=cfg.get("webcam_on_send", False))
        tk.Checkbutton(t_cam,
                       text="  Enviar imagen de webcam con cada comando",
                       variable=self._webcam_send,
                       bg=BG, fg="#aaaacc", selectcolor="#1e1e3a",
                       activebackground=BG, activeforeground=CYAN,
                       font=("Consolas", 9), bd=0).pack(anchor="w", padx=20, pady=10)

        # ═══ SERVIDOR ════════════════════════════════════════════════════════
        t_srv = tab_frame()
        nb.add(t_srv, text="  🌐  Servidor  ")

        section(t_srv, "Conexión")
        for lbl, attr, default, w in [
            ("IP del DGX  (Tailscale o LAN)", "_srv_ip",   cfg.get("server_ip",   "192.168.1.129"), 28),
            ("Puerto WebSocket",              "_srv_port", str(cfg.get("server_port", 8765)),         10),
        ]:
            tk.Label(t_srv, text=lbl, bg=BG, fg="#8888aa",
                     font=("Consolas", 9)).pack(anchor="w", padx=22, pady=(6, 1))
            var = tk.StringVar(value=default)
            setattr(self, attr, var)
            tk.Entry(t_srv, textvariable=var, bg=CARD, fg="#cccccc",
                     insertbackground=CYAN, font=("Consolas", 11),
                     bd=0, relief="flat", width=w).pack(anchor="w", padx=22, ipady=4)

        # ─── pie con guardar ─────────────────────────────────────────────────
        foot = tk.Frame(win, bg="#0d0d28", height=44)
        foot.pack(fill="x", side="bottom")
        foot.pack_propagate(False)
        self._status = tk.StringVar(value="")
        tk.Label(foot, textvariable=self._status, bg="#0d0d28",
                 fg="#00bb88", font=("Consolas", 9)).pack(side="left", padx=18)
        _btn(foot, "  GUARDAR  ", "#000000", self._save,
             bg=CYAN, font=("Consolas", 10, "bold"),
             hover_bg="#00aacc").pack(side="right", padx=14, pady=7)

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
        self._status.set("✓  Guardado")
        log.info("Configuración guardada")
        self.win.after(1000, self.win.destroy)


class JarvisControlPanel:
    """
    Panel flotante con dos modos:
    - FULL  (290×380): reactor + estado + volumen + botones + config
    - MINI  ( 68× 68): solo el reactor; hover muestra popup de controles
    Minimizable a bandeja del sistema. Arrastrable.
    """

    BG    = "#0a0a18"
    CARD  = "#12122a"
    BAR   = "#0d0d26"
    CYAN  = "#00d4ff"
    RED   = "#ff3355"
    GOLD  = "#ffd700"
    W_FULL = 290
    H_FULL = 382
    W_MINI = 68
    H_MINI = 68

    def __init__(self, cfg: dict):
        import tkinter as tk
        import tkinter.ttk as ttk

        self.tk  = tk
        self.cfg = cfg
        self._tick      = 0
        self._tray_icon = None
        self._drag_x = self._drag_y = 0
        self._mini   = False
        self._popup  = None
        self._popup_inside = False
        self._hide_id      = None

        root = tk.Tk()
        self.root = root
        root.title("J.A.R.V.I.S.")
        root.resizable(False, False)
        root.configure(bg=self.BG)
        root.overrideredirect(True)
        root.attributes("-topmost", True)

        # posición inicial: esquina inferior derecha
        root.update_idletasks()
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        self._x_full = sw - self.W_FULL - 18
        self._y_full = sh - self.H_FULL - 52
        self._x_mini = sw - self.W_MINI - 18
        self._y_mini = sh - self.H_MINI - 52
        root.geometry(f"{self.W_FULL}x{self.H_FULL}+{self._x_full}+{self._y_full}")

        # borde decorativo (1px CYAN)
        root.configure(highlightthickness=1,
                       highlightbackground="#1a1a3a",
                       highlightcolor="#00d4ff")

        self._build_full()
        self._animate()
        self._poll_queue()

    # ═══════════════════════════════════════════════════════════════════════════
    # CONSTRUCCIÓN MODO FULL
    # ═══════════════════════════════════════════════════════════════════════════

    def _build_full(self):
        tk   = self.tk
        root = self.root
        BG   = self.BG
        CARD = self.CARD
        BAR  = self.BAR
        CYAN = self.CYAN

        # ── barra superior ────────────────────────────────────────────────────
        bar = tk.Frame(root, bg=BAR, height=28)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        for w in (bar, root):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>",     self._drag_move)

        tk.Label(bar, text="  ◈  J.A.R.V.I.S.", bg=BAR,
                 fg=CYAN, font=("Consolas", 10, "bold"),
                 cursor="fleur").pack(side="left")

        # botones de la barra (mini y bandeja)
        for txt, cmd in [("▫", self._toggle_mini), ("─", self._to_tray)]:
            b = tk.Button(bar, text=txt, bg=BAR, fg="#666688",
                          font=("Consolas", 10), bd=0, padx=6,
                          activebackground="#1a1a3a", activeforeground=CYAN,
                          cursor="hand2", relief="flat", command=cmd)
            b.pack(side="right")
            b.bind("<Enter>", lambda e, b=b: b.config(fg=CYAN))
            b.bind("<Leave>", lambda e, b=b: b.config(fg="#666688"))

        # ── reactor ───────────────────────────────────────────────────────────
        self.cv = tk.Canvas(root, width=130, height=130,
                            bg=BG, highlightthickness=0)
        self.cv.pack(pady=(8, 2))
        self.cv.bind("<ButtonPress-1>", self._drag_start)
        self.cv.bind("<B1-Motion>",     self._drag_move)
        self._build_reactor(self.cv, 65, 65, CYAN)

        # ── estado ────────────────────────────────────────────────────────────
        self._state_var = tk.StringVar(value="🔵  ESCUCHANDO")
        tk.Label(root, textvariable=self._state_var,
                 bg=BG, fg=CYAN, font=("Consolas", 9)).pack(pady=(0, 4))

        # ── separador ─────────────────────────────────────────────────────────
        tk.Frame(root, bg="#1a1a3a", height=1).pack(fill="x", padx=16)

        # ── volumen ───────────────────────────────────────────────────────────
        vf = tk.Frame(root, bg=BG)
        vf.pack(fill="x", padx=16, pady=(6, 2))
        tk.Label(vf, text="VOL", bg=BG, fg="#444466",
                 font=("Consolas", 8)).pack(side="left")
        self._vol = tk.DoubleVar(value=1.0)
        self._vol_lbl = tk.Label(vf, text="100%", bg=BG, fg="#666688",
                                 font=("Consolas", 8), width=5)
        self._vol_lbl.pack(side="right")
        tk.Scale(vf, from_=0.0, to=1.0, resolution=0.05,
                 orient="horizontal", variable=self._vol,
                 bg=BG, fg=CYAN, troughcolor=CARD,
                 highlightthickness=0, sliderrelief="flat",
                 showvalue=False, length=190,
                 activebackground=CYAN,
                 command=self._on_volume).pack(side="left", padx=(6, 0))

        # ── botones Pausar / Parar ─────────────────────────────────────────────
        bf = tk.Frame(root, bg=BG)
        bf.pack(fill="x", padx=14, pady=(4, 2))
        self._pause_btn = _btn(bf, "⏸  PAUSAR", CYAN, self._on_pause,
                               bg=CARD, hover_bg="#1c1c40")
        self._pause_btn.pack(side="left", expand=True, fill="x", padx=(0, 3), ipady=3)
        _btn(bf, "⏹  PARAR", self.RED, self._on_stop,
             bg=CARD, hover_bg="#2a0a14").pack(side="right", expand=True, fill="x", ipady=3)

        # ── botón configuración ────────────────────────────────────────────────
        _btn(root, "⚙  CONFIGURACIÓN", "#7777aa", self._on_settings,
             bg=BAR, hover_bg=CARD,
             font=("Consolas", 9)).pack(fill="x", padx=14, pady=(3, 10), ipady=4)

    def _build_reactor(self, cv, cx, cy, color):
        """Dibuja el arc reactor en un Canvas dado."""
        r = min(cx, cy) - 6
        cv.create_oval(cx-r,   cy-r,   cx+r,   cy+r,   outline=color, width=1,  fill="", tags="r3")
        cv.create_oval(cx-r+16, cy-r+16, cx+r-16, cy+r-16, outline=color, width=2, fill="", tags="r2")
        cv.create_oval(cx-r+30, cy-r+30, cx+r-30, cy+r-30, outline=color, width=2,
                       fill=self.BG, tags="r1")
        cv.create_oval(cx-12, cy-12, cx+12, cy+12, outline="", fill=color, tags="core")
        for deg in range(0, 360, 60):
            rad = math.radians(deg)
            x1 = cx + (r-28) * math.cos(rad); y1 = cy + (r-28) * math.sin(rad)
            x2 = cx + (r-10) * math.cos(rad); y2 = cy + (r-10) * math.sin(rad)
            cv.create_line(x1, y1, x2, y2, fill=color, width=1, tags="spoke")

    # ═══════════════════════════════════════════════════════════════════════════
    # MODO MINI + POPUP
    # ═══════════════════════════════════════════════════════════════════════════

    def _toggle_mini(self):
        self._mini = not self._mini
        if self._mini:
            self._x_full = self.root.winfo_x()
            self._y_full = self.root.winfo_y()
            # limpiar canvas items antes de destruir widgets (evita memory leak)
            if hasattr(self, "cv"):
                try: self.cv.delete("all")
                except Exception: pass
            for w in self.root.winfo_children():
                try: w.destroy()
                except Exception: pass
            self._build_mini_root()
            self.root.geometry(
                f"{self.W_MINI}x{self.H_MINI}+{self._x_mini}+{self._y_mini}")
        else:
            if self._popup:
                try: self._popup.destroy()
                except Exception: pass
                self._popup = None
            if hasattr(self, "cv"):
                try: self.cv.delete("all")
                except Exception: pass
            for w in self.root.winfo_children():
                try: w.destroy()
                except Exception: pass
            self._build_full()
            self.root.geometry(
                f"{self.W_FULL}x{self.H_FULL}+{self._x_full}+{self._y_full}")

    def _build_mini_root(self):
        tk   = self.tk
        root = self.root
        BG   = self.BG
        CYAN = self.CYAN

        root.configure(bg=BG)
        self.cv = tk.Canvas(root, width=self.W_MINI, height=self.H_MINI,
                            bg=BG, highlightthickness=0)
        self.cv.pack()
        cx = cy = self.W_MINI // 2
        self._build_reactor(self.cv, cx, cy, CYAN)

        # hover → mostrar popup
        self.cv.bind("<Enter>", self._on_mini_enter)
        self.cv.bind("<Leave>", self._on_mini_leave)
        root.bind("<Enter>",   self._on_mini_enter)
        root.bind("<Leave>",   self._on_mini_leave)
        root.bind("<ButtonPress-1>", self._drag_start)
        root.bind("<B1-Motion>",     self._drag_move)

    def _on_mini_enter(self, _event=None):
        if self._hide_id:
            self.root.after_cancel(self._hide_id)
            self._hide_id = None
        if not self._popup:
            self._show_popup()

    def _on_mini_leave(self, _event=None):
        self._hide_id = self.root.after(300, self._maybe_hide_popup)

    def _maybe_hide_popup(self):
        if not self._popup_inside and self._popup:
            self._popup.destroy()
            self._popup = None

    def _show_popup(self):
        tk   = self.tk
        BG   = self.BG
        CARD = self.CARD
        CYAN = self.CYAN

        pop = tk.Toplevel(self.root)
        self._popup = pop
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        pop.configure(bg=BG,
                      highlightthickness=1, highlightbackground="#1a1a3a")

        W = 180
        rx = self.root.winfo_x()
        ry = self.root.winfo_y()
        # posicionar a la izquierda del icono mini
        px = rx - W - 6
        if px < 0:
            px = rx + self.W_MINI + 6
        pop.geometry(f"{W}x{230}+{px}+{ry}")

        # estado
        self._popup_state = tk.StringVar(value="🔵  ESCUCHANDO")
        tk.Label(pop, textvariable=self._popup_state,
                 bg=BG, fg=CYAN, font=("Consolas", 8)).pack(pady=(8, 4))

        tk.Frame(pop, bg="#1a1a3a", height=1).pack(fill="x", padx=10)

        # volumen compacto
        vf = tk.Frame(pop, bg=BG)
        vf.pack(fill="x", padx=10, pady=4)
        tk.Label(vf, text="VOL", bg=BG, fg="#444466",
                 font=("Consolas", 8)).pack(side="left")
        if not hasattr(self, "_vol"):
            self._vol = tk.DoubleVar(value=1.0)
        tk.Scale(vf, from_=0.0, to=1.0, resolution=0.05,
                 orient="horizontal", variable=self._vol,
                 bg=BG, fg=CYAN, troughcolor=CARD,
                 highlightthickness=0, sliderrelief="flat",
                 showvalue=False, length=110,
                 activebackground=CYAN,
                 command=self._on_volume).pack(side="right")

        tk.Frame(pop, bg="#1a1a3a", height=1).pack(fill="x", padx=10)

        # botones
        for txt, fg, cmd in [
            ("⏸  Pausar/Reanudar", CYAN,      self._on_pause),
            ("⚙  Configuración",   "#7777aa",  self._on_settings),
            ("⊞  Expandir panel",  "#556688",  self._toggle_mini),
            ("⏹  Parar",           self.RED,   self._on_stop),
        ]:
            _btn(pop, txt, fg, cmd, bg=BG, hover_bg=CARD,
                 font=("Consolas", 9), padx=6, pady=5).pack(
                     fill="x", padx=8, pady=1)

        # para evitar que el popup desaparezca al entrar en él
        pop.bind("<Enter>", lambda _: self._set_popup_inside(True))
        pop.bind("<Leave>", lambda _: self._set_popup_inside(False))

    def _set_popup_inside(self, val):
        self._popup_inside = val
        if not val:
            self._hide_id = self.root.after(300, self._maybe_hide_popup)
        elif self._hide_id:
            self.root.after_cancel(self._hide_id)
            self._hide_id = None

    # ═══════════════════════════════════════════════════════════════════════════
    # ANIMACIÓN
    # ═══════════════════════════════════════════════════════════════════════════

    def _animate(self):
        try:
            self._tick += 1
            t = self._tick
            pulse = 0.35 + 0.65 * abs(math.sin(t * 0.07))

            with state_lock:
                s = state

            if s == State.RECORDING:
                color = self.RED
                lbl   = "🔴  GRABANDO..."
            elif s == State.PROCESSING:
                color = self.GOLD if (t // 6) % 2 == 0 else "#aa8800"
                lbl   = "⚡  PROCESANDO..."
            else:
                v = int((0x88 + int(0x77 * pulse)) & 0xff)
                color = f"#00{v:02x}ff"
                lbl   = "⏸  PAUSADO" if is_paused() else "🔵  ESCUCHANDO"

            if hasattr(self, "cv") and self.cv.winfo_exists():
                try:
                    self.cv.itemconfig("core",  fill=color, outline="")
                    self.cv.itemconfig("r1",    outline=color, fill=self.BG)
                    self.cv.itemconfig("r2",    outline=color, fill="")
                    self.cv.itemconfig("r3",    outline=color, fill="")
                    self.cv.itemconfig("spoke", fill=color)
                except Exception:
                    pass

            if hasattr(self, "_state_var"):
                try: self._state_var.set(lbl)
                except Exception: pass
            if hasattr(self, "_popup_state") and self._popup:
                try:
                    if self._popup.winfo_exists():
                        self._popup_state.set(lbl)
                except Exception: pass

            if hasattr(self, "_pause_btn"):
                try:
                    if self._pause_btn.winfo_exists():
                        if is_paused():
                            self._pause_btn.config(text="▶  REANUDAR", fg=self.GOLD)
                        else:
                            self._pause_btn.config(text="⏸  PAUSAR",   fg=self.CYAN)
                except Exception: pass

        except Exception as e:
            log.debug(f"_animate error: {e}")
        finally:
            self.root.after(50, self._animate)

    # ═══════════════════════════════════════════════════════════════════════════
    # COLA AUTH
    # ═══════════════════════════════════════════════════════════════════════════

    def _poll_queue(self):
        try:
            req = _gui_request_queue.get_nowait()
            if req == "show_auth":
                show_auth_gui(self.cfg)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_queue)

    # ═══════════════════════════════════════════════════════════════════════════
    # CALLBACKS
    # ═══════════════════════════════════════════════════════════════════════════

    def _on_volume(self, val):
        set_volume(float(val))
        if hasattr(self, "_vol_lbl") and self._vol_lbl.winfo_exists():
            self._vol_lbl.config(text=f"{int(float(val)*100)}%")

    def _on_pause(self):
        set_paused(not is_paused())
        log.info("Jarvis pausado" if is_paused() else "Jarvis reanudado")

    def _on_stop(self):
        log.info("Cerrando Omni-Jarvis...")
        if self._tray_icon:
            try: self._tray_icon.stop()
            except Exception: pass
        os._exit(0)

    def _on_settings(self):
        if self._popup:
            self._popup.destroy()
            self._popup = None
        SettingsPanel(self.root, self.cfg)

    # ═══════════════════════════════════════════════════════════════════════════
    # BANDEJA
    # ═══════════════════════════════════════════════════════════════════════════

    def _to_tray(self):
        self.root.withdraw()
        if self._popup:
            self._popup.destroy()
            self._popup = None
        self._start_tray()

    def _start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            self.root.deiconify()
            return

        def _mk():
            img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse([4,  4,  60, 60], fill=(10, 10, 40, 255))
            draw.ellipse([14, 14, 50, 50], outline=(0, 212, 255, 220), width=2)
            draw.ellipse([26, 26, 38, 38], fill=(0, 212, 255, 255))
            return img

        def _show(icon, _):
            icon.stop(); self._tray_icon = None
            self.root.deiconify(); self.root.lift()

        def _quit(icon, _):
            icon.stop(); os._exit(0)

        icon = pystray.Icon(
            "jarvis", _mk(), "J.A.R.V.I.S.",
            pystray.Menu(
                pystray.MenuItem("Mostrar panel", _show, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Salir", _quit),
            ))
        self._tray_icon = icon
        threading.Thread(target=icon.run, daemon=True, name="tray").start()

    # ═══════════════════════════════════════════════════════════════════════════
    # ARRASTRE
    # ═══════════════════════════════════════════════════════════════════════════

    def _drag_start(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _drag_move(self, event):
        x = self.root.winfo_x() + (event.x - self._drag_x)
        y = self.root.winfo_y() + (event.y - self._drag_y)
        self.root.geometry(f"+{x}+{y}")
        if not self._mini:
            self._x_full, self._y_full = x, y
        else:
            self._x_mini, self._y_mini = x, y

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
