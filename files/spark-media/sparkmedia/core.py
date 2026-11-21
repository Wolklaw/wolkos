"""Everything below the interface: settings, file kinds, folders, photo loading, thumbnails."""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

from PIL import Image, ImageOps, ImageSequence, PngImagePlugin

from . import APP_NAME, APP_SLUG, MUSIC_EXT, PHOTO_EXT, VERSION, VIDEO_EXT
from . import heif, raw
from .text import tr

log = logging.getLogger(APP_SLUG)

# HEIC/HEIF and AVIF, when their Pillow plugins are installed
for _plugin in ('pillow_heif', 'pillow_avif'):
    try:
        _module = __import__(_plugin)
        if _plugin == 'pillow_heif':
            _module.register_heif_opener()
    except Exception:      # noqa: BLE001 - optional, and a broken plugin must not stop the app
        pass

THUMB_SIZES = {'normal': 128, 'large': 256}
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)    # Windows dev runs only


# ─── Paths, settings, logging ─────────────────────────────────

def _xdg_dir(variable: str, fallback: str) -> str:
    base = os.environ.get(variable) or os.path.join(os.path.expanduser('~'), fallback)
    path = os.path.join(base, APP_SLUG)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def config_dir() -> str:
    return _xdg_dir('XDG_CONFIG_HOME', '.config')


def state_dir() -> str:
    return _xdg_dir('XDG_STATE_HOME', os.path.join('.local', 'state'))


def cache_dir() -> str:
    """~/.cache/spark-media: the music index and album covers."""
    return _xdg_dir('XDG_CACHE_HOME', '.cache')


def thumbnail_root() -> str:
    """The freedesktop thumbnail cache, shared with Dolphin."""
    base = os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache')
    return os.path.join(base, 'thumbnails')


def user_dir(kind: str) -> str:
    """~/Pictures, ~/Videos or ~/Music, as xdg-user-dirs names them (they are translated)."""
    key, fallback = {'photo': ('PICTURES', 'Pictures'), 'video': ('VIDEOS', 'Videos'),
                     'music': ('MUSIC', 'Music')}[kind]
    home = os.path.expanduser('~')
    if shutil.which('xdg-user-dir'):
        try:
            out = subprocess.run(['xdg-user-dir', key], capture_output=True, text=True,
                                 timeout=3).stdout.strip()
            if out and out != home and os.path.isdir(out):
                return out
        except (OSError, subprocess.SubprocessError):
            pass
    path = os.path.join(home, fallback)
    return path if os.path.isdir(path) else home


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
    if os.environ.get('SPARK_MEDIA_REDUCED_MOTION', '').lower() in ('1', 'true', 'yes'):
        return True
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config')
    try:
        with open(os.path.join(base, 'kdeglobals'), encoding='utf-8', errors='replace') as f:
            match = re.search(r'^AnimationDurationFactor\s*=\s*([0-9.]+)', f.read(), re.M)
        return bool(match) and float(match.group(1)) == 0
    except (OSError, ValueError):
        return False


# ─── Files and folders ────────────────────────────────────────

def kind_of(path: str) -> Optional[str]:
    ext = os.path.splitext(path)[1][1:].lower()
    if ext in PHOTO_EXT:
        return 'photo'
    if ext in VIDEO_EXT:
        return 'video'
    if ext in MUSIC_EXT:
        return 'music'
    return None


def path_from_arg(arg: str) -> str:
    """Dolphin passes paths, some apps pass file:// URIs."""
    if arg.startswith('file://'):
        return unquote(urlparse(arg).path)
    return os.path.abspath(os.path.expanduser(arg))


def file_uri(path: str) -> str:
    path = os.path.abspath(path)
    if os.name == 'nt':
        return 'file:///' + quote(path.replace('\\', '/'))
    return 'file://' + quote(path)


def natural_key(name: str) -> Tuple:
    """'Track 2' before 'Track 10', ignoring case."""
    return tuple(int(part) if part.isdigit() else part.casefold()
                 for part in re.split(r'(\d+)', name))


def folder_items(folder: str, kind: Optional[str] = None,
                 hidden: bool = False) -> Tuple[List[str], List[str]]:
    """(subfolders, media files) in natural order. With a kind, only files of that kind."""
    dirs: List[str] = []
    files: List[str] = []
    try:
        entries = list(os.scandir(folder))
    except OSError as exc:
        log.warning("cannot list %s: %s", folder, exc)
        return dirs, files
    for entry in entries:
        if not hidden and entry.name.startswith('.'):
            continue
        try:
            if entry.is_dir():
                dirs.append(entry.path)
            elif entry.is_file():
                k = kind_of(entry.name)
                if k and (kind is None or k == kind):
                    files.append(entry.path)
        except OSError:
            continue
    dirs.sort(key=lambda p: natural_key(os.path.basename(p)))
    files.sort(key=lambda p: natural_key(os.path.basename(p)))
    return dirs, files


def siblings(path: str) -> List[str]:
    """The files of the same kind in the same folder, in order, including path itself."""
    kind = kind_of(path)
    _dirs, files = folder_items(os.path.dirname(os.path.abspath(path)), kind)
    path = os.path.abspath(path)
    if path not in files:
        files.append(path)
        files.sort(key=lambda p: natural_key(os.path.basename(p)))
    return files


def subtitle_for(video: str) -> Optional[str]:
    """A subtitle file next to the video with the same name (movie.srt, movie.en.srt)."""
    stem = os.path.splitext(os.path.abspath(video))[0]
    folder, base = os.path.split(stem)
    try:
        names = sorted(os.listdir(folder), key=natural_key)
    except OSError:
        return None
    for ext in ('.srt', '.ass', '.ssa', '.vtt', '.sub'):
        for name in names:
            low = name.lower()
            if low.endswith(ext) and (low[:-len(ext)] == base.lower()
                                      or low.startswith(base.lower() + '.')):
                return os.path.join(folder, name)
    return None


def folder_cover(path: str) -> Optional[str]:
    folder = os.path.dirname(os.path.abspath(path))
    try:
        names = {name.lower(): name for name in os.listdir(folder)}
    except OSError:
        return None
    for stem in ('cover', 'folder', 'front', 'album', 'albumart'):
        for ext in ('jpg', 'jpeg', 'png', 'webp'):
            name = names.get(f'{stem}.{ext}')
            if name:
                return os.path.join(folder, name)
    return None


def human_size(size: int) -> str:
    value = float(size)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            return f"{value:.0f} {unit}" if unit == 'B' else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def clock(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def move_to_trash(path: str) -> Optional[str]:
    """Move a file to the Trash (recoverable). None on success, otherwise what went wrong."""
    for cmd in (['gio', 'trash', '--', path], ['kioclient', 'move', path, 'trash:/']):
        if shutil.which(cmd[0]):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            except (OSError, subprocess.SubprocessError) as exc:
                return str(exc)
            if result.returncode == 0 and not os.path.exists(path):
                return None
            return (result.stderr or result.stdout).strip() or f"{cmd[0]} failed"
    return "no trash tool found (gio or kioclient)"


def pick_files(kind: Optional[str], start: str) -> List[str]:
    """The desktop's own file dialog (kdialog on Plasma), Tk's as a fallback. Empty if cancelled."""
    exts = {'photo': PHOTO_EXT, 'video': VIDEO_EXT, 'music': MUSIC_EXT | {'m3u', 'm3u8'}}
    chosen = [exts[kind]] if kind else list(exts.values())
    pattern = ' '.join(f'*.{e}' for group in chosen for e in sorted(group))
    if shutil.which('kdialog') and os.name != 'nt':
        try:
            result = subprocess.run(
                ['kdialog', '--getopenfilename', start, f"{pattern}|{tr('dialog.media_files')}",
                 '--multiple', '--separate-output', '--title', APP_NAME],
                capture_output=True, text=True)
            return [line for line in result.stdout.splitlines() if line.strip()]
        except OSError:
            pass
    from tkinter import filedialog
    names = filedialog.askopenfilenames(title=APP_NAME, initialdir=start,
                                        filetypes=[(tr('dialog.media_files'), pattern),
                                                   (tr('dialog.all_files'), "*")])
    return list(names)


def pick_folder(start: str) -> Optional[str]:
    if shutil.which('kdialog') and os.name != 'nt':
        try:
            result = subprocess.run(['kdialog', '--getexistingdirectory', start,
                                     '--title', APP_NAME], capture_output=True, text=True)
            return result.stdout.strip() or None
        except OSError:
            pass
    from tkinter import filedialog
    return filedialog.askdirectory(title=APP_NAME, initialdir=start) or None


def open_externally(path: str) -> Optional[str]:
    """Show a file's folder in the file manager."""
    folder = os.path.dirname(os.path.abspath(path))
    if os.name == 'nt':
        os.startfile(folder)      # type: ignore[attr-defined]
        return None
    opener = shutil.which('xdg-open')
    if not opener:
        return "xdg-open is not installed"
    try:
        subprocess.Popen([opener, folder], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return None
    except OSError as exc:
        return str(exc)


# ─── Photos ───────────────────────────────────────────────────

class PhotoError(Exception):
    pass


class Photo:
    """One opened photo: the upright full image, its frames if animated, and facts about it.
    A camera RAW opens on the JPEG the camera stored inside it; needs_decode says whether the
    sensor data would give a bigger picture (the viewer decodes it in the background)."""

    def __init__(self, path: str):
        self.path = path
        self.exif: Optional[Image.Exif] = None     # kept for saving edits
        self.icc: Optional[bytes] = None
        self.raw = False
        self.needs_decode = False
        self.developed = False                      # a RAW shown from its sensor data
        self.camera: Optional[str] = None
        if raw.is_raw(path):
            self._open_raw(path)
            return
        try:
            source = Image.open(path)
            source.load()
        except FileNotFoundError:
            raise PhotoError(tr('photo.gone')) from None
        except Exception as exc:     # noqa: BLE001 - Pillow raises many kinds
            if heif.is_heif(path):
                self._open_heif(path)                # Pillow has no HEIC reader here
                return
            log.warning("cannot open %s: %s", path, exc)
            raise PhotoError(tr('photo.damaged')) from None
        with source:
            self.format = source.format or os.path.splitext(path)[1][1:].upper()
            self.frames: List[Image.Image] = []
            self.durations: List[int] = []
            if getattr(source, 'is_animated', False) and getattr(source, 'n_frames', 1) > 1:
                for frame in ImageSequence.Iterator(source):
                    self.frames.append(_displayable(frame.copy()))
                    self.durations.append(max(20, int(frame.info.get('duration', 100) or 100)))
                    if len(self.frames) >= 500:       # a huge GIF stays usable
                        break
                self.image = self.frames[0]
            else:
                self.image = _displayable(ImageOps.exif_transpose(source))
            self.taken = _date_taken(source)
            try:
                self.exif = source.getexif()
            except Exception:      # noqa: BLE001
                self.exif = None
            self.icc = source.info.get('icc_profile')
            self.camera = _camera(self.exif)
        self._finish()

    def _finish(self):
        self.size = self.image.size
        try:
            self.bytes = os.path.getsize(self.path)
        except OSError:
            self.bytes = 0

    def _open_raw(self, path: str):
        if not os.path.exists(path):
            raise PhotoError(tr('photo.gone'))
        preview = raw.embedded_preview(path)
        if preview is None:
            # no usable preview: straight to the full decode (slower)
            full = raw.decode(path)
            if full is None:
                raise PhotoError(tr('photo.raw_unreadable'))
            self.image = full
            self.developed = True
        else:
            self.image = raw.upright(preview, raw.orientation(path, preview))
            self.needs_decode = (raw.libraw() is not None and
                                 raw.needs_decode(self.image.size, raw.sensor_size(path)))
        self.raw = True
        self.format = 'RAW ' + os.path.splitext(path)[1][1:].upper()
        self.frames, self.durations = [], []
        self.exif = raw.exif_of(path, preview)
        self.taken = _date_taken_exif(self.exif)
        self.camera = _camera(self.exif)
        self._finish()

    def _open_heif(self, path: str):
        try:
            image, exif, label = heif.read(path)
        except (OSError, heif.HeifError) as exc:
            log.warning("cannot open %s: %s", path, exc)
            why = heif.explain(path) if os.path.exists(path) else 'gone'
            ext = os.path.splitext(path)[1][1:].upper()
            raise PhotoError(tr('photo.gone') if why == 'gone' else
                             tr(f'photo.heif_{why}', ext=ext)) from None
        self.image = _displayable(image)
        self.format = label
        self.frames, self.durations = [], []
        self.exif = exif
        self.taken = _date_taken_exif(exif)
        self.camera = _camera(exif)
        self._finish()

    def use_decoded(self, image: Image.Image):
        """Swap in the full decode of a RAW (from the viewer's worker thread result)."""
        self.image = image
        self.needs_decode = False
        self.developed = True
        self.size = image.size

    @property
    def animated(self) -> bool:
        return len(self.frames) > 1


def _displayable(image: Image.Image) -> Image.Image:
    """RGB or RGBA, whatever the file stored (16-bit, CMYK, palette, ...)."""
    if image.mode in ('RGB', 'RGBA'):
        return image
    if image.mode in ('P', 'PA', 'LA', 'La') or 'transparency' in image.info:
        return image.convert('RGBA')
    if image.mode in ('I;16', 'I;16B', 'I;16L', 'I'):
        return image.point(lambda v: v / 256).convert('RGB')
    return image.convert('RGB')


def _date_taken(image: Image.Image) -> Optional[str]:
    try:
        return _date_taken_exif(image.getexif())
    except Exception:      # noqa: BLE001
        return None


def _camera(exif: Optional[Image.Exif]) -> Optional[str]:
    """'Canon EOS R6' from Make and Model, without the make said twice."""
    if not exif:
        return None
    make = str(exif.get(0x010F) or '').strip(' \x00')
    model = str(exif.get(0x0110) or '').strip(' \x00')
    if model and make and model.lower().startswith(make.split()[0].lower()):
        return model
    return ' '.join(x for x in (make, model) if x) or None


def _date_taken_exif(exif: Optional[Image.Exif]) -> Optional[str]:
    if not exif:
        return None
    try:
        sub = exif.get(0x8769)
        sub = sub if isinstance(sub, dict) else exif.get_ifd(0x8769)
        value = sub.get(0x9003) or exif.get(0x0132)    # DateTimeOriginal, DateTime
    except Exception:      # noqa: BLE001
        return None
    if isinstance(value, str) and re.match(r'\d{4}:\d\d:\d\d \d\d:\d\d', value):
        return value[:10].replace(':', '-') + ' ' + value[11:16]
    return None


def flatten(image: Image.Image, background: str) -> Image.Image:
    """RGBA onto the viewer's background, for Tk."""
    if image.mode != 'RGBA':
        return image
    base = Image.new('RGB', image.size, background)
    base.paste(image, mask=image.getchannel('A'))
    return base


def fit_scale(image_size: Tuple[int, int], box: Tuple[int, int], upscale: bool = False) -> float:
    w, h = image_size
    bw, bh = box
    if w <= 0 or h <= 0 or bw <= 0 or bh <= 0:
        return 1.0
    scale = min(bw / w, bh / h)
    return scale if upscale else min(1.0, scale)


def visible_region(image_size: Tuple[int, int], view: Tuple[int, int], scale: float,
                   center: Tuple[float, float]) -> Tuple[Tuple[int, int, int, int], Tuple[int, int]]:
    """Which part of the image a view shows, and where it lands.

    center is the image point at the middle of the view. Returns (crop box in image pixels,
    top-left of the crop on the view). Only this part gets resized, so zooming into a
    50-megapixel photo costs no more than showing a small one.
    """
    iw, ih = image_size
    vw, vh = view
    left = center[0] - vw / 2 / scale
    top = center[1] - vh / 2 / scale
    x0, y0 = max(0, int(left)), max(0, int(top))
    x1 = min(iw, int(left + vw / scale) + 2)
    y1 = min(ih, int(top + vh / scale) + 2)
    x1, y1 = max(x0 + 1, x1), max(y0 + 1, y1)
    return (x0, y0, x1, y1), (round((x0 - left) * scale), round((y0 - top) * scale))


def clamp_center(image_size: Tuple[int, int], view: Tuple[int, int], scale: float,
                 center: Tuple[float, float]) -> Tuple[float, float]:
    """Keep the image on screen: centred when it is smaller than the view, edges otherwise."""
    out = []
    for size, span, c in ((image_size[0], view[0], center[0]), (image_size[1], view[1], center[1])):
        half = span / 2 / scale
        out.append(size / 2 if size * scale <= span else max(half, min(size - half, c)))
    return out[0], out[1]


# ─── Thumbnails (freedesktop cache, shared with Dolphin) ──────

def thumbnail_path(path: str, flavour: str = 'large') -> str:
    digest = hashlib.md5(file_uri(path).encode('utf-8')).hexdigest()
    return os.path.join(thumbnail_root(), flavour, digest + '.png')


def cached_thumbnail(path: str, flavour: str = 'large') -> Optional[Image.Image]:
    """The cached thumbnail, if one exists and is newer than the file."""
    thumb = thumbnail_path(path, flavour)
    try:
        mtime = int(os.path.getmtime(path))
        with Image.open(thumb) as image:
            stored = image.info.get('Thumb::MTime')
            if stored is not None and int(stored) != mtime:
                return None
            image.load()
            return image.copy()
    except (OSError, ValueError):
        return None


def store_thumbnail(path: str, image: Image.Image, flavour: str = 'large') -> None:
    """Save to the shared cache the way the freedesktop spec asks (URI and mtime inside)."""
    thumb = thumbnail_path(path, flavour)
    try:
        os.makedirs(os.path.dirname(thumb), mode=0o700, exist_ok=True)
        info = PngImagePlugin.PngInfo()
        info.add_text('Thumb::URI', file_uri(path))
        info.add_text('Thumb::MTime', str(int(os.path.getmtime(path))))
        info.add_text('Software', APP_NAME)
        tmp = f'{thumb}.{os.getpid()}.{threading.get_ident()}.tmp'    # threads make them too
        image.save(tmp, 'PNG', pnginfo=info)
        os.chmod(tmp, 0o600)
        os.replace(tmp, thumb)
    except OSError as exc:
        log.debug("could not cache thumbnail for %s: %s", path, exc)


def photo_thumbnail(path: str, flavour: str = 'large') -> Optional[Image.Image]:
    image = cached_thumbnail(path, flavour)
    if image is not None:
        return image
    size = THUMB_SIZES[flavour]
    if raw.is_raw(path):
        preview = raw.embedded_preview(path, max_side=size * 2)
        if preview is None:
            return None
        image = raw.upright(preview, raw.orientation(path, preview))
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
        store_thumbnail(path, image, flavour)
        return image
    try:
        with Image.open(path) as source:
            source.draft('RGB', (size * 2, size * 2))      # JPEG decodes at a fraction of the size
            image = _displayable(ImageOps.exif_transpose(source))
            image.thumbnail((size, size), Image.Resampling.LANCZOS)
    except Exception as exc:      # noqa: BLE001
        if not heif.is_heif(path):
            log.debug("no thumbnail for %s: %s", path, exc)
            return None
        try:
            image = _displayable(heif.read(path, thumbnail=size)[0])   # its stored thumbnail
            image.thumbnail((size, size), Image.Resampling.LANCZOS)
        except (OSError, heif.HeifError) as why:
            log.debug("no thumbnail for %s: %s", path, why)
            return None
    store_thumbnail(path, image, flavour)
    return image


def image_formats() -> Dict[str, bool]:
    """Which photo formats this system opens, for the self-test."""
    from PIL import features
    out = {}
    registered = {ext.lstrip('.').lower() for ext in Image.registered_extensions()}
    for ext in ('jpg', 'png', 'gif', 'webp', 'bmp', 'tiff', 'avif', 'heic'):
        out[ext] = ext in registered
    out['webp'] = out['webp'] and bool(features.check('webp'))
    found = heif.decoders()             # libheif, when Pillow has no plugin for them
    out['avif'] = out['avif'] or found['avif']
    out['heic'] = out['heic'] or found['heic']
    return out
