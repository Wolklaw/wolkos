"""The app: main window, captures, OCR, recordings, and the command line."""

import argparse
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox

from PIL import Image

from . import APP_CLASS, APP_ID, APP_NAME, APP_SLUG, MODES, SHORTCUTS, VERSION
from .core import (DEFAULT_LANG, CaptureError, Clipboard, Monitor, OCREngine, OCRError, Tesseract,
                   bounding_box, cache_dir, grab_screen, language_label, language_short,
                   load_config, log, monitors, place_on_desktop, recordings_dir, reduced_motion,
                   runtime_dir, sample_image, save_config, setup_logging, system_font_file)
from .text import t
from .widgets import T, Glitch, GlitchText, Mark, Segmented, Strip, Toggle, WolkButton, pick_mono

try:
    from PIL import ImageTk  # noqa: F401 - checked here so the UI can explain what's missing
except ImportError:
    ImageTk = None

HIDE_DELAY_MS = 250         # let the compositor finish hiding our window before the grab
HEARTBEAT_MS = 250
BUTTON_LABELS = {mode: t(f'btn.{mode}') for mode in MODES}


def detect_scale(root: tk.Tk) -> float:
    """UI scale. Tk ignores the desktop's scaling on Linux, but Plasma publishes it to X
    programs as Xft.dpi, so read that."""
    forced = os.environ.get('SPARK_SNIP_SCALE')
    if forced:
        try:
            return max(0.5, min(4.0, float(forced)))
        except ValueError:
            pass
    if os.name != 'nt' and shutil.which('xrdb'):
        try:
            out = subprocess.run(['xrdb', '-query'], capture_output=True, text=True,
                                 timeout=3).stdout
            match = re.search(r'^Xft\.dpi:\s*([0-9.]+)', out, re.M)
            if match:
                return max(1.0, min(4.0, float(match.group(1)) / 96))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    try:
        return max(1.0, min(4.0, root.winfo_fpixels('1i') / 96))
    except tk.TclError:
        return 1.0


def load_icon(root: tk.Misc) -> Optional[tk.PhotoImage]:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in (os.path.join(here, 'icons', f'{APP_SLUG}-256.png'),
                 f'/usr/share/icons/hicolor/256x256/apps/{APP_ID}.png'):
        if os.path.isfile(path):
            try:
                return tk.PhotoImage(master=root, file=path)
            except tk.TclError:
                log.exception("could not load %s", path)
    return None


def _enable_dpi_awareness() -> None:
    """Windows dev runs only, so the preview isn't bitmap-stretched."""
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass


class App:
    def __init__(self, start_mode: Optional[str] = None, from_file: Optional[str] = None):
        _enable_dpi_awareness()
        self.root = tk.Tk(className=APP_CLASS)
        self.root.withdraw()
        self.snip_only = start_mode is not None
        self.from_file = from_file
        self.root.report_callback_exception = self._on_tk_error
        self.scale = detect_scale(self.root)
        try:
            self.root.tk.call('tk', 'scaling', self.scale * 96 / 72)
        except tk.TclError:
            pass
        self.mono = pick_mono(self.root)
        self._fonts: Dict[Tuple[int, bool], tkfont.Font] = {}
        self.icon = load_icon(self.root)
        if self.icon is not None:
            self.root.iconphoto(True, self.icon)

        self.config = load_config()
        saved_mode = self.config.get("mode")
        self.mode = start_mode or (saved_mode if saved_mode in MODES else "image")
        saved_lang = self.config.get("lang")
        self.tesseract = Tesseract()
        self.ocr = OCREngine(self.tesseract,
                             saved_lang if isinstance(saved_lang, str) else DEFAULT_LANG)
        self.clipboard = Clipboard(self.root)
        self.glitch = Glitch(self.root, enabled=not reduced_motion()
                             and self.config.get("glitch", True) is not False)
        self.results: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.busy = False
        self.ocr_target = None
        self.popup = None
        self.overlay = None
        self.window_built = False
        self.recording = None
        self.rec_selection = None
        self.rec_bar = None
        self.rec_frame = None
        self._stop_requested = False
        self._beat = 0
        if hasattr(signal, 'SIGUSR1'):
            # `spark-snip video` while a recording runs asks this process to stop it
            signal.signal(signal.SIGUSR1, lambda *_: setattr(self, '_stop_requested', True))
        self.root.after(HEARTBEAT_MS, self._heartbeat)

        if self.snip_only:
            self.root.after(1, self.start_capture)
        else:
            self._build_window()

    # look helpers
    def px(self, value: float) -> int:
        return int(round(value * self.scale))

    def font(self, size: int, bold: bool = True) -> Tuple[str, int, str]:
        return (self.mono, size, "bold" if bold else "normal")

    def tkfont(self, size: int, bold: bool = True) -> tkfont.Font:
        key = (size, bold)
        if key not in self._fonts:
            self._fonts[key] = tkfont.Font(self.root, family=self.mono, size=size,
                                           weight="bold" if bold else "normal")
        return self._fonts[key]

    def _on_tk_error(self, exc_type, exc_value, exc_tb):
        log.error("unhandled callback error", exc_info=(exc_type, exc_value, exc_tb))
        messagebox.showerror(APP_NAME, t('error.unexpected', error=exc_value))

    # settings
    def remember_mode(self, mode: str):
        self.mode = mode
        self.config["mode"] = mode
        save_config(self.config)
        if self.window_built:
            self.mode_switch.set(mode)
            self._show_mode()

    def set_option(self, name: str, value: Any):
        self.config[name] = value
        save_config(self.config)
        if self.window_built and name in self.toggles:
            self.toggles[name].set(value)

    def _on_language(self, code: str):
        self.ocr.lang = code
        self.config["lang"] = code
        save_config(self.config)
        log.info("language set to %s", code)

    # main window
    def _build_window(self):
        root, px, bg = self.root, self.px, T["bg"]
        root.title(APP_NAME)
        root.configure(bg=bg)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        head = tk.Frame(root, bg=bg)
        head.pack(fill="x", padx=px(16), pady=(px(14), px(10)))
        self.mark = Mark(head, px(38), bg)
        self.mark.pack(side="left")
        col = tk.Frame(head, bg=bg)
        col.pack(side="left", padx=(px(12), 0))
        self.title = GlitchText(col, "SPARK SNIP", self.tkfont(15), bg)
        self.title.pack(anchor="w")
        self.status = tk.Label(col, text="● " + t('status.ready'), bg=bg, fg=T["accent"], font=self.font(7))
        self.status.pack(anchor="w", padx=(px(6), 0))
        tk.Label(head, text=f"V{VERSION}", bg=T["panel"], fg=T["muted"], font=self.font(7),
                 padx=px(6), pady=px(2), highlightthickness=1,
                 highlightbackground=T["line"]).pack(side="right", anchor="n", pady=(px(4), 0))

        self.strip = Strip(root, height=px(2))
        self.strip.pack(fill="x")

        body = tk.Frame(root, bg=bg)
        body.pack(fill="both", expand=True, padx=px(16), pady=(px(16), px(6)))
        modes = tk.Frame(body, bg=bg)
        modes.pack(fill="x", pady=(0, px(12)))
        tk.Label(modes, text=t('label.mode'), bg=bg, fg=T["dim"], font=self.font(7)).pack(side="left")
        self.mode_switch = Segmented(modes, [(t(f'mode.{m}').upper(), m) for m in MODES],
                                     self.mode, self.remember_mode, self.font(8))
        self.mode_switch.pack(side="right")
        self.capture_btn = WolkButton(body, BUTTON_LABELS[self.mode], self._main_button,
                                      self.font(11), variant="primary", padx=px(20),
                                      pady=px(12))
        self.capture_btn.pack(fill="x")
        self.hint = tk.Label(body, text="", bg=bg, fg=T["dim"], font=self.font(7))
        self.hint.pack(pady=(px(8), px(14)))

        row = tk.Frame(body, bg=bg)
        row.pack(fill="x", pady=(0, px(8)))
        tk.Label(row, text=t('label.language'), bg=bg, fg=T["dim"], font=self.font(7)).pack(side="left")
        choices = self.ocr.language_choices()
        if not choices:
            tk.Label(row, text=t('label.no_language'), bg=bg, fg=T["pink"],
                     font=self.font(8)).pack(side="right")
        elif len(choices) <= 4:
            Segmented(row, [(language_short(c), c) for c in choices], self.ocr.lang,
                      self._on_language, self.font(8)).pack(side="right")
        else:
            self._language_menu(row, choices).pack(side="right")

        row = tk.Frame(body, bg=bg)
        row.pack(fill="x")
        tk.Label(row, text=t('label.audio'), bg=bg, fg=T["dim"], font=self.font(7)).pack(side="left")
        self.toggles = {
            "mic": Toggle(row, t('toggle.mic'), bool(self.config.get("mic", False)),
                          lambda v: self.set_option("mic", v), self.font(8)),
            "sound": Toggle(row, t('toggle.sound'), bool(self.config.get("sound", True)),
                            lambda v: self.set_option("sound", v), self.font(8)),
        }
        self.toggles["mic"].pack(side="right")
        self.toggles["sound"].pack(side="right", padx=(0, px(6)))

        tk.Frame(root, bg=T["line"], height=1).pack(fill="x", padx=px(16), pady=(px(14), 0))
        foot = tk.Frame(root, bg=bg)
        foot.pack(fill="x", padx=px(16), pady=(px(8), px(12)))
        version = self.tesseract.version()
        engine = t('foot.tesseract', version=version) if version else t('foot.no_tesseract')
        tk.Label(foot, text=t('foot.offline', engine=engine), bg=bg,
                 fg=T["dim"] if version else T["pink"], font=self.font(7)).pack(side="left")
        tk.Label(foot, text="SPARKOS", bg=bg, fg=T["dim"], font=self.font(7)).pack(side="right")

        self._show_mode()
        root.update_idletasks()
        width = max(px(430), root.winfo_reqwidth())
        height = root.winfo_reqheight()
        x, y = self.config.get("x"), self.config.get("y")
        if (isinstance(x, int) and isinstance(y, int)
                and 0 <= x < root.winfo_screenwidth() - 40
                and 0 <= y < root.winfo_screenheight() - 40):
            root.geometry(f"{width}x{height}+{x}+{y}")
        else:
            root.geometry(f"{width}x{height}")
        self.window_built = True
        root.deiconify()
        self.glitch.add(self.title, self.mark, self.strip)

    def _show_mode(self):
        if self.recording is not None:
            return
        self.capture_btn.set_text(BUTTON_LABELS[self.mode])
        key = SHORTCUTS[self.mode].upper().replace('PRINT', t('key.print'))
        extra = t('hint.also', shortcut="META+SHIFT+S") if self.mode == "image" else ""
        self.hint.config(text=t('hint', shortcut=key, extra=extra))

    def _language_menu(self, parent: tk.Misc, choices: List[str]) -> WolkButton:
        menu = tk.Menu(self.root, tearoff=0, bg=T["panel"], fg=T["text"], bd=1, relief="flat",
                       activebackground=T["accent"], activeforeground=T["on_accent"],
                       font=self.font(9, False))
        button: WolkButton

        def pick(code: str):
            self._on_language(code)
            button.set_text(f"{language_short(code)} ▾")

        for code in choices:
            menu.add_command(label=language_label(code), command=lambda c=code: pick(c))
        button = WolkButton(parent, f"{language_short(self.ocr.lang)} ▾",
                            lambda: menu.tk_popup(button.winfo_rootx(),
                                                  button.winfo_rooty() + button.winfo_height()),
                            self.font(8), padx=self.px(10), pady=self.px(3))
        return button

    def _set_status(self, text: str, colour: str):
        if self.window_built:
            self.status.config(text=f"● {text}", fg=colour)

    def _main_button(self):
        if self.recording is not None:
            self.stop_recording()
        else:
            self.start_capture()

    def _on_close(self):
        if self.recording is not None:
            self.stop_recording()
            self.root.after(500, self._on_close)
            return
        if self.window_built and self.root.state() == 'normal':
            self.config["x"], self.config["y"] = self.root.winfo_x(), self.root.winfo_y()
        self.config["version"] = VERSION
        save_config(self.config)
        log.info("--- shutting down ---")
        self.root.destroy()

    def _restore_window(self):
        if self.window_built and self.recording is None:
            self.root.deiconify()

    def _quit_if_done(self):
        if (self.snip_only and self.popup is None and self.overlay is None
                and self.recording is None):
            self.root.destroy()

    # capture
    def start_capture(self):
        if self.busy or self.overlay is not None or self.recording is not None:
            return
        self.close_popup()
        visible = self.window_built and self.root.state() == 'normal'
        if visible:
            self.root.withdraw()
        self.root.after(HIDE_DELAY_MS if visible else 1, self._capture)

    def _capture(self):
        from .overlay import SnipOverlay
        layout = monitors((self.root.winfo_screenwidth(), self.root.winfo_screenheight()))
        try:
            if self.from_file:
                with Image.open(self.from_file) as img:
                    img.load()
                    shot = place_on_desktop(img.convert('RGB'), bounding_box(layout), T["bg"])
            else:
                shot, _tool = grab_screen()
        except (CaptureError, OSError, ValueError) as exc:
            log.exception("capture failed")
            self._restore_window()
            self.open_popup(None, layout[0]).show_error(t('fail.capture'), str(exc))
            return
        try:
            self.overlay = SnipOverlay(self, shot, layout, self.mode, self._on_selection)
        except tk.TclError as exc:
            log.exception("could not open the capture overlay")
            self.overlay = None
            self._restore_window()
            self.open_popup(None, layout[0]).show_error(t('fail.overlay'), str(exc))

    def _on_selection(self, selection):
        self.overlay = None
        release_lock()
        if selection is None:
            self._restore_window()
            self._quit_if_done()
            return
        if selection.mode != self.mode:
            self.remember_mode(selection.mode)
        if selection.mode == "video":
            self.start_recording(selection)
            return
        self._restore_window()
        popup = self.open_popup(selection.image, selection.monitor)
        if selection.mode == "text":
            self.read_text(selection.image, popup)
        else:
            popup.show_image(self.clipboard.copy_image(selection.image))

    # OCR
    def read_text(self, image: Image.Image, popup):
        if self.busy:
            return
        self.busy, self.ocr_target = True, popup
        popup.reading()
        self._button_state(False, t('btn.reading'))
        self._set_status(t('status.reading'), T["gold"])
        threading.Thread(target=self._ocr_worker, args=(image,), daemon=True).start()
        self.root.after(80, self._poll_ocr)

    def _ocr_worker(self, img: Image.Image):
        try:
            self.results.put(('ok', self.ocr.extract_text(img)))
        except Exception as exc:                     # noqa: BLE001 - shown to the user
            log.exception("OCR failed")
            self.results.put(('error', exc))

    def _poll_ocr(self):
        try:
            kind, payload = self.results.get_nowait()
        except queue.Empty:
            self.root.after(80, self._poll_ocr)
            return
        self.busy = False
        self._button_state(True)
        self._set_status(t('status.ready'), T["accent"])
        popup, self.ocr_target = self.ocr_target, None
        if popup is None or popup.closed:            # closed while reading: cancelled
            return
        if kind == 'error':
            popup.show_error(t('fail.read'), str(payload))
        elif not payload.text.strip():
            log.info("no text found")
            popup.show_empty()
        else:
            popup.show_text(payload, self.clipboard.copy_text(payload.text))

    def _button_state(self, enabled: bool, label: Optional[str] = None):
        if self.window_built:
            self.capture_btn.set_enabled(enabled)
            self.capture_btn.set_text(label or BUTTON_LABELS[self.mode])

    # result window
    def open_popup(self, image: Optional[Image.Image], near: Monitor):
        from .windows import ResultWindow
        self.close_popup()
        self.popup = ResultWindow(self, image, near, self._popup_closed)
        return self.popup

    def _popup_closed(self):
        self.popup = None
        self._quit_if_done()

    def close_popup(self):
        popup, self.popup = self.popup, None
        if popup is not None:
            popup.on_close = lambda: None
            popup.close()

    # recording
    def _audio_label(self) -> str:
        sound, mic = self.config.get("sound", True), self.config.get("mic", False)
        parts = ([t('audio.sound')] if sound else []) + ([t('audio.mic')] if mic else [])
        return " + ".join(parts) if parts else t('audio.none')

    def start_recording(self, selection):
        from .recorder import Recording, RecordError
        from .windows import RecordBar
        folder = recordings_dir()
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            self._restore_window()
            self.open_popup(selection.image, selection.monitor).show_error(
                t('fail.record'), t('fail.folder', folder=folder, error=exc))
            return
        stem = os.path.join(folder, datetime.now().strftime("SparkSnip_%Y-%m-%d_%H-%M-%S"))
        tokens = self.config.get("screencast_tokens")
        token = tokens.get(selection.monitor.name) if isinstance(tokens, dict) else None
        recording = Recording(selection.monitor, selection.region, stem,
                              mic=bool(self.config.get("mic", False)),
                              sound=bool(self.config.get("sound", True)),
                              restore_token=token, preferred=self.config.get("encoder"))
        try:
            recording.begin()
        except RecordError as exc:
            self._restore_window()
            self.open_popup(selection.image, selection.monitor).show_error(
                t('fail.record'), str(exc))
            return
        self.recording, self.rec_selection = recording, selection
        self.rec_bar = RecordBar(self, selection.region, selection.monitor, self._audio_label(),
                                 self.stop_recording)
        self.rec_bar.waiting()
        write_pid_file()
        self._button_state(True, t('btn.stop'))
        self._set_status(t('status.share'), T["gold"])

    def stop_recording(self):
        if self.recording is None:
            return
        if self.rec_bar is not None:
            self.rec_bar.finishing()
        self._set_status(t('status.saving'), T["gold"])
        self.recording.stop(done=lambda: None)

    def _heartbeat(self):
        try:
            if self._stop_requested:
                self._stop_requested = False
                if self.recording is not None:
                    self.stop_recording()
            if self.recording is not None:
                self._tick_recording()
        finally:
            self.root.after(HEARTBEAT_MS, self._heartbeat)

    def _tick_recording(self):
        from .windows import RecordFrame
        rec = self.recording
        state = rec.poll()
        self._beat += 1
        if state == 'recording':
            if self.rec_frame is None:
                self.rec_frame = RecordFrame(self.root, self.rec_selection.region)
            if self._beat % 2 == 0:
                self.rec_bar.recording(rec.elapsed())
                elapsed = int(rec.elapsed())
                self._set_status(t('status.rec', time=f"{elapsed // 60:02d}:{elapsed % 60:02d}"),
                                 T["pink"])
        elif state in ('done', 'failed', 'cancelled'):
            self._end_recording(state)

    def _end_recording(self, state: str):
        rec, selection = self.recording, self.rec_selection
        self.recording = self.rec_selection = None
        for item in (self.rec_bar, self.rec_frame):
            if item is not None:
                item.close()
        self.rec_bar = self.rec_frame = None
        remove_pid_file()
        self._button_state(True)
        self._set_status(t('status.ready'), T["accent"])
        if self.window_built:
            self._show_mode()
        if rec.new_token:
            tokens = self.config.get("screencast_tokens")
            tokens = tokens if isinstance(tokens, dict) else {}
            tokens[selection.monitor.name] = rec.new_token
            self.config["screencast_tokens"] = tokens
        if state == 'done' and rec.encoder:
            self.config["encoder"] = rec.encoder.element
        save_config(self.config)
        self._restore_window()
        if state == 'cancelled':
            log.info("recording cancelled: %s", rec.error)
            self._quit_if_done()
            return
        popup = self.open_popup(selection.image, selection.monitor)
        if state == 'done':
            log.info("recording saved: %s (%.1fs)", rec.path, rec.duration)
            popup.show_video(rec.path, rec.duration, (rec.crop.width, rec.crop.height),
                             rec.encoder.label, self._audio_label())
        else:
            popup.show_error(t('fail.recording'), rec.error or t('fail.unknown'))
            self._set_status(t('status.error'), T["pink"])

    def run(self):
        self.root.mainloop()


# ─── Process helpers ──────────────────────────────────────────

_lock = None


def take_lock() -> bool:
    """Pressing a shortcut twice must not stack two overlays."""
    global _lock
    if os.name == 'nt':
        return True
    import fcntl
    try:
        _lock = open(os.path.join(runtime_dir(), f'{APP_SLUG}-capture.lock'), 'w')
        fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def release_lock():
    global _lock
    if _lock is not None:
        try:
            _lock.close()
        except OSError:
            pass
        _lock = None


def write_pid_file():
    from .recorder import pid_file, pid_file_text
    try:
        with open(pid_file(), 'w', encoding='ascii') as f:
            f.write(pid_file_text(os.getpid()))
    except OSError:
        log.exception("could not write the recording pid file")


def remove_pid_file():
    from .recorder import pid_file
    try:
        os.remove(pid_file())
    except OSError:
        pass


# ─── Self-test and command line ───────────────────────────────

def self_test(skip_record: bool = False) -> int:
    """Read generated samples with Tesseract and encode a test video with GStreamer. Needs
    no display, so the image build and CI can run it."""
    tesseract = Tesseract()
    report: Dict[str, Any] = {"version": VERSION, "tesseract": tesseract.cmd,
                              "tesseract_version": tesseract.version()}
    ok = bool(tesseract.cmd)
    if ok:
        engine = OCREngine(tesseract, DEFAULT_LANG)
        report["languages"] = engine.available
        missing = [part for part in DEFAULT_LANG.split('+') if part not in engine.available]
        report["missing_languages"] = missing
        ok = not missing
        font_file = system_font_file()
        report["font"] = font_file
        checks = [("eng", "Spark Snip self test 2026", "self test 2026", None)]
        if 'fra' in engine.available and font_file:
            checks.append(("fra", "Résumé déjà prêt", "résumé déjà prêt", font_file))
        report["reads"] = []
        for lang, text, expect, font in checks:
            engine.lang = lang
            try:
                result = engine.extract_text(sample_image(text, font_file=font))
                passed = expect in result.text.lower()
                report["reads"].append({"lang": lang, "text": result.text,
                                        "confidence": round(result.confidence, 1),
                                        "passed": passed})
            except (OCRError, OSError, ValueError) as exc:
                passed = False
                report["reads"].append({"lang": lang, "error": str(exc), "passed": False})
            ok = ok and passed
    report["ocr_passed"] = ok
    # screenshots through KWin or the portal; Spectacle is only ever a fallback
    from .capture import self_check
    report["capture"] = self_check()
    report["pygobject"] = report["capture"]["dbus"].startswith("PyGObject")
    ok = ok and report["capture"]["passed"]
    if not skip_record:
        from .recorder import self_test_encode
        report["recording"] = self_test_encode()
        ok = ok and report["pygobject"] and report["recording"]["passed"]
    report["passed"] = ok
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog=APP_SLUG,
        description="Snip the screen: copy its text or image, or record it.",
        epilog="Shortcuts on SparkOS: Print or Meta+Shift+S for a screenshot, Meta+Shift+T "
               "for text, Meta+Shift+R to start or stop a recording.")
    parser.add_argument('mode', nargs='?', choices=MODES,
                        help="capture straight away in this mode and quit when done; "
                             "`video` while recording stops the recording")
    parser.add_argument('--from', dest='from_file', metavar='FILE',
                        help="pick from an image file instead of the screen (for testing)")
    parser.add_argument('--self-test', action='store_true',
                        help="read samples with Tesseract, check screen capture needs no "
                             "Spectacle, encode a test video, print a report and exit; "
                             "needs no display")
    parser.add_argument('--skip-record', action='store_true',
                        help="with --self-test: leave out the recording check")
    parser.add_argument('--capture-test', metavar='FILE',
                        help="on a running Plasma session: take one full-screen screenshot "
                             "through KWin only, save it to FILE as PNG, print a report, and "
                             "exit 1 if it failed")
    parser.add_argument('--capture-check', nargs='?', const='', metavar='FOLDER',
                        help="on a running desktop: take a screenshot every way Spark Snip "
                             "can (KWin workspace, screen, window, area, then the portal), "
                             "save them to FOLDER (default ~/.cache/spark-snip/capture-check) "
                             "and print a report")
    parser.add_argument('--version', action='version', version=f"{APP_NAME} {VERSION}")
    args = parser.parse_args(argv)

    setup_logging()
    if args.self_test:
        return self_test(skip_record=args.skip_record)
    if args.capture_test:
        from .capture import capture_test
        report = capture_test(args.capture_test)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1
    if args.capture_check is not None:
        from .capture import capture_check
        folder = args.capture_check or os.path.join(cache_dir(), 'capture-check')
        report = capture_check(folder)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1
    if args.mode == 'video':
        from .recorder import running_recorder
        pid = running_recorder()
        if pid is not None:
            log.info("asking the recording in %d to stop", pid)
            os.kill(pid, signal.SIGUSR1)
            return 0
    if ImageTk is None:
        print(f"{APP_SLUG}: Pillow's Tk support is missing (Fedora: python3-pillow-tk)",
              file=sys.stderr)
        return 1
    if args.mode and not take_lock():
        log.info("a capture is already open")
        return 0
    try:
        App(start_mode=args.mode, from_file=args.from_file).run()
    except tk.TclError as exc:
        log.exception("could not start")
        print(f"{APP_SLUG}: {exc}", file=sys.stderr)
        return 1
    except Exception:                                # noqa: BLE001 - last chance to log
        log.exception("fatal error")
        raise
    return 0
