"""When not to update or notify: a game is running, the connection is metered, or the
battery is low. Cheap reads of /proc, /sys and one D-Bus property; nothing stays running."""

import os
import re
import subprocess
from typing import Any, Dict, Optional, Tuple

from .core import Env
from .text import EN

# A process whose command line holds one of these is a game: Steam starts every game under
# 'reaper SteamLaunch'; gamescope is the game compositor.
GAME_MARKERS = (b'SteamLaunch', b'/gamescope')
GAME_NAMES = {b'gamescope', b'gamescope-wl'}
# A Windows program under Wine or Proton (Lutris, Heroic, Bottles) shows its Windows path as
# its name: C:\...\Wow.exe. It's a game unless it is one of Wine's own programs (in
# C:\windows) or a launcher that sits open all day (Battle.net, Epic, GOG Galaxy, EA,
# Ubisoft Connect, Steam for Windows) or one of their helpers. wineserver alone is not a game:
# it runs as long as any of those launchers do, which would hold updates back for days.
NOT_GAMES = (b'battle.net', b'agent.exe', b'blizzard', b'launcher', b'helper', b'crash',
             b'updat', b'service', b'browser', b'galaxy', b'eadesktop', b'eabackground',
             b'origin', b'upc.exe', b'ubisoftconnect', b'uplay', b'steam.exe', b'socialclub',
             b'setup', b'install', b'unins', b'winecfg', b'regedit', b'notepad')


def windows_game(argv0: bytes) -> Optional[bytes]:
    """The .exe's name when argv0 is a Windows game under Wine, else None."""
    path = argv0.replace(b'\\', b'/').lower()
    if not path.endswith(b'.exe') or b'/windows/' in path:
        return None
    name = path.rsplit(b'/', 1)[-1]
    if any(word in name for word in NOT_GAMES):
        return None
    return argv0.replace(b'\\', b'/').rsplit(b'/', 1)[-1]


def game_running(env: Env) -> Optional[str]:
    """The name of a running game process, or None."""
    proc = env.path('/proc')
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return None
    for pid in pids:
        try:
            with open(os.path.join(proc, pid, 'cmdline'), 'rb') as f:
                cmd = f.read(4096)
        except OSError:
            continue
        if not cmd:
            continue
        first = cmd.split(b'\0', 1)[0]
        argv0 = os.path.basename(first)
        if argv0 in GAME_NAMES or any(m in cmd for m in GAME_MARKERS):
            if argv0 == b'gamescope-session' or b'gamescope-session' in argv0:
                continue          # the Steam Deck style session itself is not a game
            return argv0.decode(errors='replace') or pid
        exe = windows_game(first)
        if exe:
            return exe.decode(errors='replace')
    return None


def battery_low(env: Env, minimum: int = 20) -> Optional[int]:
    """The battery's charge when it is discharging below minimum %, else None."""
    base = env.path('/sys/class/power_supply')
    try:
        names = os.listdir(base)
    except OSError:
        return None
    for name in names:
        folder = os.path.join(base, name)

        def read(key):
            try:
                with open(os.path.join(folder, key), encoding='ascii', errors='replace') as f:
                    return f.read().strip()
            except OSError:
                return ''

        if read('type') != 'Battery' or read('scope') == 'Device':
            continue          # mice and controllers report batteries too
        if read('status') != 'Discharging':
            continue
        cap = read('capacity')
        if cap.isdigit() and int(cap) < minimum:
            return int(cap)
    return None


def metered(env: Env) -> bool:
    """NetworkManager says the connection is metered (1 = yes, 3 = guessed yes)."""
    if env.fake:
        return os.path.exists(env.path('/run/fake-metered'))
    try:
        proc = subprocess.run(
            ['gdbus', 'call', '--system', '--dest', 'org.freedesktop.NetworkManager',
             '--object-path', '/org/freedesktop/NetworkManager', '--method',
             'org.freedesktop.DBus.Properties.Get', 'org.freedesktop.NetworkManager',
             'Metered'], capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    match = re.search(r'uint32 (\d+)', proc.stdout)
    return bool(match) and int(match.group(1)) in (1, 3)


def wait_reason(env: Env) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Why a background run should wait for a later hour, as a sentence in text.py and its
    blanks ('wait.game', {'game': 'Hades'}), or None to go ahead."""
    game = game_running(env)
    if game:
        return 'wait.game', {'game': game}
    if metered(env):
        return 'wait.metered', {}
    low = battery_low(env)
    if low is not None:
        return 'wait.battery', {'percent': low}
    return None


def reason_to_wait(env: Env) -> Optional[str]:
    """wait_reason() in English, for the log and state.json. The window shows the sentence in
    its own language from the key saved next to it."""
    why = wait_reason(env)
    return EN[why[0]].format(**why[1]) if why else None
