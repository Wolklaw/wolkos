"""Below the interface: folders, settings, the first-login markers, logging, running programs.

Kept light (no Tk, no Pillow) so `spark-welcome --first-login` can decide to exit in a few
milliseconds on every login after the first."""

import getpass
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import APP_NAME, APP_SLUG, VERSION
from .lang import local_units

log = logging.getLogger(APP_SLUG)


# ─── Folders ──────────────────────────────────────────────────

def _xdg_base(variable: str, fallback: str) -> str:
    return os.environ.get(variable) or os.path.join(os.path.expanduser('~'), fallback)


def _xdg_dir(variable: str, fallback: str, create: bool = True) -> str:
    path = os.path.join(_xdg_base(variable, fallback), APP_SLUG)
    if create:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass
    return path


def config_dir(create: bool = True) -> str:
    return _xdg_dir('XDG_CONFIG_HOME', '.config', create)


def state_dir() -> str:
    return _xdg_dir('XDG_STATE_HOME', os.path.join('.local', 'state'))


def cache_dir() -> str:
    return _xdg_dir('XDG_CACHE_HOME', '.cache')


def autostart_file() -> str:
    """The per-user autostart entry install.sh --user writes."""
    from . import APP_ID
    return os.path.join(_xdg_base('XDG_CONFIG_HOME', '.config'), 'autostart', f'{APP_ID}.desktop')


# ─── First login ──────────────────────────────────────────────

DONE = 'done'                    # written when the user reaches the last page or closes
SHOW_AT_LOGIN = 'show-at-login'  # written when the user ticks "Show Spark Welcome at login"


def marker(name: str) -> str:
    return os.path.join(config_dir(create=False), name)


def is_done() -> bool:
    return os.path.exists(marker(DONE))


def show_at_login() -> bool:
    return os.path.exists(marker(SHOW_AT_LOGIN))


def mark_done(show_again: Optional[bool] = None) -> None:
    """Write `done`, and set or clear the show-at-login marker when given."""
    try:
        os.makedirs(config_dir(create=False), exist_ok=True)
        with open(marker(DONE), 'w', encoding='utf-8') as f:
            f.write(f"{APP_NAME} {VERSION}\n")
        if show_again is True:
            open(marker(SHOW_AT_LOGIN), 'w', encoding='utf-8').close()
        elif show_again is False and os.path.exists(marker(SHOW_AT_LOGIN)):
            os.remove(marker(SHOW_AT_LOGIN))
    except OSError as exc:
        log.warning("could not write the done marker: %s", exc)


def live_session() -> bool:
    """The installer ISO's live session has its own welcome dialog."""
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = ''
    return user == 'liveuser' or os.path.exists('/run/livesys')


def skip_first_login() -> Optional[str]:
    """Why `--first-login` should exit at once, or None to open the window."""
    if live_session():
        return "live session"
    if is_done() and not show_at_login():
        return "already done"
    return None


# ─── Settings and logging ─────────────────────────────────────

def setup_logging() -> None:
    log.setLevel(logging.INFO)
    try:
        handler = RotatingFileHandler(os.path.join(state_dir(), f'{APP_SLUG}.log'),
                                      maxBytes=512 * 1024, backupCount=2, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)-7s %(message)s'))
        log.addHandler(handler)
    except OSError:
        log.addHandler(logging.NullHandler())
    log.info("--- %s %s starting (python %s) ---", APP_NAME, VERSION, sys.version.split()[0])


def load_json(name: str) -> Dict[str, Any]:
    try:
        with open(os.path.join(config_dir(), name), encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        log.warning("%s is a %s, not an object - ignoring", name, type(data).__name__)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", name, exc)
    return {}


def save_json(name: str, data: Dict[str, Any]) -> None:
    """Write a temp file and swap it in, so an interrupted save keeps the old one."""
    path = os.path.join(config_dir(), name)
    try:
        with open(path + '.tmp', 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.replace(path + '.tmp', path)
    except (OSError, TypeError, ValueError) as exc:
        log.warning("could not write %s: %s", name, exc)


def reduced_motion() -> bool:
    """True when the desktop asks for no animation (Plasma: animation speed set to instant)."""
    if os.environ.get('SPARK_WELCOME_REDUCED_MOTION', '').lower() in ('1', 'true', 'yes'):
        return True
    try:
        with open(os.path.join(_xdg_base('XDG_CONFIG_HOME', '.config'), 'kdeglobals'),
                  encoding='utf-8', errors='replace') as f:
            match = re.search(r'^AnimationDurationFactor\s*=\s*([0-9.]+)', f.read(), re.M)
        return bool(match) and float(match.group(1)) == 0
    except (OSError, ValueError):
        return False


# ─── Running programs ─────────────────────────────────────────

def run(cmd: Sequence[str], timeout: float = 15) -> Tuple[int, str, str]:
    """Run a program and return (exit code, stdout, stderr). A missing program or a timeout
    is exit code 127 or 124, never an exception."""
    try:
        done = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, errors='replace')
        return done.returncode, done.stdout, done.stderr
    except FileNotFoundError:
        return 127, '', f"{cmd[0]}: not installed"
    except subprocess.TimeoutExpired:
        return 124, '', f"{cmd[0]}: no answer after {timeout:g} s"
    except OSError as exc:
        return 126, '', str(exc)


def launch(cmd: Sequence[str]) -> bool:
    """Start a program and don't wait for it (open a URL, Settings, the Store)."""
    if not shutil.which(cmd[0]):
        log.warning("can't launch %s: not installed", cmd[0])
        return False
    try:
        subprocess.Popen(list(cmd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        log.info("launched %s", ' '.join(cmd))
        return True
    except OSError as exc:
        log.warning("could not launch %s: %s", cmd[0], exc)
        return False


def first_available(*cmds: Sequence[str]) -> Optional[List[str]]:
    for cmd in cmds:
        if shutil.which(cmd[0]):
            return list(cmd)
    return None


def open_url(url: str) -> bool:
    cmd = first_available(['xdg-open', url], ['gio', 'open', url])
    if cmd is None and os.name == 'nt':       # Windows dev runs only
        os.startfile(url)                     # type: ignore[attr-defined]
        return True
    return launch(cmd) if cmd else False


def open_settings(module: Optional[str] = None) -> bool:
    """Plasma's System Settings, on one page when given (kcm_printer_manager...)."""
    if module:
        cmd = first_available(['systemsettings', module], ['kcmshell6', module])
    else:
        cmd = first_available(['systemsettings'])
    return launch(cmd) if cmd else False


STORE_ID = 'io.github.kolunmi.Bazaar'


def open_store() -> bool:
    """The Store (Bazaar), the way sparkos-open-package opens it."""
    for folder in ('/usr/share/applications', '/var/lib/flatpak/exports/share/applications',
                   os.path.expanduser('~/.local/share/flatpak/exports/share/applications')):
        entry = os.path.join(folder, f'{STORE_ID}.desktop')
        if os.path.isfile(entry):
            cmd = first_available(['gio', 'launch', entry], ['gtk-launch', STORE_ID])
            if cmd:
                return launch(cmd)
    return launch(['flatpak', 'run', STORE_ID]) if shutil.which('flatpak') else False


def app_installed(desktop_id: str, command: str = '') -> bool:
    """A desktop entry (or command) another Spark app installs, system-wide or per user."""
    if command and shutil.which(command):
        return True
    folders = [os.path.join(_xdg_base('XDG_DATA_HOME', os.path.join('.local', 'share')),
                            'applications')]
    folders += [os.path.join(d, 'applications') for d in
                (os.environ.get('XDG_DATA_DIRS') or '/usr/local/share:/usr/share').split(':') if d]
    return any(os.path.isfile(os.path.join(f, f'{desktop_id}.desktop')) for f in folders)


def xdg_user_dir(key: str, fallback: str) -> str:
    """~/Documents and friends, as xdg-user-dirs names them (they are translated)."""
    home = os.path.expanduser('~')
    if shutil.which('xdg-user-dir'):
        code, out, _err = run(['xdg-user-dir', key], timeout=3)
        out = out.strip()
        if code == 0 and out and os.path.normpath(out) != os.path.normpath(home):
            return out
    return os.path.join(home, fallback)


def _human_size(n: float) -> str:
    """In the units Windows and Dolphin show (1 KB = 1024 bytes), so a drive reads the same
    size here as in Windows: a 1 TB disk's C: is about 930 GB in both."""
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == 'B' else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def human_size(n: float) -> str:
    return local_units(_human_size(n))
