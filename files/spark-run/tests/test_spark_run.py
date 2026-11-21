"""Spark Run's unit tests. No Wine, display or network needed: Windows files are forged
(forge.py) and Wine is acted out (fakewine.py). The window tests run when there is a display
(CI runs them under Xvfb). Real Wine, real .exe files: tests/ci_wine.sh, in CI.

    cd files/spark-run && python3 -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import fakewine  # noqa: E402
import forge  # noqa: E402
from sparkrun import apps, cli, errors, fetch, lnk, pe, sources, text  # noqa: E402
from sparkrun import wine as winemod  # noqa: E402

try:
    from PIL import Image
except ImportError:
    Image = None

POSIX = os.name == 'posix'
BS = chr(92)


def win(*parts: str) -> str:
    return BS.join(parts)


class Temp(unittest.TestCase):
    """A temporary home: every XDG folder Spark Run uses points inside it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='spark-run-test-')
        self.env = mock.patch.dict(os.environ, {
            'XDG_DATA_HOME': os.path.join(self.tmp, 'data'),
            'XDG_CONFIG_HOME': os.path.join(self.tmp, 'config'),
            'XDG_STATE_HOME': os.path.join(self.tmp, 'state'),
            'XDG_CACHE_HOME': os.path.join(self.tmp, 'cache'),
            'HOME': os.path.join(self.tmp, 'home'),
            'USER': 'tester',
        })
        self.env.start()
        os.makedirs(os.environ['HOME'], exist_ok=True)
        os.environ.pop('SPARK_RUN_WINE', None)
        os.environ.pop('SPARK_RUN_BIN', None)
        os.environ.pop('DBUS_SESSION_BUS_ADDRESS', None)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name: str, data: bytes) -> str:
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
        return path


# ─── Reading .exe files ──────────────────────────────────────

class PETests(Temp):
    def test_program_with_icon_and_version(self):
        path = self.write('app.exe', forge.pe(strings={
            'ProductName': 'Spark Hello', 'CompanyName': 'Wolklaw test',
            'FileDescription': 'Says hello', 'ProductVersion': '1.2.3'}))
        info = pe.inspect(path)
        self.assertEqual(info.kind, 'pe')
        self.assertEqual(info.machine, 'x64')
        self.assertEqual(info.bits, 64)
        self.assertTrue(info.gui)
        self.assertFalse(info.dotnet)
        self.assertEqual(info.product, 'Spark Hello')
        self.assertEqual(info.publisher, 'Wolklaw test')
        self.assertEqual(info.version, '1.2.3')
        self.assertIsNone(info.installer)
        self.assertFalse(info.looks_like_installer)
        ico = info.icon_ico()
        self.assertTrue(ico.startswith(bytes([0, 0, 1, 0, 1, 0])))
        if Image:
            self.assertEqual(Image.open(io.BytesIO(ico)).size, (16, 16))

    def test_32_bit_console(self):
        info = pe.inspect(self.write('c.exe', forge.pe('x86', gui=False, icon=False)))
        self.assertEqual((info.machine, info.bits, info.gui), ('x86', 32, False))
        self.assertIsNone(info.icon_ico())
        self.assertEqual(info.strings, {})
        self.assertIsNone(errors.before_running(info))

    def test_dotnet(self):
        self.assertTrue(pe.inspect(self.write('n.exe', forge.pe(dotnet=True))).dotnet)

    def test_what_cant_run(self):
        arm = pe.inspect(self.write('arm.exe', forge.pe('arm64')))
        self.assertEqual(errors.before_running(arm).key, 'error.arm')
        dos = pe.inspect(self.write('dos.exe', forge.dos_program()))
        self.assertEqual(dos.kind, 'dos')
        self.assertEqual(errors.before_running(dos).key, 'error.dos')
        other = pe.inspect(self.write('readme.exe', b'hello, not a program\n' * 10))
        self.assertEqual(errors.before_running(other).key, 'error.not_windows')
        empty = pe.inspect(self.write('empty.exe', b''))
        self.assertEqual(empty.kind, 'other')
        # a half-downloaded installer: cut off in the middle of its headers
        whole = forge.pe('x64')
        at = struct.unpack_from('<I', whole, 0x3c)[0]
        for cut in (at + 4, at + 10):
            info = pe.inspect(self.write(f'cut{cut}.exe', whole[:cut]))
            self.assertEqual(errors.before_running(info).key, 'error.not_windows')
        for cut in range(at + 24, len(whole), 97):           # no stack trace, wherever it ends
            pe.inspect(self.write(f'cut{cut}.exe', whole[:cut])).icon_ico()

    def test_msi(self):
        info = pe.inspect(self.write('x.msi', pe.MSI_MAGIC + b'\0' * 600))
        self.assertEqual(info.kind, 'msi')
        self.assertTrue(info.looks_like_installer)
        self.assertIsNone(errors.before_running(info))

    def test_installers_are_recognised(self):
        nsis = pe.inspect(self.write('a.exe', forge.pe(
            manifest=b'<assemblyIdentity name="Nullsoft.NSIS.exehead" type="win32"/>')))
        self.assertEqual(nsis.installer, 'NSIS')
        inno = pe.inspect(self.write('b.exe', forge.pe(trailer=b'Inno Setup Setup Data (6.2.0)')))
        self.assertEqual(inno.installer, 'Inno Setup')
        named = pe.inspect(self.write('tool-setup.exe', forge.pe()))
        self.assertIsNone(named.installer)
        self.assertTrue(named.looks_like_installer)

    def test_broken_files_dont_raise(self):
        good = forge.pe(strings={'ProductName': 'X'})
        for cut in (2, 64, 100, 300, 0x420, len(good) - 40):
            pe.inspect(self.write(f'cut{cut}.exe', good[:cut]))
        garbage = bytearray(good)
        garbage[0x400:0x440] = b'\xff' * 0x40           # a nonsense resource directory
        info = pe.inspect(self.write('garbage.exe', bytes(garbage)))
        self.assertEqual(info.kind, 'pe')


# ─── Shortcuts ───────────────────────────────────────────────

class LnkTests(Temp):
    TARGET = win('C:', 'Program Files', 'Fake App', 'fakeapp.exe')

    def test_linkinfo(self):
        s = lnk.parse(forge.lnk(self.TARGET, 'Fake App', '--x "a b"'))
        self.assertEqual(s.target, self.TARGET)
        self.assertEqual(s.arguments, '--x "a b"')

    def test_wine_counts_the_terminator(self):
        s = lnk.parse(forge.lnk(self.TARGET, 'Fake App' + chr(0), '--a' + chr(0)))
        self.assertEqual(s.arguments, '--a')

    def test_id_list_only(self):
        s = lnk.parse(forge.lnk(self.TARGET, idlist_only=True))
        self.assertEqual(s.target, self.TARGET)

    def test_not_a_shortcut(self):
        self.assertIsNone(lnk.parse(b'nope' * 40))
        self.assertIsNone(lnk.read(os.path.join(self.tmp, 'missing.lnk')))

    def test_paths(self):
        prefix = os.path.join(self.tmp, 'pfx')
        exe = self.write(os.path.join('pfx', 'drive_c', 'Program Files', 'Fake App',
                                      'FakeApp.EXE'), b'MZ')
        found = lnk.windows_to_unix(prefix, win('C:', 'PROGRAM FILES', 'fake app',
                                                'fakeapp.exe'))
        self.assertEqual(os.path.normcase(found), os.path.normcase(exe))
        self.assertIsNone(lnk.windows_to_unix(prefix, win('C:', 'nope.exe')))
        self.assertIsNone(lnk.windows_to_unix(prefix, win('D:', 'x.exe')))
        self.assertIsNone(lnk.windows_to_unix(prefix, 'fakeapp.exe'))
        self.assertEqual(lnk.unix_to_windows(prefix, exe),
                         win('C:', 'Program Files', 'Fake App', 'FakeApp.EXE'))
        if POSIX:
            self.assertEqual(lnk.unix_to_windows(prefix, '/tmp/a b.txt'),
                             'Z:' + win('', 'tmp', 'a b.txt'))


# ─── Plain errors ────────────────────────────────────────────

class ErrorTests(unittest.TestCase):
    def test_wine_output(self):
        def key(code, out, mono=True):
            p = errors.after_exit(code, out, mono)
            return p and (p.key, p.values.get('dll'))
        self.assertEqual(key(53, '0024:err:module:import_dll Library MSVCP140.dll (which is '
                                 'needed by L"C:\\x.exe") not found'),
                         ('error.vc_runtime', 'MSVCP140.dll'))
        self.assertEqual(key(53, 'err:module:import_dll Library mscoree.dll (which is ...'),
                         ('error.dotnet', 'mscoree.dll'))
        self.assertEqual(key(53, 'err:module:import_dll Library mscoree.dll (which', False),
                         ('error.no_mono', 'mscoree.dll'))
        self.assertEqual(key(53, 'err:module:import_dll Library foo32.dll (which is ...'),
                         ('error.missing_dll', 'foo32.dll'))
        self.assertEqual(key(1, 'wine: Unhandled page fault on read access to 00000000'),
                         ('error.crashed', None))
        self.assertEqual(key(1, 'wine: Bad EXE format for Z:\\tmp\\x.exe.'),
                         ('error.not_windows', None))
        self.assertIsNone(errors.after_exit(0, '0024:fixme:ntdll:whatever\n'))
        self.assertIsNone(errors.after_exit(None, '0024:fixme:ntdll:whatever\n'))
        self.assertEqual(errors.after_exit(-9, '').key, 'error.killed')


# ─── Downloads ───────────────────────────────────────────────

class FetchTests(Temp):
    def archive(self, members, name='thing-1.0.tar.xz'):
        path = os.path.join(self.tmp, 'src', name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with tarfile.open(path, 'w:xz') as tar:
            for member, data in members:
                info = tarfile.TarInfo(member)
                info.size = len(data)
                info.mode = 0o755
                tar.addfile(info, io.BytesIO(data))
        return path

    def item(self, path, sha=None, size=None, folder='thing-1.0'):
        return sources.Download('thing', 'Thing 1.0', '1.0', pathlib.Path(path).as_uri(),
                                sha or fetch.sha256_of(path), size or os.path.getsize(path),
                                'tests', 'https://example.invalid', folder)

    def test_download_check_unpack(self):
        src = self.archive([('thing-1.0/bin/wine', b'#!/bin/sh\n'), ('thing-1.0/README', b'x')])
        into = os.path.join(self.tmp, 'runtime')
        seen = []
        out = fetch.fetch(self.item(src), into, lambda *a: seen.append(a))
        self.assertEqual(out, os.path.join(into, 'thing-1.0'))
        with open(os.path.join(out, 'bin', 'wine'), 'rb') as f:
            self.assertEqual(f.read(), b'#!/bin/sh\n')
        self.assertTrue(any(s[0] == 'download' for s in seen))
        self.assertTrue(any(s[0] == 'unpack' for s in seen))
        self.assertEqual(os.listdir(os.path.join(self.tmp, 'cache', 'spark-run')), [])
        self.assertEqual(sorted(os.listdir(into)), ['thing-1.0'])     # no temp folder left
        self.assertEqual(fetch.fetch(self.item(src), into), out)        # already there

    def test_wrong_fingerprint_is_deleted(self):
        src = self.archive([('thing-1.0/x', b'x')])
        with self.assertRaises(fetch.FetchError) as caught:
            fetch.fetch(self.item(src, sha='0' * 64), os.path.join(self.tmp, 'runtime'))
        self.assertEqual(caught.exception.kind, 'mismatch')
        self.assertEqual(os.listdir(os.path.join(self.tmp, 'cache', 'spark-run')), [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'runtime', 'thing-1.0')))

    def test_bigger_than_announced(self):
        src = self.archive([('thing-1.0/x', os.urandom(4096))])
        with self.assertRaises(fetch.FetchError) as caught:
            fetch.download(self.item(src, size=100))
        self.assertEqual(caught.exception.kind, 'mismatch')

    def test_unexpected_paths_are_refused(self):
        for members in ([('thing-1.0/ok', b''), ('../evil', b'x')],
                        [('other-folder/x', b'x')]):
            src = self.archive(members)
            into = os.path.join(self.tmp, 'runtime')
            with self.assertRaises(fetch.FetchError) as caught:
                fetch.fetch(self.item(src), into)
            self.assertEqual(caught.exception.kind, 'unpack')
            self.assertFalse(os.path.exists(os.path.join(self.tmp, 'evil')))
            self.assertEqual([n for n in os.listdir(into) if not n.startswith('.')], [])

    def test_no_network(self):
        missing = os.path.join(self.tmp, 'nowhere.tar.xz')
        item = sources.Download('x', 'X', '1', pathlib.Path(missing).as_uri(), '0' * 64, 10,
                                'tests', '', 'x')
        with self.assertRaises(fetch.FetchError) as caught:
            fetch.download(item)
        self.assertEqual(caught.exception.kind, 'network')


class SourcesTests(unittest.TestCase):
    def test_pins(self):
        for item in sources.ALL:
            self.assertRegex(item.sha256, r'^[0-9a-f]{64}$')
            self.assertTrue(item.url.startswith('https://'), item.url)
            self.assertGreater(item.size, 1 << 20)
            self.assertIn(item.version, item.url)
            self.assertTrue(item.filename.endswith(('.tar.xz', '.tar.gz')))
        self.assertIn(sources.WINE.folder, sources.WINE.url)
        self.assertIn('wow64', sources.WINE.url)

    def test_app_json_matches(self):
        with open(os.path.join(ROOT, 'spark-app.json'), encoding='utf-8') as f:
            data = json.load(f)
        listed = data['needs']['downloads_on_first_use'] + \
            data['needs']['downloads_when_turned_on']
        pins = {(d.sha256, d.size) for d in sources.ALL}
        self.assertEqual({(d['sha256'], d['bytes']) for d in listed}, pins)
        from sparkrun import VERSION, APP_ID
        self.assertEqual(data['version'], VERSION)
        self.assertEqual(data['id'], APP_ID)


# ─── Text ────────────────────────────────────────────────────

class TextTests(unittest.TestCase):
    def sources(self):
        folder = os.path.join(ROOT, 'sparkrun')
        for name in sorted(os.listdir(folder)):
            if name.endswith('.py'):
                with open(os.path.join(folder, name), encoding='utf-8') as f:
                    yield name, f.read()

    def test_every_key_exists(self):
        used = set()
        for name, src in self.sources():
            used |= set(re.findall(r"\bt\('([a-z_.0-9]+)'", src))
            used |= set(re.findall(r"Problem\('([a-z_.0-9]+)'", src))
            used |= {'step.' + s for s in re.findall(r"report\('([a-z_]+)'", src)}
            used |= {f'error.fetch_{k}' for k in ('network', 'mismatch', 'space', 'unpack',
                                                   'cancelled')}
        used |= {'win.' + w for w in winemod.WINDOWS_VERSIONS}
        used = {k for k in used if not k.endswith('.') and '.' in k}
        missing = sorted(k for k in used if k not in text.EN)
        self.assertEqual(missing, [])

    def test_french_matches_english(self):
        for key, value in text.FR.items():
            self.assertIn(key, text.EN, key)
            self.assertEqual(set(re.findall(r'\{(\w+)\}', value)),
                             set(re.findall(r'\{(\w+)\}', text.EN[key])), key)
        untranslated = [k for k in text.EN if k not in text.FR and not k.startswith('win.')
                        and k not in ('title', 'opt.dxvk')]
        self.assertEqual(untranslated, [])


# ─── Menu entries ────────────────────────────────────────────

class EntryTests(Temp):
    def test_exec_quoting(self):
        self.assertEqual(apps.exec_quote('/usr/bin/spark-run'), '/usr/bin/spark-run')
        self.assertEqual(apps.exec_quote('/home/a b/spark-run'), '"/home/a b/spark-run"')
        self.assertEqual(apps.exec_quote('50%'), '50%%')
        self.assertEqual(apps.exec_quote('a"b$c'), '"a' + BS * 2 + '"b' + BS * 2 + '$c"')
        self.assertEqual(apps.exec_quote('x' + BS + 'y'), '"x' + BS * 4 + 'y"')

    def test_windows_command_line(self):
        self.assertEqual(apps.win_split('"--profile" "C:' + BS + 'Program Files' + BS + 'x" '
                                        'a' + BS * 2 + '"b c" plain'),
                         ['--profile', 'C:' + BS + 'Program Files' + BS + 'x', 'a' + BS + 'b c',
                          'plain'])
        self.assertEqual(apps.win_split(''), [])
        self.assertEqual(apps.win_split('a  ""  b'), ['a', '', 'b'])

    def test_desktop_files_are_valid(self):
        validator = shutil.which('desktop-file-validate')
        if not validator:
            self.skipTest('desktop-file-validate not installed')
        os.makedirs(os.path.join(self.tmp, 'data', 'spark-run', 'prefixes', 'x'), exist_ok=True)
        app = apps.App(slug='x', name='Thing 2 "Pro"', program=win('C:', 'x.exe'),
                       wm_class='x.exe')
        path = apps.write_entry(app)
        for desktop in (path, os.path.join(ROOT, 'dev.wolklaw.SparkRun.desktop')):
            out = subprocess.run([validator, desktop], capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertNotIn('error', (out.stdout + out.stderr).lower())


# ─── The whole flow, on the fake Wine ────────────────────────

@unittest.skipUnless(POSIX, "the fake Wine is a shell script")
class FlowTests(Temp):
    def setUp(self):
        super().setUp()
        root = fakewine.make(os.path.join(self.tmp, 'fakewine'))
        os.environ['SPARK_RUN_WINE'] = root
        os.environ['FAKEWINE_OUT'] = os.path.join(self.tmp, 'ran.txt')
        os.environ['FAKEWINE_LOG'] = os.path.join(self.tmp, 'wine-calls.txt')
        self.reports = []

    def report(self, stage, **kw):
        self.reports.append(stage)

    def ran(self) -> str:
        try:
            with open(os.environ['FAKEWINE_OUT'], encoding='utf-8') as f:
                return f.read()
        except FileNotFoundError:
            return ''

    def calls(self) -> str:
        with open(os.environ['FAKEWINE_LOG'], encoding='utf-8') as f:
            return f.read()

    def installer(self, name='fake-setup-1.0.exe'):
        return self.write(name, forge.pe(
            manifest=b'<assemblyIdentity name="Nullsoft.NSIS.exehead"/>'))

    def test_install_open_uninstall(self):
        app = apps.install(self.installer(), self.report)
        self.assertEqual(app.slug, 'fake')
        self.assertEqual(app.name, 'Fake App')
        self.assertEqual(app.program, win('C:', 'Program Files', 'Fake App', 'fakeapp.exe'))
        self.assertEqual(app.wm_class, 'fakeapp.exe')
        self.assertEqual([c['name'] for c in app.candidates], ['Fake App'])  # no uninstaller
        self.assertIn('prefix', self.reports)
        self.assertIn('installer', self.reports)
        self.assertEqual(self.reports[-1], 'finishing')
        desktop = os.path.join(app.drive_c, 'users', 'tester', 'Desktop')
        self.assertTrue(os.path.isdir(desktop) and not os.path.islink(desktop))
        self.assertTrue(os.path.isfile(os.path.join(app.prefix, winemod.READY_MARK)))
        if Image:
            self.assertEqual(app.icon, os.path.join(app.prefix, apps.ICON))
            self.assertEqual(Image.open(app.icon).size, (16, 16))
        else:
            self.assertEqual(app.icon, os.path.join(app.prefix, apps.ICON_ICO))
        with open(app.entry, encoding='utf-8') as f:
            entry = f.read()
        self.assertIn('Name=Fake App\n', entry)
        self.assertIn(' --open fake %F\n', entry)
        self.assertIn(f'Icon={app.icon}\n', entry)
        self.assertIn('StartupWMClass=fakeapp.exe\n', entry)
        self.assertEqual([a.slug for a in apps.all_apps()], ['fake'])
        self.assertIn('winemenubuilder.exe=d', self.calls())

        doc = self.write('home/Documents/a b.txt', b'x')
        launch = apps.open_app(apps.load('fake'), [doc])
        self.assertEqual(launch.wait(), 0)
        self.assertIsNone(launch.problem(quick=True))
        self.assertIn('fakeapp.exe Z:' + doc.replace('/', BS), self.ran())

        again = apps.install(self.installer(), self.report)
        self.assertEqual(again.slug, 'fake-2')

        apps.uninstall(app)
        self.assertFalse(os.path.exists(app.prefix))
        self.assertFalse(os.path.exists(app.entry))
        self.assertEqual([a.slug for a in apps.all_apps()], ['fake-2'])

    def test_choosing_among_programs(self):
        offered = []

        def choose(found):
            offered.extend(c.name for c in found)
            return found[1]
        app = apps.install(self.installer('fake-multi-setup.exe'), choose=choose)
        self.assertEqual(sorted(offered), ['Fake App', 'Fake Tool'])
        self.assertEqual(offered[0], 'Fake App')            # on the desktop too: first
        self.assertTrue(app.program.endswith('faketool.exe'))
        self.assertEqual(app.name, 'Fake Tool')
        apps.set_program(app, app.candidates[0]['path'])
        self.assertTrue(apps.load(app.slug).program.endswith('fakeapp.exe'))

    def test_cancel_and_failures_leave_nothing(self):
        cases = (('fake-multi-setup.exe', lambda found: None, 'error.cancelled'),
                 ('fake-empty-setup.exe', None, 'error.nothing_installed'),
                 ('fake-fail-setup.exe', None, 'error.installer_failed'))
        for name, choose, key in cases:
            with self.assertRaises(apps.RunError) as caught:
                apps.install(self.installer(name), choose=choose)
            self.assertEqual(caught.exception.problem.key, key, name)
            self.assertEqual(os.listdir(os.path.join(self.tmp, 'data', 'spark-run',
                                                     'prefixes')), [], name)
            self.assertFalse(os.path.isdir(os.path.join(self.tmp, 'data', 'applications')) and
                             os.listdir(os.path.join(self.tmp, 'data', 'applications')), name)

    def test_program_without_installer_is_copied(self):
        folder = os.path.join(self.tmp, 'dl')
        exe = self.write('dl/tool.exe', forge.pe(strings={'ProductName': 'Handy Tool'}))
        self.write('dl/helper.dll', b'MZ dll')
        self.write('dl/unrelated.txt', b'x')
        app = apps.install(exe)
        self.assertEqual(app.name, 'Handy Tool')
        dest = os.path.join(app.drive_c, 'Program Files', 'Handy Tool')
        self.assertEqual(sorted(os.listdir(dest)), ['helper.dll', 'tool.exe'])
        self.assertNotIn('tool.exe', self.calls().split('wineboot')[-1].split('reg')[0])
        self.assertTrue(os.path.isfile(os.path.join(folder, 'tool.exe')))   # original stays

    def test_msi_install(self):
        msi = self.write('fake-setup.msi', pe.MSI_MAGIC + b'\0' * 600)
        app = apps.install(msi)
        self.assertEqual(app.name, 'Fake App')
        self.assertIn('msiexec /i Z:', self.calls())

    def test_run_once_and_plain_errors(self):
        prog = self.write('hello.exe', forge.pe())
        launch = apps.run_once(prog, ['--hi'], self.report)
        self.assertEqual(launch.wait(), 0)
        self.assertIn(f'ran {prog} --hi', self.ran())
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, 'data', 'spark-run', 'run-once',
                                                   'drive_c')))
        before = self.calls().count('wineboot')
        apps.run_once(prog).wait()
        self.assertEqual(self.calls().count('wineboot'), before)      # prefix reused
        crash = apps.run_once(self.write('fake-crash.exe', forge.pe()))
        self.assertEqual(crash.watch(10).key, 'error.crashed')
        dll = apps.run_once(self.write('fake-dll.exe', forge.pe()))
        self.assertEqual(dll.watch(10).key, 'error.vc_runtime')
        with self.assertRaises(apps.RunError) as caught:
            apps.run_once(self.write('old.exe', forge.dos_program()))
        self.assertEqual(caught.exception.problem.key, 'error.dos')

    def test_options(self):
        app = apps.install(self.installer())
        apps.set_windows(app, 'win7')
        self.assertIn('reg add HKCU' + BS + 'Software' + BS + 'Wine /v Version /t REG_SZ '
                      '/d win7 /f', self.calls())
        self.assertEqual(apps.load(app.slug).windows, 'win7')
        apps.rename(app, 'My App')
        with open(app.entry, encoding='utf-8') as f:
            self.assertIn('Name=My App\n', f.read())
        dxvk = os.path.join(self.tmp, 'dxvk')
        for arch in ('x64', 'x32'):
            self.write(os.path.join('dxvk', arch, 'd3d11.dll'), arch.encode())
            self.write(os.path.join('dxvk', arch, 'dxgi.dll'), arch.encode())
        os.environ['SPARK_RUN_DXVK'] = dxvk
        try:
            with mock.patch.object(winemod, 'dxvk_dir', lambda *a, **k: dxvk):
                apps.set_dxvk(app, True)
            with open(os.path.join(app.drive_c, 'windows', 'syswow64', 'd3d11.dll'), 'rb') as f:
                self.assertEqual(f.read(), b'x32')
            self.assertTrue(apps.load(app.slug).dxvk)
            apps.open_app(apps.load(app.slug)).wait()
            self.assertIn('d3d11=n;dxgi=n', self.calls().splitlines()[-1])
            apps.set_dxvk(app, False)
            apps.open_app(apps.load(app.slug)).wait()
            self.assertIn('d3d11=b;dxgi=b', self.calls().splitlines()[-1])
        finally:
            os.environ.pop('SPARK_RUN_DXVK', None)

    def test_command_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(['--install', self.installer(), '--no-window',
                                       '--quiet']), 0)
        self.assertIn('"name": "Fake App"', out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(['--list', '--json']), 0)
        self.assertEqual([a['slug'] for a in json.loads(out.getvalue())], ['fake'])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--open', 'fake', '--wait', '--no-window']), 0)
            self.assertEqual(cli.main(['--set', 'fake', '--windows', 'win81']), 0)
        self.assertEqual(apps.load('fake').windows, 'win81')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(['--open', 'nope']), 2)
            self.assertEqual(cli.main(['--run-once', self.write('fake-dll.exe', forge.pe()),
                                       '--wait', '--no-window']), 1)
        self.assertIn("Visual C++", err.getvalue())
        self.assertIn('switching.md', err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--uninstall', 'fake', '--quiet']), 0)
        self.assertEqual(apps.all_apps(), [])

    def test_self_test(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.self_test()
        report = out.getvalue()
        self.assertIn('wine says: wine-11.22 (fake)', report)
        self.assertEqual(report.count('pinned:'), 3)
        needed_missing = [s for s, _, needed in winemod.missing_libs() if needed]
        self.assertEqual(code, 1 if needed_missing else 0, report)


# ─── The window (needs a display) ────────────────────────────

@unittest.skipUnless(POSIX and os.environ.get('DISPLAY'), "needs a display (Xvfb in CI)")
class WindowTests(FlowTests):
    def setUp(self):
        super().setUp()
        os.environ['SPARK_RUN_REDUCED_MOTION'] = '1'
        from sparkrun import ui
        self.ui = ui
        self.errors = []

    def window(self, **kw):
        w = self.ui.Window(**kw)
        w.root.report_callback_exception = lambda *exc: self.errors.append(exc)
        def close():
            try:
                w.root.destroy()
            except Exception:    # noqa: BLE001 - the test closed it already
                pass
        self.addCleanup(close)
        self.pump(w)
        return w

    def pump(self, w, until=None, timeout=30.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            w.root.update()
            if until is None or until():
                return
            time.sleep(0.02)
        self.fail("the window didn't get there in time")

    def texts(self, widget):
        found = []
        for child in widget.winfo_children():
            try:
                found.append(str(child.cget('text')))
            except Exception:    # noqa: BLE001 - not every widget has text
                pass
            found.extend(self.texts(child))
        return found

    def test_list_empty_then_one(self):
        w = self.window()
        self.assertIn(text.t('apps.empty'), self.texts(w.view))
        w.root.destroy()
        apps.install(self.installer())
        w = self.window()
        self.assertEqual(w.selected, 'fake')
        self.assertIn('Fake App', self.texts(w.view))
        self.pump(w, lambda: getattr(w.apps[0], 'used', None) is not None)
        OptionsDialog = self.ui.OptionsDialog(w, w.apps[0])
        self.pump(w)
        OptionsDialog.cancel()
        confirm = self.ui.ConfirmDialog(w, w.apps[0])
        self.pump(w)
        confirm.remove(w.apps[0])
        self.pump(w, lambda: not w.worker.busy and not apps.all_apps())
        self.assertIn(text.t('rm.done', name='Fake App'), self.texts(w.view))
        self.assertEqual(self.errors, [])

    def test_chooser_installs(self):
        w = self.window(file=self.installer())
        shown = self.texts(w.view)
        self.assertIn(text.t('file.kind.installer', maker='NSIS') + ' · ' +
                      text.t('file.bits', bits=64), shown)
        w.do_install(self.installer(), pe.inspect(self.installer()))
        self.pump(w, lambda: not w.worker.busy)
        self.assertIn(text.t('done.installed', name='Fake App'), self.texts(w.view))
        self.assertEqual([a.slug for a in apps.all_apps()], ['fake'])
        self.assertEqual(self.errors, [])

    def test_text_fits(self):
        """Every view at the smallest window size, in the language the suite runs in."""
        from langcheck import clipped
        w = self.window()
        w.root.geometry('%dx%d' % w.root.minsize())
        self.pump(w)
        self.assertEqual(clipped(w.root), [], "empty list")
        w.root.destroy()
        apps.install(self.installer())
        w = self.window()
        w.root.geometry('%dx%d' % w.root.minsize())
        self.pump(w)
        self.assertEqual(clipped(w.root), [], "one app")
        self.pump(w, lambda: getattr(w.apps[0], 'used', None) is not None)
        for make in (self.ui.OptionsDialog, self.ui.ConfirmDialog):
            dialog = make(w, w.apps[0])
            self.pump(w)
            self.assertEqual(clipped(w.root), [], make.__name__)
            dialog.cancel()
            self.pump(w)
        w.root.destroy()
        w = self.window(file=self.installer())
        w.root.geometry('%dx%d' % w.root.minsize())
        self.pump(w)
        self.assertEqual(clipped(w.root), [], "run once or install")
        w.root.destroy()
        w = self.window(file=self.write('old.exe', forge.dos_program()))
        w.root.geometry('%dx%d' % w.root.minsize())
        self.pump(w)
        self.assertEqual(clipped(w.root), [], "error")
        self.assertEqual(self.errors, [])

    def test_chooser_explains_what_cant_run(self):
        w = self.window(file=self.write('old.exe', forge.dos_program()))
        self.assertIn(text.t('error.dos'), self.texts(w.view))
        self.assertEqual(w.exit_code, 1)
        self.assertEqual(self.errors, [])

    def test_options_save(self):
        apps.install(self.installer())
        w = self.window()
        dialog = self.ui.OptionsDialog(w, w.apps[0])
        dialog.name.delete(0, 'end')
        dialog.name.insert(0, 'Renamed')
        dialog.windows = 'win7'
        dialog.save()
        self.pump(w, lambda: not w.worker.busy)
        self.assertEqual(apps.load('fake').name, 'Renamed')
        self.assertEqual(apps.load('fake').windows, 'win7')
        self.assertEqual(self.errors, [])

    # the flow tests are FlowTests' job; don't run them twice
    test_install_open_uninstall = test_choosing_among_programs = None
    test_cancel_and_failures_leave_nothing = test_program_without_installer_is_copied = None
    test_msi_install = test_run_once_and_plain_errors = test_options = None
    test_command_line = test_self_test = None


if __name__ == '__main__':
    unittest.main()
