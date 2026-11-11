# Synthetic unit-test addresses; no live router, tenant or user configuration.
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'root/usr/lib/routepilot'))
import masque
import mplane
import diagnostics
from diagnostics import classify


class FeedbackTests(unittest.TestCase):
    def test_lan_choices_exclude_wan_tunnels_and_public_addresses(self):
        def addr(name, ip, prefix=24, flags=None):
            return {'ifname': name, 'flags': flags or ['UP'], 'addr_info': [{'family': 'inet', 'scope': 'global', 'local': ip, 'prefixlen': prefix}]}
        items = [addr('br-lan', '10.77.50.1'), addr('eth1', '192.168.0.119'), addr('br-guest', '10.10.0.1'), addr('warp_test', '172.16.0.2', 32), addr('vpn', '10.0.2.1', flags=['UP', 'POINTOPOINT']), addr('public', '8.8.8.8')]
        responses = [SimpleNamespace(stdout=json.dumps(items)), SimpleNamespace(stdout=json.dumps([{'dev': 'eth1'}]))]
        with patch.object(masque, 'command', side_effect=responses):
            self.assertEqual(masque.lan_interfaces(), [{'name': 'br-lan', 'network': '10.77.50.0/24', 'up': True}, {'name': 'br-guest', 'network': '10.10.0.0/24', 'up': True}])

    def test_guest_dns_permission_is_scoped_and_does_not_open_lan_interforwarding(self):
        devices = [{'scope': 'lan', 'lan_interface': 'br-lan', 'ip': '10.77.50.0/24', 'mode': 'global'}, {'scope': 'lan', 'lan_interface': 'br-guest', 'ip': '192.168.9.0/24', 'mode': 'global'}]
        def cmd(args, **kwargs):
            return SimpleNamespace(stdout=json.dumps([{'dev': 'eth1'}]) if args[0] == 'ip' else '')
        with patch.object(mplane, 'command', side_effect=cmd) as call:
            mplane.fw4_install(devices)
        text = call.call_args.kwargs['data']
        self.assertIn('input iifname "br-guest" ip saddr 192.168.9.0/24 meta l4proto { tcp, udp } th dport 12101 ct status dnat accept', text)
        self.assertNotIn('oifname "br-lan" accept', text)
        self.assertNotIn('oifname "br-guest" accept', text)

    def test_power_switch_changes_only_own_config_and_disables_login_preparation(self):
        for action, enabled in [('enable', '1'), ('disable', '0')]:
            with patch.object(masque, 'command') as call, patch('builtins.print'):
                masque.control(action)
            calls = [x.args[0] for x in call.call_args_list]
            self.assertIn(['uci', 'set', 'routepilot.main.enabled=' + enabled], calls)
            self.assertIn(['uci', 'set', 'routepilot.main.preparing=0'], calls)
            self.assertEqual(calls[-1], ['uci', 'commit', 'routepilot'])
            self.assertTrue(all(c[0] == 'uci' and all('shadowsocksr' not in v and 'firewall' not in v for v in c) for c in calls))

    def test_https_reachability_distinguishes_http_restrictions_from_network_failure(self):
        self.assertEqual(classify(0, '200 0.123')['ms'], 123)
        self.assertEqual(classify(0, '200 0.123')['state'], 'ok')
        self.assertEqual(classify(0, '403 0.200')['state'], 'restricted')
        self.assertEqual(classify(28, '000 10.00')['message'], '超时')
        self.assertEqual(classify(6, '000 0.01')['message'], 'DNS 解析失败')

    # Regression: ISSUE-001 — an HTTP status cannot override a failed transfer.
    # Found by /qa on 2026-11-10; report: .local/imok-qa-20261110/report.md
    def test_https_transfer_errors_are_not_overridden_by_received_http_status(self):
        for code,http,message in [(28,200,'超时'),(60,301,'证书验证失败'),
                                  (18,200,'响应传输未完成'),(47,302,'重定向次数超限'),
                                  (28,403,'超时')]:
            with self.subTest(code=code,http=http):
                result=classify(code,str(http)+' 0.123')
                self.assertEqual(result['state'],'failed')
                self.assertEqual(result['message'],message)
                self.assertEqual(result['http'],http)
                self.assertEqual(result['ms'],123)

    # Regression: ISSUE-002 — bad disposable progress must not block future tests.
    # Found by /qa on 2026-11-10; report: .local/imok-qa-20261110/report.md
    def test_https_detection_recovers_bad_progress_without_modifying_settings(self):
        cases=[('{',1),('[]',1),('null',1),
               ('{"state":"running","started_at":"soon"}',1),
               ('{"state":"running","started_at":true}',1),
               ('{"state":"running","started_at":1500}',1),
               ('{"state":"running","started_at":1000}',0),
               ('{"state":"running","started_at":970}',1)]
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);cache=root/'connectivity.json'
            for content,expected in cases:
                with self.subTest(content=content):
                    cache.write_text(content);before=cache.read_bytes()
                    with patch.object(diagnostics,'RUN',root),patch.object(diagnostics.time,'time',return_value=1001),patch.object(diagnostics.subprocess,'Popen') as spawn:
                        diagnostics.start()
                    self.assertEqual(spawn.call_count,expected)
                    self.assertEqual(cache.read_bytes(),before,'the worker owns progress publication')

    def test_action_nonces_do_not_restart_the_routing_generation(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(masque, 'RULES', Path(folder)):
            cfg = {'main': {'exit': 'seoul'}, 'policies': []}
            first = masque.routing_digest(cfg)
            cfg['main'].update(apply_nonce='new', update_nonce='new', update_hours='24', china_url='https://example.com/new')
            self.assertEqual(first, masque.routing_digest(cfg))
            cfg['main']['exit'] = 'usa'
            self.assertNotEqual(first, masque.routing_digest(cfg))

    def test_failed_update_preserves_last_good_files_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); rules = root / 'rules'; run = root / 'run'
            rules.mkdir(); run.mkdir()
            (rules / 'china.txt').write_text('1.0.1.0/24\n')
            (rules / 'updated.json').write_text(json.dumps({'time': 123, 'counts': {'china': 1}}))
            cfg = {'main': {'update_nonce': 'job-1', 'china_url': 'https://example.com/china', 'gfw_url': 'https://example.com/gfw'}}
            def fetch(args):
                return SimpleNamespace(stdout='8.8.8.0/24\n' if args[-1].endswith('/china') else '<html>not a rule</html>')
            with patch.object(masque, 'RULES', rules), patch.object(masque, 'RUN', run), patch.object(masque, 'read_config', return_value=cfg), patch.object(masque, 'net', side_effect=fetch), patch.dict('os.environ', {}, clear=True):
                with self.assertRaises(ValueError):
                    masque.update_rules()
            self.assertEqual((rules / 'china.txt').read_text(), '1.0.1.0/24\n')
            report = json.loads((run / 'update.json').read_text())
            self.assertEqual(report['state'], 'failed')
            self.assertEqual(report['last_success_at'], 123)
            self.assertEqual(report['request_id'], 'job-1')

    def test_successful_update_publishes_counts_and_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); rules = root / 'rules'; run = root / 'run'
            rules.mkdir(); run.mkdir()
            cfg = {'main': {'update_nonce': 'job-2', 'china_url': 'https://example.com/china', 'gfw_url': 'https://example.com/gfw'}}
            def fetch(args):
                return SimpleNamespace(stdout='1.0.1.0/24\n' if args[-1].endswith('/china') else 'example.com\nfull:login.example.com\nregexp:^ignored$\n')
            with patch.object(masque, 'RULES', rules), patch.object(masque, 'RUN', run), patch.object(masque, 'read_config', return_value=cfg), patch.object(masque, 'net', side_effect=fetch), patch.dict('os.environ', {}, clear=True):
                masque.update_rules()
            report = json.loads((run / 'update.json').read_text())
            self.assertEqual(report['state'], 'success')
            self.assertEqual(report['percent'], 100)
            self.assertEqual(report['counts'], {'china': 1, 'gfw': 2})
            self.assertEqual(report['ignored_regexp']['gfw'], 1)


if __name__ == '__main__':
    unittest.main()
