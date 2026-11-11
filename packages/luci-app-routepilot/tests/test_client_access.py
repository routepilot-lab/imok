# Synthetic unit-test addresses; no live router, tenant or user configuration.
"""Local-only lifecycle/security tests; cloud functions are explicit fakes."""
import base64
import contextlib
from email.message import Message
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'root/usr/lib/routepilot'))
import client_access as access
import client_provider as provider
from input_fixture import stage


def jwt(org='example-team', expiry=None, **extra):
    claims = dict(exp=time.time() + 120 if expiry is None else expiry,
                  iss='https://' + org + '.cloudflareaccess.com', **extra)
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'eyJhbGciOiJSUzI1NiJ9.' + body + '.synthetic-signature'


class ClientAccessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = provider.new_keys()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / 'run'
        self.run.mkdir()
        self.private = self.root / 'clients'
        for module, key, value in [(access, 'PRIVATE', self.private), (access, 'RUN', self.run),
                                   (access, 'VOLATILE', self.run / 'client-jobs')]:
            obj = patch.object(module, key, value)
            obj.start()
            self.addCleanup(obj.stop)
        self.spawn = patch.object(access, 'spawn').start()
        self.addCleanup(patch.stopall)
        self.config = patch.object(access, 'read_config', return_value={'main': {'organization': 'example-team'}}).start()
        self.client = access.create(dict(name='测试 iPhone', organization='example-team'))['client']
        self.ident = self.client['id']
        self.cloud_id = '00000000-0000-4000-8000-000000000123'
        self.device_token = 'synthetic-device-token'
        self.usa = '00000000-0000-4000-8000-000000000111'
        self.seoul = '00000000-0000-4000-8000-000000000222'
        self.default = '00000000-0000-4000-8000-000000000333'
        self.selected_network = self.usa
        def networks(*args, target=None, protocol='masque'):
            if target is not None:
                self.selected_network = target
                return {'acknowledged': True}
            return dict(selected_id=self.selected_network, networks=[dict(id=self.usa, name='美国', is_default=False),
                        dict(id=self.seoul, name='首尔', is_default=False), dict(id=self.default, name='默认', is_default=True)])
        self.cloud_networks = patch.object(provider, 'client_networks', side_effect=networks).start()


    def row(self):
        return access.load()['clients'][0]

    def submit_register(self):
        return access.start('register', dict(id=self.ident, token=jwt(), accept_terms=True))

    def reply(self, org='example-team', ipv4='100.96.0.88', **changes):
        result = dict(id=self.cloud_id, key=self.keys['public_key'], key_type='secp256r1', tunnel_type='masque',
                      account=dict(account_type='team', organization=org),
                      config=dict(interface=dict(addresses=dict(v4=ipv4, v6='2606:4700:cf1:1000::88')),
                                  peers=[dict(public_key=self.keys['public_key'], endpoint=dict(v4='162.159.197.2:0'))]))
        result.update(changes)
        return result

    def complete_register(self, enroll_reply=None, enroll_error=None, register_error=None):
        queued = self.submit_register()
        initial = dict(id=self.cloud_id, token=self.device_token, account=dict(account_type='team', organization='example-team'))
        with patch.object(provider, 'new_keys', return_value=self.keys), \
             patch.object(provider, 'register', side_effect=register_error, return_value=initial) as reg, \
             patch.object(provider, 'enroll', side_effect=enroll_error, return_value=enroll_reply or self.reply()) as enroll:
            access.job(self.ident, queued['operation_id'])
        return reg, enroll

    def test_service_exchange_uses_only_team_https_and_rejects_external_redirect(self):
        credentials = dict(client_id='a' * 32 + '.access', client_secret='cf_' + 's' * 48)
        team = 'example-team'
        assertion = jwt()
        callback = 'com.cloudflare.warp://example-team.cloudflareaccess.com/auth?token=' + assertion
        def redirect(location):
            headers = Message()
            headers['Location'] = location
            return HTTPError('https://example-team.cloudflareaccess.com/warp', 302, '', headers, io.BytesIO())

        with patch.object(provider.urllib.request, 'build_opener') as build:
            opener = build.return_value
            opener.open.side_effect = redirect(callback)
            self.assertEqual(provider.service_auth_token(team, credentials, time.time()), assertion)
            req = opener.open.call_args.args[0]
            self.assertEqual(req.full_url, 'https://example-team.cloudflareaccess.com/warp')
            self.assertEqual(req.get_method(), 'GET')
            headers = {name.lower(): value for name, value in req.header_items()}
            self.assertEqual(headers['cf-access-client-secret'], credentials['client_secret'])
            self.assertEqual(headers['cf-access-client-id'], credentials['client_id'])
            self.assertEqual(build.call_args.args[0].proxies, {})
            self.assertIsInstance(build.call_args.args[1], provider.NoRedirect)
            self.assertIsNone(build.call_args.args[1].redirect_request(req, None, 302, '', None, 'https://other.example'))
            opener.open.side_effect = redirect('https://other.example/' + credentials['client_secret'])
            with self.assertRaises(provider.ClientError) as caught:
                provider.service_auth_token(team, credentials, time.time())
            self.assertNotIn(credentials['client_secret'], str(caught.exception))
            self.assertFalse(caught.exception.uncertain)
            self.assertEqual(opener.open.call_count, 2)

    def test_service_registration_saves_only_verified_credentials_per_team(self):
        credentials = dict(client_id='a' * 32 + '.access', client_secret='s' * 64)
        with patch.object(access, 'read_config', return_value={'main': {'organization': 'other-team'}}):
            other = access.create(dict(name='其他团队'))['client']
        same = access.create(dict(name='同团队另一台', organization='example-team'))['client']
        args = dict(id=self.ident, accept_terms=True, service_auth=credentials, remember_service_auth=True)
        with self.assertRaises(provider.ClientError):
            access.start('register', dict(args, token=jwt()))
        queued = access.start('register', args)
        job = access.VOLATILE / (queued['operation_id'] + '.json')
        self.assertEqual(stat.S_IMODE(job.stat().st_mode), 0o600)
        with patch.object(provider, 'service_auth_token', side_effect=provider.ClientError('服务授权未通过')), \
             patch.object(provider, 'register') as register:
            access.job(self.ident, queued['operation_id'])
        register.assert_not_called()
        self.assertFalse(job.exists())
        self.assertEqual(self.row()['state'], 'failed')
        self.assertFalse(access.saved_service_auth('example-team'))

        queued = access.start('register', args)
        assertion = jwt()
        initial = dict(id=self.cloud_id, token=self.device_token, account=dict(account_type='team', organization='example-team'))
        with patch.object(provider, 'service_auth_token', return_value=assertion), \
             patch.object(provider, 'new_keys', return_value=self.keys), \
             patch.object(provider, 'register', return_value=initial) as register, \
             patch.object(provider, 'enroll', return_value=self.reply()):
            access.job(self.ident, queued['operation_id'])
        self.assertEqual(register.call_args.args[0], assertion)
        self.assertEqual(self.row()['state'], 'registered')
        self.assertEqual(stat.S_IMODE(access.service_auth_path('example-team').stat().st_mode), 0o600)
        listing = access.listing()
        self.assertEqual([r['service_auth_saved'] for r in listing['clients']], [True, False, True])
        for key in (credentials['client_id'], credentials['client_secret'], assertion):
            self.assertNotIn(key, json.dumps(listing))
            self.assertNotIn(key, (self.private / 'registry.json').read_text())

        reuse = dict(accept_terms=True, service_auth=None, remember_service_auth=False)
        with self.assertRaises(provider.ClientError):
            access.start('register', dict(reuse, id=other['id']))
        queued = access.start('register', dict(reuse, id=same['id']))
        with self.assertRaises(provider.ClientError):
            access.forget_service_auth({'id': self.ident})
        with patch.object(provider, 'service_auth_token', side_effect=provider.ClientError('令牌过期')) as exchange:
            access.job(same['id'], queued['operation_id'])
        self.assertEqual(exchange.call_args.args[:2], ('example-team', credentials))
        self.assertEqual(access.forget_service_auth({'id': self.ident}), {'forgotten': True})
        self.assertFalse(access.saved_service_auth('example-team'))
        self.assertEqual(self.row()['state'], 'registered')

    def test_new_clients_inherit_the_system_team_and_old_records_keep_their_team(self):
        self.assertEqual(access.listing()['team'], {'name': 'example-team', 'error': ''})
        another = access.create({'name': '第二台设备'})['client']
        self.assertEqual(another['organization'], 'example-team')
        with self.assertRaisesRegex(provider.ClientError, '统一使用'):
            access.create({'name': '外部', 'organization': 'other-team'})
        with patch.object(access, 'read_config', return_value={'main': {}}):
            self.assertEqual(access.listing()['team']['name'], '')
            self.assertEqual(len(access.listing()['clients']), 2)
            with self.assertRaisesRegex(provider.ClientError, '账户与协议'):
                access.create({'name': '无团队'})
        with patch.object(access, 'read_config', return_value={'main': {'organization': 'other-team'}}):
            with self.assertRaisesRegex(provider.ClientError, '不一致'):
                self.submit_register()
        self.assertEqual(self.row()['organization'], 'example-team')

    def test_worker_rechecks_team_before_registration(self):
        queued = self.submit_register()
        with patch.object(access, 'read_config', return_value={'main': {'organization': 'other-team'}}), \
             patch.object(provider, 'register') as register:
            access.job(self.ident, queued['operation_id'])
        register.assert_not_called()
        self.assertEqual(self.row()['state'], 'failed')

    def wireguard_reply(self, ident, key):
        return dict(id=ident, key=key, key_type='curve25519', tunnel_type='wireguard',
                    account=dict(account_type='team', organization='example-team'),
                    config=dict(client_id='AQID', interface=dict(addresses=dict(v4='100.96.0.55', v6='2606:4700:cf1:1000::55')),
                                peers=[dict(public_key=base64.b64encode(b'p' * 32).decode(),
                                            endpoint=dict(v4='162.159.193.7:0', ports=[500, 2408, 4500]))]))

    def wireguard_keys(self):
        # RFC 7748 test vector. Some local OpenSSL-compatible tools lack X25519;
        # this fixture exercises decoding and clamping without that dependency.
        private = bytes.fromhex('77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a')
        public = bytes.fromhex('8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a')
        private_der = provider.X25519_PRIVATE_PREFIX + private
        with patch.object(provider, 'openssl', side_effect=[private_der, provider.X25519_PUBLIC_PREFIX + public]) as openssl:
            keys = provider.new_wireguard_keys()
        self.assertEqual(openssl.call_args.args[1], private_der)
        self.assertEqual(base64.b64decode(keys['public_key']), public)
        self.assertEqual(base64.b64decode(keys['private_key']), b'\x70' + private[1:-1] + b'\x6a')
        return keys

    def test_wireguard_selects_its_own_exit_and_exports_the_tested_conf_format(self):
        original = self.row().copy()
        client = access.create(dict(name='WireGuard 测试', protocol='wireguard'))['client']
        self.assertEqual(client['settings']['port'], 2408)
        keys = self.wireguard_keys()
        self.assertEqual(keys['bootstrap_key'], keys['public_key'])
        self.selected_network = self.default
        args = dict(id=client['id'], token=jwt(), accept_terms=True)
        task = access.start('register', args)
        initial = dict(id=self.cloud_id, token=self.device_token, account=dict(account_type='team', organization='example-team'))
        reply = self.wireguard_reply(self.cloud_id, keys['public_key'])
        with patch.object(provider, 'new_wireguard_keys', return_value=keys), \
             patch.object(provider, 'register', return_value=initial) as register, \
             patch.object(provider, 'enroll') as masque_enroll, \
             patch.object(provider, 'request', return_value=reply) as request:
            access.job(client['id'], task['operation_id'])
        request.assert_called_once_with('GET', '/reg/' + self.cloud_id, token=self.device_token)
        masque_enroll.assert_not_called()
        self.assertEqual(register.call_args.args[1], keys['public_key'])
        with self.assertRaisesRegex(provider.ClientError, '默认'):
            access.export(dict(id=client['id'], confirm=True))
        task = access.start('select_exit', dict(id=client['id'], vnet_id=self.seoul, expected_vnet_id=self.default))
        with patch.object(provider, 'request', return_value=reply):
            access.job(client['id'], task['operation_id'])
        listing = access.listing()
        self.assertEqual(listing['clients'][1]['protocol'], 'wireguard')
        self.assertEqual(listing['clients'][1]['network']['selected_name'], '首尔')
        self.assertNotIn(keys['private_key'], json.dumps(listing))
        self.assertNotIn('reserved', listing['clients'][1])
        self.assertEqual(self.row(), original)
        self.assertEqual(self.cloud_networks.call_args.kwargs['protocol'], 'wireguard')
        config = access.export(dict(id=client['id'], confirm=True))
        self.assertTrue(config['filename'].endswith('.conf'))
        self.assertFalse(config['exit_verified'])
        self.assertIn('PrivateKey = ' + keys['private_key'], config['uri'])
        self.assertIn('Address = 100.96.0.55/32, 2606:4700:cf1:1000::55/128', config['uri'])
        self.assertIn('AllowedIPs = ::/0', config['uri'])
        self.assertIn('Endpoint = 162.159.193.7:2408', config['uri'])
        self.assertTrue(config['uri'].endswith('Reserved = 1,2,3'))
        with self.assertRaisesRegex(provider.ClientError, '配置文件'):
            access.start('publish_subscription', dict(id=client['id'], confirm=True))
        with self.assertRaises(provider.ClientError):
            access.edit(dict(id=client['id'], settings={'endpoint':'162.159.197.2','port':443}))

    def test_wireguard_does_not_claim_unverified_cloud_identity(self):
        client = access.create(dict(name='WireGuard 异常', protocol='wireguard'))['client']
        keys = self.wireguard_keys()
        task = access.start('register', dict(id=client['id'], token=jwt(), accept_terms=True))
        initial = dict(id=self.cloud_id, token=self.device_token)
        reply = self.wireguard_reply(self.cloud_id, base64.b64encode(b'x' * 32).decode())
        with patch.object(provider, 'new_wireguard_keys', return_value=keys), \
             patch.object(provider, 'register', return_value=initial), \
             patch.object(provider, 'request', return_value=reply):
            access.job(client['id'], task['operation_id'])
        row = access.load()['clients'][1]
        self.assertEqual(row['state'], 'unknown')
        self.assertTrue(row['post_uncertain'])
        self.assertNotIn('device_id', row)
        self.cloud_networks.assert_not_called()


    def test_default_network_is_not_exported_or_used_as_fallback(self):
        self.selected_network = self.default
        self.complete_register()
        client = access.listing()['clients'][0]
        self.assertEqual(client['state'], 'registered')
        self.assertFalse(client['exportable'])
        self.assertFalse(client['network_selection_verified'])
        with self.assertRaises(provider.ClientError):
            access.export(dict(id=self.ident, confirm=True))
        task = access.start('select_exit', dict(id=self.ident, vnet_id=self.usa, expected_vnet_id=self.default))
        original = self.row()
        with patch.object(provider, 'request', return_value=self.reply()):
            access.job(self.ident, task['operation_id'])
        client = access.listing()['clients'][0]
        self.assertTrue(client['exportable'])
        self.assertTrue(client['network_selection_verified'])
        self.assertFalse(client['exit_verified'])
        self.assertEqual(client['network']['selected_id'], self.usa)
        self.assertEqual(client['network']['previous_id'], self.default)
        for key in ('private_key', 'public_key', 'device_token', 'device_id', 'ipv4'):
            self.assertEqual(self.row()[key], original[key])
        patch_calls = [c for c in self.cloud_networks.call_args_list if c.kwargs.get('target')]
        self.assertEqual(len(patch_calls), 1)
        self.assertEqual(patch_calls[0].args[2], self.cloud_id)
        self.assertEqual(patch_calls[0].kwargs['target'], self.usa)

    def test_removed_or_default_exit_never_causes_a_cloud_write(self):
        self.complete_register()
        for target in (self.default, '00000000-0000-4000-8000-000000000999'):
            self.cloud_networks.reset_mock()
            task = access.start('select_exit', dict(id=self.ident, vnet_id=target, expected_vnet_id=self.usa))
            with patch.object(provider, 'request', return_value=self.reply()):
                access.job(self.ident, task['operation_id'])
            self.assertFalse(any(c.kwargs.get('target') for c in self.cloud_networks.call_args_list))
            self.assertNotEqual(self.row()['state'], 'registered')
            self.assertIn('未应用', self.row()['error'])

    def test_stale_exit_form_is_rejected_after_fresh_cloud_read(self):
        self.complete_register()
        self.selected_network = self.seoul
        task = access.start('select_exit', dict(id=self.ident, vnet_id=self.seoul, expected_vnet_id=self.usa))
        self.cloud_networks.reset_mock()
        with patch.object(provider, 'request', return_value=self.reply()):
            access.job(self.ident, task['operation_id'])
        self.assertFalse(any(c.kwargs.get('target') for c in self.cloud_networks.call_args_list))
        self.assertEqual(self.row()['network']['catalog']['selected_id'], self.seoul)
        self.assertIn('发生变化', self.row()['error'])

    def test_lost_patch_response_is_unknown_until_readback_sync(self):
        self.selected_network = self.default
        self.complete_register()
        transport = self.cloud_networks.side_effect
        def lost(*args, target=None):
            if target:
                self.selected_network = target
                raise provider.ClientError('synthetic timeout', uncertain=True)
            return transport(*args)
        self.cloud_networks.side_effect = lost
        task = access.start('select_exit', dict(id=self.ident, vnet_id=self.usa, expected_vnet_id=self.default))
        with patch.object(provider, 'request', return_value=self.reply()):
            access.job(self.ident, task['operation_id'])
        self.assertEqual(self.row()['network']['state'], 'unknown')
        self.assertFalse(access.listing()['clients'][0]['exportable'])
        self.cloud_networks.side_effect = transport
        task = access.start('refresh', dict(id=self.ident))
        with patch.object(provider, 'request', return_value=self.reply()):
            access.job(self.ident, task['operation_id'])
        self.assertEqual(self.row()['network']['state'], 'confirmed')
        self.assertEqual(self.row()['network']['catalog']['selected_id'], self.usa)
        self.assertTrue(access.listing()['clients'][0]['exportable'])

    def test_exit_directory_failure_preserves_identity_and_last_known_value(self):
        self.complete_register()
        old = self.row()
        self.cloud_networks.side_effect = provider.ClientError('synthetic directory unavailable')
        task = access.start('refresh', dict(id=self.ident))
        with patch.object(provider, 'request', return_value=self.reply()):
            access.job(self.ident, task['operation_id'])
        self.assertEqual(self.row()['state'], 'registered')
        self.assertEqual(self.row()['network']['state'], 'error')
        self.assertEqual(self.row()['private_key'], old['private_key'])
        self.assertFalse(access.listing()['clients'][0]['exportable'])

    def test_exit_metadata_never_exposes_unknown_nested_fields(self):
        self.complete_register()
        doc = access.load()
        doc['clients'][0]['network']['token'] = 'SYNTHETIC-DO-NOT-EXPOSE'
        access.save(doc)
        with self.assertRaises(provider.ClientError):
            access.listing()


    def test_subscription_requires_explicit_confirmation_and_fresh_owned_identity(self):
        self.complete_register()
        with self.assertRaises(provider.ClientError):
            access.start('publish_subscription', dict(id=self.ident, confirm=False))
        task = access.start('publish_subscription', dict(id=self.ident, confirm=True))
        with patch.object(provider, 'request', return_value=self.reply(key='not-my-key')), \
             patch.object(access.subscriptions, 'publish') as publish:
            access.job(self.ident, task['operation_id'])
        publish.assert_not_called()
        self.assertEqual(self.row()['private_key'], self.keys['private_key'])
        self.assertFalse(access.listing()['clients'][0]['exportable'])

    def test_port_edit_marks_published_subscription_dirty_without_rotating_keys(self):
        self.complete_register()
        doc = access.load()
        row = doc['clients'][0]
        uri = access.portable_uri(row)
        row['subscription'] = dict(token=access.subscriptions.token(), state='published',
                                   revision=access.subscriptions.body(uri)['revision'], synced_at=time.time())
        access.save(doc)
        before = self.row()
        access.edit(dict(id=self.ident, settings=dict(before['settings'], port=8443)))
        public = access.listing()['clients'][0]
        self.assertTrue(public['subscription']['needs_update'])
        self.assertEqual(self.row()['subscription']['token'], before['subscription']['token'])
        self.assertEqual(self.row()['private_key'], before['private_key'])
        self.assertNotIn(before['subscription']['token'], json.dumps(public))
        with self.assertRaises(provider.ClientError):
            access.subscription_link(dict(id=self.ident, confirm=True))

    def test_revoked_client_can_stop_subscription_before_deleting_record(self):
        self.complete_register()
        doc = access.load()
        row = doc['clients'][0]
        row['subscription'] = dict(token=access.subscriptions.token(), state='published',
                                   revision='a' * 64, synced_at=time.time())
        row['state'] = 'revoked'
        for key in ('private_key', 'peer_key', 'device_token'):
            row.pop(key, None)
        access.save(doc)
        with self.assertRaises(provider.ClientError):
            access.delete(dict(id=self.ident, confirm='测试 iPhone'))
        task = access.start('stop_subscription', dict(id=self.ident, confirm='测试 iPhone'))
        def stopped(private, read, row, persist):
            row['subscription']['state'] = 'disabled'
            persist()
        with patch.object(access.subscriptions, 'stop', side_effect=stopped) as stop:
            access.job(self.ident, task['operation_id'])
        stop.assert_called_once()
        self.assertEqual(self.row()['state'], 'revoked')
        access.delete(dict(id=self.ident, confirm='测试 iPhone'))
        self.assertEqual(access.listing()['clients'], [])

    def test_non_string_ip_from_cloud_is_rejected(self):
        reply = self.reply()
        reply['config']['interface']['addresses']['v4'] = True
        self.complete_register(enroll_reply=reply)
        self.assertEqual(self.row()['state'], 'review')

    def test_revoked_cloud_record_is_not_exportable(self):
        self.complete_register(enroll_reply=self.reply(revoked_at='2026-11-08T00:00:00Z'))
        self.assertEqual(self.row()['state'], 'review')
        self.assertFalse(access.listing()['clients'][0]['exportable'])

    def test_revoke_requires_positive_provider_ack(self):
        self.complete_register()
        task = access.start('revoke', dict(id=self.ident, confirm='测试 iPhone'))
        with patch.object(provider, 'request', return_value={'success': False}):
            access.job(self.ident, task['operation_id'])
        self.assertNotEqual(self.row()['state'], 'revoked')
        self.assertIn('private_key', self.row())
        self.assertIn('device_token', self.row())

    def test_existing_remote_identity_is_not_reenrolled(self):
        other = access.create(dict(name='已有客户端', organization='example-team'))['client']
        doc = access.load()
        access.find(doc, other['id']).update(device_id=self.cloud_id, device_token='existing-owned-token', state='review')
        access.save(doc)
        _, enroll = self.complete_register()
        enroll.assert_not_called()
        self.assertFalse(self.row().get('device_id'))
        self.assertTrue(self.row().get('post_uncertain'))
        self.assertEqual(access.find(access.load(), other['id'])['device_token'], 'existing-owned-token')

    def test_future_operation_clock_is_not_perpetually_busy(self):
        self.submit_register()
        doc = access.load()
        doc['clients'][0]['operation_at'] = time.time() + 3600
        access.save(doc)
        self.assertFalse(access.listing()['clients'][0]['busy'])

    def test_expired_never_started_registration_can_be_reauthorized(self):
        original = self.submit_register()
        doc = access.load()
        doc['clients'][0]['operation_at'] = time.time() - 181
        access.save(doc)
        fresh = self.submit_register()
        self.assertNotEqual(original['operation_id'], fresh['operation_id'])
        self.assertEqual(self.spawn.call_count, 2)

    def test_stale_started_registration_remains_unknown_and_not_retryable(self):
        self.submit_register()
        doc = access.load()
        doc['clients'][0].update(state='registering', operation_at=time.time() - 181)
        access.save(doc)
        self.assertEqual(access.listing()['clients'][0]['state'], 'unknown')
        with self.assertRaises(provider.ClientError):
            self.submit_register()
        with self.assertRaises(provider.ClientError):
            access.delete(dict(id=self.ident, confirm='测试 iPhone'))

    def test_future_queued_job_never_posts_to_cloud(self):
        task = self.submit_register()
        doc = access.load()
        doc['clients'][0]['operation_at'] = time.time() + 3600
        access.save(doc)
        with patch.object(provider, 'register') as register:
            access.job(self.ident, task['operation_id'])
        register.assert_not_called()
        self.assertEqual(self.row()['state'], 'failed')
        self.assertFalse((access.VOLATILE / (task['operation_id'] + '.json')).exists())

    def test_missing_initialized_registry_is_not_empty_install(self):
        (self.private / 'registry.json').unlink()
        with self.assertRaises(provider.ClientError):
            access.listing()

    def test_nested_private_fields_never_escape_read_only_list(self):
        doc = access.load()
        doc['clients'][0]['settings']['private_key'] = 'SYNTHETIC-DO-NOT-EXPOSE'
        access.save(doc)
        with self.assertRaises(provider.ClientError):
            access.listing()

    def test_registered_state_requires_complete_identity(self):
        doc = access.load()
        doc['clients'][0]['state'] = 'registered'
        access.save(doc)
        with self.assertRaises(provider.ClientError):
            access.listing()

    def test_duplicate_ipv6_is_not_exported(self):
        other = access.create(dict(name='Other', organization='example-team'))['client']
        doc = access.load()
        access.find(doc, other['id']).update(ipv4='100.96.0.77', ipv6='2606:4700:cf1:1000::88', state='review')
        access.save(doc)
        self.complete_register()
        self.assertEqual(self.row()['state'], 'review')
        self.assertFalse(access.listing()['clients'][0]['exportable'])

    def test_missing_idna_fails_before_post_and_remains_retryable(self):
        import codecs
        queued = self.submit_register()
        with patch.object(provider, 'new_keys', return_value=self.keys), \
             patch.object(codecs, 'lookup', side_effect=LookupError('synthetic missing codec')), \
             patch.object(provider.urllib.request, 'build_opener') as opener:
            access.job(self.ident, queued['operation_id'])
        self.assertEqual(self.row()['state'], 'failed')
        self.assertFalse(self.row().get('post_uncertain', False))
        opener.assert_not_called()
        self.submit_register()
        self.assertEqual(self.spawn.call_count, 2)

    def test_unrecognized_registration_reply_is_private_and_redacts_login(self):
        token = jwt()
        queued = access.start('register', dict(id=self.ident, token=token, accept_terms=True))
        with patch.object(provider, 'new_keys', return_value=self.keys), \
             patch.object(provider, 'register', return_value={'unexpected': {'id': self.cloud_id, 'token': self.device_token}, 'echo': token}):
            access.job(self.ident, queued['operation_id'])
        path = self.private / ('pending-enrollment-' + self.ident + '.json')
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        evidence = json.loads(path.read_text())
        self.assertEqual(evidence['client_id'], self.ident)
        self.assertEqual(evidence['operation_id'], queued['operation_id'])
        self.assertIn(self.device_token, path.read_text())
        self.assertNotIn(token, path.read_text())
        listing = json.dumps(access.listing())
        self.assertNotIn(self.device_token, listing)
        self.assertNotIn(token, listing)
        self.assertEqual(self.row()['state'], 'unknown')
        self.assertTrue(self.row()['post_uncertain'])
        with self.assertRaises(provider.ClientError):
            self.submit_register()

    def test_valid_registration_keeps_no_pending_reply_copy(self):
        self.complete_register()
        self.assertEqual(self.row()['state'], 'registered')
        self.assertFalse((self.private / ('pending-enrollment-' + self.ident + '.json')).exists())

    def test_team_namespaced_identity_round_trips_without_stripping_prefix(self):
        self.cloud_id = 't.' + self.cloud_id
        _, enroll = self.complete_register()
        self.assertEqual(self.row()['state'], 'registered')
        self.assertEqual(self.row()['device_id'], self.cloud_id)
        self.assertTrue(access.listing()['clients'][0]['exportable'])
        enroll.assert_called_once_with(self.cloud_id, self.device_token, self.keys['public_key'], '测试 iPhone')
        task = access.start('refresh', dict(id=self.ident))
        with patch.object(provider, 'request', return_value=self.reply()) as request:
            access.job(self.ident, task['operation_id'])
        request.assert_called_once_with('GET', '/reg/' + self.cloud_id, token=self.device_token)
        self.assertEqual(self.row()['state'], 'registered')
        task = access.start('revoke', dict(id=self.ident, confirm='测试 iPhone'))
        with patch.object(provider, 'request', return_value={'deleted': True}) as request:
            access.job(self.ident, task['operation_id'])
        request.assert_called_once_with('DELETE', '/reg/' + self.cloud_id, token=self.device_token)
        self.assertEqual(self.row()['state'], 'revoked')

    def test_team_alias_of_an_existing_identity_cannot_be_reenrolled(self):
        other = access.create(dict(name='Existing', organization='example-team'))['client']
        doc = access.load()
        access.find(doc, other['id']).update(device_id=self.cloud_id, device_token='existing-owned-token', state='review')
        access.save(doc)
        self.cloud_id = 't.' + self.cloud_id
        _, enroll = self.complete_register()
        enroll.assert_not_called()
        self.assertFalse(self.row().get('device_id'))
        self.assertTrue(self.row().get('post_uncertain'))
        self.assertEqual(access.find(access.load(), other['id'])['device_token'], 'existing-owned-token')

    def test_prepare_does_not_need_main_service_or_create_identity(self):
        self.assertEqual(access.prepare(), {'ready': True})
        self.assertTrue((self.run / 'requests').is_dir())
        self.assertNotIn('private_key', self.row())
        self.spawn.assert_not_called()

    def test_expired_unclaimed_authorization_is_removed(self):
        access.directory(access.VOLATILE)
        path = access.VOLATILE / ('a' * 32 + '.json')
        access.atomic(path, json.dumps({'jwt': 'synthetic-expired'}))
        os.utime(path, (time.time() - 181, time.time() - 181))
        access.expire_jobs()
        self.assertFalse(path.exists())

    def test_list_does_not_create_missing_storage(self):
        other = self.root / 'absent'
        with patch.object(access, 'PRIVATE', other):
            self.assertEqual(access.listing()['clients'], [])
        self.assertFalse(other.exists())

    def test_create_is_local_and_permissions_are_private(self):
        self.spawn.assert_not_called()
        self.assertEqual(self.client['state'], 'draft')
        self.assertEqual(self.client['settings']['port'], 443)
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.private / 'registry.json').stat().st_mode), 0o600)
        self.assertNotIn('private_key', self.row())

    def test_registry_symlink_is_refused(self):
        path = self.private / 'registry.json'
        path.unlink()
        path.symlink_to(self.root / 'missing')
        with self.assertRaises(provider.ClientError):
            access.listing()

    def test_corruption_is_not_reset(self):
        path = self.private / 'registry.json'
        path.write_text('{broken')
        with self.assertRaises(provider.ClientError):
            access.create(dict(name='Other', organization='example-team'))
        self.assertEqual(path.read_text(), '{broken')

    def test_create_rejects_injected_identity_and_bad_team(self):
        for payload in [dict(name='x', organization='example-team', device_id=self.cloud_id),
                        dict(name='x', organization='https://example.com'),
                        dict(name='x\nsecret', organization='example-team')]:
            with self.assertRaises(provider.ClientError):
                access.create(payload)

    def test_real_keys_are_independent_and_der_can_be_reopened(self):
        other = provider.new_keys()
        self.assertNotEqual(other['private_key'], self.keys['private_key'])
        public = provider.openssl(['pkey', '-inform', 'DER', '-pubout', '-outform', 'DER'],
                                  base64.b64decode(other['private_key']))
        self.assertEqual(base64.b64encode(public).decode(), other['public_key'])

    def test_token_validates_team_expiry_and_link(self):
        token = jwt()
        url = 'com.cloudflare.warp://example-team.cloudflareaccess.com/auth?token=' + token
        self.assertEqual(provider.team_token(url, 'example-team', time.time()), token)
        for token in [jwt(expiry=time.time() - 1), jwt('other-team'), 'a.b.c', 'not-token']:
            with self.assertRaises(provider.ClientError):
                provider.team_token(token, 'example-team', time.time())

    def test_registration_needs_terms_and_does_not_run_synchronously(self):
        with self.assertRaises(provider.ClientError):
            access.start('register', dict(id=self.ident, token=jwt(), accept_terms=False))
        queued = self.submit_register()
        self.assertTrue(queued['queued'])
        self.spawn.assert_called_once_with(self.ident, queued['operation_id'])
        job_file = access.VOLATILE / (queued['operation_id'] + '.json')
        self.assertEqual(stat.S_IMODE(job_file.stat().st_mode), 0o600)
        self.assertNotIn('device_id', self.row())

    def test_real_worker_waits_for_parent_lock_handoff(self):
        import subprocess
        task = self.submit_register()
        ready = self.root / 'worker-ready'
        code = """
import sys,json,pathlib
sys.path.insert(0,sys.argv[1])
import client_access as a,client_provider as p
a.PRIVATE=pathlib.Path(sys.argv[2]);a.VOLATILE=pathlib.Path(sys.argv[3])
a.read_config=lambda: {'main': {'organization': 'example-team'}}
reply=json.loads(sys.argv[7])
p.register=lambda *args:dict(id=reply['id'],token='synthetic-device-token',account=reply['account'])
def enroll(ident,token,key,name):
 reply['key']=key
 return reply
p.enroll=enroll
pathlib.Path(sys.argv[6]).write_text('ready')
a.job(sys.argv[4],sys.argv[5])
"""
        with access.locked():
            child = subprocess.Popen([sys.executable, '-B', '-c', code, str(Path(access.__file__).parent),
                                      str(self.private), str(access.VOLATILE), self.ident,
                                      task['operation_id'], str(ready), json.dumps(self.reply())],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            self.assertIsNone(child.poll())
        stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, stderr.decode())
        self.assertEqual(stdout, b'')
        self.assertEqual(self.row()['state'], 'registered')

    def test_double_submit_is_rejected_before_cloud_write(self):
        self.submit_register()
        with self.assertRaises(provider.ClientError):
            self.submit_register()
        self.assertEqual(self.spawn.call_count, 1)

    def test_worker_registers_only_new_identity_and_clears_jwt(self):
        reg, enroll = self.complete_register()
        row = self.row()
        self.assertEqual(row['state'], 'registered')
        enroll.assert_called_once_with(self.cloud_id, self.device_token, self.keys['public_key'], '测试 iPhone')
        self.assertEqual(list(access.VOLATILE.iterdir()), [])
        self.assertNotIn('jwt', json.dumps(row))
        self.assertFalse(access.listing()['clients'][0]['tunnel_verified'])
        self.assertFalse(access.listing()['clients'][0]['exit_verified'])

    def test_public_list_omits_all_credentials(self):
        self.complete_register()
        raw = json.dumps(access.listing())
        for value in [self.keys['private_key'], self.device_token, 'bootstrap_key', 'device_token', 'private_key', 'peer_key']:
            self.assertNotIn(value, raw)

    def test_post_timeout_is_unknown_not_retryable_or_deletable(self):
        self.complete_register(register_error=provider.ClientError('response unknown', uncertain=True))
        self.assertEqual(self.row()['state'], 'unknown')
        self.assertTrue(self.row()['post_uncertain'])
        self.assertEqual(len(self.row()['bootstrap_fingerprint']), 64)
        self.assertEqual(len(self.row()['bootstrap_serial']), 36)
        with self.assertRaises(provider.ClientError):
            self.submit_register()
        with self.assertRaises(provider.ClientError):
            access.delete(dict(id=self.ident, confirm='测试 iPhone'))

    def test_definite_registration_denial_can_be_retried_explicitly(self):
        self.complete_register(register_error=provider.ClientError('permission denied'))
        self.assertEqual(self.row()['state'], 'failed')
        self.submit_register()
        self.assertEqual(self.spawn.call_count, 2)

    def test_partial_enrollment_can_be_reconciled_with_get(self):
        self.complete_register(enroll_error=provider.ClientError('timeout', uncertain=True))
        row = self.row()
        self.assertEqual(row['state'], 'review')
        self.assertEqual(row['device_id'], self.cloud_id)
        with self.assertRaises(provider.ClientError):
            self.submit_register()
        task = access.start('refresh', dict(id=self.ident))
        with patch.object(provider, 'request', return_value=self.reply()) as read:
            access.job(self.ident, task['operation_id'])
        read.assert_called_once_with('GET', '/reg/' + self.cloud_id, token=self.device_token)
        self.assertEqual(self.row()['state'], 'registered')

    def test_personal_warp_fallback_is_rejected_before_enrollment(self):
        task = self.submit_register()
        with patch.object(provider, 'new_keys', return_value=self.keys), patch.object(provider, 'register', return_value=dict(id=self.cloud_id, token=self.device_token, account=dict(account_type='free'))), patch.object(provider, 'enroll') as enroll:
            access.job(self.ident, task['operation_id'])
        enroll.assert_not_called()
        self.assertEqual(self.row()['state'], 'review')
        self.assertEqual(self.row()['device_id'], self.cloud_id)

    def test_wrong_team_disables_export_but_preserves_cleanup_ownership(self):
        self.complete_register(enroll_reply=self.reply(org='wrong-team'))
        self.assertEqual(self.row()['state'], 'review')
        self.assertEqual(self.row()['device_id'], self.cloud_id)
        with self.assertRaises(provider.ClientError):
            access.export(dict(id=self.ident, confirm=True))

    def test_wrong_public_key_is_not_reported_as_registered(self):
        self.complete_register(enroll_reply=self.reply(key='wrong-key'))
        self.assertEqual(self.row()['state'], 'review')
        self.assertFalse(access.listing()['clients'][0]['exportable'])

    def test_duplicate_ip_is_not_exported(self):
        other = access.create(dict(name='Other', organization='example-team'))['client']
        doc = access.load()
        access.find(doc, other['id']).update(ipv4='100.96.0.88', state='review')
        access.save(doc)
        self.complete_register()
        self.assertEqual(self.row()['state'], 'review')
        self.assertIn('重复', self.row()['error'])

    def test_wrong_operation_cannot_consume_job_or_register(self):
        task = self.submit_register()
        with patch.object(provider, 'register') as register:
            with self.assertRaises(provider.ClientError):
                access.job(self.ident, 'b' * 32)
        register.assert_not_called()
        self.assertTrue((access.VOLATILE / (task['operation_id'] + '.json')).exists())

    def test_stale_worker_does_not_report_success(self):
        self.submit_register()
        doc = access.load()
        doc['clients'][0]['operation_at'] = time.time() - 181
        access.save(doc)
        row = access.listing()['clients'][0]
        self.assertEqual(row['state'], 'failed')
        self.assertFalse(row['busy'])
        self.assertFalse(row['exportable'])

    def test_export_only_after_explicit_confirmation_with_encoded_parameters(self):
        with self.assertRaises(provider.ClientError):
            access.export(dict(id=self.ident, confirm=True))
        self.complete_register()
        with self.assertRaises(provider.ClientError):
            access.export(dict(id=self.ident, confirm=False))
        result = access.export(dict(id=self.ident, confirm=True))
        uri = urlsplit(result['uri'])
        params = parse_qs(uri.query)
        self.assertEqual(uri.port, 443)
        self.assertEqual(params['privateKey'], [self.keys['private_key']])
        self.assertEqual(params['publicKey'], [self.keys['public_key']])
        self.assertEqual(params['ip'], ['100.96.0.88'])
        self.assertNotIn('vnet', params)
        self.assertFalse(result['exit_verified'])

    def test_settings_are_local_only_and_do_not_change_existing_identity(self):
        self.complete_register()
        old = self.row()
        cfg = dict(old['settings'], dns='1.1.1.1', port=500)
        with patch.object(provider, 'request') as network:
            access.edit(dict(id=self.ident, settings=cfg))
        network.assert_not_called()
        row = self.row()
        self.assertEqual(row['private_key'], old['private_key'])
        self.assertEqual(row['device_id'], old['device_id'])
        self.assertEqual(parse_qs(urlsplit(access.export(dict(id=self.ident, confirm=True))['uri']).query)['dns'], ['1.1.1.1'])

    def test_bad_settings_and_vnet_are_rejected(self):
        for cfg in [dict(endpoint='10.77.50.1'), dict(endpoint='162.159.197.2/path'),
                    dict(port=True), dict(port=2408), dict(dns='127.0.0.1'), dict(dns='https://evil.example'),
                    dict(mtu=400), dict(keepalive=0), dict(vnet='usa'), dict(privateKey='injected')]:
            with self.assertRaises(provider.ClientError):
                access.settings(cfg)

    def test_revoke_requires_name_and_only_targets_owned_id(self):
        self.complete_register()
        with self.assertRaises(provider.ClientError):
            access.start('revoke', dict(id=self.ident, confirm='wrong'))
        task = access.start('revoke', dict(id=self.ident, confirm='测试 iPhone'))
        with patch.object(provider, 'request', return_value={'deleted': True}) as delete:
            access.job(self.ident, task['operation_id'])
        delete.assert_called_once_with('DELETE', '/reg/' + self.cloud_id, token=self.device_token)
        self.assertEqual(self.row()['state'], 'revoked')
        for key in ('private_key', 'device_token', 'peer_key'):
            self.assertNotIn(key, self.row())
        access.delete(dict(id=self.ident, confirm='测试 iPhone'))
        self.assertEqual(access.listing()['clients'], [])

    def test_unconfirmed_revoke_preserves_ownership_and_secrets(self):
        self.complete_register()
        task = access.start('revoke', dict(id=self.ident, confirm='测试 iPhone'))
        with patch.object(provider, 'request', side_effect=provider.ClientError('timeout', uncertain=True)):
            access.job(self.ident, task['operation_id'])
        self.assertEqual(self.row()['state'], 'unknown')
        self.assertIn('device_token', self.row())
        with self.assertRaises(provider.ClientError):
            access.delete(dict(id=self.ident, confirm='测试 iPhone'))

    def test_delete_registered_client_is_refused(self):
        self.complete_register()
        with self.assertRaises(provider.ClientError):
            access.delete(dict(id=self.ident, confirm='测试 iPhone'))

    def test_bound_request_and_replay_protection(self):
        ident = stage(access.RUN / 'client-input', dict(name='二号', organization='example-team'))
        result = access.dispatch('create', ident)
        self.assertEqual(result['request_id'], ident.split(':')[0])
        with self.assertRaises(Exception):
            access.dispatch('create', ident)
        self.assertEqual(len(access.listing()['clients']), 2)

    def test_expired_client_requests_are_cleaned(self):
        ident = stage(access.RUN / 'client-input', dict(name='二号', organization='example-team'))
        path = access.RUN / 'requests' / ('client-input-' + ident.split(':')[0] + '.json')
        os.utime(path, (time.time() - 400, time.time() - 400))
        access.reap_requests(access.RUN)
        self.assertFalse(path.exists())

    def test_read_acl_cannot_execute_mutations_or_read_credentials(self):
        package = Path(__file__).resolve().parents[1]
        acl = json.loads((package / 'root/usr/share/rpcd/acl.d/luci-app-routepilot.json').read_text())['luci-app-routepilot']
        self.assertNotIn('/usr/libexec/routepilot-clients', acl['read']['file'])
        self.assertEqual(acl['read']['file']['/usr/libexec/routepilot-clients-list'], ['exec'])
        self.assertFalse(any('/etc/routepilot/clients' in k for k in acl['read']['file']))
        self.assertNotIn('dispatch', (package / 'root/usr/libexec/routepilot-clients-list').read_text().split('import json')[1])

    def test_client_modules_have_no_main_control_dependencies(self):
        import ast
        for module in (access, provider):
            tree = ast.parse(Path(module.__file__).read_text())
            imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
            self.assertFalse(set(imports) & {'mplane', 'masque', 'auth_control', 'endpoint_control', 'warp_control'})
            self.assertNotIn('warp-cli', Path(module.__file__).read_text().replace('warp-cli,', ''))


class ProviderTransportTest(unittest.TestCase):
    def test_team_registration_id_is_accepted_but_unknown_namespaces_are_not(self):
        ident = 't.00000000-0000-4000-8000-000000000123'
        self.assertEqual(provider.response_identity({'id': ident, 'token': 'synthetic-token'}), (ident, 'synthetic-token'))
        for bad in ['x.' + ident[2:], 't.../../etc/passwd', 't.' + 'a' * 36]:
            with self.assertRaises(provider.ClientError):
                provider.response_identity({'id': bad, 'token': 'synthetic-token'})

    def test_team_id_path_uses_exact_owned_namespace(self):
        class Response(io.BytesIO):
            status = 200
        ident = 't.00000000-0000-4000-8000-000000000123'
        with patch.object(provider.urllib.request, 'build_opener') as factory:
            factory.return_value.open.return_value = Response(b'{"ok":true}')
            self.assertEqual(provider.request('GET', '/reg/' + ident, token='synthetic'), {'ok': True})
            self.assertEqual(factory.return_value.open.call_args.args[0].full_url, provider.API + '/reg/' + ident)

    def test_missing_idna_dependency_is_rejected_before_network(self):
        import codecs
        with patch.object(codecs, 'lookup', side_effect=LookupError('synthetic missing codec')), \
             patch.object(provider.urllib.request, 'build_opener') as opener:
            with self.assertRaises(provider.ClientError) as error:
                provider.register('synthetic', 'synthetic', '00000000-0000-4000-8000-000000000001')
        self.assertFalse(error.exception.uncertain)
        opener.assert_not_called()

    def respond(self, method, status, body):
        class Response(io.BytesIO):
            pass
        response = Response(body if isinstance(body, bytes) else json.dumps(body).encode())
        response.status = status
        with patch.object(provider.urllib.request, 'build_opener') as factory:
            factory.return_value.open.return_value = response
            return provider.request(method, '/reg/00000000-0000-4000-8000-000000000123', token='synthetic')

    def test_http_success_with_explicit_api_failure_is_not_success(self):
        with self.assertRaises(provider.ClientError):
            self.respond('DELETE', 200, {'success': False, 'errors': [{'message': 'SYNTHETIC-SENSITIVE'}]})

    def test_accepted_delete_is_not_completed_delete(self):
        with self.assertRaises(provider.ClientError) as error:
            self.respond('DELETE', 202, {'success': True})
        self.assertTrue(error.exception.uncertain)

    def test_unexpected_delete_payload_is_not_acknowledgment(self):
        with self.assertRaises(provider.ClientError):
            self.respond('DELETE', 200, {'status': 'pending'})

    def test_delete_completion_has_typed_ack(self):
        self.assertEqual(self.respond('DELETE', 204, b''), {'deleted': True})
        self.assertEqual(self.respond('DELETE', 200, {'success': True}), {'deleted': True})

    def test_redirect_after_write_is_uncertain_and_never_retried(self):
        import urllib.error
        with patch.object(provider.urllib.request, 'build_opener') as factory:
            factory.return_value.open.side_effect = urllib.error.HTTPError(provider.API, 302, 'found', {}, io.BytesIO())
            with self.assertRaises(provider.ClientError) as error:
                provider.request('POST', '/reg', jwt='synthetic', body={})
        self.assertTrue(error.exception.uncertain)

    def test_protocol_rejects_arbitrary_targets_before_network(self):
        with patch.object(provider.urllib.request, 'build_opener') as network:
            for path in ['/reg/../../etc/config/routepilot', 'https://evil.example', '/reg\nHeader: secret']:
                with self.assertRaises(provider.ClientError):
                    provider.request('GET', path)
        network.assert_not_called()

    def test_redirect_is_not_followed(self):
        self.assertIsNone(provider.NoRedirect().redirect_request(None, None, 302, 'found', {}, 'https://evil.example'))

    def test_http_errors_do_not_echo_cloud_response_or_token(self):
        import urllib.error
        for code, uncertain in [(401, False), (403, False), (429, False), (500, True)]:
            error = urllib.error.HTTPError(provider.API, code, 'SECRET', {}, io.BytesIO(b'SECRET'))
            with patch.object(provider.urllib.request, 'build_opener') as factory:
                factory.return_value.open.side_effect = error
                with self.assertRaises(provider.ClientError) as result:
                    provider.request('POST', '/reg', jwt='SECRET', body={})
                self.assertEqual(result.exception.uncertain, uncertain)
                self.assertNotIn('SECRET', str(result.exception))

    def test_delete_404_does_not_prove_remote_revocation(self):
        import urllib.error
        with patch.object(provider.urllib.request, 'build_opener') as factory:
            factory.return_value.open.side_effect = urllib.error.HTTPError(provider.API, 404, 'not found', {}, io.BytesIO(b'unknown API route'))
            with self.assertRaises(provider.ClientError) as error:
                provider.request('DELETE', '/reg/00000000-0000-4000-8000-000000000123', token='synthetic')
            self.assertTrue(error.exception.uncertain)



if __name__ == '__main__':
    unittest.main()
