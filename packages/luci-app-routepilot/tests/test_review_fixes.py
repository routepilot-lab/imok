# Synthetic unit-test addresses; no live router, tenant or user configuration.
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'root/usr/lib/routepilot'))
import masque


class RuleGenerationTests(unittest.TestCase):
    def test_failed_write_never_publishes_partial_rules(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(masque, 'RULES', Path(folder)):
            rules = Path(folder)
            (rules / 'china.txt').write_text('old china')
            (rules / 'gfw.txt').write_text('old gfw')
            real_atomic = masque.atomic
            def failing(path, text, *args):
                if path.name == 'gfw.txt' and text == 'new gfw':
                    raise OSError('synthetic write failure')
                real_atomic(path, text, *args)
            with patch.object(masque, 'atomic', side_effect=failing):
                with self.assertRaises(OSError):
                    masque.commit_rules([(rules / 'china.txt', 'new china'), (rules / 'gfw.txt', 'new gfw')], {})
            with masque.rule_snapshot() as snapshot:
                self.assertEqual((snapshot / 'china.txt').read_text(), 'old china')
                self.assertEqual((snapshot / 'gfw.txt').read_text(), 'old gfw')
            self.assertFalse((rules / 'current').exists())
            self.assertEqual(list((rules / '.generations').iterdir()), [])

    def test_complete_switch_preserves_unspecified_rules_and_previous_generation(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(masque, 'RULES', Path(folder)):
            rules = Path(folder)
            (rules / 'gfw.txt').write_text('retained.example\n')
            snapshots = []
            for i in range(4):
                meta = masque.commit_rules([(rules / 'china.txt', str(i) + '\n')], {'time': i})
                with masque.rule_snapshot() as snapshot:
                    snapshots.append(snapshot)
                    self.assertEqual((snapshot / 'china.txt').read_text(), str(i) + '\n')
                    self.assertEqual((snapshot / 'gfw.txt').read_text(), 'retained.example\n')
                    self.assertEqual(json.loads((snapshot / 'updated.json').read_text()), meta)
            self.assertEqual({p.resolve() for p in (rules / '.generations').iterdir()}, set(snapshots[-2:]))

    def test_publish_failure_leaves_previous_committed_generation(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(masque, 'RULES', Path(folder)):
            rules = Path(folder)
            masque.commit_rules([(rules / 'china.txt', 'old')], {})
            real_replace = masque.os.replace
            def replace(src, dst):
                if Path(dst).name == 'current':
                    raise OSError('synthetic publish failure')
                return real_replace(src, dst)
            with patch.object(masque.os, 'replace', side_effect=replace):
                with self.assertRaises(OSError):
                    masque.commit_rules([(rules / 'china.txt', 'new')], {})
            with masque.rule_snapshot() as snapshot:
                self.assertEqual((snapshot / 'china.txt').read_text(), 'old')
            self.assertEqual(len(list((rules / '.generations').iterdir())), 1)

    def test_reader_pins_generation_until_compilation_finishes(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(masque, 'RULES', Path(folder)):
            rules = Path(folder)
            masque.commit_rules([(rules / 'china.txt', 'old')], {})
            started, done = threading.Event(), threading.Event()
            errors = []
            def publish():
                started.set()
                try:
                    masque.commit_rules([(rules / 'china.txt', 'new')], {})
                except Exception as exc:
                    errors.append(exc)
                finally:
                    done.set()
            with masque.rule_snapshot() as snapshot:
                worker = threading.Thread(target=publish)
                worker.start()
                self.assertTrue(started.wait(1))
                self.assertFalse(done.wait(0.05))
                self.assertEqual((snapshot / 'china.txt').read_text(), 'old')
            worker.join(2)
            self.assertTrue(done.is_set())
            self.assertEqual(errors, [])
            with masque.rule_snapshot() as snapshot:
                self.assertEqual((snapshot / 'china.txt').read_text(), 'new')

    def test_fault_keeps_legacy_service_paused_but_manual_stop_restores_it(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(masque, 'RUN', Path(folder)), patch.object(masque, 'command') as command, patch.object(masque, 'uci_value', return_value='1'):
            marker = Path(folder) / 'phone-paused'; marker.write_text('paused')
            service = masque.Service(); service.plane = Mock(); service.dns = Mock()
            service.stop(restore=False)
            self.assertTrue(marker.exists())
            command.assert_not_called()
            service.stop()
            self.assertFalse(marker.exists())
            command.assert_called_once_with(['/etc/init.d/warp-phone', 'start'])

    def test_cleanup_failure_keeps_journal_and_does_not_claim_direct(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(masque, 'RUN', Path(folder)):
            marker = Path(folder) / 'active-owner'; marker.write_text('owned')
            def command(args, **kw):
                if args[:4] == ['nft', '-j', 'list', 'tables']:
                    return SimpleNamespace(stdout=json.dumps({'nftables': [{'table': {'name': 'routepilot', 'family': 'inet'}}]}))
                return SimpleNamespace(stdout='[]', returncode=0)
            service = masque.Service(); service.plane = Mock(); service.dns = Mock()
            with patch.object(masque, 'command', side_effect=command):
                self.assertFalse(service.stop(restore=False)['direct'])
            self.assertTrue(marker.exists())

    def test_verified_cleanup_removes_journal(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(masque, 'RUN', Path(folder)):
            marker = Path(folder) / 'active-owner'; marker.write_text('owned')
            def command(args, **kw):
                return SimpleNamespace(stdout=json.dumps({'nftables': []}) if args[0] == 'nft' else '[]', returncode=0)
            service = masque.Service(); service.plane = Mock(); service.dns = Mock()
            with patch.object(masque, 'command', side_effect=command):
                self.assertTrue(service.stop(restore=False)['direct'])
            self.assertFalse(marker.exists())

    def test_cleanup_exception_does_not_escape_or_restore_phone(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(masque, 'RUN', Path(folder)), patch.object(masque, 'command') as command:
            marker = Path(folder) / 'phone-paused'; marker.write_text('paused')
            service = masque.Service(); service.plane = Mock(); service.dns = Mock()
            service.plane.stop.side_effect = RuntimeError('synthetic failure')
            self.assertFalse(service.stop(restore=False)['direct'])
            self.assertTrue(marker.exists())
            command.assert_not_called()

    def test_failed_phone_restore_keeps_marker(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(masque, 'RUN', Path(folder)), patch.object(masque, 'uci_value', return_value='1'), patch.object(masque, 'command', side_effect=RuntimeError('start failed')):
            marker = Path(folder) / 'phone-paused'; marker.write_text('paused')
            service = masque.Service(); service.plane = Mock(); service.dns = Mock()
            self.assertFalse(service.stop()['direct'])
            self.assertTrue(marker.exists())

    def test_dead_update_progress_becomes_unknown(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(masque, 'RUN', Path(folder)):
            path = Path(folder) / 'update.json'
            path.write_text(json.dumps({'state': 'running', 'request_id': 'test'}))
            masque.finish_interrupted_update()
            state = json.loads(path.read_text())
            self.assertEqual(state['state'], 'unknown')
            self.assertEqual(state['request_id'], 'test')

    def test_failed_phone_pause_is_retried_despite_existing_journal(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(masque, 'RUN', Path(folder)), patch.object(masque, 'uci_value', return_value='1'):
            marker = Path(folder) / 'phone-paused'; marker.write_text('intent')
            compiled = {'nft': '', 'vnet': 'test', 'exit': 'usa', 'devices': [{'mode': 'global', 'ip': '10.77.50.10', 'mac': '02:00:00:00:00:01'}]}
            cfg = {'main': {'legacy_phone_ip': '10.77.50.10', 'legacy_phone_mac': '02:00:00:00:00:01'}}
            service = masque.Service(); service.plane = Mock(); service.plane.probe.return_value = '192.0.2.1'
            attempts = []
            def command(args, **kw):
                if args[0] == '/etc/init.d/warp-phone':
                    attempts.append(args)
                    raise RuntimeError('stop failed')
                return SimpleNamespace(stdout='', returncode=0)
            with patch.object(masque, 'command', side_effect=command):
                for _ in range(2):
                    with self.assertRaises(RuntimeError):
                        service.apply(cfg, compiled)
            self.assertEqual(len(attempts), 2)
            self.assertTrue(marker.exists())

    def test_interruption_immediately_after_publish_keeps_generation(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(masque, 'RULES', Path(folder)):
            rules = Path(folder)
            real_replace = masque.os.replace
            def interrupted(src, dst):
                result = real_replace(src, dst)
                if Path(dst).name == 'current':
                    raise RuntimeError('synthetic signal after rename')
                return result
            with patch.object(masque.os, 'replace', side_effect=interrupted):
                with self.assertRaises(RuntimeError):
                    masque.commit_rules([(rules / 'china.txt', 'new')], {})
            with masque.rule_snapshot() as snapshot:
                self.assertEqual((snapshot / 'china.txt').read_text(), 'new')

    def test_missing_policy_rule_is_not_reported_healthy(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(masque, 'RUN', Path(folder)), patch.object(masque, 'cli', return_value=SimpleNamespace(stdout='Status update: Connected')):
            service = masque.Service(); service.dns = Mock(); service.dns.alive.return_value = True
            service.plane = Mock(); service.compiled = {'devices': []}
            def command(args, **kw):
                if args == ['ip', '-4', 'rule', 'show']:
                    return SimpleNamespace(stdout='', returncode=0)
                return SimpleNamespace(stdout='routepilot-control-out routepilot-device-out', returncode=0)
            with patch.object(masque, 'command', side_effect=command):
                with self.assertRaisesRegex(RuntimeError, '专用策略路由规则已丢失'):
                    service.health()

    def test_successful_apply_refreshes_runtime_identity(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(masque, 'RUN', Path(folder)), patch.object(masque, 'uci_value', return_value='0'), patch.object(masque, 'net'), patch.object(masque.time, 'sleep'):
            compiled = {'nft': '', 'vnet': 'test', 'exit': 'usa', 'dns': {}, 'devices': [{'mode': 'global', 'ip': '10.77.50.10', 'mac': '02:00:00:00:00:01'}]}
            service = masque.Service(); service.plane = Mock(); service.plane.probe.return_value = '192.0.2.1'
            service.dns = Mock(); service.dns.alive.return_value = True
            def command(args, **kw):
                return SimpleNamespace(stdout=str(masque.PRIORITY) + ': from all', returncode=0)
            with patch.object(masque, 'command', side_effect=command):
                service.apply({'main': {}}, compiled)
                first = service.runtime_id
                self.assertEqual(masque.uuid.UUID(first).version, 4)
                service.apply({'main': {}}, compiled)
                self.assertNotEqual(service.runtime_id, first)
