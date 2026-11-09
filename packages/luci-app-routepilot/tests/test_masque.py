# Synthetic unit-test addresses; no live router, tenant or user configuration.
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'root/usr/lib/routepilot'))
import masque_config as config
from native import question, answer_addresses, suffix_match, response_ttl, age_response, upstream


class MasqueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.rules = Path(self.temp.name)
        (self.rules / 'china.txt').write_text('1.0.1.0/24\n')
        (self.rules / 'gfw.txt').write_text('example.net\n')
        (self.rules / 'whitelist.txt').write_text('example.cn\n')
        self.cfg = {'main': {'exit': 'seoul', 'vnet_seoul': '11111111-1111-4111-8111-111111111111'},
                    'policies': [{'id': 'phone', 'enabled': '1', 'ip': '10.77.50.100',
                                  'mac': '02:00:00:00:00:01', 'mode': 'bypass_cn'}]}

    def tearDown(self):
        self.temp.cleanup()

    def test_capture_requires_interface_mac_and_ip(self):
        out = config.compile_config(self.cfg, self.rules)
        self.assertIn('iifname "br-lan" ether saddr 02:00:00:00:00:01 ip saddr 10.77.50.100', out['nft'])
        self.assertNotIn('flush ruleset', out['nft'])
        self.assertEqual(out['dns']['policies'][0]['mark'], config.MARK)

    def test_all_five_modes_and_direct_not_captured(self):
        for mode in ('direct', 'global', 'bypass_cn', 'gfw', 'whitelist'):
            self.cfg['policies'][0]['mode'] = mode
            out = config.compile_config(self.cfg, self.rules)
            self.assertEqual(len(out['dns']['policies']), 0 if mode == 'direct' else 1)

    def test_invalid_exit_device_or_injected_interface_rejected(self):
        for key, value in (('exit', 'bad'), ('lan_interface', 'br-lan";flush ruleset')):
            broken = json.loads(json.dumps(self.cfg))
            broken['main'][key] = value
            with self.assertRaises(ValueError):
                config.compile_config(broken, self.rules)
        self.cfg['policies'].append(dict(self.cfg['policies'][0], id='duplicate'))
        with self.assertRaises(ValueError):
            config.compile_config(self.cfg, self.rules)

    def test_private_destinations_precede_custom_rules(self):
        self.cfg['main']['custom_proxy_ips'] = ['192.168.0.0/16', '8.8.8.8/32']
        text = config.compile_config(self.cfg, self.rules)['nft']
        self.assertLess(text.index('ip daddr { 0.0.0.0/8'), text.index('ip daddr { 192.168.0.0/16'))

    def test_missing_domain_rules_fail_without_modifying_anything(self):
        self.cfg['policies'][0]['mode'] = 'gfw'
        (self.rules / 'gfw.txt').unlink()
        with self.assertRaises(ValueError):
            config.compile_config(self.cfg, self.rules)

    def test_dns_suffix_boundaries_and_ttl_aging(self):
        self.assertTrue(suffix_match({'www.example.com'}, {'example.com'}))
        self.assertFalse(suffix_match({'badexample.com'}, {'example.com'}))
        name = b'\x07example\x03com\x00'
        packet = struct.pack('!6H', 123, 0x8180, 1, 1, 0, 0) + name + struct.pack('!HH', 1, 1)
        packet += b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 60, 4) + b'\x08\x08\x08\x08'
        self.assertEqual(answer_addresses(packet)[0], ['8.8.8.8'])
        self.assertEqual(response_ttl(age_response(packet, 15)), 45)

    def test_exact_domain_and_top_level_entries_are_not_broadened(self):
        text, skipped = config.normalize_domains('cn\ndomain:example.cn\nfull:login.example.com\nregexp:^untrusted$\n')
        entries = set(text.splitlines())
        self.assertEqual(skipped, 1)
        self.assertTrue(suffix_match({'x.example.cn'}, entries))
        self.assertTrue(suffix_match({'login.example.com'}, entries))
        self.assertFalse(suffix_match({'x.login.example.com'}, entries))

    def test_rule_update_rejects_html_or_empty_content(self):
        for text in ('<!doctype html>\n<html>bad</html>', '# only comments\n'):
            with self.assertRaises(ValueError):
                config.normalize_domains(text)

    def test_lan_scope_needs_no_device_bindings(self):
        self.cfg['main'].update(scope='lan', lan_network='10.77.50.0/24', lan_mode='bypass_cn')
        self.cfg['policies'] = []
        out = config.compile_config(self.cfg, self.rules)
        self.assertEqual(out['scope'], 'lan')
        self.assertEqual(out['devices'][0]['ip'], '10.77.50.0/24')
        self.assertEqual(len(out['dns']['policies']), 1)
        self.assertIn('iifname "br-lan" ip saddr 10.77.50.0/24', out['nft'])
        self.assertNotIn('ether saddr', out['nft'])
        self.assertIn('162.159.197.0/24', out['nft'])

    def test_lan_scope_ignores_old_bindings_but_preserves_them(self):
        self.cfg['main'].update(scope='lan', lan_network='10.77.50.0/24', lan_mode='global')
        original = json.loads(json.dumps(self.cfg['policies']))
        out = config.compile_config(self.cfg, self.rules)
        self.assertEqual(self.cfg['policies'], original)
        self.assertEqual(out['devices'][0]['mode'], 'global')
        self.assertEqual(out['devices'][0]['id'], 'lan')

    def test_lan_scope_rejects_public_overlapping_or_unbounded_networks(self):
        self.cfg['main']['scope'] = 'lan'
        for subnet in ('0.0.0.0/0', '8.8.8.0/24', '10.254.239.0/30', ''):
            self.cfg['main']['lan_network'] = subnet
            with self.assertRaises(ValueError):
                config.compile_config(self.cfg, self.rules)

    def test_multiple_lans_have_distinct_capture_dns_and_ipv6_guards(self):
        self.cfg['main'].update(scope='lan', lan_interfaces=['br-lan', 'br-guest'], lan_networks=[{'interface': 'br-lan', 'network': '10.77.50.0/24'}, {'interface': 'br-guest', 'network': '192.168.9.0/24'}])
        old = json.loads(json.dumps(self.cfg['policies']))
        out = config.compile_config(self.cfg, self.rules)
        self.assertEqual(self.cfg['policies'], old)
        self.assertEqual([(d['lan_interface'], d['ip']) for d in out['devices']], [('br-lan', '10.77.50.0/24'), ('br-guest', '192.168.9.0/24')])
        self.assertEqual([d['port'] for d in out['dns']['policies']], [12100, 12101])
        for iface, subnet, port in [('br-lan', '10.77.50.0/24', 12100), ('br-guest', '192.168.9.0/24', 12101)]:
            self.assertIn('iifname "%s" ip saddr %s' % (iface, subnet), out['nft'])
            self.assertIn('redirect to :%d' % port, out['nft'])
            self.assertIn('iifname "%s" meta nfproto ipv6 reject' % iface, out['nft'])

    def test_multilan_rejects_empty_duplicate_overlapping_or_unresolved_choices(self):
        for selected, resolved in [([], []), (['br-lan', 'br-lan'], []), (['br-lan', 'br-guest'], [{'interface': 'br-lan', 'network': '10.77.50.0/24'}]), (['br-lan', 'br-guest'], [{'interface': 'br-lan', 'network': '10.77.50.0/24'}, {'interface': 'br-guest', 'network': '10.77.50.0/25'}])]:
            self.cfg['main'].update(scope='lan', lan_interfaces=selected, lan_networks=resolved)
            with self.assertRaises(ValueError):
                config.compile_config(self.cfg, self.rules)

    def test_device_binding_uses_its_selected_lan_and_rejects_unselected_subnet(self):
        self.cfg['main'].update(lan_interfaces=['br-lan', 'br-guest'], lan_networks=[{'interface': 'br-lan', 'network': '10.77.50.0/24'}, {'interface': 'br-guest', 'network': '192.168.9.0/24'}])
        self.cfg['policies'][0]['ip'] = '192.168.9.20'
        out = config.compile_config(self.cfg, self.rules)
        self.assertEqual(out['devices'][0]['lan_interface'], 'br-guest')
        self.cfg['policies'][0]['ip'] = '192.168.10.20'
        with self.assertRaises(ValueError):
            config.compile_config(self.cfg, self.rules)

    def test_aaaa_block_does_not_send_any_upstream_request(self):
        query = struct.pack('!6H', 7, 0x0100, 1, 0, 0, 0) + b'\x07example\x03com\x00' + struct.pack('!HH', 28, 1)
        with patch('native.socket.socket') as socket:
            reply = upstream(query, {'block_aaaa': True})
            socket.assert_not_called()
        self.assertEqual(struct.unpack('!6H', reply[:12])[3], 0)


if __name__ == '__main__':
    unittest.main()
