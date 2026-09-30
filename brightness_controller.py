"""
Screen Brightness Controller for Windows
=========================================
A modern, dark-themed desktop application to smoothly control the brightness
of your monitor(s) in real time.

Features
--------
* Smooth horizontal slider (0% - 100%) with real-time brightness updates.
* Digital percentage label that follows the slider.
* Quick preset buttons: 25%, 50%, 75%, 100%.
* Brightness is applied on a dedicated worker thread (coalesced updates) so the
  UI stays fluid and never freezes, even on slow DDC/CI monitors.
* Graceful fallback to a gamma-ramp overlay if `screen_brightness_control`
  cannot reach the physical monitor (e.g. desktop monitors without DDC/CI).
* Single-file design, ready to be compiled with PyInstaller.

Build a standalone .exe
-----------------------
    pip install -r requirements.txt
    build.bat

Author: Velo Agent
License: MIT
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
from typing import List, Optional

import customtkinter as ctk

try:
    import screen_brightness_control as sbc
except Exception:  # pragma: no cover - only used if the wheel is missing
    sbc = None  # type: ignore


# --------------------------------------------------------------------------- #
#  Look & feel
# --------------------------------------------------------------------------- #
APP_TITLE = "Screen Brightness Controller"
APP_VERSION = "1.0.0"

BG_COLOR = "#121212"          # window background
CARD_COLOR = "#1E1E24"        # panel background
CARD_HOVER = "#26262E"        # button idle background
ACCENT = "#4A9EFF"            # slider / active accent
ACCENT_DARK = "#2E6BB8"       # accent hover
TEXT_PRIMARY = "#FFFFFF"
TEXT_SECONDARY = "#9AA4B2"    # muted label text
TRACK_COLOR = "#2A2A33"       # slider track background

PRESETS = (25, 50, 75, 100)

# Target UI refresh rate for the label while dragging the slider.
TARGET_FPS = 60
FRAME_INTERVAL = 1.0 / TARGET_FPS


# --------------------------------------------------------------------------- #
#  Low-level gamma ramp helper (software fallback)
# --------------------------------------------------------------------------- #
class _GammaRamp(ctypes.Structure):
    """Windows GAMMA_RAMP structure used by SetDeviceGammaRamp."""

    _fields_ = [
        ("red", ctypes.c_uint16 * 256),
        ("green", ctypes.c_uint16 * 256),
        ("blue", ctypes.c_uint16 * 256),
    ]


class GammaController:
    """
    Software brightness dimmer using the Win32 gamma ramp.

    This is used as a fallback when the physical monitor is not reachable
    through DDC/CI (very common on desktop monitors), and also to smooth out
    the transition between hardware brightness steps.
    """

    def __init__(self) -> None:
        self._gdi32 = ctypes.windll.gdi32
        self._user32 = ctypes.windll.user32
        self._dc = self._user32.GetDC(0)

    def apply(self, percent: float) -> None:
        """Apply a brightness level (0-100) to the gamma ramp."""
        percent = max(0.0, min(100.0, float(percent)))
        # Non-linear curve so low values stay usable, like a real backlight.
        factor = (percent / 100.0) ** 1.4
        ramp = _GammaRamp()
        for i in range(256):
            value = int(max(0, min(65535, round(i * 257 * factor))))
            ramp.red[i] = value
            ramp.green[i] = value
            ramp.blue[i] = value
        self._gdi32.SetDeviceGammaRamp(self._dc, ctypes.byref(ramp))

    def reset(self) -> None:
        """Restore the default (full brightness) gamma ramp."""
        self.apply(100.0)


# --------------------------------------------------------------------------- #
#  Hardware brightness access
# --------------------------------------------------------------------------- #
class BrightnessBackend:
    """
    Reads and writes monitor brightness using `screen_brightness_control`.

    All hardware writes are pushed onto a background thread so the UI thread is
    never blocked by a slow DDC/CI transaction.
    """

    def __init__(self) -> None:
        self._gamma = GammaController()
        self._queue: "queue.Queue[float]" = queue.Queue()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._last_applied = -1.0
        self._use_gamma_only = False
        self._lock = threading.Lock()

    # -- discovery --------------------------------------------------------- #
    def monitors(self) -> List[str]:
        """Return a list of monitor display names."""
        if sbc is None:
            return ["Display 1"]
        try:
            names = sbc.list_monitors()
            if names:
                return [str(n) for n in names]
        except Exception:
            pass
        return ["Display 1"]

    def get_brightness(self) -> float:
        """Read the current brightness level as a percentage (0-100)."""
        if sbc is None:
            return 100.0
        try:
            values = sbc.get_brightness()
            if isinstance(values, (list, tuple)):
                values = [float(v) for v in values if v is not None]
                return sum(values) / len(values) if values else 100.0
            return float(values)
        except Exception:
            return 100.0

    # -- worker thread ----------------------------------------------------- #
    def start(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._run, daemon=True, name="brightness-worker"
        )
        self._worker.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._queue.put(-1.0)  # sentinel to wake the worker up
        if self._worker is not None:
            self._worker.join(timeout=2.0)
        self._gamma.reset()

    def set_brightness(self, percent: float, smooth: bool = True) -> None:
        """
        Request a new brightness level.

        Requests are coalesced on a background thread: if the user drags the
        slider quickly, only the most recent value is actually written to the
        hardware, which keeps the UI perfectly responsive.
        """
        percent = max(0.0, min(100.0, float(percent)))
        self._queue.put(percent if smooth else -abs(percent))

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                value = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if self._stop_event.is_set():
                break

            # A negative value means "apply immediately, no interpolation".
            immediate = value < 0
            target = abs(value)

            if immediate:
                self._apply(target)
                continue

            # Smooth ramp towards the target so the transition never steps.
            start = self._last_applied if self._last_applied >= 0 else target
            steps = max(1, int(abs(target - start) / 2.0))
            aborted = False
            for i in range(1, steps + 1):
                if self._stop_event.is_set() or not self._queue.empty():
                    aborted = True
                    break
                self._apply(start + (target - start) * (i / steps))
                time.sleep(0.008)
            if not aborted:
                self._apply(target)

    def _apply(self, percent: float) -> None:
        """Write a value to every detected monitor (best effort) + gamma."""
        percent = max(0.0, min(100.0, float(percent)))
        with self._lock:
            self._last_applied = percent
        self._gamma.apply(percent)
        if self._use_gamma_only or sbc is None:
            return
        try:
            sbc.set_brightness(percent)
        except Exception:
            # Hardware path unavailable (no DDC/CI) - gamma overlay covers us.
            self._use_gamma_only = True


# --------------------------------------------------------------------------- #
#  Custom widgets
# --------------------------------------------------------------------------- #
class PresetButton(ctk.CTkButton):
    """Quick-select preset button with hover feedback."""

    def __init__(self, master, value: int, command, **kwargs):
        self.value = value
        super().__init__(
            master,
            text=f"{value}%",
            command=lambda: command(value),
            fg_color=CARD_HOVER,
            hover_color=ACCENT_DARK,
            border_color=ACCENT,
            border_width=1,
            **kwargs,
        )


# --------------------------------------------------------------------------- #
#  Main application
# --------------------------------------------------------------------------- #
class BrightnessApp(ctk.CTk):
    """Root application window."""

    def __init__(self) -> None:
        super().__init__()

        self.backend = BrightnessBackend()
        self.backend.start()

        self._last_frame = 0.0

        self._build_window()
        self._build_widgets()

        # Seed the UI with the current system brightness.
        try:
            current = self.backend.get_brightness()
        except Exception:
            current = 100.0
        self._sync_ui(float(current), apply=False)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- window setup ------------------------------------------------------ #
    def _build_window(self) -> None:
        self.title(APP_TITLE)
        self.geometry("460x340")
        self.minsize(420, 320)
        self.configure(fg_color=BG_COLOR)

        # Center the window on the primary monitor.
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = (self.winfo_screenwidth() // 2) - (width // 2)
        y = (self.winfo_screenheight() // 2) - (height // 2)
        self.geometry(f"+{x}+{y}")

    # -- widgets ------------------------------------------------------------ #
    def _build_widgets(self) -> None:
        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(22, 6))

        ctk.CTkLabel(
            header,
            text="Screen Brightness",
            font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=TEXT_PRIMARY,
        ).pack(anchor="w")

        ctk.CTkLabel(
            header,
            text=f"{APP_VERSION}  •  Real-time hardware control",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=TEXT_SECONDARY,
        ).pack(anchor="w", pady=(2, 0))

        # Main card
        card = ctk.CTkFrame(self, fg_color=CARD_COLOR, corner_radius=16)
        card.pack(fill="both", expand=True, padx=24, pady=(14, 20))

        # Percentage read-out
        self.value_label = ctk.CTkLabel(
            card,
            text="Brightness: 100%",
            font=ctk.CTkFont(family="Segoe UI", size=17, weight="bold"),
            text_color=TEXT_PRIMARY,
        )
        self.value_label.pack(anchor="w", padx=22, pady=(20, 4))

        self.status_label = ctk.CTkLabel(
            card,
            text="",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=TEXT_SECONDARY,
            anchor="w",
        )
        self.status_label.pack(anchor="w", padx=22, pady=(0, 10))

        # Slider
        self.slider = ctk.CTkSlider(
            card,
            command=self._on_slider,
            from_=0,
            to=100,
            number_of_steps=200,
            fg_color=TRACK_COLOR,
            progress_color=ACCENT,
            button_color=TEXT_PRIMARY,
            button_hover_color="#E8E8F0",
            width=380,
            height=26,
        )
        self.slider.pack(fill="x", padx=22, pady=(4, 8))

        # Min/max hints
        hints = ctk.CTkFrame(card, fg_color="transparent")
        hints.pack(fill="x", padx=22, pady=(0, 14))
        ctk.CTkLabel(
            hints,
            text="0%",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=TEXT_SECONDARY,
        ).pack(side="left")
        ctk.CTkLabel(
            hints,
            text="100%",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=TEXT_SECONDARY,
        ).pack(side="right")

        # Preset buttons
        presets = ctk.CTkFrame(card, fg_color="transparent")
        presets.pack(fill="x", padx=18, pady=(2, 18))
        for value in PRESETS:
            PresetButton(
                presets,
                value=value,
                command=self._apply_preset,
                font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
                height=38,
            ).pack(side="left", expand=True, fill="x", padx=5)

        # Footer
        ctk.CTkLabel(
            self,
            text="Tip: drag the slider or pick a preset — changes apply instantly.",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=TEXT_SECONDARY,
        ).pack(side="bottom", pady=(0, 12))

    # -- events ------------------------------------------------------------- #
    def _on_slider(self, value: float) -> None:
        """
        Called on every slider movement.

        The label is throttled to ~60 FPS and the hardware write is handed off
        to the background worker, so this method always returns immediately.
        """
        now = time.perf_counter()
        if now - self._last_frame < FRAME_INTERVAL:
            # Too soon for a new frame: still queue the hardware change, but
            # skip the label redraw to keep the animation perfectly smooth.
            self.backend.set_brightness(float(value))
            return
        self._last_frame = now
        self._sync_ui(float(value), apply=True)

    def _apply_preset(self, value: int) -> None:
        self._sync_ui(float(value), apply=True)

    def _sync_ui(self, percent: float, apply: bool = True) -> None:
        """Update every on-screen element to reflect `percent`."""
        percent = max(0.0, min(100.0, float(percent)))
        rounded = int(round(percent))

        self.value_label.configure(text=f"Brightness: {rounded}%")
        self.status_label.configure(
            text=self._status_text(rounded),
            text_color=ACCENT if rounded <= 20 else TEXT_SECONDARY,
        )

        # Keep the slider thumb in sync when a preset button is used.
        try:
            if abs(float(self.slider.get()) - percent) > 0.01:
                self.slider.set(percent)
        except Exception:
            pass

        if apply:
            self.backend.set_brightness(percent)

    @staticmethod
    def _status_text(percent: int) -> str:
        if percent == 0:
            return "Display off (minimum backlight)"
        if percent <= 20:
            return "Low — easy on the eyes"
        if percent <= 50:
            return "Dim — comfortable indoors"
        if percent <= 80:
            return "Balanced — recommended"
        if percent < 100:
            return "Bright — good for daylight"
        return "Maximum brightness"

    def _on_close(self) -> None:
        """Restore a sane brightness level before quitting."""
        try:
            self.backend.stop()
        except Exception:
            pass
        self.destroy()


# --------------------------------------------------------------------------- #
#  Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")

    app = BrightnessApp()
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
