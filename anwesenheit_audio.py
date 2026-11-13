"""Anwesenheit -> Audio-Eingang
Erkennt per Webcam, ob jemand vor dem PC sitzt, und wechselt das
Standard-Aufnahmegerät (Mikrofon) von Windows entsprechend.
"""
import json
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
import ctypes
from ctypes import c_int, c_wchar_p, wintypes

import cv2
import comtypes
import pystray
from PIL import Image, ImageDraw
from comtypes import GUID, HRESULT, COMMETHOD, IUnknown, CLSCTX_ALL
from pycaw.pycaw import AudioUtilities

APP_NAME = "Anwesenheit Audio"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "AnwesenheitAudio")
CONFIG_FILE = os.path.join(CONFIG_DIR, "einstellungen.json")
INSTANCE_PORT = 47863  # lokaler Port, damit nur eine Instanz läuft

COLORS = {"present": "#2e9e44", "absent": "#e08a00", "problem": "#e02020", "unknown": "#888888",
          "paused": "#3a7bd5"}
DEFAULT_HOTKEYS = {  # Strg|Alt|Umschalt + Taste
    "toggle": {"mods": 0x2 | 0x1 | 0x4, "vk": 0x4D, "text": "Strg+Alt+Umschalt+M"},
    "manual": {"mods": 0x2 | 0x1 | 0x4, "vk": 0x55, "text": "Strg+Alt+Umschalt+U"},
}
HOTKEY_INFO = {
    "toggle": "Schaltet das automatische Umschalten an/aus.",
    "manual": "Wechselt zwischen den beiden Mikrofonen und pausiert das automatische Umschalten.",
}
CFG_KEYS = {"toggle": "hotkey", "manual": "hotkey_manual"}
OVERLAY_SIZES = {"Klein": 16, "Mittel": 24, "Groß": 36}


def make_icon(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=color)
    d.rounded_rectangle((24, 14, 40, 38), radius=8, fill="white")   # Mikrofon
    d.arc((18, 22, 46, 46), 0, 180, fill="white", width=4)
    d.line((32, 46, 32, 52), fill="white", width=4)
    return img


# ---------------------------------------------------------------- Audio
class IPolicyConfig(IUnknown):
    """Undokumentierte Windows-Schnittstelle zum Setzen des Standardgeräts."""
    _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
    _methods_ = [COMMETHOD([], HRESULT, f"Unused{i}") for i in range(10)] + [
        COMMETHOD([], HRESULT, "SetDefaultEndpoint",
                  (["in"], c_wchar_p, "deviceId"), (["in"], c_int, "role")),
        COMMETHOD([], HRESULT, "SetEndpointVisibility",
                  (["in"], c_wchar_p, "deviceId"), (["in"], c_int, "visible")),
    ]


CLSID_PolicyConfig = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")


def list_inputs():
    """Liefert {Name: Geräte-ID} aller aktiven Aufnahmegeräte."""
    result = {}
    for dev in AudioUtilities.GetAllDevices():
        try:
            if not dev.id.startswith("{0.0.1."):  # 0.0.1 = Aufnahme, 0.0.0 = Wiedergabe
                continue
            if "Active" not in str(dev.state):
                continue
            result[dev.FriendlyName] = dev.id
        except Exception:
            pass
    return result


def set_default_input(device_id):
    policy = comtypes.CoCreateInstance(CLSID_PolicyConfig, IPolicyConfig, CLSCTX_ALL)
    for role in (0, 1, 2):  # Konsole, Multimedia, Kommunikation
        policy.SetDefaultEndpoint(device_id, role)


# ---------------------------------------------------------------- Kamera
def list_cameras():
    try:
        from pygrabber.dshow_graph import FilterGraph
        names = FilterGraph().get_input_devices()
        if names:
            return [f"{i}: {n}" for i, n in enumerate(names)]
    except Exception:
        pass
    found = []
    for i in range(5):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        if cap.isOpened():
            found.append(f"{i}: Kamera {i}")
        cap.release()
    return found


RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
FACE_MODEL = os.path.join(RES_DIR, "face_detection_yunet_2023mar.onnx")


def sensitivity_to_threshold(sens):
    """Empfindlichkeit 1..10 -> Mindest-Sicherheit der Gesichtserkennung."""
    return max(0.2, min(0.9, 0.9 - sens * 0.07))


class Detector(threading.Thread):
    """Kamera-Thread: liest Bilder so schnell wie die Kamera liefert.
    Die Gesichtserkennung läuft in einem eigenen Thread, damit das Bild nie wartet."""
    DETECT_EVERY = 0.2   # Sekunden zwischen Gesichtserkennungen
    DETECT_WIDTH = 640
    PREVIEW_WIDTH = 320

    def __init__(self, cam_index, threshold=0.5, min_size=0.09):
        super().__init__(daemon=True)
        self.cam_index = cam_index
        self.threshold = threshold
        self.min_size = min_size           # Mindest-Gesichtsbreite (Anteil der Bildbreite)
        self.running = True
        self.want_preview = True
        self.face_seen = False
        self.last_face_time = time.time()  # Start als "anwesend" behandeln
        self.last_frame_time = time.time()
        self.latest = None
        self.faces = []                    # [(x, y, w, h, score, zählt)] im 640er-Bild
        self.frame_ppm = None
        self.frame_no = 0
        self.error = None

    def open(self):
        cap = cv2.VideoCapture(self.cam_index, cv2.CAP_DSHOW)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        return cap

    def detect_loop(self):
        try:
            net = cv2.FaceDetectorYN.create(FACE_MODEL, "", (self.DETECT_WIDTH, 360), self.threshold)
        except Exception as e:
            self.error = f"Gesichtserkennung lädt nicht: {e}"
            return
        size = None
        while self.running:
            time.sleep(self.DETECT_EVERY)
            img = self.latest
            if img is None:
                continue
            if size != (img.shape[1], img.shape[0]):
                size = (img.shape[1], img.shape[0])
                net.setInputSize(size)
            net.setScoreThreshold(self.threshold)
            try:
                _, res = net.detect(img)
            except Exception:
                continue
            iw = img.shape[1]
            faces = [] if res is None else [
                (r[0], r[1], r[2], r[3], r[14], r[2] / iw >= self.min_size) for r in res]
            self.faces = faces
            self.face_seen = any(f[5] for f in faces)  # nur ausreichend große Gesichter zählen
            if self.face_seen:
                self.last_face_time = time.time()

    def run(self):
        threading.Thread(target=self.detect_loop, daemon=True).start()
        cap = self.open()
        fails = 0
        while self.running:
            ok, frame = cap.read() if cap.isOpened() else (False, None)
            if not self.running:
                break
            if not ok:
                fails += 1
                self.error = "Kamera nicht verfügbar" if not cap.isOpened() else "Kein Bild von der Kamera"
                time.sleep(1)
                if fails % 3 == 0:  # Kamera neu öffnen (z.B. nach Abstecken)
                    cap.release()
                    cap = self.open()
                continue
            fails = 0
            if self.error and not self.error.startswith("Gesichtserkennung"):
                self.error = None
            self.last_frame_time = time.time()

            h = int(frame.shape[0] * self.DETECT_WIDTH / frame.shape[1])
            self.latest = cv2.resize(frame, (self.DETECT_WIDTH, h))

            if self.want_preview:
                f = self.PREVIEW_WIDTH / self.DETECT_WIDTH
                small = cv2.resize(self.latest, (self.PREVIEW_WIDTH, int(h * f)))
                for (x, y, w, fh, score, counts) in self.faces:
                    color = (0, 200, 0) if counts else (150, 150, 150)  # grau = ignoriert (zu klein)
                    p1, p2 = (int(x * f), int(y * f)), (int((x + w) * f), int((y + fh) * f))
                    cv2.rectangle(small, p1, p2, color, 2 if counts else 1)
                    label = f"{score * 100:.0f}% Gr. {w / self.DETECT_WIDTH * 100:.0f}"
                    ly = p1[1] - 4 if p1[1] >= 16 else p2[1] + 12  # oben kein Platz -> unter den Rahmen
                    cv2.putText(small, label, (p1[0], ly),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA)
                ok, buf = cv2.imencode(".ppm", small)
                if ok:
                    self.frame_ppm = buf.tobytes()
                    self.frame_no += 1
        cap.release()


# ---------------------------------------------------------------- Tastenkombination
class HotkeyListener(threading.Thread):
    """Globale Tastenkombination (funktioniert auch, wenn das Fenster nicht im Vordergrund ist)."""
    WM_HOTKEY, WM_QUIT, MOD_NOREPEAT = 0x0312, 0x0012, 0x4000

    def __init__(self, mods, vk, callback):
        super().__init__(daemon=True)
        self.mods, self.vk, self.callback = mods, vk, callback
        self.ok = False
        self.thread_id = None
        self.ready = threading.Event()

    def run(self):
        user32 = ctypes.windll.user32
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        self.ok = bool(user32.RegisterHotKey(None, 1, self.mods | self.MOD_NOREPEAT, self.vk))
        self.ready.set()
        if not self.ok:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == self.WM_HOTKEY:
                self.callback()
        user32.UnregisterHotKey(None, 1)

    def stop(self):
        self.ready.wait(2)
        if self.ok and self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, self.WM_QUIT, 0, 0)


# ---------------------------------------------------------------- Overlay
class Overlay:
    """Kleiner, immer sichtbarer Punkt: grün = anwesend, orange = abwesend,
    rot blinkend = Problem. Verschieben per Ziehen, Doppelklick öffnet das Fenster."""
    KEY = "#ff00fe"  # Farbe, die transparent geschaltet wird

    def __init__(self, app):
        self.app = app
        self.win = None
        self.state = "unknown"
        self.blink = False

    def show(self, size, x, y):
        self.hide()
        self.size = size
        w = tk.Toplevel(self.app.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.config(bg=self.KEY)
        w.attributes("-transparentcolor", self.KEY)
        c = tk.Canvas(w, width=size, height=size, bg=self.KEY, highlightthickness=0, cursor="fleur")
        c.pack()
        self.dot = c.create_oval(1, 1, size - 1, size - 1, fill=COLORS["unknown"], outline="white", width=2)
        if x is None or y is None:
            x = w.winfo_screenwidth() - size - 20
            y = 20
        w.geometry(f"{size}x{size}+{x}+{y}")
        c.bind("<ButtonPress-1>", self.drag_start)
        c.bind("<B1-Motion>", self.drag_move)
        c.bind("<ButtonRelease-1>", lambda e: self.app.save_cfg())
        c.bind("<Double-Button-1>", lambda e: self.app.show())
        self.win, self.canvas = w, c
        self.paint()

    def hide(self):
        if self.win:
            self.win.destroy()
            self.win = None

    def position(self):
        if self.win:
            return self.win.winfo_x(), self.win.winfo_y()
        return None

    def drag_start(self, e):
        self.dx, self.dy = e.x, e.y

    def drag_move(self, e):
        x = self.win.winfo_x() + e.x - self.dx
        y = self.win.winfo_y() + e.y - self.dy
        self.win.geometry(f"+{x}+{y}")

    def set_state(self, state):
        self.state = state
        self.paint()

    def paint(self):
        if not self.win:
            return
        color = COLORS[self.state]
        if self.state == "problem" and self.blink:
            color = "#600000"
        self.canvas.itemconfig(self.dot, fill=color)

    def pulse(self):
        """Wird regelmäßig aufgerufen: Blinken + wieder nach vorne holen."""
        self.blink = not self.blink
        if self.win:
            self.win.attributes("-topmost", True)
            self.win.lift()
        self.paint()


# ---------------------------------------------------------------- GUI
class App:
    def __init__(self, root, hidden=False):
        self.root = root
        root.title("Anwesenheit → Mikrofon")
        root.resizable(False, False)
        if hidden:
            root.withdraw()
        self.commands = queue.Queue()  # Befehle aus Tray/Zweitstart -> GUI-Thread
        self.cfg = self.load_cfg()
        self.detector = None
        self.present = None          # aktueller Zustand (True/False/None)
        self.present_since = None    # für Verzögerung beim Zurückkommen
        self.applied = None          # zuletzt gesetztes Mikrofon
        self.last_try = 0.0
        self.switch_error = None
        self.shown_frame = -1
        self.inputs = {}
        self.ui_state = None

        pad = {"padx": 8, "pady": 4}
        f = ttk.Frame(root, padding=10)
        f.pack()

        ttk.Label(f, text="Kamera:").grid(row=0, column=0, sticky="w", **pad)
        self.cam_var = tk.StringVar()
        self.cam_box = ttk.Combobox(f, textvariable=self.cam_var, state="readonly", width=40)
        self.cam_box.grid(row=0, column=1, **pad)
        self.cam_box.bind("<<ComboboxSelected>>", lambda e: self.start_camera())

        ttk.Label(f, text="Anwesend:").grid(row=1, column=0, sticky="w", **pad)
        self.in_present = tk.StringVar(value=self.cfg.get("present", ""))
        self.box_present = ttk.Combobox(f, textvariable=self.in_present, state="readonly", width=40)
        self.box_present.grid(row=1, column=1, **pad)

        ttk.Label(f, text="Abwesend:").grid(row=2, column=0, sticky="w", **pad)
        self.in_absent = tk.StringVar(value=self.cfg.get("absent", ""))
        self.box_absent = ttk.Combobox(f, textvariable=self.in_absent, state="readonly", width=40)
        self.box_absent.grid(row=2, column=1, **pad)

        ttk.Label(f, text="Abwesend nach (Sek.):").grid(row=3, column=0, sticky="w", **pad)
        self.delay_away = tk.IntVar(value=self.cfg.get("delay_away", 10))
        ttk.Spinbox(f, from_=1, to=600, textvariable=self.delay_away, width=8).grid(row=3, column=1, sticky="w", **pad)

        ttk.Label(f, text="Anwesend nach (Sek.):").grid(row=4, column=0, sticky="w", **pad)
        self.delay_back = tk.IntVar(value=self.cfg.get("delay_back", 1))
        ttk.Spinbox(f, from_=0, to=60, textvariable=self.delay_back, width=8).grid(row=4, column=1, sticky="w", **pad)

        self.active = tk.BooleanVar(value=self.cfg.get("active", True))
        self.active_flag = self.active.get()  # Kopie für den Tray-Thread
        ttk.Checkbutton(f, text="Automatisch umschalten", variable=self.active).grid(row=5, column=1, sticky="w", **pad)
        ttk.Button(f, text="Geräte neu laden", command=self.refresh).grid(row=5, column=0, sticky="w", **pad)

        # --- Erweiterte Einstellungen (ausklappbar)
        self.adv_toggle = ttk.Button(f, text="▶ Erweiterte Einstellungen", command=self.toggle_advanced)
        self.adv_toggle.grid(row=6, column=0, columnspan=2, sticky="w", padx=8, pady=(8, 0))
        adv = ttk.Frame(f, padding=6, relief="groove", borderwidth=2)
        adv.grid(row=7, column=0, columnspan=2, sticky="we", padx=8, pady=(4, 4))
        self.adv_frame = adv

        ttk.Label(adv, text="Empfindlichkeit:").grid(row=0, column=0, sticky="w", padx=4, pady=3)
        self.sens = tk.IntVar(value=self.cfg.get("sensitivity", 6))
        self.sens_scale = ttk.Scale(adv, from_=1, to=10, length=160,
                                    command=lambda v: self.sens.set(round(float(v))))
        self.sens_scale.set(self.sens.get())
        self.sens_scale.grid(row=0, column=1, sticky="w")
        self.sens_label = ttk.Label(adv, text=str(self.sens.get()), width=3)
        self.sens_label.grid(row=0, column=2, padx=6)
        ttk.Label(adv, text="höher = auch stark gedrehter Kopf", foreground="gray").grid(row=0, column=3, sticky="w")

        ttk.Label(adv, text="Mindestgröße Gesicht:").grid(row=1, column=0, sticky="w", padx=4, pady=3)
        self.min_size = tk.IntVar(value=self.cfg.get("min_face_size", 9))
        self.min_scale = ttk.Scale(adv, from_=1, to=30, length=160,
                                   command=lambda v: self.min_size.set(round(float(v))))
        self.min_scale.set(self.min_size.get())
        self.min_scale.grid(row=1, column=1, sticky="w")
        self.min_label = ttk.Label(adv, text=str(self.min_size.get()), width=3)
        self.min_label.grid(row=1, column=2, padx=6)
        ttk.Label(adv, text="höher = Personen im Hintergrund ignorieren", foreground="gray").grid(row=1, column=3, sticky="w")

        self.hotkeys, self.hotkey_vars, self.hotkey_infos, self.hotkey_listeners = {}, {}, {}, {}
        self.capturing = None
        for row, (name, label) in enumerate((("toggle", "Taste: Auto an/aus:"),
                                             ("manual", "Taste: Manuell wechseln:"))):
            r = 2 + row * 2
            ttk.Label(adv, text=label).grid(row=r, column=0, sticky="w", padx=4, pady=(6, 0))
            self.hotkeys[name] = self.cfg.get(CFG_KEYS[name], DEFAULT_HOTKEYS[name])
            self.hotkey_vars[name] = tk.StringVar()
            hk = ttk.Frame(adv)
            hk.grid(row=r, column=1, columnspan=3, sticky="w", pady=(6, 0))
            ttk.Entry(hk, textvariable=self.hotkey_vars[name], state="readonly", width=24).grid(row=0, column=0)
            ttk.Button(hk, text="Ändern", command=lambda n=name: self.capture_hotkey(n)).grid(row=0, column=1, padx=(6, 0))
            ttk.Button(hk, text="Entfernen", command=lambda n=name: self.set_hotkey(n, None)).grid(row=0, column=2, padx=(6, 0))
            self.hotkey_infos[name] = ttk.Label(adv, foreground="gray", wraplength=400)
            self.hotkey_infos[name].grid(row=r + 1, column=1, columnspan=3, sticky="w")
        if not self.cfg.get("advanced_open", False):
            adv.grid_remove()
        else:
            self.adv_toggle.config(text="▼ Erweiterte Einstellungen")

        # --- Overlay-Einstellungen
        of = ttk.LabelFrame(f, text="Overlay", padding=6)
        of.grid(row=8, column=0, columnspan=2, sticky="we", padx=8, pady=(8, 4))
        self.ov_on = tk.BooleanVar(value=self.cfg.get("overlay", True))
        ttk.Checkbutton(of, text="Overlay anzeigen", variable=self.ov_on,
                        command=self.apply_overlay).grid(row=0, column=0, sticky="w", padx=4)
        ttk.Label(of, text="Größe:").grid(row=0, column=1, sticky="e", padx=(20, 4))
        self.ov_size = tk.StringVar(value=self.cfg.get("overlay_size", "Mittel"))
        sb = ttk.Combobox(of, textvariable=self.ov_size, values=list(OVERLAY_SIZES), state="readonly", width=8)
        sb.grid(row=0, column=2, sticky="w")
        sb.bind("<<ComboboxSelected>>", lambda e: self.apply_overlay())
        ttk.Button(of, text="Position zurücksetzen", command=self.reset_overlay).grid(row=0, column=3, padx=(12, 0))
        ttk.Label(of, text="Grün = anwesend · Orange = abwesend · Blau = automatisches Umschalten pausiert · Rot blinkend = Problem. "
                           "Ziehen zum Verschieben, Doppelklick öffnet dieses Fenster.",
                  foreground="gray", wraplength=420).grid(row=1, column=0, columnspan=4, sticky="w", padx=4, pady=(4, 0))

        self.status = ttk.Label(f, text="...", font=("Segoe UI", 12, "bold"))
        self.status.grid(row=9, column=0, columnspan=2, pady=(10, 4))

        self.preview = tk.Label(f, bg="black", width=320, height=240)
        self.preview.grid(row=10, column=0, columnspan=2, pady=4)

        ttk.Label(f, text="Schließen (X) läuft im Hintergrund weiter – beenden über das Tray-Symbol.",
                  foreground="gray").grid(row=11, column=0, columnspan=2, pady=(4, 0))

        for v in (self.in_present, self.in_absent, self.delay_away, self.delay_back, self.active, self.sens,
                  self.min_size):
            v.trace_add("write", lambda *a: self.save_cfg())
        self.sens.trace_add("write", lambda *a: self.apply_sensitivity())
        self.active.trace_add("write", lambda *a: self.on_active_changed())
        self.min_size.trace_add("write", lambda *a: self.apply_sensitivity())
        self.pending_inputs = None
        self.refreshing = False

        self.overlay = Overlay(self)
        self.icons = {s: make_icon(c) for s, c in COLORS.items()}
        self.tray = pystray.Icon(
            "anwesenheit_audio", self.icons["unknown"], APP_NAME,
            menu=pystray.Menu(
                pystray.MenuItem("Öffnen", lambda: self.commands.put("show"), default=True),
                pystray.MenuItem("Automatisch umschalten", lambda: self.commands.put("toggle"),
                                 checked=lambda item: self.active_flag),
                pystray.MenuItem("Beenden", lambda: self.commands.put("quit")),
            ))

        self.refresh()
        self.apply_overlay()
        for name in self.hotkeys:
            self.set_hotkey(name, self.hotkeys[name], save=False)
        root.protocol("WM_DELETE_WINDOW", self.hide)
        self.tray.run_detached()
        self.tick()
        self.preview_tick()
        self.slow_tick()

    # --- Fenster
    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self):
        self.save_cfg()
        self.root.withdraw()

    def visible(self):
        return self.root.state() != "withdrawn"

    # --- Erweiterte Einstellungen / Tastenkombination
    def toggle_advanced(self):
        is_open = self.adv_frame.winfo_ismapped()
        if is_open:
            self.adv_frame.grid_remove()
        else:
            self.adv_frame.grid()
        self.adv_toggle.config(text=("▶" if is_open else "▼") + " Erweiterte Einstellungen")
        self.cfg["advanced_open"] = not is_open
        self.save_cfg()

    def toggle_active(self):
        self.active.set(not self.active.get())
        try:
            self.tray.notify("Automatisches Umschalten " + ("eingeschaltet" if self.active.get() else "pausiert"),
                             APP_NAME)
        except Exception:
            pass

    def on_active_changed(self):
        self.active_flag = bool(self.active.get())
        try:
            self.tray.update_menu()
        except Exception:
            pass

    def manual_switch(self):
        """Wechselt zum jeweils anderen Mikrofon und pausiert das automatische Umschalten."""
        present_name, absent_name = self.in_present.get(), self.in_absent.get()
        try:
            current = AudioUtilities.GetMicrophone().GetId()
        except Exception:
            current = None
        target = absent_name if current and self.inputs.get(present_name) == current else present_name
        if self.active.get():
            self.active.set(False)
        dev_id = self.inputs.get(target)
        try:
            if not dev_id:
                raise RuntimeError(f"Mikrofon nicht gefunden: {target or '(nicht ausgewählt)'}")
            set_default_input(dev_id)
            msg = f"Manuell gewechselt auf: {target}\nAutomatisches Umschalten pausiert"
        except Exception as e:
            msg = f"Manueller Wechsel fehlgeschlagen: {e}"
        try:
            self.tray.notify(msg, APP_NAME)
        except Exception:
            pass

    def set_hotkey(self, name, hotkey, save=True):
        old = self.hotkey_listeners.pop(name, None)
        if old:
            old.stop()
        self.hotkeys[name] = hotkey
        var, info = self.hotkey_vars[name], self.hotkey_infos[name]
        other = [n for n, h in self.hotkeys.items() if n != name and h and hotkey
                 and (h["mods"], h["vk"]) == (hotkey["mods"], hotkey["vk"])]
        if not hotkey:
            var.set("(keine)")
            info.config(text="Keine Tastenkombination gesetzt.", foreground="gray")
        elif other:
            var.set(hotkey["text"])
            info.config(text="Diese Kombination ist schon für die andere Funktion vergeben.",
                        foreground=COLORS["problem"])
        else:
            var.set(hotkey["text"])
            lst = HotkeyListener(hotkey["mods"], hotkey["vk"], lambda: self.commands.put(name))
            lst.start()
            lst.ready.wait(2)
            if lst.ok:
                self.hotkey_listeners[name] = lst
                info.config(text=HOTKEY_INFO[name] + " Funktioniert überall.", foreground="gray")
            else:
                info.config(text="Diese Kombination wird schon von einem anderen Programm benutzt.",
                            foreground=COLORS["problem"])
        if save:
            self.save_cfg()

    def capture_hotkey(self, name):
        if self.capturing:
            return
        self.capturing = name
        for lst in self.hotkey_listeners.values():  # beim Aufnehmen keine Kombination auslösen
            lst.stop()
        self.hotkey_listeners.clear()
        self.hotkey_vars[name].set("Tasten drücken…")
        self.hotkey_infos[name].config(text="Z. B. Strg+Alt+M drücken. Esc = abbrechen.", foreground="gray")
        self.root.bind("<KeyPress>", self.on_capture_key)
        self.root.focus_force()

    def restore_hotkeys(self, except_name=None):
        for n, h in self.hotkeys.items():
            if n != except_name:
                self.set_hotkey(n, h, save=False)

    def on_capture_key(self, e):
        modifiers = ("Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R", "Win_L", "Win_R")
        if e.keysym in modifiers:
            return "break"
        self.root.unbind("<KeyPress>")
        name, self.capturing = self.capturing, None
        if e.keysym == "Escape":
            self.restore_hotkeys()
            return "break"
        mods, names = 0, []
        if e.state & 0x4:
            mods |= 0x2
            names.append("Strg")
        if e.state & 0x20000:
            mods |= 0x1
            names.append("Alt")
        if e.state & 0x1:
            mods |= 0x4
            names.append("Umschalt")
        key = e.keysym.upper() if len(e.keysym) == 1 else e.keysym
        is_fkey = key.startswith("F") and key[1:].isdigit()
        if not mods and not is_fkey:
            self.restore_hotkeys()
            self.hotkey_infos[name].config(text="Bitte mit Strg, Alt oder Umschalt kombinieren (oder eine F-Taste).",
                                           foreground=COLORS["problem"])
            return "break"
        self.restore_hotkeys(except_name=name)
        self.set_hotkey(name, {"mods": mods, "vk": e.keycode, "text": "+".join(names + [key])})
        return "break"

    # --- Overlay
    def apply_overlay(self):
        if self.ov_on.get():
            pos = self.overlay.position() or self.cfg.get("overlay_pos") or (None, None)
            self.overlay.show(OVERLAY_SIZES.get(self.ov_size.get(), 24), *pos)
            if self.ui_state:
                self.overlay.set_state(self.ui_state)
        else:
            self.overlay.hide()
        self.save_cfg()

    def reset_overlay(self):
        self.cfg["overlay_pos"] = None
        self.overlay.hide()
        self.apply_overlay()

    # --- Einstellungen
    def load_cfg(self):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def save_cfg(self):
        if not hasattr(self, "overlay"):
            return  # GUI noch nicht fertig aufgebaut
        try:
            pos = self.overlay.position()
            if pos:
                self.cfg["overlay_pos"] = list(pos)
            self.cfg.update({
                "camera": self.cam_var.get(),
                "present": self.in_present.get(),
                "absent": self.in_absent.get(),
                "delay_away": int(self.delay_away.get()),
                "delay_back": int(self.delay_back.get()),
                "active": bool(self.active.get()),
                "overlay": bool(self.ov_on.get()),
                "overlay_size": self.ov_size.get(),
                "sensitivity": int(self.sens.get()),
                "min_face_size": int(self.min_size.get()),
                "hotkey": self.hotkeys["toggle"],
                "hotkey_manual": self.hotkeys["manual"],
            })
        except (tk.TclError, ValueError):
            return  # z.B. leeres Zahlenfeld während der Eingabe
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump(self.cfg, fh, indent=2, ensure_ascii=False)

    # --- Geräte
    def refresh_inputs(self):
        try:
            self.set_inputs(list_inputs())
        except Exception:
            pass

    def set_inputs(self, inputs):
        self.inputs = inputs
        names = list(inputs)
        self.box_present["values"] = names
        self.box_absent["values"] = names

    def refresh_inputs_background(self):
        """Mikrofone in einem eigenen Thread einlesen, damit das Fenster nicht stockt."""
        if self.refreshing:
            return
        self.refreshing = True

        def work():
            try:
                comtypes.CoInitialize()
                self.pending_inputs = list_inputs()
            except Exception:
                pass
            finally:
                self.refreshing = False

        threading.Thread(target=work, daemon=True).start()

    def apply_sensitivity(self):
        try:
            s = int(self.sens.get())
        except (tk.TclError, ValueError):
            return
        self.sens_label.config(text=str(s))
        if round(float(self.sens_scale.get())) != s:
            self.sens_scale.set(s)
        try:
            m = int(self.min_size.get())
        except (tk.TclError, ValueError):
            return
        self.min_label.config(text=str(m))
        if round(float(self.min_scale.get())) != m:
            self.min_scale.set(m)
        if self.detector:
            self.detector.threshold = sensitivity_to_threshold(s)
            self.detector.min_size = m / 100

    def refresh(self):
        self.refresh_inputs()
        self.applied = None  # nach Neuladen Mikrofon erneut setzen
        cams = list_cameras()
        self.cam_box["values"] = cams
        saved = self.cam_var.get() or self.cfg.get("camera")
        if saved in cams:
            self.cam_var.set(saved)
        elif cams:
            self.cam_var.set(cams[0])
        self.start_camera()

    def start_camera(self):
        if self.detector:
            self.detector.running = False
        self.detector = None
        self.present = None
        self.save_cfg()
        if not self.cam_var.get():
            return
        self.detector = Detector(int(self.cam_var.get().split(":")[0]),
                                 sensitivity_to_threshold(int(self.sens.get())),
                                 int(self.min_size.get()) / 100)
        self.detector.start()

    def restart_hung_camera(self):
        """Watchdog: Kamera liefert keine Bilder mehr -> neu starten,
        Anwesenheits-Zustand dabei behalten."""
        old = self.detector
        old.running = False
        new = Detector(old.cam_index, old.threshold, old.min_size)
        new.last_face_time = old.last_face_time
        new.error = "Kamera hing – wird neu gestartet"
        self.detector = new
        new.start()

    # --- Schleifen
    def preview_tick(self):
        """Kamerabild ~30x pro Sekunde, nur wenn das Fenster offen ist."""
        d = self.detector
        vis = self.visible()
        if d:
            d.want_preview = vis
            if vis and d.frame_ppm and d.frame_no != self.shown_frame:
                self.shown_frame = d.frame_no
                img = tk.PhotoImage(data=d.frame_ppm)
                self.preview.config(image=img, width=img.width(), height=img.height())
                self.preview.image = img
        self.root.after(33, self.preview_tick)

    def slow_tick(self):
        """Alle 0,5 s: Overlay blinken, Kamera-Watchdog; alle 10 s Mikrofone neu einlesen."""
        self.overlay.pulse()
        self._n = getattr(self, "_n", 0) + 1
        if self._n % 20 == 0:
            self.refresh_inputs_background()
        if self.pending_inputs is not None:
            self.set_inputs(self.pending_inputs)
            self.pending_inputs = None
        d = self.detector
        hung_ok = d and (not d.error or d.error.startswith("Kamera hing"))  # sonst versucht es der Thread selbst
        if hung_ok and time.time() - d.last_frame_time > 4:
            self.restart_hung_camera()
        self.root.after(500, self.slow_tick)

    def tick(self):
        while not self.commands.empty():
            cmd = self.commands.get()
            if cmd == "show":
                self.show()
            elif cmd == "toggle":
                self.toggle_active()
            elif cmd == "manual":
                self.manual_switch()
            elif cmd == "quit":
                self.close()
                return
        d = self.detector
        if d and not d.error:
            self.evaluate(d)
        self.update_status()
        self.root.after(200, self.tick)

    def evaluate(self, d):
        now = time.time()
        try:
            away_after = max(1, int(self.delay_away.get()))
            back_after = max(0, int(self.delay_back.get()))
        except (tk.TclError, ValueError):
            return

        if self.present is not False:
            # anwesend (oder unbekannt): abwesend, wenn lange kein Gesicht
            new_state = (now - d.last_face_time) < away_after
            self.present_since = None
        else:
            # abwesend: anwesend, wenn Gesicht lange genug gesehen
            if d.face_seen:
                self.present_since = self.present_since or now
            else:
                self.present_since = None
            new_state = self.present_since is not None and now - self.present_since >= back_after
        self.present = new_state
        self.apply_input()

    def apply_input(self):
        """Setzt das passende Mikrofon, falls es noch nicht gesetzt ist.
        Bei Fehlern wird alle 5 s erneut versucht."""
        if not self.active.get():
            self.applied = None
            self.switch_error = None
            return
        name = self.in_present.get() if self.present else self.in_absent.get()
        if not name:
            self.switch_error = "Mikrofon für „{}“ nicht ausgewählt".format("Anwesend" if self.present else "Abwesend")
            return
        if name == self.applied:
            return
        if time.time() - self.last_try < 5 and self.switch_error:
            return
        self.last_try = time.time()
        dev_id = self.inputs.get(name)
        if not dev_id:
            self.switch_error = f"Mikrofon nicht gefunden: {name}"
            return
        try:
            set_default_input(dev_id)
            self.applied = name
            self.switch_error = None
        except Exception as e:
            self.switch_error = f"Umschalten fehlgeschlagen: {e}"

    def update_status(self):
        d = self.detector
        if not d:
            state, text = "problem", "Keine Kamera gefunden"
        elif d.error:
            state, text = "problem", d.error
        elif not self.active.get():
            who = "" if self.present is None else (" – anwesend" if self.present else " – abwesend")
            state, text = "paused", "Umschalten pausiert" + who
        elif self.switch_error:
            state, text = "problem", self.switch_error
        elif self.present is None:
            state, text = "unknown", "Starte..."
        elif self.present:
            state, text = "present", "Anwesend"
        else:
            state, text = "absent", "Abwesend"
        self.status.config(text=text, foreground=COLORS[state])
        if state != self.ui_state:
            self.ui_state = state
            self.overlay.set_state(state)
            self.tray.icon = self.icons[state]
            self.tray.title = f"{APP_NAME}: {text}"[:127]

    def close(self):
        if self.detector:
            self.detector.running = False
        self.save_cfg()
        for lst in self.hotkey_listeners.values():
            lst.stop()
        try:
            self.tray.stop()
        except Exception:
            pass
        self.root.destroy()


def single_instance(app_ref):
    """Startet den Instanz-Server. False, wenn schon eine Instanz läuft
    (die bekommt dann den Befehl, ihr Fenster zu zeigen)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", INSTANCE_PORT))
    except OSError:
        try:
            with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=2) as c:
                c.sendall(b"show")
        except OSError:
            pass
        return False
    srv.listen(1)

    def serve():
        while True:
            conn, _ = srv.accept()
            with conn:
                if conn.recv(16) == b"show" and app_ref:
                    app_ref[0].commands.put("show")

    threading.Thread(target=serve, daemon=True).start()
    return True


if __name__ == "__main__":
    app_ref = []
    if not single_instance(app_ref):
        sys.exit(0)
    root = tk.Tk()
    app_ref.append(App(root, hidden="--hidden" in sys.argv))
    root.mainloop()
