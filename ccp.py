"""
AAOS Rotary Control Panel

A compact always-on-top Tkinter dashboard that sends ADB commands to simulate
an Android Automotive OS rotary controller.

Features:
- Nudge left, right, up, down
- Rotate counterclockwise / clockwise
- Center button
- Home and Back buttons
- Device screenshot capture to the computer Downloads folder using adb pull
- Custom command runner loaded from comands.txt / commands.txt
- Automatic physical touchscreen recording through adb getevent
- Named gesture profiles and timing-aware adb input replay
- Optional Always on top mode
- Vertically resizable window
- Windows console hiding support

Requirements:
- Python 3 with tkinter
- adb available in PATH, or set the full adb path in the UI
- An Android Automotive OS emulator/device connected and authorized
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import re
import shlex
import struct
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

APP_DIR = Path(__file__).resolve().parent
CUSTOM_COMMAND_PRIMARY_FILE = "comands.txt"  # Kept as requested by the user.
CUSTOM_COMMAND_FALLBACK_FILE = "commands.txt"
GESTURE_PROFILE_FILE = APP_DIR / "gesture_profiles.json"
GESTURE_PROFILE_VERSION = 1
MAX_PROFILE_GESTURES = 5000
MAX_GESTURE_DURATION_MS = 10_000

GESTURE_LABELS = {
    "tap": "Tap",
    "double_tap": "Double tap",
    "long_press": "Long click",
    "swipe": "Swipe",
    "zoom_in": "Zoom in",
    "zoom_out": "Zoom out",
}

# AAOS car_service commands and Android key events.
COMMANDS = {
    "rotate_left": ["shell", "cmd", "car_service", "inject-rotary"],
    "rotate_right": ["shell", "cmd", "car_service", "inject-rotary", "-c", "true"],
    "tilt_up": ["shell", "cmd", "car_service", "inject-key", "280"],
    "tilt_down": ["shell", "cmd", "car_service", "inject-key", "281"],
    "tilt_left": ["shell", "cmd", "car_service", "inject-key", "282"],
    "tilt_right": ["shell", "cmd", "car_service", "inject-key", "283"],
    "enter": ["shell", "cmd", "car_service", "inject-key", "23"],
    "home": ["shell", "input", "keyevent", "3"],
    "back": ["shell", "input", "keyevent", "4"],
}

ICONS = {
    "rotate_left": "left_rotation.png",
    "rotate_right": "right_rotation.png",
    "tilt_up": "up_arrow.png",
    "tilt_down": "down_arrow.png",
    "tilt_left": "left_arrow.png",
    "tilt_right": "right_arrow.png",
    "enter": "enter.png",
    "home": "home_button.png",
    "back": "back_button.png",
    "screenshot": "screen_shot.png",
}

ACTION_NAMES = {
    "rotate_left": "Rotate left",
    "rotate_right": "Rotate right",
    "tilt_up": "Tilt up",
    "tilt_down": "Tilt down",
    "tilt_left": "Tilt left",
    "tilt_right": "Tilt right",
    "enter": "Enter",
    "home": "Home",
    "back": "Back",
    "screenshot": "Screenshot",
}

TOOLTIPS = {
    "rotate_left": "Rotate counterclockwise",
    "rotate_right": "Rotate clockwise",
    "tilt_up": "Nudge up",
    "tilt_down": "Nudge down",
    "tilt_left": "Nudge left",
    "tilt_right": "Nudge right",
    "enter": "Center button",
    "home": "Home button",
    "back": "Back button",
    "screenshot": "Capture device screenshot to Downloads",
}


SAMPLE_COMMANDS_TEXT = """adb shell dumpsys window
adb shell dumpsys meminfo
adb devices
"""


class GestureProfileStore:
    """Small, validated and atomically-written JSON store for gesture profiles."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.profiles: dict[str, dict[str, object]] = {}

    @staticmethod
    def validate_name(name: str) -> str:
        clean = " ".join(name.strip().split())
        if not clean:
            raise ValueError("Profile name cannot be empty.")
        if len(clean) > 80:
            raise ValueError("Profile name must contain at most 80 characters.")
        if any(ord(char) < 32 for char in clean):
            raise ValueError("Profile name contains unsupported control characters.")
        return clean

    @staticmethod
    def _validate_int(value: object, minimum: int, maximum: int, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"Invalid {field} value.")
        return value

    @classmethod
    def validate_profile(cls, profile: object) -> dict[str, object]:
        if not isinstance(profile, dict):
            raise ValueError("Profile must be a JSON object.")
        name = cls.validate_name(str(profile.get("name", "")))
        width = cls._validate_int(profile.get("screen_width"), 1, 32_768, "screen width")
        height = cls._validate_int(profile.get("screen_height"), 1, 32_768, "screen height")
        gestures = profile.get("gestures")
        if not isinstance(gestures, list) or len(gestures) > MAX_PROFILE_GESTURES:
            raise ValueError("Invalid gesture list.")

        clean_gestures: list[dict[str, object]] = []
        last_offset = -1
        coordinate_fields = ("x", "y", "x1", "y1", "x2", "y2", "p1x1", "p1y1", "p1x2", "p1y2", "p2x1", "p2y1", "p2x2", "p2y2")
        required_coordinates = {
            "tap": ("x", "y"),
            "double_tap": ("x", "y"),
            "long_press": ("x", "y"),
            "swipe": ("x1", "y1", "x2", "y2"),
            "zoom_in": ("p1x1", "p1y1", "p1x2", "p1y2", "p2x1", "p2y1", "p2x2", "p2y2"),
            "zoom_out": ("p1x1", "p1y1", "p1x2", "p1y2", "p2x1", "p2y1", "p2x2", "p2y2"),
        }
        for raw in gestures:
            if not isinstance(raw, dict) or raw.get("type") not in GESTURE_LABELS:
                raise ValueError("Profile contains an unsupported gesture.")
            gesture = dict(raw)
            kind = str(gesture["type"])
            if any(field not in gesture for field in required_coordinates[kind]):
                raise ValueError(f"{GESTURE_LABELS[kind]} is missing required coordinates.")
            if kind == "double_tap" and "gap_ms" not in gesture:
                raise ValueError("Double tap is missing its interval.")
            offset = cls._validate_int(gesture.get("offset_ms"), 0, 86_400_000, "gesture offset")
            if offset < last_offset:
                raise ValueError("Gesture offsets must be ordered.")
            last_offset = offset
            duration = cls._validate_int(gesture.get("duration_ms", 1), 1, MAX_GESTURE_DURATION_MS, "gesture duration")
            gesture["duration_ms"] = duration
            if "gap_ms" in gesture:
                gesture["gap_ms"] = cls._validate_int(gesture["gap_ms"], 20, 1000, "double-tap interval")
            for field in coordinate_fields:
                if field in gesture:
                    maximum = width - 1 if "x" in field else height - 1
                    gesture[field] = cls._validate_int(gesture[field], 0, maximum, field)
            clean_gestures.append(gesture)

        return {
            "name": name,
            "screen_width": width,
            "screen_height": height,
            "created_at": str(profile.get("created_at", datetime.now().isoformat(timespec="seconds"))),
            "updated_at": str(profile.get("updated_at", datetime.now().isoformat(timespec="seconds"))),
            "gestures": clean_gestures,
        }

    def load(self) -> list[str]:
        self.profiles = {}
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != GESTURE_PROFILE_VERSION:
                raise ValueError("Unsupported gesture profile file version.")
            raw_profiles = payload.get("profiles", [])
            if not isinstance(raw_profiles, list):
                raise ValueError("Invalid profile file.")
            errors: list[str] = []
            for raw in raw_profiles:
                try:
                    profile = self.validate_profile(raw)
                    self.profiles[str(profile["name"])] = profile
                except ValueError as error:
                    errors.append(str(error))
            return errors
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return [f"Could not load gesture profiles: {error}"]

    def save(self) -> None:
        payload = {
            "version": GESTURE_PROFILE_VERSION,
            "profiles": list(self.profiles.values()),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", newline="\n", delete=False, dir=self.path.parent, suffix=".tmp"
            ) as temp_file:
                json.dump(payload, temp_file, ensure_ascii=False, indent=2)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path = Path(temp_file.name)
            os.replace(temp_path, self.path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)


class TouchEventRecorder:
    """Convert a live getevent stream into device-pixel gesture records."""

    def __init__(
        self,
        screen_size: tuple[int, int],
        x_range: tuple[int, int],
        y_range: tuple[int, int],
        rotation: int,
        started_at: float,
    ) -> None:
        self.screen_size = screen_size
        self.x_range = x_range
        self.y_range = y_range
        self.rotation = rotation % 4
        self.started_at = started_at
        self.current_slot = 0
        self.slots: dict[int, dict[str, object]] = {}
        self.session: dict[str, object] | None = None
        self.gestures: list[dict[str, object]] = []
        self.last_event_at = started_at

    @staticmethod
    def _event_parts(line: str) -> tuple[str, str, str] | None:
        if "]" in line:
            line = line.split("]", 1)[1].strip()
        parts = line.split()
        if parts and parts[0].endswith(":"):
            parts = parts[1:]
        for index, token in enumerate(parts):
            if token in {"EV_ABS", "EV_KEY", "EV_SYN"} and len(parts) >= index + 3:
                return token, parts[index + 1], parts[index + 2]
        return None

    @staticmethod
    def _event_value(value: str) -> int:
        if value.upper() in {"DOWN", "PRESS"}:
            return 1
        if value.upper() in {"UP", "RELEASE"}:
            return 0
        parsed = int(value, 16)
        return parsed - (1 << 32) if parsed & (1 << 31) else parsed

    def feed(self, line: str, received_at: float) -> None:
        event = self._event_parts(line)
        if event is None:
            return
        event_type, code, raw_value = event
        try:
            value = self._event_value(raw_value)
        except ValueError:
            return
        self.last_event_at = received_at

        if event_type == "EV_ABS":
            if code == "ABS_MT_SLOT":
                self.current_slot = max(0, value)
                return
            slot = self.slots.setdefault(self.current_slot, {"active": False})
            if code == "ABS_MT_TRACKING_ID":
                if value < 0:
                    slot["active"] = False
                else:
                    slot.clear()
                    slot.update({"active": True, "tracking_id": value, "down_at": received_at})
                return
            if code in {"ABS_MT_POSITION_X", "ABS_X"}:
                slot["raw_x"] = value
            elif code in {"ABS_MT_POSITION_Y", "ABS_Y"}:
                slot["raw_y"] = value
        elif event_type == "EV_KEY" and code == "BTN_TOUCH":
            slot = self.slots.setdefault(0, {"active": False})
            if value:
                slot["active"] = True
                slot["down_at"] = received_at
            else:
                slot["active"] = False
        elif event_type == "EV_SYN" and code in {"SYN_REPORT", "0000"}:
            self._commit_frame(received_at)

    def _raw_to_pixel(self, raw_x: int, raw_y: int) -> tuple[int, int]:
        min_x, max_x = self.x_range
        min_y, max_y = self.y_range
        nx = min(1.0, max(0.0, (raw_x - min_x) / max(1, max_x - min_x)))
        ny = min(1.0, max(0.0, (raw_y - min_y) / max(1, max_y - min_y)))
        width, height = self.screen_size
        if self.rotation == 1:
            px, py = ny, 1.0 - nx
        elif self.rotation == 2:
            px, py = 1.0 - nx, 1.0 - ny
        elif self.rotation == 3:
            px, py = 1.0 - ny, nx
        else:
            px, py = nx, ny
        return round(px * (width - 1)), round(py * (height - 1))

    def _commit_frame(self, timestamp: float) -> None:
        active: dict[int, tuple[int, int, float]] = {}
        for slot_number, slot in self.slots.items():
            if slot.get("active") and "raw_x" in slot and "raw_y" in slot:
                x, y = self._raw_to_pixel(int(slot["raw_x"]), int(slot["raw_y"]))
                active[slot_number] = (x, y, float(slot.get("down_at", timestamp)))

        if active and self.session is None:
            self.session = {"started_at": min(point[2] for point in active.values()), "pointers": {}}
        if self.session is not None:
            pointers = self.session["pointers"]
            assert isinstance(pointers, dict)
            for slot_number, (x, y, down_at) in active.items():
                pointer = pointers.setdefault(
                    slot_number,
                    {"start_x": x, "start_y": y, "start_at": down_at, "end_x": x, "end_y": y},
                )
                pointer["end_x"] = x
                pointer["end_y"] = y
            if not active:
                self._finish_session(timestamp)

    def _finish_session(self, ended_at: float) -> None:
        session = self.session
        self.session = None
        if session is None:
            return
        pointers = session["pointers"]
        if not isinstance(pointers, dict) or not pointers:
            return
        started_at = float(session["started_at"])
        duration_ms = min(MAX_GESTURE_DURATION_MS, max(1, round((ended_at - started_at) * 1000)))
        offset_ms = max(0, round((started_at - self.started_at) * 1000))
        ordered = sorted(pointers.values(), key=lambda pointer: float(pointer["start_at"]))
        if len(ordered) == 1:
            pointer = ordered[0]
            x1, y1 = int(pointer["start_x"]), int(pointer["start_y"])
            x2, y2 = int(pointer["end_x"]), int(pointer["end_y"])
            distance = math.hypot(x2 - x1, y2 - y1)
            threshold = max(12.0, math.hypot(*self.screen_size) * 0.01)
            if distance >= threshold:
                gesture = {
                    "type": "swipe", "offset_ms": offset_ms, "duration_ms": duration_ms,
                    "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                }
            else:
                kind = "long_press" if duration_ms >= 500 else "tap"
                gesture = {
                    "type": kind, "offset_ms": offset_ms, "duration_ms": duration_ms,
                    "x": round((x1 + x2) / 2), "y": round((y1 + y2) / 2),
                }
        else:
            first, second = ordered[:2]
            initial_distance = math.hypot(
                int(first["start_x"]) - int(second["start_x"]),
                int(first["start_y"]) - int(second["start_y"]),
            )
            final_distance = math.hypot(
                int(first["end_x"]) - int(second["end_x"]),
                int(first["end_y"]) - int(second["end_y"]),
            )
            gesture = {
                "type": "zoom_in" if final_distance >= initial_distance else "zoom_out",
                "offset_ms": offset_ms,
                "duration_ms": duration_ms,
                "p1x1": int(first["start_x"]), "p1y1": int(first["start_y"]),
                "p1x2": int(first["end_x"]), "p1y2": int(first["end_y"]),
                "p2x1": int(second["start_x"]), "p2y1": int(second["start_y"]),
                "p2x2": int(second["end_x"]), "p2y2": int(second["end_y"]),
            }
        self.gestures.append(gesture)

    def finish(self, timestamp: float | None = None) -> list[dict[str, object]]:
        if self.session is not None:
            self._finish_session(timestamp or self.last_event_at)
        merged: list[dict[str, object]] = []
        index = 0
        while index < len(self.gestures):
            first = self.gestures[index]
            if index + 1 < len(self.gestures) and first["type"] == "tap":
                second = self.gestures[index + 1]
                gap = int(second["offset_ms"]) - int(first["offset_ms"])
                distance = math.hypot(int(second.get("x", -9999)) - int(first["x"]), int(second.get("y", -9999)) - int(first["y"]))
                if second["type"] == "tap" and 20 <= gap <= 500 and distance <= max(30, min(self.screen_size) * 0.04):
                    merged.append({
                        "type": "double_tap", "offset_ms": int(first["offset_ms"]),
                        "duration_ms": max(int(first["duration_ms"]), int(second["duration_ms"])),
                        "gap_ms": gap, "x": round((int(first["x"]) + int(second["x"])) / 2),
                        "y": round((int(first["y"]) + int(second["y"])) / 2),
                    })
                    index += 2
                    continue
            merged.append(first)
            index += 1
        return merged


def hide_console_window() -> None:
    """Hide the console window on Windows when the script is launched with python.exe."""
    if not sys.platform.startswith("win"):
        return
    try:
        console_window = ctypes.windll.kernel32.GetConsoleWindow()
        if console_window:
            ctypes.windll.user32.ShowWindow(console_window, 0)
    except Exception:
        # The GUI should still open even if the console cannot be hidden.
        pass


def subprocess_no_window_options() -> dict[str, object]:
    """Return subprocess options that prevent child console windows on Windows."""
    if not sys.platform.startswith("win"):
        return {}

    options: dict[str, object] = {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    options["startupinfo"] = startupinfo

    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if create_no_window:
        options["creationflags"] = create_no_window

    return options


class RotaryControlPanel(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("AAOS Rotary Panel")
        self.geometry("520x680")
        self.minsize(520, 580)
        # Allow changing only the vertical size. Width stays compact.
        self.resizable(False, True)

        self.icons: dict[str, tk.PhotoImage] = {}
        self.adb_path_var = tk.StringVar(value="adb")
        self.serial_var = tk.StringVar(value="")
        self.repeat_var = tk.IntVar(value=1)
        self.delay_ms_var = tk.IntVar(value=80)
        self.always_on_top_var = tk.BooleanVar(value=True)
        self.last_command_var = tk.StringVar(value="Ready")
        self.custom_command_var = tk.StringVar(value="")
        self.custom_commands: list[str] = []
        self.command_file_path = self._resolve_command_file_path()
        self.gesture_store = GestureProfileStore(GESTURE_PROFILE_FILE)
        self.replay_stop_event = threading.Event()
        self.replay_process: subprocess.Popen[str] | None = None
        self.replay_process_lock = threading.Lock()
        self.touch_record_stop_event = threading.Event()
        self.touch_record_process: subprocess.Popen[str] | None = None
        self.touch_record_lock = threading.Lock()
        self.touch_recording = False
        self.touch_profile_var = tk.StringVar(value="")
        self.touch_profile_name_var = tk.StringVar(value=datetime.now().strftime("Record %Y-%m-%d %H-%M-%S"))
        self.touch_status_var = tk.StringVar(value="Ready to record touch events from the connected device.")

        profile_errors = self.gesture_store.load()
        self._configure_style()
        self._build_ui()
        self._load_custom_commands(show_log=False)
        for error in profile_errors:
            self._append_output(error + "\n")
        self._apply_window_mode()
        self.bind("<Escape>", lambda _: self._on_close())
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_style(self) -> None:
        self.configure(bg="#151515")
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("TFrame", background="#151515")
        style.configure("Panel.TFrame", background="#202020")
        style.configure("TLabel", background="#151515", foreground="#eeeeee", font=("Segoe UI", 8))
        style.configure("Title.TLabel", background="#151515", foreground="#ffffff", font=("Segoe UI", 13, "bold"))
        style.configure("Hint.TLabel", background="#151515", foreground="#bbbbbb", font=("Segoe UI", 8))
        style.configure("Panel.TLabel", background="#202020", foreground="#eeeeee", font=("Segoe UI", 8))
        style.configure("Panel.TCheckbutton", background="#202020", foreground="#eeeeee", font=("Segoe UI", 8))
        style.map(
            "Panel.TCheckbutton",
            background=[("active", "#202020")],
            foreground=[("active", "#ffffff")],
        )
        style.configure("TButton", font=("Segoe UI", 8, "bold"), padding=4)
        style.configure("Icon.TButton", padding=3)
        style.configure("TEntry", padding=3)
        style.configure("TSpinbox", padding=3)
        style.configure("TCombobox", padding=3)
        style.configure("Danger.TButton", foreground="#9b1c1c")
        style.configure("Record.TButton", foreground="#9b1c1c")

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        title = ttk.Label(root, text="AAOS Rotary Panel", style="Title.TLabel")
        title.pack(anchor="w")

        hint = ttk.Label(
            root,
            text="ADB simulator for AAOS rotary input.",
            style="Hint.TLabel",
        )
        hint.pack(anchor="w", pady=(1, 7))

        config = ttk.Frame(root, style="Panel.TFrame", padding=7)
        config.pack(fill="x", pady=(0, 8))

        ttk.Label(config, text="ADB", style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 5), pady=2)
        ttk.Entry(config, textvariable=self.adb_path_var, width=18).grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=2)

        ttk.Label(config, text="Serial", style="Panel.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 5), pady=2)
        ttk.Entry(config, textvariable=self.serial_var, width=18).grid(row=0, column=3, sticky="ew", padx=(0, 8), pady=2)

        ttk.Button(config, text="Check devices", command=self.check_devices).grid(row=0, column=4, sticky="ew", pady=2)

        ttk.Label(config, text="Repeat", style="Panel.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 5), pady=2)
        ttk.Spinbox(config, from_=1, to=100, textvariable=self.repeat_var, width=5).grid(row=1, column=1, sticky="w", pady=2)

        ttk.Label(config, text="Delay ms", style="Panel.TLabel").grid(row=1, column=2, sticky="w", padx=(0, 5), pady=2)
        ttk.Spinbox(config, from_=0, to=2000, increment=10, textvariable=self.delay_ms_var, width=5).grid(row=1, column=3, sticky="w", pady=2)

        ttk.Checkbutton(
            config,
            text="Always on top",
            variable=self.always_on_top_var,
            command=self._apply_window_mode,
            style="Panel.TCheckbutton",
        ).grid(row=1, column=4, sticky="w", pady=2)

        config.columnconfigure(1, weight=1)
        config.columnconfigure(3, weight=1)

        control = ttk.Frame(root, style="Panel.TFrame", padding=8)
        control.pack(anchor="center", pady=(0, 8))

        for c in range(5):
            control.columnconfigure(c, minsize=50)
        for r in range(3):
            control.rowconfigure(r, minsize=50)

        # Compact physical layout:
        #                 tilt up | screenshot
        # rotate left | tilt left | enter | tilt right | rotate right
        # back                  | tilt down | home
        self._add_icon_button(control, "tilt_up", 0, 2)
        self._add_icon_button(control, "screenshot", 0, 3)
        self._add_icon_button(control, "rotate_left", 1, 0)
        self._add_icon_button(control, "tilt_left", 1, 1)
        self._add_icon_button(control, "enter", 1, 2)
        self._add_icon_button(control, "tilt_right", 1, 3)
        self._add_icon_button(control, "rotate_right", 1, 4)
        self._add_icon_button(control, "back", 2, 1)
        self._add_icon_button(control, "tilt_down", 2, 2)
        self._add_icon_button(control, "home", 2, 3)

        custom = ttk.Frame(root, style="Panel.TFrame", padding=7)
        custom.pack(fill="x", pady=(0, 8))

        ttk.Label(custom, text="Command file", style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 5), pady=2)
        ttk.Label(
            custom,
            textvariable=tk.StringVar(value=self.command_file_path.name),
            style="Panel.TLabel",
        ).grid(row=0, column=1, sticky="w", pady=2)

        ttk.Button(custom, text="Reload", command=lambda: self._load_custom_commands(show_log=True)).grid(
            row=0, column=2, sticky="ew", padx=(8, 4), pady=2
        )
        ttk.Button(custom, text="Run", command=self.run_selected_custom_command).grid(
            row=0, column=3, sticky="ew", padx=(4, 0), pady=2
        )

        self.custom_command_combo = ttk.Combobox(
            custom,
            textvariable=self.custom_command_var,
            values=[],
            state="readonly",
            width=52,
        )
        self.custom_command_combo.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(3, 2))
        custom.columnconfigure(1, weight=1)

        gesture_panel = ttk.Frame(root, style="Panel.TFrame", padding=7)
        gesture_panel.pack(fill="x", pady=(0, 8))
        ttk.Label(gesture_panel, text="Touch recorder", style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 5))
        self.touch_profile_combo = ttk.Combobox(
            gesture_panel, textvariable=self.touch_profile_var, state="readonly", width=25
        )
        self.touch_profile_combo.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(0, 4))
        self.touch_profile_combo.bind("<<ComboboxSelected>>", self._touch_profile_selected)
        self.touch_replay_button = ttk.Button(gesture_panel, text="Replay", command=self.replay_saved_touch_profile)
        self.touch_replay_button.grid(row=0, column=3, padx=2)
        ttk.Button(gesture_panel, text="Delete", command=self.delete_saved_touch_profile, style="Danger.TButton").grid(
            row=0, column=4, padx=(2, 0)
        )

        ttk.Label(gesture_panel, text="Record name", style="Panel.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 5), pady=(5, 0))
        ttk.Entry(gesture_panel, textvariable=self.touch_profile_name_var).grid(
            row=1, column=1, sticky="ew", pady=(5, 0), padx=(0, 4)
        )
        ttk.Button(gesture_panel, text="Rename", command=self.rename_saved_touch_profile).grid(row=1, column=2, padx=2, pady=(5, 0))
        self.touch_record_button = ttk.Button(
            gesture_panel, text="● Record", command=self.start_touch_recording, style="Record.TButton"
        )
        self.touch_record_button.grid(row=1, column=3, padx=2, pady=(5, 0))
        self.touch_stop_button = ttk.Button(gesture_panel, text="■ Stop", command=self.stop_touch_recording, state="disabled")
        self.touch_stop_button.grid(row=1, column=4, padx=(2, 0), pady=(5, 0))
        ttk.Label(gesture_panel, textvariable=self.touch_status_var, style="Panel.TLabel", wraplength=455).grid(
            row=2, column=0, columnspan=5, sticky="w", pady=(6, 0)
        )
        gesture_panel.columnconfigure(1, weight=1)
        self._refresh_touch_profile_combo()

        command_box = ttk.Frame(root, style="Panel.TFrame", padding=7)
        command_box.pack(fill="both", expand=True)

        ttk.Label(command_box, text="Last command", style="Panel.TLabel").pack(anchor="w")
        ttk.Label(
            command_box,
            textvariable=self.last_command_var,
            background="#202020",
            foreground="#9be38f",
            font=("Consolas", 8),
        ).pack(anchor="w", pady=(1, 4))

        self.output = tk.Text(
            command_box,
            height=4,
            wrap="word",
            bg="#111111",
            fg="#eeeeee",
            insertbackground="#eeeeee",
            font=("Consolas", 8),
            relief="flat",
        )
        self.output.pack(fill="both", expand=True)

        bottom_bar = ttk.Frame(root, style="TFrame")
        bottom_bar.pack(fill="x", pady=(5, 0))
        ttk.Label(
            bottom_bar,
            text="Edit comands.txt, click Reload, choose a command, then Run.",
            style="Hint.TLabel",
        ).pack(side="left")
        ttk.Sizegrip(bottom_bar).pack(side="right", anchor="se")

        self._append_output("Ready. Connect an AAOS emulator/device, then click a button.\n")

    def _on_close(self) -> None:
        self.stop_touch_recording(silent=True)
        self.stop_gesture_replay(silent=True)
        self.destroy()

    def _refresh_touch_profile_combo(self, selected: str | None = None) -> None:
        names = sorted(self.gesture_store.profiles, key=str.casefold)
        self.touch_profile_combo.configure(values=names)
        desired = selected if selected in self.gesture_store.profiles else self.touch_profile_var.get()
        self.touch_profile_var.set(desired if desired in self.gesture_store.profiles else "")

    def _touch_profile_selected(self, _: tk.Event | None = None) -> None:
        name = self.touch_profile_var.get()
        profile = self.gesture_store.profiles.get(name)
        if profile is None:
            return
        self.touch_profile_name_var.set(name)
        count = len(profile["gestures"])  # type: ignore[arg-type]
        self.touch_status_var.set(
            f"Selected '{name}': {count} command(s), {profile['screen_width']}×{profile['screen_height']}."
        )

    def rename_saved_touch_profile(self) -> None:
        old_name = self.touch_profile_var.get()
        if old_name not in self.gesture_store.profiles:
            self.touch_status_var.set("Select a saved profile before renaming it.")
            return
        try:
            new_name = GestureProfileStore.validate_name(self.touch_profile_name_var.get())
            if new_name != old_name and new_name in self.gesture_store.profiles:
                raise ValueError(f"A profile named '{new_name}' already exists.")
            profile = dict(self.gesture_store.profiles.pop(old_name))
            profile["name"] = new_name
            profile["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self.gesture_store.profiles[new_name] = profile
            self.gesture_store.save()
        except (OSError, ValueError) as error:
            if "profile" in locals():
                self.gesture_store.profiles.pop(new_name, None)
                self.gesture_store.profiles[old_name] = profile
            self.touch_status_var.set(f"Rename failed: {error}")
            return
        self._refresh_touch_profile_combo(new_name)
        self.touch_profile_name_var.set(new_name)
        self.touch_status_var.set(f"Renamed '{old_name}' to '{new_name}'.")

    def delete_saved_touch_profile(self) -> None:
        name = self.touch_profile_var.get()
        if name not in self.gesture_store.profiles:
            self.touch_status_var.set("Select a saved profile before deleting it.")
            return
        if not messagebox.askyesno("Delete touch record?", f"Delete '{name}' permanently?", parent=self):
            return
        old_profile = self.gesture_store.profiles.pop(name)
        try:
            self.gesture_store.save()
        except OSError as error:
            self.gesture_store.profiles[name] = old_profile
            self.touch_status_var.set(f"Delete failed: {error}")
            return
        self._refresh_touch_profile_combo()
        self.touch_profile_name_var.set(datetime.now().strftime("Record %Y-%m-%d %H-%M-%S"))
        self.touch_status_var.set(f"Deleted '{name}'.")

    def start_touch_recording(self) -> None:
        if self.touch_recording or self.replay_process is not None:
            return
        try:
            name = GestureProfileStore.validate_name(self.touch_profile_name_var.get())
        except ValueError as error:
            self.touch_status_var.set(str(error))
            return
        if name in self.gesture_store.profiles and not messagebox.askyesno(
            "Replace touch record?", f"Recording will replace '{name}'. Continue?", parent=self
        ):
            return
        self.touch_recording = True
        self.touch_record_stop_event.clear()
        self.touch_record_button.configure(state="disabled")
        self.touch_replay_button.configure(state="disabled")
        self.touch_stop_button.configure(state="normal")
        self.touch_status_var.set("Preparing touch recorder: finding the touchscreen and current display…")
        adb_base = self._adb_base()
        threading.Thread(target=self._touch_record_worker, args=(name, adb_base), daemon=True).start()

    def stop_touch_recording(self, silent: bool = False) -> None:
        if not self.touch_recording:
            self.stop_gesture_replay(silent=silent)
            return
        self.touch_record_stop_event.set()
        with self.touch_record_lock:
            process = self.touch_record_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        if not silent:
            self.touch_status_var.set("Stopping and converting captured touch events to input commands…")

    @staticmethod
    def _parse_touch_device(capabilities: str) -> tuple[str, tuple[int, int], tuple[int, int]]:
        candidates: list[tuple[int, str, tuple[int, int], tuple[int, int]]] = []
        blocks = re.split(r"(?=add device\s+\d+:)", capabilities)
        for block in blocks:
            path_match = re.search(r"add device\s+\d+:\s*(/dev/input/event\d+)", block)
            if path_match is None:
                continue
            x_match = re.search(
                r"ABS_MT_POSITION_X\s*:\s*value\s+-?\d+,\s*min\s+(-?\d+),\s*max\s+(-?\d+)", block
            )
            y_match = re.search(
                r"ABS_MT_POSITION_Y\s*:\s*value\s+-?\d+,\s*min\s+(-?\d+),\s*max\s+(-?\d+)", block
            )
            multitouch = x_match is not None and y_match is not None
            if not multitouch:
                x_match = re.search(r"\bABS_X\s*:\s*value\s+-?\d+,\s*min\s+(-?\d+),\s*max\s+(-?\d+)", block)
                y_match = re.search(r"\bABS_Y\s*:\s*value\s+-?\d+,\s*min\s+(-?\d+),\s*max\s+(-?\d+)", block)
            if x_match is None or y_match is None:
                continue
            score = 10 if multitouch else 3
            lowered = block.lower()
            if "touch" in lowered:
                score += 4
            if "btn_touch" in lowered:
                score += 2
            candidates.append(
                (
                    score,
                    path_match.group(1),
                    (int(x_match.group(1)), int(x_match.group(2))),
                    (int(y_match.group(1)), int(y_match.group(2))),
                )
            )
        if not candidates:
            raise ValueError("No readable touchscreen input device was found via 'getevent -lp'.")
        _, path, x_range, y_range = max(candidates, key=lambda item: item[0])
        return path, x_range, y_range

    def _touch_record_worker(self, name: str, adb_base: list[str]) -> None:
        recorder: TouchEventRecorder | None = None
        error = ""
        try:
            capabilities_result = subprocess.run(
                adb_base + ["shell", "getevent", "-lp"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                **subprocess_no_window_options(),
            )
            if capabilities_result.returncode != 0:
                raise RuntimeError(
                    capabilities_result.stderr.strip() or capabilities_result.stdout.strip() or "getevent capability query failed."
                )
            device_path, x_range, y_range = self._parse_touch_device(capabilities_result.stdout)

            temp_path = Path(tempfile.gettempdir()) / f"ccp_record_screen_{os.getpid()}_{threading.get_ident()}.png"
            remote_path = f"/data/local/tmp/ccp_record_screen_{os.getpid()}_{threading.get_ident()}.png"
            ok, capture_message = self._capture_png_to_path(adb_base, remote_path, temp_path)
            screen_size = self._png_dimensions(temp_path) if ok else None
            temp_path.unlink(missing_ok=True)
            if not ok or screen_size is None:
                raise RuntimeError(capture_message)

            rotation_result = subprocess.run(
                adb_base + ["shell", "dumpsys", "input"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                **subprocess_no_window_options(),
            )
            rotation_values = re.findall(r"SurfaceOrientation:\s*([0-3])", rotation_result.stdout)
            if not rotation_values:
                rotation_values = re.findall(r"\bOrientation:\s*Rotation([0-3])", rotation_result.stdout)
            rotation = int(max(set(rotation_values), key=rotation_values.count)) if rotation_values else 0
            if self.touch_record_stop_event.is_set():
                raise RuntimeError("Recording cancelled before capture started.")

            process = subprocess.Popen(
                adb_base + ["shell", "getevent", "-lt", device_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
                **subprocess_no_window_options(),
            )
            with self.touch_record_lock:
                self.touch_record_process = process
            started_at = time.monotonic()
            recorder = TouchEventRecorder(screen_size, x_range, y_range, rotation, started_at)
            self.after(
                0,
                lambda: self.touch_status_var.set(
                    f"● RECORDING {device_path} — interact normally on the Android screen, then press Stop."
                ),
            )
            assert process.stdout is not None
            while not self.touch_record_stop_event.is_set():
                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None:
                        break
                    continue
                recorder.feed(line, time.monotonic())
                if len(recorder.gestures) >= MAX_PROFILE_GESTURES:
                    error = f"Recording stopped at the safety limit of {MAX_PROFILE_GESTURES} gestures."
                    self.touch_record_stop_event.set()
                    break
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
            if not self.touch_record_stop_event.is_set() and process.returncode not in {0, -15, 1}:
                error = f"getevent stopped unexpectedly (exit {process.returncode})."
        except FileNotFoundError:
            error = "adb not found. Set the full ADB path or add adb to PATH."
        except subprocess.TimeoutExpired:
            error = "ADB timed out while preparing the touch recorder."
        except (OSError, RuntimeError, ValueError) as caught:
            error = str(caught)
        finally:
            with self.touch_record_lock:
                self.touch_record_process = None

        gestures = recorder.finish(time.monotonic()) if recorder is not None else []
        screen_size = recorder.screen_size if recorder is not None else None
        self.after(0, lambda: self._finish_touch_recording(name, screen_size, gestures, error))

    @staticmethod
    def _gesture_input_text(gesture: dict[str, object]) -> str:
        kind = str(gesture["type"])
        duration = int(gesture.get("duration_ms", 1))
        if kind == "tap":
            return f"input touchscreen tap {gesture['x']} {gesture['y']}"
        if kind == "double_tap":
            return (
                f"input touchscreen tap {gesture['x']} {gesture['y']}; sleep {int(gesture['gap_ms']) / 1000:.3f}; "
                f"input touchscreen tap {gesture['x']} {gesture['y']}"
            )
        if kind == "long_press":
            return f"input touchscreen swipe {gesture['x']} {gesture['y']} {gesture['x']} {gesture['y']} {duration}"
        if kind == "swipe":
            return (
                f"input touchscreen swipe {gesture['x1']} {gesture['y1']} "
                f"{gesture['x2']} {gesture['y2']} {duration}"
            )
        first = (
            f"input touchscreen swipe {gesture['p1x1']} {gesture['p1y1']} "
            f"{gesture['p1x2']} {gesture['p1y2']} {duration}"
        )
        second = (
            f"input touchscreen swipe {gesture['p2x1']} {gesture['p2y1']} "
            f"{gesture['p2x2']} {gesture['p2y2']} {duration}"
        )
        return f"{first} & {second} & wait"

    def _finish_touch_recording(
        self,
        name: str,
        screen_size: tuple[int, int] | None,
        gestures: list[dict[str, object]],
        error: str,
    ) -> None:
        self.touch_recording = False
        self.touch_record_stop_event.set()
        self.touch_record_button.configure(state="normal")
        self.touch_replay_button.configure(state="normal")
        self.touch_stop_button.configure(state="disabled")
        if screen_size is None:
            self.touch_status_var.set(f"Record failed: {error or 'No screen information was captured.'}")
            return
        if not gestures:
            self.touch_status_var.set(f"No complete touch gesture was captured. {error}".strip())
            return
        now = datetime.now().isoformat(timespec="seconds")
        for gesture in gestures:
            gesture["command"] = self._gesture_input_text(gesture)
        raw_profile = {
            "name": name,
            "screen_width": screen_size[0],
            "screen_height": screen_size[1],
            "created_at": self.gesture_store.profiles.get(name, {}).get("created_at", now),
            "updated_at": now,
            "gestures": gestures,
        }
        try:
            profile = GestureProfileStore.validate_profile(raw_profile)
            self.gesture_store.profiles[name] = profile
            self.gesture_store.save()
        except (OSError, ValueError) as save_error:
            self.touch_status_var.set(f"Captured gestures but could not save the profile: {save_error}")
            return
        self._refresh_touch_profile_combo(name)
        self.touch_profile_name_var.set(name)
        suffix = f" Warning: {error}" if error else ""
        self.touch_status_var.set(f"Saved '{name}': {len(gestures)} input command(s).{suffix}")
        preview = gestures[:50]
        command_lines = "\n".join(
            f"  {int(gesture['offset_ms']) / 1000:8.3f}s  $ {gesture['command']}" for gesture in preview
        )
        if len(gestures) > len(preview):
            command_lines += f"\n  … {len(gestures) - len(preview)} more command(s) saved in {GESTURE_PROFILE_FILE.name}"
        self._append_output(
            f"Recorded touch profile '{name}' with {len(gestures)} command(s):\n{command_lines}\n"
        )

    def replay_saved_touch_profile(self) -> None:
        name = self.touch_profile_var.get()
        profile = self.gesture_store.profiles.get(name)
        if profile is None:
            self.touch_status_var.set("Select a saved touch profile to replay.")
            return
        gestures = [dict(item) for item in profile["gestures"]]  # type: ignore[index]
        if not gestures:
            self.touch_status_var.set("The selected profile contains no commands.")
            return
        expected_size = (int(profile["screen_width"]), int(profile["screen_height"]))
        self.replay_stop_event.clear()
        self.touch_record_button.configure(state="disabled")
        self.touch_replay_button.configure(state="disabled")
        self.touch_stop_button.configure(state="normal")
        self.touch_status_var.set("Safety check: verifying the connected device and screen size…")
        threading.Thread(
            target=self._touch_replay_worker,
            args=(name, expected_size, gestures, self._adb_base()),
            daemon=True,
        ).start()

    def _touch_replay_worker(
        self,
        name: str,
        expected_size: tuple[int, int],
        gestures: list[dict[str, object]],
        adb_base: list[str],
    ) -> None:
        temp_path = Path(tempfile.gettempdir()) / f"ccp_touch_replay_{os.getpid()}_{threading.get_ident()}.png"
        remote_path = f"/data/local/tmp/ccp_touch_replay_{os.getpid()}_{threading.get_ident()}.png"
        ok, message = self._capture_png_to_path(adb_base, remote_path, temp_path)
        actual_size = self._png_dimensions(temp_path) if ok else None
        temp_path.unlink(missing_ok=True)
        if not ok:
            self.after(0, lambda: self._finish_touch_replay(f"Replay cancelled: {message}"))
            return
        if actual_size != expected_size:
            actual_text = f"{actual_size[0]}×{actual_size[1]}" if actual_size else "unknown"
            self.after(
                0,
                lambda: self._finish_touch_replay(
                    f"Replay cancelled for safety: profile is {expected_size[0]}×{expected_size[1]}, screen is {actual_text}."
                ),
            )
            return
        started = time.monotonic()
        for index, gesture in enumerate(gestures, start=1):
            target = started + int(gesture["offset_ms"]) / 1000
            if self.replay_stop_event.wait(max(0.0, target - time.monotonic())):
                self.after(0, lambda: self._finish_touch_replay("Touch replay stopped."))
                return
            success, error = self._run_replay_process(
                self._gesture_adb_command(adb_base, gesture), int(gesture.get("duration_ms", 1)) + 12_000
            )
            if not success:
                message = "Touch replay stopped." if self.replay_stop_event.is_set() else f"Replay failed at command {index}: {error}"
                self.after(0, lambda text=message: self._finish_touch_replay(text))
                return
        elapsed = time.monotonic() - started
        self.after(0, lambda: self._finish_touch_replay(f"Completed '{name}': {len(gestures)} command(s) in {elapsed:.3f}s."))

    def _finish_touch_replay(self, message: str) -> None:
        self.replay_stop_event.set()
        self.touch_record_button.configure(state="normal")
        self.touch_replay_button.configure(state="normal")
        self.touch_stop_button.configure(state="disabled")
        self.touch_status_var.set(message)
        self._append_output(message + "\n")

    def _capture_png_to_path(
        self, adb_base: list[str], remote_path: str, local_path: Path
    ) -> tuple[bool, str]:
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            capture = self._run_process_for_screenshot(adb_base + ["shell", "screencap", "-p", remote_path])
            if capture.returncode != 0:
                return False, capture.stderr.strip() or capture.stdout.strip() or "Device screenshot failed."
            pull = self._run_process_for_screenshot(adb_base + ["pull", remote_path, str(local_path)])
            self._run_process_for_screenshot(adb_base + ["shell", "rm", "-f", remote_path], timeout=8)
            if pull.returncode != 0:
                return False, pull.stderr.strip() or pull.stdout.strip() or "Could not pull the device screenshot."
            if not self._is_valid_png(local_path):
                return False, "The device returned an invalid screenshot."
            return True, "OK"
        except FileNotFoundError:
            return False, "adb not found. Set the full ADB path in the main window."
        except subprocess.TimeoutExpired:
            return False, "Screenshot timeout. Check the device connection and authorization."
        except OSError as error:
            return False, f"Screenshot failed: {error}"

    @staticmethod
    def _png_dimensions(path: Path) -> tuple[int, int] | None:
        try:
            with path.open("rb") as file:
                header = file.read(24)
            if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
                return None
            return struct.unpack(">II", header[16:24])
        except (OSError, struct.error):
            return None

    def stop_gesture_replay(self, silent: bool = False) -> None:
        self.replay_stop_event.set()
        with self.replay_process_lock:
            process = self.replay_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        if not silent:
            self.touch_status_var.set("Stopping touch replay…")

    @staticmethod
    def _gesture_adb_command(adb_base: list[str], gesture: dict[str, object]) -> list[str]:
        kind = str(gesture["type"])
        duration = max(1, int(gesture.get("duration_ms", 1)))
        if kind == "tap":
            return adb_base + ["shell", "input", "touchscreen", "tap", str(gesture["x"]), str(gesture["y"])]
        if kind == "double_tap":
            gap_seconds = int(gesture["gap_ms"]) / 1000
            x, y = int(gesture["x"]), int(gesture["y"])
            script = f"input touchscreen tap {x} {y}; sleep {gap_seconds:.3f}; input touchscreen tap {x} {y}"
            return adb_base + ["shell", "sh", "-c", script]
        if kind == "long_press":
            x, y = str(gesture["x"]), str(gesture["y"])
            return adb_base + ["shell", "input", "touchscreen", "swipe", x, y, x, y, str(duration)]
        if kind == "swipe":
            return adb_base + [
                "shell", "input", "touchscreen", "swipe",
                str(gesture["x1"]), str(gesture["y1"]), str(gesture["x2"]), str(gesture["y2"]), str(duration),
            ]
        first = (
            f"input touchscreen swipe {int(gesture['p1x1'])} {int(gesture['p1y1'])} "
            f"{int(gesture['p1x2'])} {int(gesture['p1y2'])} {duration}"
        )
        second = (
            f"input touchscreen swipe {int(gesture['p2x1'])} {int(gesture['p2y1'])} "
            f"{int(gesture['p2x2'])} {int(gesture['p2y2'])} {duration}"
        )
        return adb_base + ["shell", "sh", "-c", f"{first} & {second} & wait"]

    def _run_replay_process(self, cmd: list[str], timeout_ms: int) -> tuple[bool, str]:
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **subprocess_no_window_options(),
            )
            with self.replay_process_lock:
                self.replay_process = process
            try:
                stdout, stderr = process.communicate(timeout=max(1.0, timeout_ms / 1000))
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                return False, "ADB gesture command timed out."
            if process.returncode != 0:
                return False, stderr.strip() or stdout.strip() or f"ADB exited with code {process.returncode}."
            return True, "OK"
        except FileNotFoundError:
            return False, "adb not found."
        except OSError as error:
            return False, str(error)
        finally:
            with self.replay_process_lock:
                self.replay_process = None

    def _apply_window_mode(self) -> None:
        is_topmost = bool(self.always_on_top_var.get())

        # Keep the panel floating above Android Studio / Emulator when enabled.
        self.attributes("-topmost", is_topmost)

        # Windows-only: make the panel look like a small floating tool window.
        if sys.platform.startswith("win"):
            try:
                self.attributes("-toolwindow", True)
            except tk.TclError:
                pass

        if is_topmost:
            self.lift()

    def _load_icon(self, key: str) -> tk.PhotoImage | None:
        icon_path = APP_DIR / ICONS[key]
        if not icon_path.exists():
            return None
        image = tk.PhotoImage(file=str(icon_path))
        max_dim = max(image.width(), image.height())
        factor = max(1, round(max_dim / 36))
        if factor > 1:
            image = image.subsample(factor, factor)
        self.icons[key] = image
        return image

    def _add_icon_button(self, parent: ttk.Frame, key: str, row: int, column: int) -> None:
        icon = self._load_icon(key)

        if key == "screenshot":
            command = self.take_screenshot
        else:
            command = lambda k=key: self.send_rotary_command(k)

        button = ttk.Button(
            parent,
            text="" if icon else ACTION_NAMES[key],
            image=icon,
            compound="center" if icon else "none",
            style="Icon.TButton",
            command=command,
        )
        button.grid(row=row, column=column, sticky="nsew", padx=3, pady=3, ipadx=0, ipady=0)
        button.configure(width=4)
        self._add_tooltip(button, TOOLTIPS[key])

    def _add_tooltip(self, widget: tk.Widget, text: str) -> None:
        tooltip: dict[str, tk.Toplevel | None] = {"window": None}

        def show(_: tk.Event) -> None:
            if tooltip["window"] is not None:
                return
            x = widget.winfo_rootx() + 10
            y = widget.winfo_rooty() + widget.winfo_height() + 3
            window = tk.Toplevel(widget)
            window.wm_overrideredirect(True)
            window.wm_geometry(f"+{x}+{y}")
            try:
                window.attributes("-topmost", True)
            except tk.TclError:
                pass
            label = tk.Label(window, text=text, bg="#303030", fg="#eeeeee", padx=6, pady=3, font=("Segoe UI", 8))
            label.pack()
            tooltip["window"] = window

        def hide(_: tk.Event) -> None:
            window = tooltip["window"]
            if window is not None:
                window.destroy()
                tooltip["window"] = None

        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)

    def _adb_base(self) -> list[str]:
        adb = self.adb_path_var.get().strip() or "adb"
        serial = self.serial_var.get().strip()
        base = [adb]
        if serial:
            base.extend(["-s", serial])
        return base

    def _resolve_command_file_path(self) -> Path:
        primary = APP_DIR / CUSTOM_COMMAND_PRIMARY_FILE
        fallback = APP_DIR / CUSTOM_COMMAND_FALLBACK_FILE
        if primary.exists():
            return primary
        if fallback.exists():
            return fallback
        return primary

    def _ensure_command_file(self) -> None:
        if self.command_file_path.exists():
            return
        self.command_file_path.write_text(SAMPLE_COMMANDS_TEXT, encoding="utf-8")

    def _load_custom_commands(self, show_log: bool) -> None:
        self.command_file_path = self._resolve_command_file_path()
        try:
            self._ensure_command_file()
            lines = self.command_file_path.read_text(encoding="utf-8").splitlines()
            commands = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
            self.custom_commands = commands
            self.custom_command_combo.configure(values=commands)
            if commands:
                current = self.custom_command_var.get().strip()
                if current not in commands:
                    self.custom_command_var.set(commands[0])
            else:
                self.custom_command_var.set("")
            if show_log:
                self._append_output(f"Loaded {len(commands)} custom command(s) from {self.command_file_path.name}.\n")
        except OSError as error:
            self.custom_commands = []
            self.custom_command_combo.configure(values=[])
            self.custom_command_var.set("")
            if show_log:
                self._append_output(f"Failed to read {self.command_file_path.name}: {error}\n")

    @staticmethod
    def _looks_like_adb_executable(token: str) -> bool:
        normalized = token.strip('"').replace("\\", "/")
        name = normalized.rsplit("/", 1)[-1].lower()
        return name in {"adb", "adb.exe"}

    def _build_custom_command(self, command_line: str) -> list[str]:
        try:
            tokens = shlex.split(command_line, comments=False, posix=True)
        except ValueError as error:
            raise ValueError(f"Invalid command syntax: {error}") from error

        if not tokens:
            raise ValueError("No command selected.")

        if self._looks_like_adb_executable(tokens[0]):
            adb = self.adb_path_var.get().strip() or tokens[0]
            rest = tokens[1:]
            if rest and rest[0] == "devices":
                # 'adb devices' should list every device, so it should not be narrowed by -s SERIAL.
                return [adb] + rest
            base = [adb]
            serial = self.serial_var.get().strip()
            if serial:
                base.extend(["-s", serial])
            return base + rest

        return tokens

    def run_selected_custom_command(self) -> None:
        command_line = self.custom_command_var.get().strip()
        if not command_line:
            self._append_output("No custom command selected. Edit comands.txt, then click Reload.\n")
            return

        try:
            cmd = self._build_custom_command(command_line)
        except ValueError as error:
            self._append_output(str(error) + "\n")
            return

        self._run_command_async(cmd, f"Custom: {command_line}", repeat=1, delay_ms=0, timeout=45)

    def check_devices(self) -> None:
        cmd = [self.adb_path_var.get().strip() or "adb", "devices"]
        self._run_command_async(cmd, "Check devices", repeat=1, delay_ms=0)

    def send_rotary_command(self, key: str) -> None:
        repeat = max(1, int(self.repeat_var.get()))
        delay_ms = max(0, int(self.delay_ms_var.get()))
        cmd = self._adb_base() + COMMANDS[key]
        self._run_command_async(cmd, ACTION_NAMES[key], repeat=repeat, delay_ms=delay_ms)

    def take_screenshot(self) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"aaos_screenshot_{timestamp}.png"
        downloads_dir = Path.home() / "Downloads"
        screenshot_path = downloads_dir / filename
        remote_path = f"/sdcard/Download/{filename}"

        self.last_command_var.set(f"screencap -> adb pull -> {screenshot_path}")
        self._append_output(
            "\n▶ Screenshot\n"
            f"$ {' '.join(self._adb_base() + ['shell', 'screencap', '-p', remote_path])}\n"
            f"$ {' '.join(self._adb_base() + ['pull', remote_path, str(screenshot_path)])}\n"
        )

        thread = threading.Thread(
            target=self._screenshot_worker,
            args=(remote_path, screenshot_path),
            daemon=True,
        )
        thread.start()

    def _run_process_for_screenshot(self, cmd: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            **subprocess_no_window_options(),
        )

    @staticmethod
    def _is_valid_png(path: Path) -> bool:
        try:
            if not path.exists() or path.stat().st_size < 8:
                return False
            with path.open("rb") as file:
                return file.read(8) == b"\x89PNG\r\n\x1a\n"
        except OSError:
            return False

    def _screenshot_worker(self, remote_path: str, screenshot_path: Path) -> None:
        try:
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            adb = self._adb_base()

            # More reliable than streaming PNG bytes through stdout on Windows.
            # First write a real PNG file on the device, then pull that file to Downloads.
            capture = self._run_process_for_screenshot(adb + ["shell", "screencap", "-p", remote_path])
            if capture.returncode != 0:
                error = capture.stderr.strip() or capture.stdout.strip()
                message = error or f"Screenshot failed. Exit code: {capture.returncode}"
                self.after(0, lambda: self._append_output(message + "\n"))
                return

            pull = self._run_process_for_screenshot(adb + ["pull", remote_path, str(screenshot_path)])

            # Clean up the temporary screenshot on the Android device.
            self._run_process_for_screenshot(adb + ["shell", "rm", "-f", remote_path], timeout=8)

            if pull.returncode != 0:
                error = pull.stderr.strip() or pull.stdout.strip()
                try:
                    screenshot_path.unlink(missing_ok=True)
                except OSError:
                    pass
                message = error or f"adb pull failed. Exit code: {pull.returncode}"
            elif self._is_valid_png(screenshot_path):
                message = f"Saved screenshot to: {screenshot_path}"
            else:
                try:
                    screenshot_path.unlink(missing_ok=True)
                except OSError:
                    pass
                message = "Screenshot failed. The pulled file was not a valid PNG."
        except FileNotFoundError:
            message = "adb not found. Set the full adb path or add adb to PATH."
        except subprocess.TimeoutExpired:
            message = "Screenshot timeout. Check the emulator/device and authorization state."
        except OSError as error:
            message = f"Screenshot failed: {error}"

        self.after(0, lambda: self._append_output(message + "\n"))

    def _run_command_async(self, cmd: list[str], label: str, repeat: int, delay_ms: int, timeout: int = 12) -> None:
        self.last_command_var.set(" ".join(cmd))
        self._append_output(f"\n▶ {label}\n$ {' '.join(cmd)}\n")
        thread = threading.Thread(target=self._run_command_worker, args=(cmd, repeat, delay_ms, timeout), daemon=True)
        thread.start()

    def _run_command_worker(self, cmd: list[str], repeat: int, delay_ms: int, timeout: int) -> None:
        outputs: list[str] = []
        for index in range(repeat):
            try:
                completed = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                    **subprocess_no_window_options(),
                )
                if completed.stdout.strip():
                    outputs.append(completed.stdout.strip())
                if completed.stderr.strip():
                    outputs.append(completed.stderr.strip())
                if completed.returncode != 0:
                    outputs.append(f"Exit code: {completed.returncode}")
                    break
            except FileNotFoundError:
                outputs.append("adb not found. Set the full adb path or add adb to PATH.")
                break
            except subprocess.TimeoutExpired:
                outputs.append("Command timeout. Check the emulator/device and authorization state.")
                break

            if index < repeat - 1 and delay_ms > 0:
                time.sleep(delay_ms / 1000)

        if not outputs:
            outputs.append("OK")
        self.after(0, lambda: self._append_output("\n".join(outputs) + "\n"))

    def _append_output(self, text: str) -> None:
        self.output.insert("end", text)
        self.output.see("end")


def main() -> None:
    hide_console_window()
    app = RotaryControlPanel()
    app.mainloop()


if __name__ == "__main__":
    main()
