import contextlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'root/usr/lib/routepilot'))
from input_fixture import stage
import masque as core
import rules_catalog as rules
from public_errors import ControlError


class RulesCatalogTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.rules, self.run = root / 'rules', root / 'run'
        self.rules.mkdir(); self.run.mkdir()
        self.cfg = {'main': {'enabled': '1', 'update_hours': '24', 'gfw_url': 'https://example.com/list?token=secret'}}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(core, 'RULES', self.rules))
        self.stack.enter_context(patch.object(core, 'RUN', self.run))
        self.stack.enter_context(patch.object(core, 'read_config', side_effect=lambda: self.cfg))

    def publish(self, values, **meta):
        return core.commit_rules([(self.rules / (kind + '.txt'), value) for kind, value in values.items()], dict(time=int(time.time()), **meta))

    def token(self, payload):
        return stage(self.run / 'rules-input', payload)

    def query(self, **changes):
        payload = dict(kind='gfw', view='current', search='', page=1, page_size=2, revision=rules.info()['current']['revision'])
        payload.update(changes)
        return rules.query(self.token(payload))

    def seed_decline(self):
        self.publish({'gfw': ''.join('host%d.example.com\n' % n for n in range(100))})
        with patch.object(core, 'net', return_value=SimpleNamespace(stdout='only.example.com\n')):
            with self.assertRaises(rules.RulesDecline):
                core.update_rules()
        return rules.info()

    def test_paginated_search_and_persisted_changes_are_bound_to_current_revision(self):
        first = self.publish({'gfw': 'a.example.com\nb.example.com\nc.example.com\n'})
        result = self.query(page=2)
        self.assertEqual(result['entries'], ['c.example.com'])
        self.assertEqual(result['total'], 3)
        self.assertTrue(result['request_id'])
        self.assertEqual(self.query(search='B.EXAMPLE')['entries'], ['b.example.com'])
        second = self.publish({'gfw': 'a.example.com\nc.example.com\nd.example.com\n'})
        self.assertNotEqual(second['revision'], first['revision'])
        self.assertEqual(self.query(view='added')['entries'], ['d.example.com'])
        self.assertEqual(self.query(view='removed')['entries'], ['b.example.com'])
        with self.assertRaises(ControlError):
            self.query(revision=first['revision'])

    def test_query_rejects_unbounded_inputs_unknown_keys_and_path_selection(self):
        self.publish({'gfw': 'example.com\n'})
        for changes in [dict(kind='../../etc/passwd'), dict(view='../bad'), dict(page=0), dict(page=True), dict(page_size=101), dict(search='x'*254), dict(extra=True)]:
            with self.subTest(changes=changes), self.assertRaises(ControlError):
                self.query(**changes)

    def test_claimed_query_cannot_replay(self):
        current = rules.info()['current']['revision']
        token = self.token(dict(revision=current))
        self.assertEqual(rules.query(token)['entries'], [])
        with self.assertRaises(ControlError):
            rules.query(token)

    def test_legacy_revision_and_fresh_runtime_application_are_truthful(self):
        (self.rules / 'gfw.txt').write_text('example.com\n')
        first = rules.info()
        self.assertEqual(first['current']['revision'], rules.directory_revision(self.rules))
        self.assertIsNone(first['previous'])
        status = dict(state='running', time=time.time(), rules_revision=first['current']['revision'])
        (self.run / 'status.json').write_text(json.dumps(status))
        self.assertEqual(rules.info()['application']['state'], 'applied')
        status['rules_revision'] = 'old'
        (self.run / 'status.json').write_text(json.dumps(status))
        self.assertEqual(rules.info()['application']['state'], 'pending')
        status['time'] -= 20
        (self.run / 'status.json').write_text(json.dumps(status))
        self.assertEqual(rules.info()['application']['state'], 'unknown')

    def test_info_uses_metadata_without_reading_every_rule(self):
        self.publish({'gfw': 'example.com\n'})
        with patch.object(rules, 'content', side_effect=AssertionError('large rule read')):
            self.assertEqual(rules.info()['current']['counts']['gfw'], 1)

    def test_decline_preserves_whole_current_batch_and_exposes_reviewable_candidate(self):
        initial = self.publish({'china': '1.0.1.0/24\n', 'gfw': ''.join('host%d.example.com\n' % n for n in range(100))})
        with patch.object(core, 'net', return_value=SimpleNamespace(stdout='only.example.com\n')):
            with self.assertRaises(rules.RulesDecline):
                core.update_rules()
        document = rules.info()
        self.assertEqual(document['current']['revision'], initial['revision'])
        self.assertEqual(document['candidate']['declines'], {'gfw': {'before': 100, 'after': 1}})
        self.assertEqual(json.loads((self.run/'update.json').read_text())['state'], 'blocked')
        self.assertEqual(self.query(view='candidate')['entries'], ['only.example.com'])
        self.assertEqual(self.query(view='candidate_added')['entries'], ['only.example.com'])
        self.assertEqual(self.query(view='candidate_removed')['total'], 100)
        self.assertEqual(self.query(kind='china', view='candidate')['entries'], ['1.0.1.0/24'])

    def test_accept_is_bound_to_candidate_and_current_then_runs_once(self):
        doc = self.seed_decline()
        payload = dict(revision=doc['current']['revision'], candidate_revision=doc['candidate']['revision'], confirm=True)
        token = self.token(payload)
        result = rules.accept(token)
        self.assertEqual(result['revision'], doc['candidate']['revision'])
        self.assertIsNone(rules.info()['candidate'])
        self.assertEqual(self.query()['entries'], ['only.example.com'])
        with self.assertRaises(ControlError):
            rules.accept(token)
        with self.assertRaises(ControlError):
            rules.accept(self.token(payload))

    def test_accept_rejects_expiry_tampering_and_changed_sources(self):
        for condition in ('expired', 'tampered', 'source'):
            with self.subTest(condition=condition):
                doc = self.seed_decline()
                payload = dict(revision=doc['current']['revision'], candidate_revision=doc['candidate']['revision'], confirm=True)
                record_path = self.rules / '.candidate.json'
                record = json.loads(record_path.read_text())
                if condition == 'expired':
                    record['expires_at'] = time.time() - 1
                    record_path.write_text(json.dumps(record))
                elif condition == 'tampered':
                    (self.rules/record['path']/'gfw.txt').write_text('tampered.example.com\n')
                else:
                    self.cfg['main']['gfw_url'] = 'https://example.com/changed'
                with self.assertRaises(ControlError):
                    rules.accept(self.token(payload))
                self.assertEqual(rules.info()['current']['revision'], doc['current']['revision'])

    def test_rollback_preserves_exact_previous_categories_without_network(self):
        old = self.publish({'gfw': 'old.example.com\n'})
        current = self.publish({'gfw': 'new.example.com\n', 'china_domains': 'new.example.cn\n'})
        payload = dict(revision=current['revision'], previous_revision=old['revision'], confirm=True)
        with patch.object(core, 'net', side_effect=AssertionError('network')):
            result = rules.rollback(self.token(payload))
        self.assertEqual(result['revision'], old['revision'])
        self.assertEqual(self.query()['entries'], ['old.example.com'])
        self.assertEqual(self.query(kind='china_domains')['entries'], [])
        self.assertEqual(rules.info()['previous']['revision'], current['revision'])

    def test_same_content_update_retains_last_distinct_previous_version(self):
        old = self.publish({'gfw': 'old.example.com\n'})
        self.publish({'gfw': 'new.example.com\n'})
        self.publish({'gfw': 'new.example.com\n'})
        document = rules.info()
        self.assertEqual(document['previous']['revision'], old['revision'])
        self.assertEqual(len(list((self.rules/'.generations').iterdir())), 2)

    def test_mutations_serialize_against_downloads(self):
        old = self.publish({'gfw': 'old.example.com\n'})
        current = self.publish({'gfw': 'new.example.com\n'})
        with rules.operation_lock(self.rules):
            with self.assertRaises(BlockingIOError):
                core.update_rules()
            payload = dict(revision=current['revision'], previous_revision=old['revision'], confirm=True)
            with self.assertRaises(BlockingIOError):
                rules.rollback(self.token(payload))
        self.assertEqual(rules.info()['current']['revision'], current['revision'])

    def test_source_summary_strips_credentials_and_unsafe_current_pointer_rejected(self):
        self.assertEqual(rules.safe_source('https://user:password@example.com/list?token=secret#secret'), 'https://example.com/list')
        self.assertEqual(rules.safe_source('https://example.com/api-key/secret'), 'https://example.com/')
        (self.rules/'current').symlink_to(self.run)
        with self.assertRaises(ControlError):
            rules.info()


if __name__ == '__main__':
    unittest.main()
