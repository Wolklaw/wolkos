"""Screen recording: the desktop's ScreenCast portal hands over a PipeWire stream of one
monitor, and GStreamer crops it to the picked area and encodes it to a file.

The portal is the only way a Wayland program may record the screen. On KDE Plasma it asks
once which screen to share; the answer is remembered per monitor through restore tokens.
"""

import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from . import APP_SLUG
from .core import Box, Monitor, log
from .text import t

PORTAL_BUS = 'org.freedesktop.portal.Desktop'
PORTAL_PATH = '/org/freedesktop/portal/desktop'
SCREENCAST = 'org.freedesktop.portal.ScreenCast'
PORTAL_TIMEOUT = 180            # the user may take a while in the share dialog
FPS = 30
EARLY_EXIT = 2.5                # an encoder that dies this fast is treated as unusable
STOP_TIMEOUT = 15


class RecordError(Exception):
    pass


class RecordCancelled(RecordError):
    pass


class Encoder(NamedTuple):
    element: str                # what gst-inspect checks for
    pipeline: Tuple[str, ...]   # the encoder and its settings
    raw_format: str             # what the encoder wants in
    parser: Optional[str]
    mux: str
    extension: str
    label: str


ENCODERS = (
    # GPU first: the AMD and Intel VA-API encoders cost almost no CPU
    Encoder('vah264enc', ('vah264enc', 'bitrate=12000', 'key-int-max=60'), 'NV12',
            'h264parse', 'mp4mux', 'mp4', 'H.264 (GPU)'),
    Encoder('x264enc', ('x264enc', 'tune=zerolatency', 'speed-preset=veryfast',
                        'bitrate=10000', 'key-int-max=60'), 'I420',
            'h264parse', 'mp4mux', 'mp4', 'H.264'),
    Encoder('openh264enc', ('openh264enc', 'bitrate=10000000', 'complexity=low'), 'I420',
            'h264parse', 'mp4mux', 'mp4', 'H.264 (OpenH264)'),
    Encoder('vp8enc', ('vp8enc', 'deadline=1', 'cpu-used=8', 'target-bitrate=10000000',
                       'keyframe-max-dist=60'), 'I420',
            None, 'webmmux', 'webm', 'VP8'),
)
# the rest of the chain; checked by --self-test, so a broken image build fails loudly
REQUIRED_ELEMENTS = ('videoconvert', 'videoscale', 'videocrop', 'videorate', 'queue',
                     'audiomixer', 'audioconvert', 'audioresample', 'opusenc', 'filesink')
LIVE_ELEMENTS = ('pipewiresrc', 'pulsesrc')


class Crop(NamedTuple):
    frame_w: int                # the monitor, in the pixels the stream is scaled to
    frame_h: int
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.frame_w - self.left - self.width

    @property
    def bottom(self) -> int:
        return self.frame_h - self.top - self.height


def crop_for(region: Box, monitor: Monitor) -> Crop:
    """The picked area, relative to its monitor, with even sides (H.264 needs them)."""
    left = max(0, min(monitor.w - 2, region[0] - monitor.x))
    top = max(0, min(monitor.h - 2, region[1] - monitor.y))
    width = max(2, min(monitor.w - left, region[2] - region[0]) // 2 * 2)
    height = max(2, min(monitor.h - top, region[3] - region[1]) // 2 * 2)
    return Crop(monitor.w, monitor.h, left, top, width, height)


def gst_tool(name: str) -> Optional[str]:
    return shutil.which(name)


def element_exists(element: str) -> bool:
    tool = gst_tool('gst-inspect-1.0')
    if not tool:
        return False
    try:
        return subprocess.run([tool, '--exists', element], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def available_encoders() -> List[Encoder]:
    return [enc for enc in ENCODERS if element_exists(enc.element)
            and (enc.parser is None or element_exists(enc.parser)) and element_exists(enc.mux)]


def build_pipeline(video_source: List[str], crop: Crop, encoder: Encoder,
                   audio_sources: List[List[str]], out_path: str) -> List[str]:
    """gst-launch-1.0 arguments for: source -> scale to monitor size -> crop -> 30 fps ->
    encode -> mux, with any audio sources mixed into one Opus track."""
    tokens = [*video_source, '!', 'videoconvert', '!', 'videoscale', '!',
              f'video/x-raw,width={crop.frame_w},height={crop.frame_h}', '!',
              'videocrop', f'left={crop.left}', f'top={crop.top}',
              f'right={crop.right}', f'bottom={crop.bottom}', '!',
              'videorate', '!', f'video/x-raw,framerate={FPS}/1', '!',
              'videoconvert', '!', f'video/x-raw,format={encoder.raw_format}', '!',
              'queue', 'max-size-buffers=0', 'max-size-bytes=0', 'max-size-time=3000000000', '!',
              *encoder.pipeline]
    if encoder.parser:
        tokens += ['!', encoder.parser]
    tokens += ['!', 'queue', '!', 'mux.']
    if audio_sources:
        tokens += ['audiomixer', 'name=amix', '!', 'audioconvert', '!', 'audioresample', '!',
                   'opusenc', 'bitrate=128000', '!', 'queue', '!', 'mux.']
        for source in audio_sources:
            tokens += [*source, '!', 'audioconvert', '!', 'audioresample', '!',
                       'queue', '!', 'amix.']
    tokens += [encoder.mux, 'name=mux', '!', 'filesink', f'location={out_path}']
    return tokens


def audio_sources(mic: bool, sound: bool) -> List[List[str]]:
    """PipeWire, through its PulseAudio side: the default microphone, and what the
    speakers play (the default output's monitor)."""
    sources = []
    if sound:
        sink = ''
        if shutil.which('pactl'):
            try:
                sink = subprocess.run(['pactl', 'get-default-sink'], capture_output=True,
                                      text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
        sources.append(['pulsesrc', f'device={sink}.monitor' if sink else 'device=@DEFAULT_MONITOR@',
                        'do-timestamp=true'])
    if mic:
        sources.append(['pulsesrc', 'do-timestamp=true'])
    return sources


# ─── Portal ───────────────────────────────────────────────────

class PortalStream(NamedTuple):
    fd: int
    node: int
    props: Dict[str, Any]
    session: str
    restore_token: Optional[str]


class ScreenCastPortal:
    """org.freedesktop.portal.ScreenCast over D-Bus, through PyGObject's Gio.

    Blocking: run start() on a worker thread. Each request answers with a Response signal
    on a Request object, so the thread iterates its own GLib main context while waiting.
    """

    def __init__(self):
        try:
            import gi
            gi.require_version('Gio', '2.0')
            from gi.repository import Gio, GLib
        except (ImportError, ValueError) as exc:
            raise RecordError(t('rec.no_gi')) from exc
        self.Gio, self.GLib = Gio, GLib
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception as exc:                    # noqa: BLE001 - GLib.Error
            raise RecordError(t('rec.no_bus', error=exc)) from exc
        self.sender = self.bus.get_unique_name()[1:].replace('.', '_')
        self.session: Optional[str] = None
        self.cancelled = False

    def _call(self, method: str, params, reply: Optional[str] = None,
              interface: str = SCREENCAST, path: str = PORTAL_PATH):
        GLib = self.GLib
        return self.bus.call_sync(PORTAL_BUS, path, interface, method, params,
                                  GLib.VariantType(reply) if reply else None,
                                  self.Gio.DBusCallFlags.NONE, -1, None)

    def _property(self, name: str, default):
        try:
            value = self._call('Get', self.GLib.Variant('(ss)', (SCREENCAST, name)), '(v)',
                               interface='org.freedesktop.DBus.Properties')
            return value.unpack()[0]
        except Exception:                           # noqa: BLE001 - old portal
            return default

    def _request(self, method: str, build: Callable[[str], Any]) -> Dict[str, Any]:
        GLib, Gio = self.GLib, self.Gio
        token = 'sparksnip_' + secrets.token_hex(6)
        path = f'{PORTAL_PATH}/request/{self.sender}/{token}'
        context = GLib.MainContext.new()
        context.push_thread_default()
        box: Dict[str, Any] = {}

        def on_response(_conn, _sender, _path, _iface, _signal, params, *_):
            box['response'] = params.unpack()

        try:
            sub = self.bus.signal_subscribe(PORTAL_BUS, 'org.freedesktop.portal.Request',
                                            'Response', path, None,
                                            Gio.DBusSignalFlags.NONE, on_response)
            try:
                self._call(method, build(token))
                deadline = time.monotonic() + PORTAL_TIMEOUT
                while 'response' not in box:
                    if self.cancelled:
                        raise RecordCancelled("cancelled")
                    if time.monotonic() > deadline:
                        raise RecordError(t('rec.no_answer'))
                    if not context.iteration(False):
                        time.sleep(0.02)
            finally:
                self.bus.signal_unsubscribe(sub)
        except RecordError:
            raise
        except Exception as exc:                    # noqa: BLE001 - GLib.Error from D-Bus
            raise RecordError(t('rec.portal', error=exc)) from exc
        finally:
            context.pop_thread_default()
        code, results = box['response']
        if code == 1:
            raise RecordCancelled(t('rec.declined'))
        if code != 0:
            raise RecordError(t('rec.refused'))
        return results

    def start(self, restore_token: Optional[str]) -> PortalStream:
        GLib = self.GLib
        version = self._property('version', 1)
        cursors = self._property('AvailableCursorModes', 1)
        log.info("ScreenCast portal version=%s cursor modes=%s", version, cursors)

        def v(kind, value):
            return GLib.Variant(kind, value)

        session_token = 'sparksnip_' + secrets.token_hex(6)
        created = self._request('CreateSession', lambda t: v('(a{sv})', (
            {'handle_token': v('s', t), 'session_handle_token': v('s', session_token)},)))
        self.session = str(created['session_handle'])

        def select(token: str):
            options = {'handle_token': v('s', token), 'types': v('u', 1),       # monitors
                       'multiple': v('b', False)}
            if cursors & 2:
                options['cursor_mode'] = v('u', 2)                            # embedded
            if version >= 4:
                options['persist_mode'] = v('u', 2)                           # until revoked
                if restore_token:
                    options['restore_token'] = v('s', restore_token)
            return v('(oa{sv})', (self.session, options))

        self._request('SelectSources', select)
        started = self._request('Start', lambda t: v('(osa{sv})', (
            self.session, '', {'handle_token': v('s', t)})))
        streams = started.get('streams') or []
        if not streams:
            raise RecordError(t('rec.no_screen'))
        node, props = streams[0]
        result, fds = self.bus.call_with_unix_fd_list_sync(
            PORTAL_BUS, PORTAL_PATH, SCREENCAST, 'OpenPipeWireRemote',
            v('(oa{sv})', (self.session, {})), GLib.VariantType('(h)'),
            self.Gio.DBusCallFlags.NONE, -1, None, None)
        fd = fds.get(result.unpack()[0])
        log.info("portal stream node=%s props=%s", node, props)
        return PortalStream(fd, int(node), dict(props), self.session,
                            started.get('restore_token'))

    def close(self):
        self.cancelled = True
        if self.session:
            try:
                self._call('Close', None, interface='org.freedesktop.portal.Session',
                           path=self.session)
            except Exception:                       # noqa: BLE001 - already gone
                pass
            self.session = None


def stream_matches(monitor: Monitor, props: Dict[str, Any]) -> bool:
    """Did the user share the monitor they picked the area on?

    The portal reports position and size in logical pixels; the X server under XWayland
    may report physical ones, so compare after scaling by the width ratio.
    """
    size, position = props.get('size'), props.get('position')
    if not size or not position or not size[0] or not size[1]:
        return True                               # nothing to compare; trust the user
    scale = monitor.w / size[0]
    if abs(size[1] * scale - monitor.h) > 2:
        return False
    return abs(position[0] * scale - monitor.x) <= 2 and abs(position[1] * scale - monitor.y) <= 2


# ─── A recording ──────────────────────────────────────────────

class Recording:
    """One recording, from the portal request to the finished file.

    Every method runs on the Tk thread except the portal handshake, which runs on a worker
    thread and reports back through a queue that poll() empties.
    """

    def __init__(self, monitor: Monitor, region: Box, out_path_stem: str,
                 mic: bool, sound: bool, restore_token: Optional[str],
                 preferred: Optional[str] = None):
        self.monitor, self.region = monitor, region
        self.crop = crop_for(region, monitor)
        self.stem, self.mic, self.sound = out_path_stem, mic, sound
        self.restore_token = restore_token
        self.portal: Optional[ScreenCastPortal] = None
        self.stream: Optional[PortalStream] = None
        self.process: Optional[subprocess.Popen] = None
        self.encoders = available_encoders()
        if preferred:
            self.encoders.sort(key=lambda e: e.element != preferred)
        self.encoder: Optional[Encoder] = None
        self.path = ''
        self.state = 'idle'       # idle, asking, starting, recording, stopping, done, failed
        self.error = ''
        self.new_token: Optional[str] = None
        self.started_at = 0.0
        self.launched_at = 0.0
        self.duration = 0.0
        self._stderr: List[str] = []
        self._lock = threading.Lock()

    # start
    def begin(self):
        if not gst_tool('gst-launch-1.0'):
            raise RecordError(t('rec.no_gst'))
        if not self.encoders:
            raise RecordError(t('rec.no_encoder'))
        self.portal = ScreenCastPortal()
        self.state = 'asking'
        threading.Thread(target=self._ask, daemon=True).start()

    def _ask(self):
        try:
            stream = self.portal.start(self.restore_token)
        except RecordCancelled as exc:
            self._fail(str(exc), cancelled=True)
            return
        except RecordError as exc:
            self._fail(str(exc))
            return
        with self._lock:
            if self.state != 'asking':          # stopped while the dialog was open
                os.close(stream.fd)
                return
            self.stream = stream
            self.new_token = stream.restore_token
            self.state = 'starting'

    def _fail(self, message: str, cancelled: bool = False):
        with self._lock:
            self.error = message
            self.state = 'cancelled' if cancelled else 'failed'
        log.warning("recording failed: %s", message)
        self._cleanup()

    def _launch(self):
        self.encoder = self.encoders.pop(0)
        self.path = f"{self.stem}.{self.encoder.extension}"
        source = ['pipewiresrc', f'fd={self.stream.fd}', f'path={self.stream.node}',
                  'do-timestamp=true', 'keepalive-time=1000', 'always-copy=true']
        args = [gst_tool('gst-launch-1.0'), '-e', '-q',
                *build_pipeline(source, self.crop, self.encoder,
                                audio_sources(self.mic, self.sound), self.path)]
        log.info("recording with %s: %s", self.encoder.element, ' '.join(args))
        self._stderr = []
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, pass_fds=(self.stream.fd,))
        threading.Thread(target=self._drain, args=(self.process,), daemon=True).start()
        self.launched_at = time.monotonic()
        if not self.started_at:
            self.started_at = self.launched_at
        self.state = 'recording'

    def _drain(self, process: subprocess.Popen):
        for line in process.stderr:
            text = line.decode('utf-8', 'replace').rstrip()
            if text:
                self._stderr = (self._stderr + [text])[-20:]

    # called every few hundred ms from the Tk thread
    def poll(self) -> str:
        with self._lock:
            state = self.state
        if state == 'starting':
            if not stream_matches(self.monitor, self.stream.props):
                self._fail(t('rec.other_screen', screen=self.monitor.name))
                return self.state
            self._launch()
        elif state == 'recording' and self.process.poll() is not None:
            early = time.monotonic() - self.launched_at < EARLY_EXIT
            detail = self._stderr[-1] if self._stderr else f"exit {self.process.returncode}"
            log.warning("gst-launch with %s exited: %s", self.encoder.element, detail)
            self._remove_partial()
            if early and self.encoders:
                self._launch()                     # that encoder can't run here; next one
            else:
                self._fail(t('rec.stopped', detail=detail))
        return self.state

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at if self.started_at else 0.0

    # stop
    def stop(self, done: Callable[[], None]):
        """Ask GStreamer to finish the file (EOS on SIGINT, thanks to -e), then call done
        on a worker thread; the caller polls the state."""
        with self._lock:
            if self.state == 'asking':
                self.state = 'cancelled'
                self.error = 'cancelled'
                if self.portal:
                    self.portal.cancelled = True
                threading.Thread(target=lambda: (self._cleanup(), done()), daemon=True).start()
                return
            if self.state != 'recording':
                threading.Thread(target=done, daemon=True).start()
                return
            self.state = 'stopping'
        self.duration = self.elapsed()

        def finish():
            process = self.process
            try:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                log.warning("gst-launch did not finish in %ss; killing it", STOP_TIMEOUT)
                process.kill()
                process.wait()
            except OSError:
                pass
            ok = os.path.isfile(self.path) and os.path.getsize(self.path) > 0
            with self._lock:
                self.state = 'done' if ok else 'failed'
                if not ok:
                    self.error = self._stderr[-1] if self._stderr else t('rec.empty')
            self._cleanup()
            done()

        threading.Thread(target=finish, daemon=True).start()

    def _remove_partial(self):
        try:
            if self.path and os.path.isfile(self.path):
                os.remove(self.path)
        except OSError:
            pass

    def _cleanup(self):
        if self.portal:
            self.portal.close()
        if self.stream:
            try:
                os.close(self.stream.fd)
            except OSError:
                pass


def pid_file() -> str:
    from .core import runtime_dir
    return os.path.join(runtime_dir(), f'{APP_SLUG}-recording.pid')


def process_start(pid: int, proc: str = '/proc') -> Optional[str]:
    """When a process started (clock ticks since boot), which tells a reused PID apart."""
    try:
        with open(f'{proc}/{pid}/stat', 'rb') as f:
            data = f.read()
        return data[data.rfind(b')') + 2:].split()[19].decode()
    except (OSError, IndexError):
        return None


def pid_file_text(pid: int, proc: str = '/proc') -> str:
    return f"{pid} {process_start(pid, proc) or ''}".strip()


def running_recorder(path: Optional[str] = None, proc: str = '/proc') -> Optional[int]:
    """The pid of a Spark Snip that is recording right now, if any. The file holds the pid
    and its start time: a file left by a crash names a PID that may belong to another program
    by now, and SIGUSR1 would end that program."""
    try:
        with open(path or pid_file(), encoding='ascii') as f:
            fields = f.read().split()
        pid = int(fields[0])
    except (OSError, ValueError, IndexError):
        return None
    if len(fields) < 2 or process_start(pid, proc) != fields[1]:
        return None
    try:
        with open(f'{proc}/{pid}/cmdline', 'rb') as f:
            if b'spark' not in f.read().lower():
                return None
    except OSError:
        return None
    return pid


def self_test_encode(seconds: float = 1.5) -> Dict[str, Any]:
    """Encode a test pattern and a tone with every available encoder, to prove the
    GStreamer chain works on this system. Needs no display or portal."""
    import tempfile
    report: Dict[str, Any] = {'gst_launch': gst_tool('gst-launch-1.0')}
    missing = [e for e in REQUIRED_ELEMENTS if not element_exists(e)]
    report['missing_elements'] = missing
    report['live_elements'] = {e: element_exists(e) for e in LIVE_ELEMENTS}
    report['encoders'] = {}
    if not report['gst_launch']:
        report['passed'] = False
        return report
    frames, buffers = int(seconds * FPS), int(seconds * 48000 / 1024)
    crop = Crop(640, 360, 100, 60, 320, 180)
    for encoder in available_encoders():
        folder = tempfile.mkdtemp(prefix=f'{APP_SLUG}-test-')
        path = os.path.join(folder, f'test.{encoder.extension}')
        args = [gst_tool('gst-launch-1.0'), '-e', '-q', *build_pipeline(
            ['videotestsrc', f'num-buffers={frames}', 'pattern=smpte'], crop, encoder,
            [['audiotestsrc', f'num-buffers={buffers}', 'samplesperbuffer=1024']], path)]
        try:
            proc = subprocess.run(args, capture_output=True, timeout=60)
            ok = proc.returncode == 0 and os.path.isfile(path) and os.path.getsize(path) > 1000
            entry: Dict[str, Any] = {'passed': ok, 'bytes': os.path.getsize(path)
                                     if os.path.isfile(path) else 0}
            if not ok:
                entry['error'] = proc.stderr.decode('utf-8', 'replace').strip()[-400:]
        except (OSError, subprocess.SubprocessError) as exc:
            entry = {'passed': False, 'error': str(exc)}
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        report['encoders'][encoder.element] = entry
    # the GPU encoder can't work in a build container; one software encoder must
    report['passed'] = (not missing and any(entry['passed'] for name, entry in
                                            report['encoders'].items() if name != 'vah264enc'))
    return report
