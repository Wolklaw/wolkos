"""Spark Update tests. Plain unittest:

    python -m unittest discover -s files/spark-update/tests -v

Status parsing runs against fake `rpm-ostree status --json` and `bootc status --json` output in
fixtures/ (no update, staged, pinned, rolled back, layered packages, and what bootc prints in a
container). The background runs use a fake root folder and a fake command runner, so no real
upgrade, Flatpak update, rollback or restart can happen here. UI tests need a display (on CI:
xvfb-run). Real upgrades and rollbacks need a booted SparkOS PC; CI can't do those.
"""

import gc
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
FIX = os.path.join(HERE, 'fixtures')
sys.path.insert(0, APP_DIR)
for var in ('XDG_STATE_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME'):
    os.environ[var] = tempfile.mkdtemp(prefix=f'spark-update-{var.lower()}-')
os.environ['SPARK_UPDATE_REDUCED_MOTION'] = '1'

from sparkupdate import IMAGE, STATE_PATH, VERSION, core, release  # noqa: E402
from sparkupdate import guard, notify, settings, status, updater  # noqa: E402
from sparkupdate.cli import main as cli_main, other_updaters, self_test  # noqa: E402
from sparkupdate.core import Env  # noqa: E402
from sparkupdate.text import t  # noqa: E402

LINUX = sys.platform.startswith('linux')
A_SUM, B_SUM, P_SUM = "a1" * 32, "b2" * 32, "c3" * 32
A_VER, B_VER, P_VER = "45.20261117.1", "45.20261119.0", "45.20261110.0"


def fixture(name):
    with open(os.path.join(FIX, name), encoding='utf-8') as f:
        return json.load(f)


def write(root, rel, text):
    path = os.path.join(root, rel.lstrip('/'))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    return path


def manifest(version, soot, monitor, kernel, extra=()):
    comps = [{'id': 'soot', 'name': 'Soot', 'version': soot},
             {'id': 'freesuite', 'name': 'FreeSuite', 'version': '0.4.1'},
             {'id': 'spark-monitor', 'name': 'Spark Monitor', 'version': monitor}] + list(extra)
    return {'schema': 1, 'name': 'SparkOS', 'image': IMAGE, 'version': version,
            'built': '', 'components': comps,
            'platform': [{'id': 'kernel', 'name': 'Linux kernel', 'version': kernel}]}


def make_root(status_file='rpm-ostree-staged.json', updater_on='uupd'):
    """A fake SparkOS: status, the booted version's release.json, the staged one's, and a
    previous version built before release.json existed (only its files). updater_on: which
    timer is enabled, 'uupd' (as SparkOS ships today), 'spark', or None."""
    root = tempfile.mkdtemp(prefix='spark-update-root-')
    write(root, '/usr/lib/os-release', 'NAME="SparkOS"\nID=bazzite\nOSTREE_VERSION=\'45.20261117.1\'\n')
    write(root, '/status/' + ('rpm-ostree.json' if status_file.startswith('rpm') else 'bootc.json'),
          json.dumps(fixture(status_file)))
    write(root, '/usr/share/sparkos/release.json',
          json.dumps(manifest(A_VER, '0.2.0', '1.0.0', '6.17.7')))
    staged = f'/ostree/deploy/default/deploy/{B_SUM}.0'
    write(root, staged + '/usr/share/sparkos/release.json', json.dumps(manifest(
        B_VER, '0.3.0', '1.0.0', '6.17.8',
        extra=[{'id': 'spark-update', 'name': 'Spark Update', 'version': '1.0.0'}])))
    prev = f'/ostree/deploy/default/deploy/{P_SUM}.0'
    write(root, prev + '/usr/lib/soot/SOOT_RELEASE', 'v0.1.9\n')
    write(root, prev + '/usr/lib/opt/freesuite/resources/app/package.json', '{"version":"0.4.0"}')
    write(root, prev + '/usr/lib/spark-monitor/spark-app.json', '{"name":"Spark Monitor","version":"0.9.0"}')
    write(root, prev + '/usr/lib/spark-snip/sparksnip/__init__.py', 'VERSION = "1.0.0"\n')
    os.makedirs(os.path.join(root, prev.lstrip('/'), 'usr/lib/modules/6.17.5-ba01.fc45.x86_64'))
    os.makedirs(os.path.join(root, 'proc'))
    if updater_on:
        timer = 'uupd.timer' if updater_on == 'uupd' else 'spark-update.timer'
        write(root, f'/etc/systemd/system/timers.target.wants/{timer}', '')
    return root


def add_proc(root, pid, cmdline):
    write(root, f'/proc/{pid}/cmdline', '\0'.join(cmdline) + '\0')


class FakeRunner:
    """Answers the commands the updater runs; records them; can stage a new version by
    switching the fake status file, like a real upgrade would."""

    def __init__(self, root, check_out=('', 0), upgrade_code=0, stage_to=None,
                 flatpak_before='', flatpak_after=None, upgrade_err=''):
        self.root, self.calls = root, []
        self.check_out, self.upgrade_code, self.upgrade_err = check_out, upgrade_code, upgrade_err
        self.stage_to = stage_to
        self.flatpak = flatpak_before
        self.flatpak_after = flatpak_before if flatpak_after is None else flatpak_after

    def __call__(self, cmd, timeout=0, quiet=False, env=None):
        self.calls.append(list(cmd))
        if cmd[1:3] == ['upgrade', '--check']:
            return self.check_out[1], self.check_out[0], ''
        if cmd[1] == 'upgrade':
            if self.stage_to:
                write(self.root, '/status/rpm-ostree.json', json.dumps(fixture(self.stage_to)))
            return self.upgrade_code, '', self.upgrade_err
        if cmd[:2] == ['flatpak', 'list']:
            return 0, self.flatpak, ''
        if cmd[:2] == ['flatpak', 'update']:
            self.flatpak = self.flatpak_after
            return 0, 'Nothing to do.\n', ''
        if cmd[0] == 'rpm-ostree' and cmd[1] in ('rollback', 'cleanup'):
            return 0, '', ''
        return 127, '', 'unexpected command'

    def ran(self, word):
        return [c for c in self.calls if word in ' '.join(c)]


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# ─── Status ─────────────────────────────────────────────────

class StatusTests(unittest.TestCase):
    def test_rpm_ostree_no_update(self):
        host = status.parse_rpm_ostree(fixture('rpm-ostree-none.json'))
        self.assertEqual(host.booted.version, A_VER)
        self.assertEqual(host.booted.image, 'ghcr.io/wolklaw/sparkos:latest')
        self.assertEqual(host.booted.transport, 'ostree-image-signed:docker://')
        self.assertIsNone(host.staged)
        self.assertEqual(host.rollback.version, P_VER)
        self.assertFalse(host.update_ready)
        self.assertFalse(host.restart_changes)
        self.assertIsNone(host.next_boot)

    def test_rpm_ostree_staged(self):
        host = status.parse_rpm_ostree(fixture('rpm-ostree-staged.json'))
        self.assertTrue(host.update_ready)
        self.assertEqual(host.staged.version, B_VER)
        self.assertIs(host.next_boot, host.staged)
        self.assertEqual(host.rollback.version, P_VER)
        self.assertEqual(host.staged.key, 'sha256:' + '2' * 64)
        self.assertEqual(host.staged.timestamp, core.parse_time('2026-11-19T09:30:00Z'))

    def test_staged_same_image_is_not_an_update(self):
        host = status.parse_rpm_ostree(fixture('rpm-ostree-staged-same.json'))
        self.assertIsNotNone(host.staged)
        self.assertFalse(host.update_ready)

    def test_rpm_ostree_pinned(self):
        host = status.parse_rpm_ostree(fixture('rpm-ostree-pinned.json'))
        self.assertEqual(host.rollback.version, P_VER)
        self.assertFalse(host.rollback.pinned)
        self.assertEqual([d.version for d in host.others], ['45.20261101.0'])
        self.assertTrue(host.others[0].pinned)

    def test_rpm_ostree_rollback_queued(self):
        host = status.parse_rpm_ostree(fixture('rpm-ostree-rollback-queued.json'))
        self.assertTrue(host.rollback_queued)
        self.assertEqual(host.next_boot.version, P_VER)
        self.assertTrue(host.restart_changes)
        self.assertFalse(host.update_ready)

    def test_rpm_ostree_layered(self):
        host = status.parse_rpm_ostree(fixture('rpm-ostree-layered.json'))
        self.assertEqual(host.booted.layered, ['htop'])
        self.assertEqual(updater.method_for(host), 'rpm-ostree')

    def test_rpm_ostree_empty(self):
        self.assertIsNone(status.parse_rpm_ostree({}).booted)
        self.assertIsNone(status.parse_rpm_ostree(None).booted)

    def test_bootc(self):
        host = status.parse_bootc(fixture('bootc-staged.json'))
        self.assertEqual((host.booted.version, host.staged.version, host.rollback.version),
                         (A_VER, B_VER, P_VER))
        self.assertTrue(host.update_ready)
        self.assertEqual(host.booted.transport, 'ostree-image-signed:docker://')
        self.assertEqual(host.staged.checksum, B_SUM)
        none = status.parse_bootc(fixture('bootc-none.json'))
        self.assertFalse(none.update_ready)
        queued = status.parse_bootc(fixture('bootc-rollback-queued.json'))
        self.assertTrue(queued.rollback_queued)
        self.assertEqual(queued.next_boot.version, P_VER)

    def test_bootc_in_a_container(self):
        host = status.parse_bootc(fixture('bootc-container.json'))
        self.assertIsNone(host.booted)
        self.assertIn('not booted', host.error)

    def test_both_parsers_agree(self):
        for name in ('none', 'staged', 'rollback-queued'):
            r = status.parse_rpm_ostree(fixture(f'rpm-ostree-{name}.json'))
            b = status.parse_bootc(fixture(f'bootc-{name}.json'))
            self.assertEqual((r.booted.version, r.update_ready, r.rollback_queued,
                              r.rollback.version if r.rollback else None),
                             (b.booted.version, b.update_ready, b.rollback_queued,
                              b.rollback.version if b.rollback else None), name)
            self.assertEqual(r.booted.root(Env('/x')), b.booted.root(Env('/x')))

    def test_image_name(self):
        self.assertEqual(status.image_name('ghcr.io/wolklaw/sparkos:latest'), IMAGE)
        self.assertEqual(status.image_name('ghcr.io/wolklaw/sparkos@sha256:abc'), IMAGE)
        self.assertEqual(status.image_name('localhost:5000/sparkos'), 'localhost:5000/sparkos')
        self.assertEqual(status.split_ref('ostree-unverified-registry:ghcr.io/a/b:1'),
                         ('ostree-unverified-registry:', 'ghcr.io/a/b:1'))

    def test_find_image_name(self):
        root = make_root()
        env = Env(root)
        host = status.read_host(env)
        self.assertEqual(status.find_image_name(env, host)[0], IMAGE)
        os.remove(os.path.join(root, 'status', 'rpm-ostree.json'))
        name, where = status.find_image_name(env, status.read_host(env))
        self.assertEqual((name, where), (IMAGE, '/usr/share/sparkos/release.json'))
        os.remove(os.path.join(root, 'usr/share/sparkos/release.json'))
        write(root, '/etc/containers/policy.json', json.dumps({'transports': {'docker': {
            'ghcr.io/wolklaw/sparkos': [{'type': 'sigstoreSigned'}], 'docker.io': []}}}))
        name, where = status.find_image_name(env)
        self.assertEqual(name, IMAGE)
        self.assertIn('policy.json', where)

    def test_deployment_root(self):
        host = status.parse_rpm_ostree(fixture('rpm-ostree-staged.json'))
        env = Env('/fake')
        self.assertEqual(host.booted.root(env), env.path('/'))
        self.assertTrue(host.staged.root(env).replace('\\', '/').endswith(
            f'/ostree/deploy/default/deploy/{B_SUM}.0'))


# ─── Upgrade output ─────────────────────────────────────────

class CheckOutputTests(unittest.TestCase):
    def test_bootc(self):
        self.assertEqual(updater.parse_check('bootc', 0,
                         "Update available for: docker://ghcr.io/wolklaw/sparkos:latest\n"
                         "  Version: 45.20261119.0\n  Digest: sha256:22\n"),
                         (True, '45.20261119.0'))
        self.assertEqual(updater.parse_check(
            'bootc', 0, "No changes in: docker://ghcr.io/wolklaw/sparkos:latest\n"), (False, ''))
        self.assertEqual(updater.parse_check('bootc', 1, "error: Upgrading: connection refused"),
                         (None, ''))

    def test_rpm_ostree(self):
        self.assertEqual(updater.parse_check(
            'rpm-ostree', 0, "AvailableUpdate:\n        Version: 45.20261119.0 (2026-11-19)\n"
            "         Digest: sha256:22\n"), (True, '45.20261119.0'))
        self.assertEqual(updater.parse_check('rpm-ostree', 77, ''), (False, ''))
        self.assertEqual(updater.parse_check('rpm-ostree', 0, 'No updates available.\n'),
                         (False, ''))
        self.assertEqual(updater.parse_check('rpm-ostree', 1, 'error: Failed to fetch'),
                         (None, ''))


# ─── What's inside a version ─────────────────────────────────

class ReleaseTests(unittest.TestCase):
    def test_collect_from_files(self):
        root = make_root()
        prev = os.path.join(root, f'ostree/deploy/default/deploy/{P_SUM}.0')
        data = release.collect(prev, use_rpm=False)
        comps = {c['id']: c['version'] for c in data['components']}
        self.assertEqual(comps, {'soot': '0.1.9', 'freesuite': '0.4.0', 'spark-monitor': '0.9.0',
                                 'spark-snip': '1.0.0'})
        self.assertEqual([c['name'] for c in data['components']][-1], 'Spark Snip')
        self.assertEqual({p['id']: p['version'] for p in data['platform']}, {'kernel': '6.17.5'})

    def test_load_prefers_the_file(self):
        root = make_root()
        data = release.load(root)
        self.assertTrue(data['from_file'])
        self.assertEqual(data['version'], A_VER)
        prev = release.load(os.path.join(root, f'ostree/deploy/default/deploy/{P_SUM}.0'),
                            use_rpm=False)
        self.assertFalse(prev['from_file'])
        self.assertIsNone(release.load(os.path.join(root, 'nowhere')))

    def test_rpm_version(self):
        self.assertEqual(release.rpm_version('26.2.7\n26.2.7\n'), '26.2.7')    # x86_64 + i686
        self.assertEqual(release.rpm_version('1.19.0^really1.18.4\n'), '1.18.4')
        self.assertEqual(release.rpm_version(''), '')

    def test_diff(self):
        old = manifest(A_VER, '0.2.0', '1.0.0', '6.17.7')
        new = manifest(B_VER, '0.3.0', '1.0.0', '6.17.8',
                       extra=[{'id': 'spark-update', 'name': 'Spark Update', 'version': '1.0.0'}])
        new['components'] = [c for c in new['components'] if c['id'] != 'freesuite']
        kinds = {c.name: (c.kind, c.old, c.new) for c in release.diff(old, new)}
        self.assertEqual(kinds['Soot'], ('changed', '0.2.0', '0.3.0'))
        self.assertEqual(kinds['Spark Monitor'][0], 'same')
        self.assertEqual(kinds['Spark Update'], ('added', '', '1.0.0'))
        self.assertEqual(kinds['FreeSuite'], ('removed', '0.4.1', ''))
        self.assertEqual(kinds[t('platform.kernel')], ('changed', '6.17.7', '6.17.8'))
        self.assertTrue(all(c.kind == 'added' for c in release.diff(None, new)))


# ─── Settings ───────────────────────────────────────────────

class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        s, warnings = settings.parse_conf('')
        self.assertEqual((s.automatic, s.mode, s.every_hours, s.flatpak), (True, 'stage', 6, True))
        self.assertEqual(warnings, [])

    def test_parse(self):
        s, warnings = settings.parse_conf(
            "# comment\n[update]\nautomatic = no\nmode = notify  # only tell me\n"
            "check_every_hours = 12\nflatpak=off\n")
        self.assertEqual((s.automatic, s.mode, s.every_hours, s.flatpak),
                         (False, 'notify', 12, False))
        self.assertEqual(warnings, [])

    def test_bad_values_keep_defaults(self):
        s, warnings = settings.parse_conf("automatic = maybe\nmode = apply\n"
                                          "check_every_hours = 1\ncolour = red\nnonsense\n")
        self.assertEqual((s.automatic, s.mode, s.every_hours), (True, 'stage', 6))
        self.assertEqual(len(warnings), 5)

    def test_round_trip(self):
        s = settings.SystemSettings(False, 'notify', 24, False)
        again, warnings = settings.parse_conf(settings.format_conf(s))
        self.assertEqual(again, s)
        self.assertEqual(warnings, [])

    def test_helper_arguments(self):
        s, errors = settings.parse_assignments(['automatic=no', 'check_every_hours=3'])
        self.assertEqual(errors, [])
        self.assertEqual((s.automatic, s.every_hours, s.mode), (False, 3, 'stage'))
        for bad in (['automatic=yes\nmode=x'], ['colour=red'], ['mode=stage;reboot'],
                    ['check_every_hours=2'], ['mode=apply'], ['automatic = yes']):
            s, errors = settings.parse_assignments(bad)
            self.assertIsNone(s, bad)
            self.assertTrue(errors, bad)
        self.assertEqual(settings.parse_assignments(settings.as_args(
            settings.SystemSettings(False, 'notify', 24, False)))[0],
            settings.SystemSettings(False, 'notify', 24, False))

    def test_write_system(self):
        root = tempfile.mkdtemp()
        env = Env(root)
        settings.write_system(env, settings.SystemSettings(True, 'notify', 3, True))
        s, _ = settings.load_system(env)
        self.assertEqual((s.mode, s.every_hours), ('notify', 3))
        if LINUX:
            self.assertEqual(os.stat(env.path('/etc/sparkos/update.conf')).st_mode & 0o777, 0o644)

    def test_user_prefs(self):
        p = settings.load_prefs()
        self.assertEqual((p.notify, p.remind_hours), (True, 24))
        settings.save_prefs(settings.UserPrefs(False, 72, True))
        self.assertEqual((settings.load_prefs().notify, settings.load_prefs().remind_hours),
                         (False, 72))
        core.save_json(settings.prefs_path(), {'notify': 'yes', 'remind_hours': 1})
        self.assertEqual((settings.load_prefs().notify, settings.load_prefs().remind_hours),
                         (True, 24))
        os.remove(settings.prefs_path())


# ─── The notification ───────────────────────────────────────

class NotifyTests(unittest.TestCase):
    def test_throttle(self):
        now = 1_800_000_000.0
        self.assertTrue(notify.should_notify(now, 'v2', None))
        rec = {'key': 'v2', 'at': now}
        self.assertFalse(notify.should_notify(now + 3600, 'v2', rec))
        self.assertFalse(notify.should_notify(now + 23.9 * 3600, 'v2', rec))
        self.assertTrue(notify.should_notify(now + 24 * 3600, 'v2', rec))
        self.assertTrue(notify.should_notify(now + 60, 'v3', rec))       # a newer version
        self.assertFalse(notify.should_notify(now + 48 * 3600, 'v2', rec, remind_hours=72))
        self.assertTrue(notify.should_notify(now + 72 * 3600, 'v2', rec, remind_hours=72))
        self.assertFalse(notify.should_notify(now + 3 * 3600, 'v2', rec, remind_hours=1))

    def test_message_is_the_mockup(self):
        title, body, actions = notify.message('ready', B_VER, True)
        self.assertEqual(title, t('notify.ready.title'))      # "SparkOS updated"
        self.assertEqual(body, t('notify.ready.body') + ' ' + t('notify.apps_ok'))
        self.assertEqual([a[1] for a in actions], [t('notify.later'), t('notify.restart')])
        _, body, _ = notify.message('ready', B_VER, False)
        self.assertNotIn(t('notify.apps_ok'), body)
        title, body, actions = notify.message('available', B_VER, False)
        self.assertIn(B_VER, body)
        self.assertEqual([a[0] for a in actions], ['later', 'open'])


# ─── Background runs ────────────────────────────────────────

def staged_flatpak():
    return ('org.mozilla.firefox\tc0ffee\ncom.valvesoftware.Steam\tabc123\n',
            'org.mozilla.firefox\tc0ffee\ncom.valvesoftware.Steam\tdef456\n')


class SystemRunTests(unittest.TestCase):
    def setUp(self):
        self.root = make_root('rpm-ostree-none.json')
        self.env = Env(self.root)
        self.clock = Clock()

    def state(self):
        return updater.load_state(self.env)

    def test_stage(self):
        before, after = staged_flatpak()
        run = FakeRunner(self.root, stage_to='rpm-ostree-staged.json', flatpak_before=before,
                         flatpak_after=after)
        self.assertEqual(updater.system_run(self.env, runner=run, clock=self.clock), 0)
        st = self.state()
        self.assertEqual((st['result'], st['staged_version']), ('staged', B_VER))
        self.assertFalse(st['running'])
        self.assertEqual(st['flatpak']['updated'], ['com.valvesoftware.Steam'])
        self.assertTrue(run.ran('upgrade'))
        self.assertEqual(run.ran('upgrade --check'), [])
        self.assertEqual(run.ran('flatpak update --system --noninteractive -y')[0][:2],
                         ['flatpak', 'update'])
        for call in run.calls:      # never restarts, never applies
            self.assertNotIn('reboot', call)
            self.assertNotIn('--apply', call)

    def test_not_due_runs_nothing(self):
        run = FakeRunner(self.root)
        updater.save_state(self.env, {'last_check': self.clock.t - 3600, 'result': 'up_to_date'})
        updater.system_run(self.env, runner=run, clock=self.clock)
        self.assertEqual(run.calls, [])
        self.clock.t += 6 * 3600
        updater.system_run(self.env, runner=run, clock=self.clock)
        self.assertTrue(run.ran('upgrade'))

    def test_up_to_date(self):
        run = FakeRunner(self.root, upgrade_code=77)
        updater.system_run(self.env, runner=run, clock=self.clock)
        self.assertEqual(self.state()['result'], 'up_to_date')

    def test_notify_only_checks(self):
        settings.write_system(self.env, settings.SystemSettings(True, 'notify', 6, True))
        run = FakeRunner(self.root, check_out=("AvailableUpdate:\n  Version: 45.20261119.0\n", 0))
        updater.system_run(self.env, runner=run, clock=self.clock)
        st = self.state()
        self.assertEqual((st['result'], st['available_version']), ('available', B_VER))
        self.assertEqual(len(run.ran('upgrade --check')), 1)
        self.assertEqual(run.ran('flatpak update'), [])          # notify only downloads nothing

    def test_failed_and_retry(self):
        run = FakeRunner(self.root, upgrade_code=1, upgrade_err='error: Fetching: 503\n')
        self.assertEqual(updater.system_run(self.env, runner=run, clock=self.clock), 1)
        st = self.state()
        self.assertEqual((st['result'], st['error']), ('failed', 'error: Fetching: 503'))
        run.calls.clear()
        self.clock.t += 1800
        updater.system_run(self.env, runner=run, clock=self.clock)
        self.assertEqual(run.calls, [])                          # not again within the hour
        self.clock.t += 1800
        updater.system_run(self.env, runner=run, clock=self.clock)
        self.assertTrue(run.ran('upgrade'))

    def test_waits_for_a_game(self):
        add_proc(self.root, 4242, ['/home/me/.steam/ubuntu12_32/reaper', 'SteamLaunch',
                                   'AppId=1086940', '--', 'proton', 'waitforexitandrun'])
        run = FakeRunner(self.root)
        updater.system_run(self.env, runner=run, clock=self.clock)
        self.assertEqual(run.calls, [])
        st = self.state()
        self.assertEqual(st['result'], 'postponed')
        self.assertIn('game', st['reason'])
        # Check now still works while playing (it's your click)
        updater.system_run(self.env, now_mode='check', runner=run, clock=self.clock)
        self.assertTrue(run.ran('upgrade --check'))

    def test_automatic_off(self):
        settings.write_system(self.env, settings.SystemSettings(False, 'stage', 6, True))
        run = FakeRunner(self.root)
        updater.system_run(self.env, runner=run, clock=self.clock)
        self.assertEqual(run.calls, [])
        updater.system_run(self.env, now_mode='stage', runner=run, clock=self.clock,)
        self.assertTrue(run.ran('upgrade'))

    def test_no_status(self):
        os.remove(os.path.join(self.root, 'status', 'rpm-ostree.json'))
        run = FakeRunner(self.root)
        self.assertEqual(updater.system_run(self.env, runner=run, clock=self.clock), 1)
        self.assertEqual(self.state()['result'], 'failed')
        self.assertEqual(run.calls, [])

    def test_rollback(self):
        staged = make_root('rpm-ostree-staged.json')
        run = FakeRunner(staged)
        self.assertEqual(updater.rollback(Env(staged), runner=run), (0, ''))
        self.assertEqual([c[:2] for c in run.calls],
                         [['rpm-ostree', 'cleanup'], ['rpm-ostree', 'rollback']])
        lonely = make_root('rpm-ostree-none.json')
        doc = fixture('rpm-ostree-none.json')
        doc['deployments'] = doc['deployments'][:1]
        write(lonely, '/status/rpm-ostree.json', json.dumps(doc))
        code, why = updater.rollback(Env(lonely), runner=FakeRunner(lonely))
        self.assertEqual(code, 1)
        self.assertEqual(why, t('err.no_previous'))


class UserRunTests(unittest.TestCase):
    """Spark Update's own engine (spark-update.timer on); UupdTests covers uupd."""

    def setUp(self):
        self.root = make_root('rpm-ostree-staged.json', updater_on='spark')
        self.env = Env(self.root)
        self.clock = Clock()
        self.shown = []
        for path in (updater.user_state_path(), settings.prefs_path()):
            if os.path.exists(path):
                os.remove(path)
        updater.save_state(self.env, {'result': 'staged', 'flatpak': {
            'ok': True, 'at': self.clock.t - 600, 'updated': []}})

    def notifier(self, answer='later'):
        def show(offer, apps_ok):
            self.shown.append((offer.kind, offer.version, apps_ok))
            return answer
        return show

    def test_notifies_once_a_day(self):
        run = FakeRunner(self.root)
        updater.user_run(self.env, runner=run, clock=self.clock, notifier=self.notifier())
        self.assertEqual(self.shown, [('ready', B_VER, True)])
        self.assertEqual(len(run.ran('flatpak update --user')), 1)
        self.clock.t += 3 * 3600
        updater.user_run(self.env, runner=run, clock=self.clock, notifier=self.notifier())
        self.assertEqual(len(self.shown), 1)
        self.clock.t += 21 * 3600
        updater.user_run(self.env, runner=run, clock=self.clock, notifier=self.notifier())
        self.assertEqual(len(self.shown), 2)

    def test_never_during_a_game(self):
        add_proc(self.root, 777, ['C:\\Program Files (x86)\\World of Warcraft\\_retail_\\Wow.exe'])
        updater.user_run(self.env, runner=FakeRunner(self.root), clock=self.clock,
                         notifier=self.notifier())
        self.assertEqual(self.shown, [])

    def test_gamescope_session_is_not_a_game(self):
        add_proc(self.root, 778, ['/usr/bin/gamescope-session'])
        self.assertIsNone(guard.game_running(self.env))

    def test_turned_off(self):
        settings.save_prefs(settings.UserPrefs(notify=False))
        updater.user_run(self.env, runner=FakeRunner(self.root), clock=self.clock,
                         notifier=self.notifier())
        self.assertEqual(self.shown, [])

    def test_rolled_back_says_nothing(self):
        write(self.root, '/status/rpm-ostree.json',
              json.dumps(fixture('rpm-ostree-rollback-queued.json')))
        updater.user_run(self.env, runner=FakeRunner(self.root), clock=self.clock,
                         notifier=self.notifier())
        self.assertEqual(self.shown, [])

    def test_notify_only_offer(self):
        write(self.root, '/status/rpm-ostree.json', json.dumps(fixture('rpm-ostree-none.json')))
        updater.save_state(self.env, {'result': 'available', 'available_version': B_VER})
        settings.write_system(self.env, settings.SystemSettings(True, 'notify', 6, True))
        run = FakeRunner(self.root)
        updater.user_run(self.env, runner=run, clock=self.clock, notifier=self.notifier())
        self.assertEqual(self.shown, [('available', B_VER, False)])
        self.assertEqual(run.ran('flatpak update'), [])

    def test_restart_only_on_the_click(self):
        answers = []
        updater.user_run(self.env, runner=FakeRunner(self.root), clock=self.clock,
                         notifier=self.notifier('restart'), on_answer=answers.append)
        self.assertEqual(answers, ['restart'])
        answers.clear()
        self.clock.t += 2 * 86400
        updater.user_run(self.env, runner=FakeRunner(self.root), clock=self.clock,
                         notifier=self.notifier(None), on_answer=answers.append)
        self.assertEqual(answers, [])

    def test_apps_sentence_is_honest(self):
        conf = settings.SystemSettings()
        now = self.clock.t
        ok = {'ok': True, 'at': now - 60}
        self.assertTrue(updater.apps_up_to_date(conf, {'flatpak': ok}, {'flatpak': ok}, now))
        self.assertFalse(updater.apps_up_to_date(conf, {'flatpak': ok}, {}, now))
        self.assertFalse(updater.apps_up_to_date(conf, {'flatpak': ok},
                                                 {'flatpak': {'ok': False, 'at': now}}, now))
        self.assertFalse(updater.apps_up_to_date(conf, {'flatpak': {'ok': True, 'at': now - 3 * 86400}},
                                                 {'flatpak': ok}, now))
        conf.flatpak = False
        self.assertFalse(updater.apps_up_to_date(conf, {'flatpak': ok}, {'flatpak': ok}, now))


def uupd_units(ok=True, ended=None, running=False):
    ended = ended or 1_800_000_000 - 3600
    return {'uupd.service': {'ActiveState': 'activating' if running else 'inactive',
                             'Result': 'success' if ok else 'exit-code',
                             'ExecMainStatus': '0' if ok else '1',
                             'ExecMainExitTimestamp': f'@{int(ended)}'},
            'uupd-manual.service': {'ActiveState': 'inactive', 'Result': 'success',
                                    'ExecMainStatus': '0', 'ExecMainExitTimestamp': ''}}


class UupdTests(unittest.TestCase):
    """SparkOS as it ships today: Bazzite's uupd stages updates, Spark Update notifies."""

    def setUp(self):
        self.root = make_root('rpm-ostree-staged.json')
        self.env = Env(self.root)
        self.clock = Clock()
        self.shown = []
        for path in (updater.user_state_path(), settings.prefs_path()):
            if os.path.exists(path):
                os.remove(path)

    def show(self, offer, apps_ok):
        self.shown.append((offer.kind, offer.version, apps_ok))
        return 'later'

    def test_engine(self):
        from sparkupdate import engine as eng
        self.assertEqual(eng.engine(self.env), eng.UUPD)
        self.assertEqual(eng.engine(Env(make_root(updater_on='spark'))), eng.SPARK)
        self.assertEqual(eng.engine(Env(make_root(updater_on=None))), eng.NONE)

    def test_status_from_systemctl_show(self):
        from sparkupdate import engine as eng
        shown = {'uupd.service': "ActiveState=inactive\nResult=success\nExecMainStatus=0\n"
                                 "ExecMainExitTimestamp=@1799996400\n",
                 'uupd-manual.service': "ActiveState=activating\nResult=success\n"
                                        "ExecMainStatus=0\nExecMainExitTimestamp=\n"}
        calls = []

        def runner(cmd, timeout=0, quiet=False):
            calls.append(cmd)
            return 0, shown[cmd[-1]], ''

        st = eng.uupd_status(Env(None), runner)
        self.assertEqual(st, {'running': True, 'last_run': 1799996400.0, 'ok': True,
                              'unit': 'uupd.service'})
        self.assertIn('--timestamp=unix', calls[0])
        failed = eng.summarize({'uupd.service': eng.parse_show(
            "ActiveState=failed\nResult=exit-code\nExecMainStatus=1\n"
            "ExecMainExitTimestamp=@1799996400\n")})
        self.assertIs(failed['ok'], False)
        self.assertFalse(failed['running'])
        self.assertIsNone(eng.summarize({})['last_run'])

    def test_check_now_starts_uupd_manual(self):
        from sparkupdate import engine as eng
        calls = []
        eng.start_uupd(lambda cmd, timeout=0: calls.append(cmd) or (0, '', ''))
        self.assertEqual(calls, [['systemctl', 'start', '--no-block', 'uupd-manual.service']])

    def test_notifies_without_updating_apps_twice(self):
        write(self.root, '/status/uupd.json', json.dumps(uupd_units(ok=True)))
        run = FakeRunner(self.root)
        updater.user_run(self.env, runner=run, clock=self.clock, notifier=self.show)
        self.assertEqual(self.shown, [('ready', B_VER, True)])
        self.assertEqual(run.ran('flatpak'), [])            # uupd does every user's apps

    def test_apps_sentence_needs_uupd_to_have_worked(self):
        write(self.root, '/status/uupd.json', json.dumps(uupd_units(ok=False)))
        updater.user_run(self.env, runner=FakeRunner(self.root), clock=self.clock,
                         notifier=self.show)
        self.assertEqual(self.shown, [('ready', B_VER, False)])
        os.remove(updater.user_state_path())
        write(self.root, '/status/uupd.json',
              json.dumps(uupd_units(ok=True, ended=self.clock.t - 3 * 86400)))
        self.shown.clear()
        updater.user_run(self.env, runner=FakeRunner(self.root), clock=self.clock,
                         notifier=self.show)
        self.assertEqual(self.shown, [('ready', B_VER, False)])


class GuardTests(unittest.TestCase):
    def test_battery(self):
        root = tempfile.mkdtemp()
        env = Env(root)
        bat = '/sys/class/power_supply/BAT0/'
        write(root, bat + 'type', 'Battery\n')
        write(root, bat + 'status', 'Discharging\n')
        write(root, bat + 'capacity', '12\n')
        mouse = '/sys/class/power_supply/hidpp_battery_0/'
        write(root, mouse + 'type', 'Battery\n')
        write(root, mouse + 'scope', 'Device\n')
        write(root, mouse + 'status', 'Discharging\n')
        write(root, mouse + 'capacity', '5\n')
        self.assertEqual(guard.battery_low(env), 12)
        write(root, bat + 'status', 'Charging\n')
        self.assertIsNone(guard.battery_low(env))
        self.assertIsNone(guard.reason_to_wait(env))
        write(root, '/run/fake-metered', '')
        self.assertIn('metered', guard.reason_to_wait(env))

    def test_games(self):
        root = make_root()
        env = Env(root)
        add_proc(root, 1, ['/usr/lib/systemd/systemd', '--switched-root'])
        add_proc(root, 2, ['/usr/bin/plasmashell'])
        self.assertIsNone(guard.game_running(env))
        add_proc(root, 3, ['/usr/bin/gamescope', '-W', '2560', '--', 'game'])
        self.assertEqual(guard.game_running(env), 'gamescope')

    def test_a_launcher_open_all_day_is_not_a_game(self):
        """Battle.net under Lutris keeps Wine running; only the game itself holds updates."""
        root = make_root()
        env = Env(root)
        add_proc(root, 10, ['/usr/bin/wineserver'])
        add_proc(root, 11, ['C:\\windows\\system32\\services.exe'])
        add_proc(root, 12, ['C:\\windows\\system32\\winedevice.exe'])
        add_proc(root, 13, ['C:\\Program Files (x86)\\Battle.net\\Battle.net.exe',
                            '--type=renderer'])
        add_proc(root, 14, ['C:\\ProgramData\\Battle.net\\Agent\\Agent.9113\\Agent.exe'])
        add_proc(root, 15, ['C:\\Program Files (x86)\\Epic Games\\Launcher\\Portal\\Binaries'
                            '\\Win64\\EpicGamesLauncher.exe'])
        self.assertIsNone(guard.game_running(env))
        add_proc(root, 16, ['C:\\Program Files (x86)\\World of Warcraft\\_retail_\\Wow.exe'])
        self.assertEqual(guard.game_running(env), 'Wow.exe')


# ─── Command line ───────────────────────────────────────────

class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli_main(list(args))
        return code, out.getvalue()

    def test_self_test_on_sparkos(self):
        root = make_root()
        code, out = self.run_cli('--self-test', '--root', root)
        self.assertEqual(code, 0, out)
        self.assertIn(f'image:     {IMAGE} (from rpm-ostree status)', out)
        self.assertIn('self-test passed', out)
        self.assertIn('Soot 0.2.0', out)

    def test_self_test_in_a_container(self):
        root = make_root()
        os.remove(os.path.join(root, 'status', 'rpm-ostree.json'))
        write(root, '/status/bootc.json', json.dumps(fixture('bootc-container.json')))
        os.remove(os.path.join(root, 'usr/share/sparkos/release.json'))
        code, out = self.run_cli('--self-test', '--root', root)
        self.assertEqual(code, 1, out)                  # SparkOS without an image name
        self.assertIn('NOT FOUND', out)
        write(root, '/etc/containers/policy.json', json.dumps({'transports': {'docker': {
            'ghcr.io/wolklaw/sparkos': [{'type': 'sigstoreSigned'}]}}}))
        code, out = self.run_cli('--self-test', '--root', root)
        self.assertEqual(code, 0, out)
        self.assertIn('not booted from an image here', out)
        self.assertIn(f'image:     {IMAGE} (from /etc/containers/policy.json', out)

    def system_install(self, root):
        write(root, '/usr/lib/systemd/system/spark-update.timer', '')
        write(root, '/etc/systemd/user/timers.target.wants/spark-update-user.timer', '')
        write(root, '/usr/libexec/spark-update-helper', '')
        write(root, '/usr/share/polkit-1/actions/dev.wolklaw.SparkUpdate.policy', '')

    def test_self_test_spark_engine_and_a_second_updater(self):
        root = make_root(updater_on='spark')
        self.system_install(root)
        code, out = self.run_cli('--self-test', '--root', root)
        self.assertEqual(code, 0, out)
        self.assertIn('updater:   Spark Update (spark-update.timer enabled; uupd off)', out)
        self.assertIn('no other automatic updater is on', out)
        write(root, '/etc/systemd/system/timers.target.wants/uupd.timer', '')
        write(root, '/etc/rpm-ostreed.conf', '[Daemon]\nAutomaticUpdatePolicy=stage\n')
        self.assertEqual(other_updaters(Env(root)),
                         ['uupd.timer', 'rpm-ostreed AutomaticUpdatePolicy=stage'])
        code, out = self.run_cli('--self-test', '--root', root)
        self.assertEqual(code, 1, out)
        self.assertIn('TWO UPDATERS ON', out)
        self.assertIn('FAILED: two updaters, other updaters', out)

    def test_self_test_uupd_engine(self):
        root = make_root()                      # uupd.timer on, as SparkOS ships today
        self.system_install(root)
        code, out = self.run_cli('--self-test', '--root', root)
        self.assertEqual(code, 0, out)
        self.assertIn("updater:   Universal Blue's uupd (uupd.timer enabled", out)
        self.assertIn('others:    no other automatic updater is on', out)
        os.remove(os.path.join(root, 'etc/systemd/user/timers.target.wants/'
                                     'spark-update-user.timer'))
        code, out = self.run_cli('--self-test', '--root', root)
        self.assertEqual(code, 1, out)
        self.assertIn('FAILED: notification timer', out)

    def test_self_test_fails_with_no_updater(self):
        """Updates must never stop: SparkOS with no updater on fails the build."""
        root = make_root(updater_on=None)
        code, out = self.run_cli('--self-test', '--root', root)
        self.assertEqual(code, 1, out)
        self.assertIn('updater:   NONE', out)
        self.assertIn('FAILED: no updater', out)

    def test_status(self):
        code, out = self.run_cli('--status', '--root', make_root())
        self.assertEqual(code, 0)
        self.assertIn(f'Next:     SparkOS {B_VER} (staged', out)
        self.assertIn(f'Previous: SparkOS {P_VER}', out)

    def test_version(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            cli_main(['--version'])
        self.assertIn(VERSION, out.getvalue())

    @unittest.skipUnless(LINUX, "needs Linux")
    def test_helper_refuses_without_root(self):
        if os.geteuid() == 0:
            self.skipTest("running as root")
        proc = subprocess.run([sys.executable, '-I', os.path.join(APP_DIR, 'spark-update-helper'),
                               'rollback'], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('pkexec', proc.stderr)

    def test_json_log_lines(self):
        import logging
        line = core.JsonLines('%(message)s').format(logging.LogRecord(
            'x', logging.INFO, __file__, 1, "SparkOS %s is staged", ('45.1',), None))
        self.assertEqual(json.loads(line), {'level': 'info',
                                            'description': 'SparkOS 45.1 is staged'})

    def test_units_never_restart(self):
        for name in os.listdir(os.path.join(APP_DIR, 'systemd')):
            with open(os.path.join(APP_DIR, 'systemd', name), encoding='utf-8') as f:
                text = f.read()
            for word in ('reboot', '--apply', 'Restart=always'):
                self.assertNotIn(word, text.split('[Service]')[-1] if '[Service]' in text
                                 else '', name)
            if name.endswith('.service'):
                self.assertIn('Type=oneshot', text)


# ─── The window ─────────────────────────────────────────────

def have_display():
    return bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')) or os.name == 'nt'


@unittest.skipUnless(have_display(), "needs a display")
class UITests(unittest.TestCase):
    def open(self, status_file='rpm-ostree-staged.json', root=None, updater_on='uupd'):
        from sparkupdate.app import App
        self.root_dir = root or make_root(status_file, updater_on)
        started = time.monotonic()
        app = App(Env(self.root_dir))
        def close():
            # destroy and collect here, on the main thread: a Tk image collected later by a
            # worker thread of the next test would crash Tcl
            while app.pending:
                app.root.update()
                time.sleep(0.01)
            app.root.destroy()
            app.__dict__.clear()
            gc.collect()

        self.addCleanup(close)
        app.root.update()
        self.opened = time.monotonic() - started
        deadline = time.monotonic() + 15
        while app.snap is None and time.monotonic() < deadline:
            app.root.update()
            time.sleep(0.02)
        self.assertIsNotNone(app.snap)
        app.root.update()
        return app

    def test_staged(self):
        app = self.open()
        self.assertLess(self.opened, 3.0)
        self.assertIn(t('status.test_data'), app.status.cget('text'))
        self.assertEqual(app.pc_version.cget('text'), f'SparkOS {A_VER}')
        self.assertEqual(app.next_version.cget('text'), f'SparkOS {B_VER}')
        rows = {r[0]: r[1:] for r in app.change_rows}
        self.assertEqual(rows['Soot'], ('0.2.0', '0.3.0', 'changed'))
        self.assertEqual(rows['Spark Update'], ('', '1.0.0', 'added'))
        self.assertEqual(rows[t('platform.kernel')], ('6.17.7', '6.17.8', 'changed'))
        self.assertNotIn('Spark Monitor', rows)                      # unchanged, counted
        self.assertTrue(app.buttons['restart'].winfo_ismapped())
        self.assertEqual(app.buttons['restart'].label.cget('text'), t('btn.restart').upper())

    def test_previous_version_from_files(self):
        app = self.open('rpm-ostree-none.json')
        self.assertFalse(app.buttons['restart'].winfo_ismapped())
        self.assertIn(P_VER, app.changes_note.cget('text'))
        rows = {r[0]: r[1:] for r in app.change_rows}
        self.assertEqual(rows['Soot'], ('0.1.9', '0.2.0', 'changed'))
        self.assertEqual(rows['Spark Monitor'], ('0.9.0', '1.0.0', 'changed'))
        self.assertEqual(rows['Spark Snip'], ('1.0.0', '', 'removed'))

    def test_rollback_queued(self):
        app = self.open('rpm-ostree-rollback-queued.json')
        self.assertEqual(app.buttons['rollback'].label.cget('text'), t('btn.undo_rollback').upper())
        self.assertTrue(app.buttons['restart'].winfo_ismapped())
        self.assertEqual(app.next_version.cget('text'), f'SparkOS {P_VER}')

    def test_actions_on_test_data_do_nothing(self):
        app = self.open()
        for key in ('check', 'rollback', 'restart'):
            app.buttons[key]._click()
            app.root.update()
            self.assertEqual(app.message.cget('text'), t('msg.test_data'))
        self.assertIsNone(app.dialog)

    def test_rollback_dialog(self):
        app = self.open('rpm-ostree-none.json')
        app.env = Env(None)               # pretend it's real; the dialog is all we click
        answers = []
        app.on_rollback = app.on_rollback          # the real one
        orig = app._do_rollback
        app._do_rollback = lambda undo=False: answers.append(('rolled', undo))
        app.on_rollback()
        app.root.update()
        self.assertIsNotNone(app.dialog)
        self.assertIn(t('rollback.body', old='@', date='@', current='@').split('\n\n')[1][:20], ''.join(
            w.cget('text') for w in app.dialog.winfo_children()[0].winfo_children()[0]
            .winfo_children() if isinstance(w, __import__('tkinter').Label)))
        app.dialog.answer('cancel')
        self.assertEqual(answers, [])
        app.on_rollback()
        app.root.update()
        app.dialog.answer('rollback')
        self.assertEqual(answers, [('rolled', False)])
        app._do_rollback = orig

    def test_settings_and_log(self):
        app = self.open(updater_on='spark')
        app.show('settings')
        app.root.update()
        app.set_widgets['every'].set(12, fire=True)
        app.set_widgets['mode'].set('notify', fire=True)
        app.set_widgets['notify'].set(False, fire=True)
        app.on_save()
        deadline = time.monotonic() + 10
        while app.pending and time.monotonic() < deadline:
            app.root.update()
            time.sleep(0.02)
        s, _ = settings.load_system(Env(self.root_dir))
        self.assertEqual((s.every_hours, s.mode), (12, 'notify'))
        self.assertFalse(settings.load_prefs().notify)
        settings.save_prefs(settings.UserPrefs())
        app.show('log')
        app.root.update()
        self.assertIn(t('log.system').upper(), app.log_text.get('1.0', 'end'))
        app.show('main')
        app.root.update()
        self.assertIn(t('check.notify_only'), app.check_line.cget('text'))

    def test_failed_state_shows(self):
        root = make_root('rpm-ostree-none.json', updater_on='spark')
        updater.save_state(Env(root), {'result': 'failed', 'error': 'error: Fetching: 503',
                                       'last_check': time.time() - 60, 'last_run': time.time()})
        app = self.open(root=root)
        self.assertIn('error: Fetching: 503', app.check_line.cget('text'))


    def test_under_uupd(self):
        root = make_root('rpm-ostree-none.json')
        write(root, '/status/uupd.json', json.dumps(uupd_units(ok=True, ended=time.time() - 600)))
        app = self.open(root=root)
        self.assertEqual(app.snap.engine, 'uupd')
        self.assertIn(t('check.uupd'), app.check_line.cget('text'))
        self.assertIn(t('check.last', when=t('when.today', clock='@')).split('@')[0],
                      app.check_line.cget('text'))
        self.assertIn(t('apps.uupd_ok', when='@').split('(')[0], app.apps_line.cget('text'))
        app.show('settings')
        app.root.update()
        self.assertFalse(app.can_system)              # uupd's schedule isn't Spark Update's
        app.show('main')
        write(root, '/status/uupd.json', json.dumps(uupd_units(ok=False, ended=time.time() - 60)))
        app.reload()
        deadline = time.monotonic() + 10
        while app.pending and time.monotonic() < deadline:
            app.root.update()
            time.sleep(0.02)
        self.assertIn(t('check.uupd_failed', when='@').split('(')[0], app.check_line.cget('text'))


if __name__ == '__main__':
    unittest.main()
