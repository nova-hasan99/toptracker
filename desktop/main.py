# main.py — PyQt6
# Toggle ON/OFF + idle auto-pause/resume (accumulated timer)
# Screenshot every 10 min with small preview + 10s auto-accept
# Professional per-event sounds via QSoundEffect (WAV files in ./sounds)

import sys, time, threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import requests

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtMultimedia import QSoundEffect
from pynput import keyboard, mouse
import mss, mss.tools

API_BASE = "http://127.0.0.1:8000"  # change if needed
SOUNDS_DIR = Path(__file__).parent / "sounds"

# ---------------- Sound Manager ----------------
class SoundManager(QtCore.QObject):
    """Manages cached QSoundEffect instances for UI event sounds."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.effects: dict[str, QSoundEffect] = {}

    def play(self, name: str, volume: float = 0.75) -> None:
        """Play a named sound effect, loading and caching it on first use."""
        file_map = {
            "login": "login.wav",
            "toggle_on": "toggle_on.wav",
            "toggle_off": "toggle_off.wav",
            "auto_resume": "auto_resume.wav",
            "ss_shutter": "ss_shutter.wav",
            "ss_accept": "ss_accept.wav",
            "ss_auto_accept": "ss_auto_accept.wav",
            "ss_reject": "ss_reject.wav",
        }
        fname = file_map.get(name)
        if not fname:
            return
        path = SOUNDS_DIR / fname
        if not path.exists():
            return
        eff = self.effects.get(name)
        if eff is None:
            eff = QSoundEffect(self)
            self.effects[name] = eff
        eff.setSource(QtCore.QUrl.fromLocalFile(str(path)))
        eff.setVolume(max(0.0, min(1.0, volume)))
        eff.play()

sound: "SoundManager|None" = None  # set in main()

# ---------------- State ----------------
@dataclass
class TrackerState:
    token: str | None = None
    user: dict | None = None

    session_id: int | None = None
    session_started_at: datetime | None = None
    session_started_ts: float | None = None
    accumulated_secs: float = 0.0

    key_count: int = 0
    mouse_count: int = 0
    last_activity: float = time.time()
    idle_threshold_sec: int = 60

    tracking_enabled: bool = False
    capture_screens: bool = True
    stop_flag: bool = False

    screenshot_interval_sec: int = 10
    last_screenshot_ts: float = 0.0
    preview_pending: bool = False

state = TrackerState()

# ---------------- API helpers ----------------
def api_headers() -> dict[str, str]:
    """Return the Authorization header dict for the current session, or empty if logged out."""
    return {"Authorization": f"Bearer {state.token}"} if state.token else {}

def api_post(path, json=None, files=None, data=None):
    """POST to the backend API and return the parsed JSON response, raising on HTTP errors."""
    r = requests.post(f"{API_BASE}{path}", json=json, files=files, data=data,
                      headers=api_headers(), timeout=20)
    r.raise_for_status()
    return r.json()

def api_get(path, params=None):
    """GET from the backend API and return the parsed JSON response, raising on HTTP errors."""
    r = requests.get(f"{API_BASE}{path}", params=params, headers=api_headers(), timeout=20)
    r.raise_for_status()
    return r.json()

def format_duration(seconds: int) -> str:
    """Format a duration in seconds as H:MM:SS for display in the UI."""
    seconds = max(0, int(seconds))
    hrs, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    return f"{hrs}:{mins:02d}:{secs:02d}"

# ---------------- Activity listeners ----------------
def on_key(_) -> None:
    """Pynput keyboard listener callback: bump the key counter and refresh activity timestamp."""
    state.key_count += 1
    state.last_activity = time.time()

def on_move(*_) -> None:
    """Pynput mouse listener callback: bump the mouse counter and refresh activity timestamp."""
    state.mouse_count += 1
    state.last_activity = time.time()

# ---------------- Screenshot utilities ----------------
def take_screenshot_bytes() -> bytes:
    """Capture the full screen and return the image as PNG bytes."""
    with mss.mss() as sct:
        monitor = sct.monitors[0]
        img = sct.grab(monitor)
        return mss.tools.to_png(img.rgb, img.size)

# ---------------- Thread→UI bridge ----------------
class Bridge(QtCore.QObject):
    show_preview = QtCore.pyqtSignal(bytes)          # png bytes
    upload_now = QtCore.pyqtSignal(bytes, int, bool) # png, session_id, auto_accept?
    rejected = QtCore.pyqtSignal()
    session_started = QtCore.pyqtSignal(bool)        # manual? True/False
    session_stopped = QtCore.pyqtSignal()

bridge = Bridge()

# ---------------- Small Preview Popup ----------------
class PreviewPopup(QtWidgets.QWidget):
    def __init__(self, png_bytes: bytes, countdown_sec: int = 10):
        super().__init__()
        self._png = png_bytes
        self._remaining = countdown_sec
        self._auto_next_accept = False
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.Tool
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)

        card = QtWidgets.QFrame()
        card.setStyleSheet("""
        QFrame { background:#101214; border-radius:10px; border:1px solid #2a2e33; }
        QLabel { color:#e6e6e6; font-size:12px; }
        QPushButton { padding:4px 10px; border-radius:8px; font-weight:600; }
        QPushButton#accept { background:#1f6f43; color:white; }
        QPushButton#reject { background:#7a1e1e; color:white; }
        """)
        v = QtWidgets.QVBoxLayout(card); v.setContentsMargins(8,8,8,8); v.setSpacing(6)

        self.img = QtWidgets.QLabel(alignment=QtCore.Qt.AlignmentFlag.AlignCenter)
        pm = QtGui.QPixmap(); pm.loadFromData(png_bytes)
        pm = pm.scaled(280, 170, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                       QtCore.Qt.TransformationMode.SmoothTransformation)
        self.img.setPixmap(pm)
        v.addWidget(self.img)

        self.timerLabel = QtWidgets.QLabel("Auto-save in 10s",
                                           alignment=QtCore.Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.timerLabel)

        row = QtWidgets.QHBoxLayout()
        self.acceptBtn = QtWidgets.QPushButton("Accept (10)")
        self.acceptBtn.setObjectName("accept")
        self.rejectBtn = QtWidgets.QPushButton("Reject")
        self.rejectBtn.setObjectName("reject")
        row.addWidget(self.acceptBtn); row.addWidget(self.rejectBtn)
        v.addLayout(row)

        root = QtWidgets.QVBoxLayout(self); root.addWidget(card)

        # Position: top-right
        self.adjustSize()
        scr = QtWidgets.QApplication.primaryScreen().availableGeometry()
        self.move(scr.right() - self.width() - 16, scr.top() + 16)

        self.acceptBtn.clicked.connect(self._accept_manual)
        self.rejectBtn.clicked.connect(self._reject)

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

    def _tick(self):
        self._remaining -= 1
        if self._remaining <= 0:
            self._timer.stop()
            self._auto_next_accept = True
            self._accept_internal()
            return
        self.acceptBtn.setText(f"Accept ({self._remaining})")
        self.timerLabel.setText(f"Auto-save in {self._remaining}s")

    def _accept_manual(self):
        self._auto_next_accept = False
        self._accept_internal()

    def _accept_internal(self):
        if state.session_id:
            bridge.upload_now.emit(self._png, state.session_id, self._auto_next_accept)
            if sound:
                sound.play("ss_auto_accept" if self._auto_next_accept else "ss_accept")
        self.close()

    def _reject(self):
        bridge.rejected.emit()
        if sound: sound.play("ss_reject")
        self.close()

    def sizeHint(self):
        return QtCore.QSize(300, 240)

# ---------------- iOS-style Toggle ----------------
class ToggleButton(QtWidgets.QAbstractButton):
    toggledAnimated = QtCore.pyqtSignal(bool)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self._offset = 0.0
        self._anim = QtCore.QPropertyAnimation(self, b"offset", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QtCore.QEasingCurve.Type.InOutCubic)
        self.setFixedSize(64, 32)
        self.toggled.connect(self._on_toggled)
    def _on_toggled(self, checked: bool):
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()
        self.toggledAnimated.emit(checked)
    def getOffset(self) -> float: return self._offset
    def setOffset(self, v: float): self._offset = float(v); self.update()
    offset = QtCore.pyqtProperty(float, fget=getOffset, fset=setOffset)
    def paintEvent(self, e):
        p = QtGui.QPainter(self); p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        r = self.rect()
        track = QtGui.QColor("#2b6e3d") if self.isChecked() else QtGui.QColor("#2c3137")
        p.setPen(QtCore.Qt.PenStyle.NoPen); p.setBrush(track)
        p.drawRoundedRect(r.adjusted(1,1,-1,-1), 16, 16)
        knob_d = r.height() - 6
        x = 3 + (r.width() - knob_d - 6) * self._offset
        p.setBrush(QtGui.QColor("#f2f2f2"))
        p.drawEllipse(QtCore.QRectF(x, 3, knob_d, knob_d))

# ---------------- Heartbeat worker ----------------
def heartbeat_worker(interval_sec=15):
    while not state.stop_flag:
        if state.session_id:
            now = time.time()
            is_idle = (now - state.last_activity) > state.idle_threshold_sec

            # heartbeat
            try:
                api_post("/heartbeats", data={
                    "session_id": str(state.session_id),
                    "key_count": str(state.key_count),
                    "mouse_count": str(state.mouse_count),
                    "is_idle": "true" if is_idle else "false",
                })
            except Exception as e:
                print("heartbeat failed:", e)

            state.key_count = 0
            state.mouse_count = 0

            # scheduled screenshot (play shutter right when captured)
            if (state.capture_screens and not is_idle and not state.preview_pending
                and (now - state.last_screenshot_ts) >= state.screenshot_interval_sec):
                try:
                    png = take_screenshot_bytes()
                    if sound: sound.play("ss_shutter")
                    state.preview_pending = True
                    bridge.show_preview.emit(png)
                except Exception as e:
                    print("screenshot failed:", e)

        time.sleep(interval_sec)

# ---------------- UI ----------------
class LoginDialog(QtWidgets.QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Login")
        f = QtWidgets.QFormLayout(self)
        self.email = QtWidgets.QLineEdit()
        self.pw = QtWidgets.QLineEdit(); self.pw.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.btnLogin = QtWidgets.QPushButton("Login")
        self.btnRegister = QtWidgets.QPushButton("Register")
        row = QtWidgets.QHBoxLayout(); row.addWidget(self.btnLogin); row.addWidget(self.btnRegister)
        f.addRow("Email", self.email); f.addRow("Password", self.pw); f.addRow(row)
        self.btnLogin.clicked.connect(self.do_login); self.btnRegister.clicked.connect(self.do_register)

    def do_register(self):
        try:
            api_post("/auth/register", json={"email": self.email.text(), "password": self.pw.text(), "full_name": ""})
            QtWidgets.QMessageBox.information(self, "Success", "Registered. Now login.")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Register failed", str(e))

    def do_login(self):
        try:
            res = api_post("/auth/login", json={"email": self.email.text(), "password": self.pw.text()})
            state.token = res["access_token"]; state.user = res["user"]
            if sound: sound.play("login")
            self.accept()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Login failed", str(e))

class MainWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Time Tracker")
        self.resize(580, 280)

        self.taskBox = QtWidgets.QComboBox()
        self.taskInput = QtWidgets.QLineEdit()
        self.btnAddTask = QtWidgets.QPushButton("Add Task")
        self.btnRefresh = QtWidgets.QPushButton("Refresh Tasks")

        self.toggleSession = ToggleButton()
        self.toggleLabel = QtWidgets.QLabel("Tracker")
        self.chkScreens = QtWidgets.QCheckBox("Screenshots every 10 min"); self.chkScreens.setChecked(True)

        self.timeLabel = QtWidgets.QLabel("00:00:00"); self.timeLabel.setStyleSheet("font-size:18px; font-weight:700;")
        self.status = QtWidgets.QLabel("Idle")

        form = QtWidgets.QFormLayout(); form.addRow("Task", self.taskBox)
        rowNew = QtWidgets.QHBoxLayout(); rowNew.addWidget(self.taskInput); rowNew.addWidget(self.btnAddTask)
        ctrlRow = QtWidgets.QHBoxLayout()
        ctrlRow.addWidget(self.toggleLabel); ctrlRow.addWidget(self.toggleSession); ctrlRow.addSpacing(12)
        ctrlRow.addWidget(self.chkScreens); ctrlRow.addStretch(1)

        v = QtWidgets.QVBoxLayout(self)
        v.addLayout(form); v.addLayout(rowNew); v.addLayout(ctrlRow)
        v.addWidget(self.btnRefresh); v.addWidget(self.timeLabel); v.addWidget(self.status)

        self.btnRefresh.clicked.connect(self.load_tasks)
        self.btnAddTask.clicked.connect(self.add_task)
        self.toggleSession.toggled.connect(self.on_toggle_session)
        self.chkScreens.stateChanged.connect(self.on_toggle_screens)

        self.ui_timer = QtCore.QTimer(self); self.ui_timer.timeout.connect(self.update_time); self.ui_timer.start(1000)
        self.idle_timer = QtCore.QTimer(self); self.idle_timer.timeout.connect(self.check_idle_resume); self.idle_timer.start(1000)

        bridge.show_preview.connect(self.on_show_preview)
        bridge.upload_now.connect(self.on_upload_now)
        bridge.rejected.connect(self.on_rejected)
        bridge.session_started.connect(self.on_session_started)

        self.load_tasks()

    # ---- slots ----
    def on_toggle_screens(self):
        state.capture_screens = self.chkScreens.isChecked()

    def on_toggle_session(self, checked: bool):
        state.tracking_enabled = checked
        if checked:
            # manual ON
            started = self.try_start_session()
            if started and sound: sound.play("toggle_on")
        else:
            # manual OFF
            self.force_stop_session()
            if sound: sound.play("toggle_off")

    def try_start_session(self) -> bool:
        if state.session_id is not None:
            return True
        tid = self.taskBox.currentData()
        if not tid:
            QtWidgets.QMessageBox.warning(self, "Select Task", "Please select or create a task first.")
            self.toggleSession.setChecked(False); state.tracking_enabled = False
            return False
        try:
            res = api_post("/sessions/start", json={"task_id": tid})
            state.session_id = res["session_id"]
            state.session_started_at = datetime.now(timezone.utc)
            state.session_started_ts = time.time()
            self.status.setText(f"Running… (session {state.session_id})")
            bridge.session_started.emit(True)   # manual
            return True
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Start failed", str(e))
            self.toggleSession.setChecked(False); state.tracking_enabled = False
            return False

    def force_stop_session(self):
        if state.session_id is None:
            state.session_started_at = None
            state.session_started_ts = None
            self.status.setText("Idle")
            return
        if state.session_started_ts is not None:
            state.accumulated_secs += time.time() - state.session_started_ts
        try:
            api_post("/sessions/stop")
        except requests.HTTPError as e:
            resp = getattr(e, "response", None)
            if not (resp is not None and resp.status_code == 400):
                QtWidgets.QMessageBox.critical(self, "Stop failed", str(e))
                self.toggleSession.setChecked(True); state.tracking_enabled = True
                return
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Stop failed", str(e))
            self.toggleSession.setChecked(True); state.tracking_enabled = True
            return

        state.session_id = None
        state.session_started_at = None
        state.session_started_ts = None
        self.status.setText("Idle")

    def check_idle_resume(self):
        now = time.time()
        idle = (now - state.last_activity) > state.idle_threshold_sec

        # Auto-pause
        if idle and state.session_id is not None:
            if state.session_started_ts is not None:
                state.accumulated_secs += now - state.session_started_ts
            try:
                api_post("/sessions/stop")
            except requests.HTTPError as e:
                resp = getattr(e, "response", None)
                if not (resp is not None and resp.status_code == 400):
                    print("auto-pause stop error:", e)
            except Exception as e:
                print("auto-pause stop error:", e)
            state.session_id = None
            state.session_started_at = None
            state.session_started_ts = None
            self.status.setText("Paused (idle)")

        # Auto-resume (user back)
        if (not idle) and state.tracking_enabled and state.session_id is None:
            ok = self.try_start_session()
            if ok and sound: sound.play("auto_resume")

    def update_time(self):
        total = state.accumulated_secs
        if state.session_started_ts is not None:
            total += (time.time() - state.session_started_ts)
        t = int(total)
        h, m, s = t // 3600, (t % 3600) // 60, t % 60
        self.timeLabel.setText(f"{h:02d}:{m:02d}:{s:02d}")

    def load_tasks(self):
        try:
            items = api_get("/tasks"); self.taskBox.clear()
            for t in items: self.taskBox.addItem(t["name"], t["id"])
            self.status.setText("Tasks loaded." if self.taskBox.count() else "No tasks yet. Create one.")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Error", f"Failed to load tasks: {e}")

    def add_task(self):
        name = self.taskInput.text().strip()
        if not name:
            QtWidgets.QMessageBox.warning(self, "Missing", "Task name required"); return
        try:
            api_post("/tasks", json={"name": name}); self.taskInput.clear(); self.load_tasks()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Add task failed", str(e))

    # ---- screenshot preview handlers ----
    def on_show_preview(self, png_bytes: bytes):
        self.popup = PreviewPopup(png_bytes, countdown_sec=10)
        self.popup.show()

    def on_upload_now(self, png_bytes: bytes, session_id: int, auto_accept: bool):
        try:
            files = {"screenshot": (f"ss_{int(time.time())}.png", png_bytes, "image/png")}
            data = {"session_id": str(session_id), "key_count": "0", "mouse_count": "0",
                    "is_idle": "false", "note": "accepted_by_user" if not auto_accept else "auto_accepted"}
            api_post("/heartbeats", data=data, files=files)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Upload failed", str(e))
        finally:
            state.last_screenshot_ts = time.time()
            state.preview_pending = False

    def on_rejected(self):
        state.last_screenshot_ts = time.time()
        state.preview_pending = False

    def on_session_started(self, manual: bool):
        # not used further, but kept for extensibility
        pass

# ---------------- Boot ----------------
def main():
    # global listeners
    kl = keyboard.Listener(on_press=on_key)
    ml = mouse.Listener(on_move=on_move, on_click=lambda *a: on_move(), on_scroll=lambda *a: on_move())
    kl.start(); ml.start()

    # worker thread
    threading.Thread(target=heartbeat_worker, daemon=True).start()

    app = QtWidgets.QApplication(sys.argv)
    global sound
    sound = SoundManager(app)

    app.setWindowIcon(QtGui.QIcon("app.ico"))  # optional window icon
    dlg = LoginDialog()
    if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
        win = MainWindow(); win.show()
        sys.exit(app.exec())

if __name__ == "__main__":
    main()
