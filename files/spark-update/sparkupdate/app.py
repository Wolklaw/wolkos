"""The window: this version and the next, what changed (read from the versions' own files),
Check now, Restart, Roll back, settings and the log. Everything slow runs in a thread; the
window opens at once and says READING until the status is in."""

import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import tkinter as tk
import tkinter.font as tkfont

from . import APP_CLASS, APP_NAME, APP_SLUG, HELPER, SYSTEM_LOG, VERSION
from . import core, release
from .core import Env, load_json, log, reduced_motion, run, save_json
from .notify import restart_pc
from .settings import (EVERY_HOURS, NOTIFY, REMIND_HOURS, STAGE, SystemSettings, UserPrefs,
                       as_args, load_prefs, load_system, save_prefs, write_system)
from .status import Deployment, Host, read_host
from .text import EN, t
from .engine import UUPD, engine, start_uupd, uupd_status
from .updater import (direct_state_path, load_state, rollback as do_rollback, system_run,
                      update_flatpak, user_state_path)
from .widgets import (MONO_FAMILIES, T, Glitch, GlitchPanel, GlitchText, Mark, Segmented,
                      Strip, Toggle, WolkButton)

NOW_UNIT = '/usr/lib/systemd/system/spark-update-now@.service'


# ─── What the window shows, gathered in a thread ─────────────

@dataclass
class Snapshot:
    host: Host
    conf: SystemSettings
    conf_warnings: List[str]
    state: Dict[str, Any]           # the system run's state (or the window's own checks)
    user_state: Dict[str, Any]
    current: Optional[Dict[str, Any]] = None     # release of the booted version
    other: Optional[Dict[str, Any]] = None       # release of the next or previous version
    other_dep: Optional[Deployment] = None
    compare: str = 'none'           # 'update', 'rollback', 'previous', 'none'
    system_install: bool = False
    engine: str = 'none'            # who stages images: 'uupd', 'spark' or 'none' (engine.py)
    read_at: float = field(default_factory=time.time)


def gather(env: Env) -> Snapshot:
    host = read_host(env)
    conf, warnings = load_system(env)
    state = load_state(env)
    direct = load_json(direct_state_path())
    if (direct.get('last_run') or 0) > (state.get('last_run') or 0):
        state = direct
    eng = engine(env)
    if eng == UUPD:
        state = uupd_state(env, host)
    snap = Snapshot(host=host, conf=conf, conf_warnings=warnings, state=state,
                    user_state=load_json(user_state_path()),
                    system_install=os.path.isfile(env.path(NOW_UNIT)), engine=eng)
    if host.booted is not None:
        snap.current = release.load(host.booted.root(env))
        if host.rollback_queued and host.next_boot is not None:
            snap.other_dep, snap.compare = host.next_boot, 'rollback'
        elif host.update_ready:
            snap.other_dep, snap.compare = host.staged, 'update'
        elif host.rollback is not None:
            snap.other_dep, snap.compare = host.rollback, 'previous'
        if snap.other_dep is not None:
            snap.other = release.load(snap.other_dep.root(env))
    else:
        snap.current = release.load(env.path('/'), use_rpm=False)
    return snap


def uupd_state(env: Env, host: Host) -> Dict[str, Any]:
    """uupd's last run in the same shape as Spark Update's own state file."""
    u = uupd_status(env)
    state: Dict[str, Any] = {'engine': UUPD, 'running': u['running'],
                             'last_run': u['last_run'], 'last_check': u['last_run']}
    if u['ok'] is False:
        state.update(result='failed', error=t('uupd.failed'))
    elif host.update_ready:
        state.update(result='staged', staged_version=host.staged.version)
    elif u['last_run']:
        state['result'] = 'up_to_date'
    state['flatpak_ok'] = u['ok']
    return state


def tail(path: str, lines: int = 200) -> List[str]:
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read().splitlines()[-lines:]
    except OSError:
        return []


# ─── Look helpers (as in Spark Monitor) ──────────────────────

def detect_scale(root: tk.Tk) -> float:
    forced = os.environ.get('SPARK_UPDATE_SCALE')
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


def pick_mono_cached(root: tk.Tk) -> str:
    path = os.path.join(core.cache_dir(), 'font')
    try:
        with open(path, encoding='utf-8') as f:
            name = f.read().strip()
        if name and tkfont.Font(root, family=name).actual('family') == name:
            return name
    except OSError:
        pass
    families = {n.lower(): n for n in tkfont.families(root)}
    name = next((families[w.lower()] for w in MONO_FAMILIES if w.lower() in families),
                "TkFixedFont")
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(name)
    except OSError:
        pass
    return name


def load_icon(root: tk.Misc) -> Optional[tk.PhotoImage]:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in (os.path.join(here, 'icons', f'{APP_SLUG}-64.png'),
                 os.path.join(here, 'icons', f'{APP_SLUG}-256.png')):
        if os.path.isfile(path):
            try:
                return tk.PhotoImage(master=root, file=path)
            except tk.TclError:
                pass
    return None


class Dialog(tk.Toplevel):
    """A house-style question: glitch title, plain text, buttons. on_answer(key or None)."""

    def __init__(self, app: 'App', title: str, body: str,
                 buttons: List[Tuple[str, str, str]], on_answer: Callable[[Optional[str]], None]):
        super().__init__(app.root, bg=T["bg"])
        self.app, self.on_answer, self.done = app, on_answer, False
        px = app.px
        self.title(title)
        self.transient(app.root)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", lambda: self.answer(None))
        edge = tk.Frame(self, bg=T["line2"], padx=1, pady=1)
        edge.pack(fill="both", expand=True)
        inner = tk.Frame(edge, bg=T["panel"], padx=px(18), pady=px(14))
        inner.pack(fill="both", expand=True)
        self.heading = GlitchText(inner, title.upper(), app.tkfont(11), T["panel"])
        self.heading.pack(anchor="w")
        tk.Label(inner, text=body, bg=T["panel"], fg=T["text"], font=app.font(9, False),
                 justify="left", anchor="w", wraplength=px(460)).pack(anchor="w",
                                                                       pady=(px(8), px(14)))
        row = tk.Frame(inner, bg=T["panel"])
        row.pack(anchor="e")
        self.buttons: Dict[str, WolkButton] = {}
        for key, label, variant in buttons:
            b = WolkButton(row, label, lambda k=key: self.answer(k), app.font(8), variant)
            b.pack(side="left", padx=(px(8), 0))
            self.buttons[key] = b
        self.bind("<Escape>", lambda _e: self.answer(None))
        self.update_idletasks()
        x = app.root.winfo_rootx() + (app.root.winfo_width() - self.winfo_reqwidth()) // 2
        y = app.root.winfo_rooty() + (app.root.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        try:
            self.grab_set()
        except tk.TclError:
            pass
        app.glitch.now(self.heading)

    def answer(self, key: Optional[str]):
        if self.done:
            return
        self.done = True
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        self.on_answer(key)


# ─── The window ──────────────────────────────────────────────

class App:
    def __init__(self, env: Optional[Env] = None, view: Optional[str] = None):
        if os.name == 'nt':
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except (AttributeError, OSError):
                pass
        self.env = env or Env()
        self.root = tk.Tk(className=APP_CLASS)
        self.root.withdraw()
        self.root.report_callback_exception = self._on_tk_error
        self.scale = detect_scale(self.root)
        try:
            self.root.tk.call('tk', 'scaling', self.scale * 96 / 72)
        except tk.TclError:
            pass
        self.mono = pick_mono_cached(self.root)
        self._fonts: Dict[Tuple[int, bool], tkfont.Font] = {}
        self.prefs: UserPrefs = load_prefs()
        self.glitch = Glitch(self.root, enabled=not reduced_motion() and self.prefs.glitch)
        self.snap: Optional[Snapshot] = None
        self.jobs: "queue.Queue[Tuple[Callable, Any]]" = queue.Queue()
        self.pending = 0
        self.busy: Optional[str] = None       # what's running: 'check', 'rollback', ...
        self.watch_until = 0.0                # poll the state file while a check runs
        self.view_name = ''
        self.views: Dict[str, tk.Frame] = {}
        self.change_rows: List[Tuple[str, str, str, str]] = []   # for tests: name, old, new, kind
        self.dialog: Optional[Dialog] = None
        self.first_paint: Optional[float] = None
        self.check_started = 0.0
        self._poll_id: Optional[str] = None
        self._watch_id: Optional[str] = None
        self._build()
        self.show(view if view in ('main', 'settings', 'log') else 'main', glitch=False)
        self.root.deiconify()
        self.glitch.add(self.title, self.mark, self.strip)
        self.reload()

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
        self.say(t('msg.failed', why=str(exc_value) or exc_type.__name__), error=True)

    # — building —
    def _build(self):
        root, px, bg = self.root, self.px, T["bg"]
        root.title(APP_NAME)
        root.configure(bg=bg)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.minsize(px(640), px(460))
        icon = load_icon(root)
        if icon is not None:
            root.iconphoto(True, icon)
            self._icon = icon
        head = tk.Frame(root, bg=bg)
        head.pack(fill="x", padx=px(14), pady=(px(10), px(8)))
        self.mark = Mark(head, px(34), bg)
        self.mark.pack(side="left")
        col = tk.Frame(head, bg=bg)
        col.pack(side="left", padx=(px(10), 0))
        self.title = GlitchText(col, "SPARK UPDATE", self.tkfont(14), bg)
        self.title.pack(anchor="w")
        self.status = tk.Label(col, text="● " + t('status.reading'), bg=bg, fg=T["muted"],
                               font=self.font(7), anchor="w")
        self.status.pack(anchor="w", padx=(px(6), 0))
        tk.Label(head, text=f"V{VERSION}", bg=T["panel"], fg=T["muted"], font=self.font(7),
                 padx=px(6), pady=px(2), highlightthickness=1,
                 highlightbackground=T["line"]).pack(side="right", anchor="n", pady=(px(4), 0))
        self.strip = Strip(root, height=px(2))
        self.strip.pack(fill="x")
        self.body = tk.Frame(root, bg=bg)
        self.body.pack(fill="both", expand=True, padx=px(14), pady=(px(10), 0))
        foot = tk.Frame(root, bg=bg)
        foot.pack(fill="x", padx=px(14), pady=(px(6), px(10)))
        self.message = tk.Label(foot, text="", bg=bg, fg=T["muted"], font=self.font(8, False),
                                anchor="w", justify="left")
        self.message.pack(fill="x")
        self.foot = foot
        root.bind('<Expose>', self._exposed, add='+')
        root.bind('<FocusIn>', self._focused, add='+')
        root.bind('<Escape>', lambda _e: self.show('main') if self.view_name != 'main' else None)
        geometry = load_json(os.path.join(core.config_dir(), 'window.json')).get('geometry')
        if isinstance(geometry, str) and re.fullmatch(r'\d+x\d+(\+-?\d+\+-?\d+)?', geometry):
            root.geometry(geometry)
        else:
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            root.geometry(f"{min(px(860), int(sw * 0.85))}x{min(px(640), int(sh * 0.85))}")

    def _exposed(self, event):
        if self.first_paint is None and event.widget is self.root:
            self.first_paint = time.monotonic()

    def _focused(self, event):
        if event.widget is self.root and self.snap and time.time() - self.snap.read_at > 30 \
                and not self.pending:
            self.reload()

    def label(self, parent, text, size=7, fg=None, bold=True, **kw) -> tk.Label:
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=fg or T["muted"],
                        font=self.font(size, bold), anchor="w", justify="left", **kw)

    def panel(self, parent, heading: str) -> Tuple[GlitchPanel, tk.Frame]:
        p = GlitchPanel(parent, T["panel"])
        inner = tk.Frame(p.inner, bg=T["panel"], padx=self.px(12), pady=self.px(10))
        inner.pack(fill="both", expand=True)
        self.label(inner, heading.upper(), 7, T["accent"]).pack(anchor="w")
        return p, inner

    # — views —
    def show(self, name: str, glitch: bool = True):
        if name == self.view_name:
            return
        for v in self.views.values():
            v.pack_forget()
        self.view_name = name
        if name not in self.views or name == 'log':
            if name in self.views:
                self.views[name].destroy()
            frame = tk.Frame(self.body, bg=T["bg"])
            self.views[name] = frame
            {'main': self._build_main, 'settings': self._build_settings,
             'log': self._build_log}[name](frame)
        self.views[name].pack(fill="both", expand=True)
        if name == 'main' and self.snap:
            self.render()
        if glitch:
            self.glitch.now(self.title)

    def _build_main(self, f: tk.Frame):
        px = self.px
        top = tk.Frame(f, bg=T["bg"])
        top.pack(fill="x")
        top.columnconfigure(0, weight=1, uniform="pc")
        top.columnconfigure(1, weight=1, uniform="pc")
        self.pc_panel, pc = self.panel(top, t('panel.this_pc'))
        self.pc_panel.grid(row=0, column=0, sticky="nsew", padx=(0, px(5)))
        self.pc_version = self.label(pc, "—", 12, T["text"])
        self.pc_version.pack(anchor="w", pady=(px(4), 0))
        self.pc_detail = self.label(pc, "", 8, T["muted"], bold=False, wraplength=px(250))
        self.pc_detail.pack(anchor="w")
        self.next_panel, nx = self.panel(top, t('panel.next'))
        self.next_panel.grid(row=0, column=1, sticky="nsew", padx=(px(5), 0))
        self.next_version = self.label(nx, "—", 12, T["text"])
        self.next_version.pack(anchor="w", pady=(px(4), 0))
        self.next_detail = self.label(nx, "", 8, T["muted"], bold=False, wraplength=px(250))
        self.next_detail.pack(anchor="w")

        self.changes_panel, ch = self.panel(f, t('panel.changes_current'))
        self.changes_panel.pack(fill="both", expand=True, pady=(px(10), 0))
        self.changes_head = ch.winfo_children()[0]
        self.changes_note = self.label(ch, "", 8, T["muted"], bold=False)
        self.changes_note.pack(anchor="w", pady=(px(2), px(6)))
        self.changes_grid = tk.Frame(ch, bg=T["panel"])
        self.changes_grid.pack(fill="x", anchor="w")

        info = tk.Frame(f, bg=T["bg"])
        info.pack(fill="x", pady=(px(10), 0))
        self.apps_line = self.label(info, "", 8, T["text"], bold=False, wraplength=px(800))
        self.apps_line.pack(anchor="w")
        self.check_line = self.label(info, "", 8, T["muted"], bold=False, wraplength=px(800))
        self.check_line.pack(anchor="w", pady=(px(2), 0))

        row = tk.Frame(f, bg=T["bg"])
        row.pack(fill="x", pady=(px(12), 0))
        b = self.buttons = {}
        b['restart'] = WolkButton(row, t('btn.restart'), self.on_restart, self.font(8), 'primary')
        b['check'] = WolkButton(row, t('btn.check'), self.on_check, self.font(8), 'ghost')
        b['rollback'] = WolkButton(row, t('btn.rollback'), self.on_rollback, self.font(8),
                                   'danger')
        b['log'] = WolkButton(row, t('btn.log'), lambda: self.show('log'), self.font(8), 'ghost')
        b['settings'] = WolkButton(row, t('btn.settings'), lambda: self.show('settings'),
                                   self.font(8), 'ghost')
        b['log'].pack(side="right")
        b['settings'].pack(side="right", padx=(0, px(8)))
        self.button_row = row

    def _layout_buttons(self):
        b, px = self.buttons, self.px
        for key in ('restart', 'check', 'rollback'):
            b[key].pack_forget()
        snap = self.snap
        if snap and snap.host.restart_changes:
            b['restart'].pack(side="left", padx=(0, px(8)))
        b['check'].pack(side="left", padx=(0, px(8)))
        b['rollback'].pack(side="left")

    # — rendering the snapshot —
    def running(self) -> bool:
        state = self.snap.state if self.snap else {}
        if state.get('engine') == UUPD:
            return bool(state.get('running'))
        return bool(state.get('running')) and time.time() - (state.get('last_run') or 0) < 3 * 3600

    def render_status(self):
        snap = self.snap
        if snap is None:
            return
        host, state, running = snap.host, snap.state, self.running()
        if self.env.fake:
            status, colour = t('status.test_data'), T["gold"]
        elif running:
            status = t({'downloading': 'status.downloading', 'flatpak': 'status.flatpak'}
                       .get(state.get('phase'), 'status.checking'))
            colour = T["gold"]
        elif host.booted is None:
            status, colour = t('status.no_image'), T["pink"]
        elif host.rollback_queued:
            status, colour = t('status.rolled_back'), T["gold"]
        elif host.update_ready:
            status, colour = t('status.ready'), T["accent"]
        elif state.get('result') == 'available':
            status, colour = t('status.available'), T["accent"]
        elif state.get('result') == 'failed':
            status, colour = t('status.failed'), T["pink"]
        else:
            status, colour = t('status.up_to_date'), T["accent"]
        self.status.config(text="● " + status, fg=colour)

    def render(self):
        snap = self.snap
        if snap is None:
            return
        self.render_status()
        if 'main' not in self.views:
            return
        host, state, running = snap.host, snap.state, self.running()
        # this PC
        if host.booted is not None:
            b = host.booted
            self.pc_version.config(text=t('pc.version', version=b.version or '?'))
            self.pc_detail.config(text='\n'.join(x for x in (
                t('pc.built', date=core.day(b.timestamp)) if b.timestamp else '',
                b.image) if x))
        else:
            cur = snap.current or {}
            self.pc_version.config(text=t('pc.version', version=cur.get('version') or '?'))
            self.pc_detail.config(text=t('pc.unknown', why=host.error))
        # next restart
        nxt = host.next_boot if host.restart_changes else None
        if nxt is not None:
            self.next_version.config(text=t('pc.version', version=nxt.version or '?'),
                                     fg=T["accent"] if host.update_ready else T["gold"])
            words = t('next.rollback', version=nxt.version) if host.rollback_queued else \
                t('next.staged', version=nxt.version)
            words = words.split(', ', 1)[-1]
            extra = [words]
            if nxt.timestamp:
                extra.append(t('pc.built', date=core.day(nxt.timestamp)))
            self.next_detail.config(text='\n'.join(extra))
        else:
            self.next_version.config(text="—", fg=T["dim"])
            self.next_detail.config(text=t('next.same'))

        self._render_changes(snap)
        self._render_lines(snap, running)
        self._layout_buttons()
        b = self.buttons
        b['restart'].set_text(t('btn.restart') if host.update_ready else t('btn.restart_plain'))
        available = state.get('result') == 'available' and not host.update_ready
        b['check'].set_text(t('btn.download') if available else t('btn.check'))
        b['check'].set_enabled(not running and self.busy is None and host.booted is not None)
        b['rollback'].set_text(t('btn.undo_rollback') if host.rollback_queued
                               else t('btn.rollback'))
        b['rollback'].set_enabled(self.busy is None and (host.rollback is not None or
                                                         host.rollback_queued))
        b['restart'].set_enabled(self.busy is None)
        if running:
            self._watch()

    def _render_changes(self, snap: Snapshot):
        for w in self.changes_grid.winfo_children():
            w.destroy()
        self.change_rows = []
        px = self.px
        g = self.changes_grid
        heading = {'update': 'panel.changes_update', 'rollback': 'panel.changes_rollback',
                   'previous': 'panel.changes_current'}.get(snap.compare, 'panel.contents')
        self.changes_head.config(text=t(heading).upper())
        other = snap.other_dep
        note = ''
        if snap.compare == 'previous' and other is not None:
            note = t('changes.since', version=other.version or '?')
        elif snap.compare in ('update', 'rollback'):
            note = t('changes.update')
        elif snap.host.booted is not None and snap.compare == 'none':
            note = t('changes.no_previous')
        if other is not None and snap.other is None:
            note = t('changes.unreadable', version=other.version or '?')
        self.changes_note.config(text=note)

        if snap.compare == 'none' or snap.other is None:
            rows = release.diff(None, snap.current) if snap.current else []
            for i, c in enumerate(rows):
                self._row(g, i, c.name, '', c.new, 'same')
            return
        if snap.compare == 'previous':
            old, new = snap.other, snap.current
        else:
            old, new = snap.current, snap.other
        changes = release.diff(old, new)
        moved = [c for c in changes if c.kind != 'same']
        same = [c for c in changes if c.kind == 'same']
        if not moved:
            self.label(g, t('changes.none'), 8, T["text"], bold=False).grid(
                row=0, column=0, columnspan=3, sticky="w")
        for i, c in enumerate(moved):
            self._row(g, i, c.name, c.old, c.new, c.kind)
        if same:
            self.label(g, t('changes.same', n=len(same)) + ": " +
                       core.short([c.name for c in same], 4), 7, T["dim"], bold=False,
                       wraplength=px(760)).grid(row=len(moved) + 1, column=0, columnspan=3,
                                                sticky="w", pady=(px(6), 0))

    def _row(self, g, i, name, old, new, kind):
        px = self.px
        self.change_rows.append((name, old, new, kind))
        self.label(g, name, 9, T["text"], bold=kind != 'same').grid(row=i, column=0, sticky="w",
                                                                    padx=(0, px(18)))
        if kind == 'same':
            self.label(g, new, 9, T["muted"], bold=False).grid(row=i, column=1, sticky="w")
            return
        if kind == 'added':
            text, colour = f"{new}  {t('changes.added').upper()}", T["gold"]
        elif kind == 'removed':
            text, colour = f"{old}  {t('changes.removed').upper()}", T["pink"]
        else:
            text, colour = f"{old}  →  {new}", T["accent"]
        self.label(g, text, 9, colour, bold=False).grid(row=i, column=1, sticky="w")

    def _render_lines(self, snap: Snapshot, running: bool):
        state, conf = snap.state, snap.conf
        if snap.engine == UUPD:
            self._render_uupd_lines(snap, running)
            return
        # apps
        if not conf.flatpak:
            apps = t('apps.off')
        else:
            parts = []
            for label, result in ((t('apps.system'), state.get('flatpak')),
                                  (t('apps.yours'), snap.user_state.get('flatpak'))):
                if not isinstance(result, dict):
                    parts.append(f"{label}: {t('apps.not_yet')}")
                elif result.get('ok') is False:
                    parts.append(f"{label}: " + t('apps.failed', when=core.when(result.get('at')),
                                                  why=result.get('error', '?')))
                elif result.get('updated'):
                    parts.append(f"{label}: " + t('apps.updated', n=len(result['updated']),
                                                  when=core.when(result.get('at')),
                                                  names=core.short(result['updated'])))
                else:
                    parts.append(f"{label}: " + t('apps.none', when=core.when(result.get('at'))))
            apps = '   ·   '.join(parts)
        self.apps_line.config(text=t('panel.apps').upper() + "   " + apps)
        # checks
        lines = []
        if state.get('result') == 'failed' and state.get('error'):
            lines.append(t('check.failed', why=state['error']))
        elif state.get('last_check'):
            lines.append(t('check.last', when=core.when(state['last_check'])))
        else:
            lines.append(t('check.never'))
        if state.get('result') == 'postponed' and state.get('reason'):
            key, args = state.get('reason_key'), state.get('reason_args')
            why = t(key, **args) if key in EN and isinstance(args, dict) else state['reason']
            lines.append(t('check.postponed', why=why))
        if not snap.system_install and not self.env.fake:
            lines.append(t('check.direct'))
        elif not conf.automatic:
            lines.append(t('check.off'))
        else:
            if conf.mode == NOTIFY:
                lines.append(t('check.notify_only'))
            if state.get('last_check') and not running:
                lines.append(t('check.next', when=core.when(state['last_check']
                                                            + conf.every_hours * 3600)))
        if snap.conf_warnings:
            lines.append(t('set.conf_warnings', lines='; '.join(snap.conf_warnings)))
        self.check_line.config(text=' '.join(lines),
                               fg=T["pink"] if state.get('result') == 'failed' else T["muted"])

    def _render_uupd_lines(self, snap: Snapshot, running: bool):
        state = snap.state
        when = core.when(state.get('last_run'))
        if not state.get('last_run'):
            apps = t('apps.uupd_never')
        elif state.get('flatpak_ok'):
            apps = t('apps.uupd_ok', when=when)
        else:
            apps = t('apps.uupd_failed', when=when)
        self.apps_line.config(text=t('panel.apps').upper() + "   " + apps)
        lines = [t('check.uupd')]
        if running:
            lines.append(t('check.uupd_running'))
        elif state.get('result') == 'failed':
            lines.append(t('check.uupd_failed', when=when))
        elif state.get('last_run'):
            lines.append(t('check.last', when=when))
        self.check_line.config(text=' '.join(lines),
                               fg=T["pink"] if state.get('result') == 'failed' else T["muted"])

    def say(self, text: str, error: bool = False, good: bool = False):
        self.message.config(text=text, fg=T["pink"] if error else T["accent"] if good
                            else T["muted"])
        if error:
            self.glitch.now(self.title, self.changes_panel if 'main' in self.views else
                            self.title, strong=True)

    # — threads —
    def work(self, fn: Callable[[], Any], done: Callable[[Any], None]):
        """Run fn in a thread and done(result) back in Tk's loop."""
        self.pending += 1

        def worker():
            try:
                result = fn()
            except Exception as exc:          # noqa: BLE001 - shown, and logged
                log.exception("background work failed")
                result = exc
            self.jobs.put((done, result))

        threading.Thread(target=worker, daemon=True).start()
        if self._poll_id is None:
            self._poll_id = self.root.after(40, self._poll)

    def _poll(self):
        self._poll_id = None
        while True:
            try:
                done, result = self.jobs.get_nowait()
            except queue.Empty:
                break
            self.pending -= 1
            done(result)
        if self.pending > 0:
            self._poll_id = self.root.after(40, self._poll)

    def reload(self, then: Optional[Callable[[], None]] = None):
        def done(result):
            if isinstance(result, Exception):
                self.say(t('msg.failed', why=result), error=True)
                return
            first = self.snap is None
            self.snap = result
            if self.view_name == 'main':
                self.render()
            else:
                self.render_status()
            if first and 'main' in self.views:
                self.glitch.now(self.changes_panel)
            if then:
                then()

        self.work(lambda: gather(self.env), done)

    def _watch(self):
        """While a background check runs, re-read the state every 2 s (nothing otherwise)."""
        if self._watch_id is None:
            self._watch_id = self.root.after(2000, self._watched)

    def _watched(self):
        self._watch_id = None
        if self.pending:
            self._watch()
            return
        before = self.snap.state if self.snap else {}

        def then():
            st = self.snap.state
            if st.get('running'):
                return
            if self.busy == 'check' and (st.get('last_run') or 0) < self.check_started - 1:
                # the unit hasn't started its run yet (or uupd-manual hasn't finished one since
                # the click): the state still holds the last run's result, not this one's
                if time.time() - self.check_started < 120:
                    self._watch()
                    return
            if before.get('running') or self.busy == 'check':
                self.busy = None
                self.report_check(st)
                self.render()

        self.reload(then)

    # — actions —
    def test_data(self) -> bool:
        if self.env.fake:
            self.say(t('msg.test_data'))
            self.glitch.now(self.title)
            return True
        return False

    def on_check(self):
        snap = self.snap
        if snap is None or self.busy or self.test_data():
            return
        mode = 'stage' if (snap.conf.mode == STAGE or
                           snap.state.get('result') == 'available') else 'check'
        self.busy = 'check'
        self.say(t('msg.checking'))
        self.status.config(text="● " + t('status.checking'), fg=T["gold"])
        self.buttons['check'].set_enabled(False)
        self.glitch.now(self.title)
        self.check_started = time.time()
        if snap.engine == UUPD:
            self.work(start_uupd, self._unit_started)
        elif snap.system_install:
            self.work(lambda: self._start_unit(mode), self._unit_started)
            if snap.conf.flatpak and mode == 'stage':
                self.work(self._user_flatpak, lambda _r: None)
        else:
            self.work(lambda: system_run(self.env, now_mode=mode, state_path=direct_state_path(),
                                         flatpak_scope='user'), self._direct_done)

    def _start_unit(self, mode: str):
        code, out, err = run(['systemctl', 'start', '--no-block',
                              f'spark-update-now@{mode}.service'], timeout=120)
        if code != 0 and os.path.isfile(HELPER):
            code, out, err = run(['pkexec', HELPER, mode], timeout=300)
        return code, (err or out).strip()

    def _unit_started(self, result):
        if isinstance(result, Exception) or result[0] != 0:
            self.busy = None
            why = result if isinstance(result, Exception) else result[1] or f"exit {result[0]}"
            self.say(t('msg.check_failed', why=why), error=True)
            self.render()
            return
        self.say(t('msg.check_started'))
        self.watch_until = time.time() + 3 * 3600
        self._watch()

    def _user_flatpak(self):
        ustate = load_json(user_state_path())
        ustate['flatpak'] = update_flatpak('user', run, time.time())
        save_json(user_state_path(), ustate)

    def _direct_done(self, result):
        self.busy = None
        if isinstance(result, Exception):
            self.say(t('msg.failed', why=result), error=True)
            return
        self.reload(lambda: self.report_check(self.snap.state))

    def report_check(self, st: Dict[str, Any]):
        res = st.get('result')
        if res == 'failed':
            self.say(t('msg.failed', why=st.get('error', '?')), error=True)
        elif res == 'staged':
            self.say(t('msg.staged', version=st.get('staged_version', '')), good=True)
            self.glitch.now(self.title, self.next_panel, strong=True)
        elif res == 'available':
            self.say(t('msg.available', version=st.get('available_version', '')), good=True)
            self.glitch.now(self.title)
        elif res == 'up_to_date':
            self.say(t('msg.up_to_date'), good=True)
            self.glitch.now(self.title)

    def on_restart(self):
        if self.busy or self.test_data():
            return
        self.ask(t('restart.title'), t('restart.body'),
                 [('cancel', t('btn.cancel'), 'ghost'),
                  ('restart', t('btn.restart_plain'), 'primary')], self._restart_answer)

    def _restart_answer(self, key):
        if key != 'restart':
            return

        def done(ok):
            if ok is not True:
                self.say(t('msg.restart_failed'), error=True)

        self.work(restart_pc, done)

    def on_rollback(self):
        snap = self.snap
        if snap is None or self.busy or self.test_data():
            return
        host = snap.host
        if host.rollback_queued:
            self._do_rollback(undo=True)
            return
        old = host.rollback
        if old is None or host.booted is None:
            return
        self.ask(t('rollback.title'),
                 t('rollback.body', old=old.version or '?', date=core.day(old.timestamp),
                   current=host.booted.version or '?'),
                 [('cancel', t('btn.cancel'), 'ghost'),
                  ('rollback', t('rollback.go'), 'danger')],
                 lambda key: self._do_rollback() if key == 'rollback'
                 else self.say(t('msg.cancelled')))

    def _do_rollback(self, undo: bool = False):
        self.busy = 'rollback'
        self.render()

        def go():
            if os.path.isfile(HELPER):
                code, out, err = run(['pkexec', HELPER, 'rollback'], timeout=900)
                return code, (err or out).strip()
            return do_rollback(self.env)

        def done(result):
            self.busy = None
            if isinstance(result, Exception) or result[0] != 0:
                why = result if isinstance(result, Exception) else result[1]
                if not isinstance(result, Exception) and result[0] in (126, 127):
                    why = why or "no permission"
                self.say(t('msg.rollback_failed', why=why or '?'), error=True)
                self.render()
                return

            def after():
                host = self.snap.host
                if undo or not host.rollback_queued:
                    self.say(t('msg.unrolled'), good=True)
                    return
                version = host.next_boot.version if host.next_boot else '?'
                self.say(t('msg.rolled_back', version=version), good=True)
                self.glitch.now(self.title, self.next_panel, strong=True)
                self.ask(t('restart.title'), t('rollback.restart_now', version=version),
                         [('later', t('rollback.later'), 'ghost'),
                          ('restart', t('btn.restart_plain'), 'primary')],
                         self._restart_answer)

            self.reload(after)

        self.work(go, done)

    def ask(self, title, body, buttons, on_answer):
        if self.dialog is not None and self.dialog.winfo_exists():
            return

        def answered(key):
            self.dialog = None
            on_answer(key)

        self.dialog = Dialog(self, title, body, buttons, answered)

    # — settings —
    def _build_settings(self, f: tk.Frame):
        px = self.px
        snap = self.snap
        conf = snap.conf if snap else load_system(self.env)[0]
        self.edit = SystemSettings(conf.automatic, conf.mode, conf.every_hours, conf.flatpak)
        self.edit_prefs = UserPrefs(self.prefs.notify, self.prefs.remind_hours, self.prefs.glitch)
        under_uupd = (snap.engine if snap else engine(self.env)) == UUPD
        can_system = (self.env.fake or os.path.isfile(HELPER)) and not under_uupd

        p, sysp = self.panel(f, t('set.system'))
        p.pack(fill="x")

        def setter(obj, attr):
            return lambda v: setattr(obj, attr, v)

        self.set_widgets = {}
        w = self.set_widgets
        w['automatic'] = Toggle(sysp, t('set.automatic'), self.edit.automatic,
                                setter(self.edit, 'automatic'), self.font(8))
        w['automatic'].pack(anchor="w", pady=(px(8), 0))
        self.label(sysp, t('set.automatic_note'), 8, T["muted"], bold=False,
                   wraplength=px(700)).pack(anchor="w", pady=(px(2), px(8)))
        line = tk.Frame(sysp, bg=T["panel"])
        line.pack(anchor="w", pady=(0, px(6)))
        self.label(line, t('set.every').upper(), 7).pack(side="left", padx=(0, px(10)))
        w['every'] = Segmented(line, [(t('set.hours', n=h), h) for h in EVERY_HOURS],
                               self.edit.every_hours, setter(self.edit, 'every_hours'),
                               self.font(8))
        w['every'].pack(side="left")
        line = tk.Frame(sysp, bg=T["panel"])
        line.pack(anchor="w", pady=(0, px(6)))
        self.label(line, t('set.mode').upper(), 7).pack(side="left", padx=(0, px(10)))
        w['mode'] = Segmented(line, [(t('set.mode.stage'), STAGE), (t('set.mode.notify'), NOTIFY)],
                              self.edit.mode, setter(self.edit, 'mode'), self.font(8))
        w['mode'].pack(side="left")
        w['flatpak'] = Toggle(sysp, t('set.flatpak'), self.edit.flatpak,
                              setter(self.edit, 'flatpak'), self.font(8))
        w['flatpak'].pack(anchor="w", pady=(px(2), px(6)))
        self.label(sysp, t('set.admin_note') if can_system else
                   t('set.uupd') if under_uupd else t('set.no_helper'), 7,
                   T["dim"] if can_system else T["gold"], bold=False,
                   wraplength=px(540)).pack(anchor="w")
        if not can_system:
            for key in ('automatic', 'flatpak'):
                w[key].unbind("<ButtonRelease-1>")
                w[key].config(fg=T["dim"], cursor="arrow")

        p2, user = self.panel(f, t('set.user'))
        p2.pack(fill="x", pady=(px(10), 0))
        w['notify'] = Toggle(user, t('set.notify'), self.edit_prefs.notify,
                             setter(self.edit_prefs, 'notify'), self.font(8))
        w['notify'].pack(anchor="w", pady=(px(8), px(6)))
        line = tk.Frame(user, bg=T["panel"])
        line.pack(anchor="w")
        self.label(line, t('set.remind').upper(), 7).pack(side="left", padx=(0, px(10)))
        w['remind'] = Segmented(line, [(t(f'set.remind.{h}'), h) for h in REMIND_HOURS],
                                self.edit_prefs.remind_hours,
                                setter(self.edit_prefs, 'remind_hours'), self.font(8))
        w['remind'].pack(side="left")

        row = tk.Frame(f, bg=T["bg"])
        row.pack(fill="x", pady=(px(12), 0))
        WolkButton(row, t('btn.save'), self.on_save, self.font(8), 'primary').pack(side="left")
        WolkButton(row, t('btn.back'), lambda: self.show('main'), self.font(8),
                   'ghost').pack(side="left", padx=(px(8), 0))
        self.can_system = can_system

    def on_save(self):
        self.prefs = self.edit_prefs
        save_prefs(self.prefs)
        conf = self.snap.conf if self.snap else load_system(self.env)[0]
        if not self.can_system or as_args(conf) == as_args(self.edit):
            self.say(t('msg.saved'), good=True)
            return
        wanted = self.edit

        def go():
            if self.env.fake:
                write_system(self.env, wanted)
                return 0, ''
            code, out, err = run(['pkexec', HELPER, 'set', *as_args(wanted)], timeout=300)
            return code, (err or out).strip()

        def done(result):
            if isinstance(result, Exception) or result[0] != 0:
                why = result if isinstance(result, Exception) else \
                    result[1] or ("cancelled" if result[0] in (126, 127) else f"exit {result[0]}")
                self.say(t('msg.save_failed', why=why), error=True)
                return
            self.say(t('msg.saved'), good=True)
            self.glitch.now(self.title)
            self.reload()

        self.work(go, done)

    # — the log —
    def _build_log(self, f: tk.Frame):
        px = self.px
        box = tk.Frame(f, bg=T["line"], padx=1, pady=1)
        box.pack(fill="both", expand=True)
        text = tk.Text(box, bg=T["out_bg"], fg=T["text"], font=self.font(8, False),
                       relief="flat", borderwidth=0, padx=px(10), pady=px(8), wrap="word",
                       insertbackground=T["accent"], highlightthickness=0)
        bar = tk.Scrollbar(box, command=text.yview, bg=T["panel"], troughcolor=T["bg"],
                           activebackground=T["hover"], highlightthickness=0, bd=0)
        text.config(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.tag_config('h', foreground=T["accent"], font=self.font(8))
        text.tag_config('cmd', foreground=T["ch_b"])
        text.tag_config('err', foreground=T["pink"])
        for heading, path in ((t('log.system'), self.env.path(SYSTEM_LOG)),
                              (t('log.user'), core.user_log_path())):
            text.insert('end', f"{heading.upper()}  {path}\n", 'h')
            lines = tail(path)
            if not lines:
                text.insert('end', t('log.empty') + "\n\n")
                continue
            for ln in lines:
                tag = 'cmd' if ' $ ' in ln else 'err' if ('ERROR' in ln or 'exit 1' in ln) else ''
                text.insert('end', ln + "\n", tag)
            text.insert('end', "\n")
        if self.snap and self.snap.engine == UUPD and not self.env.fake:
            text.insert('end', f"{t('log.uupd').upper()}  journalctl -u uupd.service "
                               "-u uupd-manual.service\n", 'h')
            code, out, err = run(['journalctl', '--no-pager', '-n', '150', '-o', 'short-iso',
                                  '-u', 'uupd.service', '-u', 'uupd-manual.service'],
                                 timeout=10, quiet=True)
            text.insert('end', (out if code == 0 else err or t('log.empty')).rstrip() + "\n")
        text.config(state="disabled")
        text.see('end')
        self.log_text = text
        row = tk.Frame(f, bg=T["bg"])
        row.pack(fill="x", pady=(px(10), 0))
        WolkButton(row, t('btn.back'), lambda: self.show('main'), self.font(8),
                   'ghost').pack(side="left")

    # — lifetime —
    def close(self):
        try:
            if self.root.state() == 'normal':
                save_json(os.path.join(core.config_dir(), 'window.json'),
                          {'geometry': self.root.geometry()})
        except tk.TclError:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()
