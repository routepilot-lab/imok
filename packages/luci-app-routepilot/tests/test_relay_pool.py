# Synthetic unit-test addresses; no live router, tenant or user configuration.
from input_fixture import stage
import contextlib, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'root/usr/lib/routepilot'))
import relay_pool as r
import worker_backend as w

NODE=dict(name='test',uuid='11111111-1111-4111-8111-111111111111',address='104.21.39.117',port=443,sni='worker.example.com',path='/tunnel?ed=2048')
ROWS=[dict(address='relay1.example.com:443',source='public docs',source_url='https://example.com/docs',enabled=True),dict(address='relay2.example.com:8443',source='user',source_url='',enabled=True)]

class RelayTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
  for module,key,value in [(r,'PRIVATE',self.root/'private'),(r,'POOL',self.root/'private/pool'),(r,'INPUT',self.root/'input'),(r,'TEST',self.root/'test'),(r,'OBSERVED',self.root/'observed'),(r,'RUN',self.root),(w,'RUN',self.root),(w,'NODE',self.root/'private/node')]:self.stack.enter_context(patch.object(module,key,value))
  r.PRIVATE.mkdir();self.stack.enter_context(patch.object(w,'guard',return_value=contextlib.nullcontext()))
  self.stack.enter_context(patch.object(w,'load',return_value=NODE));self.stack.enter_context(patch.object(w,'boot_id',return_value='boot'))
  self.stack.enter_context(patch.object(w,'uptime',return_value=110));self.stack.enter_context(patch.object(r,'read_config',return_value={'main':{'backend':'masque'}}))
 def save(self,rows=ROWS):r.INPUT.write_text(json.dumps({'candidates':rows}));return r.save(stage(r.INPUT))
 def proof(self,**changes):
  row=dict(address=ROWS[0]['address'],fingerprint=w.relay_fingerprint(NODE),boot='boot',finished_uptime=100,forced=True,profile=r.PROFILE,checks=[{'ok':True},{'ok':True}]);row.update(changes);return row
 def test_public_addresses_and_bounded_pool(self):
  self.assertEqual(r.address(' Relay.Example.com:0443 '),'relay.example.com:443')
  for bad in ['127.0.0.1:443','10.77.50.1:443','100.64.1.2:443','192.0.2.1:443','https://example.com:443','example.com:0','example.com:65536','[::1]:443']:
   with self.subTest(bad=bad),self.assertRaises(ValueError):r.address(bad)
  for bad in [[],ROWS*4,[ROWS[0],ROWS[0]],[{**ROWS[0],'enabled':False}],[{**ROWS[0],'enabled':'false'}],[{**ROWS[0],'source_url':'https://example.com/?token=private'}],[{**ROWS[0],'source_url':'http://example.com/'}]]:
   with self.subTest(bad=bad),self.assertRaises(ValueError):r.spec(bad)
 def test_save_is_private_and_preserves_node(self):
  w.NODE.write_text('original');out=self.save();self.assertTrue(out['saved']);self.assertEqual(w.NODE.read_text(),'original');self.assertEqual(r.POOL.stat().st_mode&0o777,0o600);self.assertFalse(r.INPUT.exists())
  self.assertEqual([x['address'] for x in r.load()],[x['address'] for x in ROWS])
 def test_active_backup_blocks_saving_and_removes_input(self):
  with patch.object(r,'read_config',return_value={'main':{'backend':'worker'}}),self.assertRaises(r.RelayError):self.save()
  self.assertFalse(r.INPUT.exists());self.assertFalse(r.POOL.exists())
 def test_legacy_user_relays_migrate_without_claiming_upstream(self):
  (r.PRIVATE/'cloud.json').write_text(json.dumps({'proxyip':'a.example.com:443,b.example.com:443'}))
  self.assertEqual(len(r.load()),2);self.assertIn('用户',r.load()[0]['source']);self.assertFalse(r.POOL.exists())
 def test_forced_probe_has_exactly_one_candidate_and_keeps_identity(self):
  node=r.with_pool({**NODE,'path':'/tunnel?rp_relays=old.example.com:443&rp_force=1&ed=123'},[ROWS[0]['address']],True)
  q=parse_qs(urlsplit(node['path']).query);self.assertEqual(q['rp_relays'],[ROWS[0]['address']]);self.assertEqual(q['rp_force'],['1']);self.assertEqual(q['rp_probe'],['1']);self.assertEqual(q['ed'],['123']);self.assertEqual(node['uuid'],NODE['uuid'])
  with self.assertRaises(r.RelayError):r.with_pool(NODE,[x['address'] for x in ROWS],True)
 def test_relay_order_changes_do_not_change_core_but_identity_does(self):
  changed=r.with_pool(NODE,[x['address'] for x in ROWS])
  self.assertEqual(w.relay_fingerprint(changed),w.relay_fingerprint(NODE));self.assertNotEqual(w.fingerprint(changed),w.fingerprint(NODE))
  self.assertNotEqual(w.relay_fingerprint({**NODE,'sni':'other.example.com'}),w.relay_fingerprint(NODE))
 def test_proofs_expire_or_fail_closed(self):
  self.assertTrue(r.eligible(self.proof(),NODE))
  for change in [{'boot':'other'},{'finished_uptime':-200},{'finished_uptime':111},{'forced':False},{'checks':[{'ok':True}]},{'checks':[{'ok':True},{'ok':False}]}]:self.assertFalse(r.eligible(self.proof(**change),NODE))
 def test_apply_requires_every_enabled_endpoint_to_pass_and_preserves_main(self):
  self.save();w.NODE.write_text('original')
  r.TEST.write_text(json.dumps({'state':'done','results':[self.proof()]}))
  with patch.object(r,'require_support') as support,self.assertRaises(r.RelayError):r.apply()
  support.assert_not_called();self.assertEqual(w.NODE.read_text(),'original')
  r.TEST.write_text(json.dumps({'state':'done','results':[self.proof(),self.proof(address=ROWS[1]['address'])]}))
  with patch.object(r,'require_support'):r.apply()
  changed=json.loads(w.NODE.read_text());self.assertEqual(changed['address'],NODE['address']);self.assertEqual(changed['uuid'],NODE['uuid']);self.assertEqual(parse_qs(urlsplit(changed['path']).query)['rp_relays'],[','.join(x['address'] for x in ROWS)])
 def test_unsupported_worker_never_claims_candidate_success(self):
  self.save();fd=os.open(self.root/'lock',os.O_CREAT|os.O_RDWR,0o600)
  with patch.object(r,'request',return_value={'service':'routepilot-worker','build':'v1'}),patch.object(w,'spawn') as spawn:r.test(fd)
  spawn.assert_not_called();out=json.loads(r.TEST.read_text());self.assertEqual(out['state'],'failed');self.assertEqual(out['results'],[]);self.assertIn('模板',out['message'])
 def test_global_deadline_is_not_swallowed_by_per_candidate_errors(self):
  import worker_scan
  self.save();fd=os.open(self.root/'lock2',os.O_CREAT|os.O_RDWR,0o600)
  with patch.object(r,'require_support'),patch.object(w,'pinned',side_effect=lambda x:x),patch.object(w,'spawn',return_value=object()),patch.object(w,'ready'),patch.object(w,'stop_process') as stop,patch.object(worker_scan,'measure',side_effect=r.ProbeDeadline) as measure:r.test(fd)
  self.assertEqual(measure.call_count,1);stop.assert_called_once();out=json.loads(r.TEST.read_text());self.assertEqual(out['state'],'failed');self.assertIn('超时',out['message'])
 def test_runtime_projection_filters_untrusted_fields(self):
  self.save()
  with patch.object(r,'require_support'),patch.object(r,'request',return_value={'observed':{ROWS[0]['address']:{'successes':2,'last':'connected','token':'secret'},'secret':{'successes':999}}}):r.runtime_status()
  out=json.loads(r.OBSERVED.read_text());self.assertEqual(out['scope'],'current-isolate');self.assertNotIn('secret',r.OBSERVED.read_text());self.assertEqual(out['observed'][ROWS[0]['address']]['successes'],2)

if __name__=='__main__':unittest.main()
