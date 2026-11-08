import hashlib,json,sys,tempfile,unittest,contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'root/usr/lib/routepilot'))
import endpoint_control as e
import mplane,worker_backend,masque

class Endpoints(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.p=Path(self.temp.name)
  for name,value in {'ROOT':self.p/'client','DIRECTORY':self.p,'RUN':self.p/'run','CONFIG':self.p/'cfg.json','CACHE':self.p/'cache.json','STATE':self.p/'run/state.json','INPUT':self.p/'run/input','APPLIED':self.p/'applied.json','TRANSACTION':self.p/'transaction.json','WARNING':self.p/'run/warning.json','RECOVERY':self.p/'recovery.json','PROCESS_BLOCK':self.p/'run/process-block.json'}.items():
   p=patch.object(e,name,value);p.start();self.addCleanup(p.stop)
  e.ROOT.mkdir();e.RUN.mkdir();(e.ROOT/'settings.json').write_text(json.dumps({'override_warp_endpoint':'162.159.197.2:8443'}))
  self.calls=[];self.svc=SimpleNamespace(update_job=None,exit_ip='8.8.8.8',backend='masque',compiled={'ready':True},plane=SimpleNamespace(owned=True,connect=Mock(),probe=Mock(return_value='8.8.8.8')),suspend_capture=Mock(return_value=True))
  for p in [patch.object(worker_backend,'guard',contextlib.nullcontext),patch.object(e,'capability',return_value={'supported':True}),patch.object(masque,'output'),patch.object(mplane,'cli',side_effect=self.cli),patch.object(e,'read_config',side_effect=lambda:{'main':self.main})]:p.start();self.addCleanup(p.stop)
  self.main={'exit':'usa','vnet_usa':'00000000-0000-0000-0000-000000000001','expected_usa':'8.8.8.8'}
 def save_input(self):
  if e.INPUT.exists() and not e.INPUT.is_symlink() and e.INPUT.stat().st_size<=1024:
   try:payload=json.loads(e.INPUT.read_text());e.INPUT.write_text(json.dumps({'request_id':'a'*32,'payload':payload}))
   except ValueError:pass
  return e.save('a'*32+':'+hashlib.sha256(e.INPUT.read_bytes()).hexdigest())
 def cli(self,*args,**kw):
  self.calls.append(args)
  if args[:3]==('tunnel','endpoint','set'):(e.ROOT/'settings.json').write_text(json.dumps({'override_warp_endpoint':args[3]}))
  if args==('tunnel','endpoint','reset'):(e.ROOT/'settings.json').write_text('{}')
  return SimpleNamespace(returncode=0,stdout='')
 def config(self,mode='manual',addr='162.159.197.2:443',rev='one'):
  e.CONFIG.write_text(json.dumps({'mode':mode,'address':addr,'hours':24,'revision':rev}))
  if not e.APPLIED.exists():e.APPLIED.write_text('{}')
 def test_default_preserves_without_client_mutation(self):
  self.assertFalse(e.Manager().tick(self.svc,self.main));self.assertEqual(self.calls,[]);self.assertFalse(e.CONFIG.exists())
 def test_refresh_sanitizes_and_never_connects(self):
  c={'tunnel_key_data':{'tunnel_type':'masque'},'endpoints':[{'v4':'162.159.197.2:443','v6':'[2606:4700:102::2]:443'}],'token':'SENSITIVE'}
  (e.ROOT/'conf.json').write_text(json.dumps(c));e.refresh();self.assertNotIn('SENSITIVE',e.CACHE.read_text());self.assertEqual(self.calls,[])
 def test_failed_refresh_retains_success(self):
  e.CACHE.write_text('{"entries":["162.159.197.2:443"]}');before=e.CACHE.read_bytes();e.refresh();self.assertEqual(e.CACHE.read_bytes(),before);self.assertEqual(e.info()['collection']['state'],'error')
 def test_malformed_candidate_retains_cache(self):
  e.CACHE.write_text('{}');(e.ROOT/'conf.json').write_text(json.dumps({'tunnel_key_data':{'tunnel_type':'masque'},'endpoints':[{'v4':'127.0.0.1:443'}]}));e.refresh();self.assertEqual(e.CACHE.read_text(),'{}')
 def test_rejects_local_multicast_injection_and_ipv6_application(self):
  for addr in ['127.0.0.1:443','100.96.0.22:443','224.0.0.1:443','192.168.8.1:443','169.254.169.254:80','8.8.8.8:443;id','8.8.8.8:0','domain.com:443','[2606:4700:102::2]:443']:
   with self.subTest(addr=addr),self.assertRaises(ValueError):e.validate({'mode':'manual','address':addr})
 def test_symlink_source_rejected(self):
  target=self.p/'other';target.write_text('{}');(e.ROOT/'conf.json').symlink_to(target);e.refresh();self.assertEqual(e.info()['collection']['state'],'error')
 def test_same_endpoint_is_reverified_for_explicit_apply(self):
  self.config(addr='162.159.197.2:8443');self.assertTrue(e.Manager().tick(self.svc,self.main));self.svc.suspend_capture.assert_called_once();self.svc.plane.probe.assert_called_once();self.assertTrue(e.read(e.APPLIED)['exit_verified'])
 def test_auto_already_unfixed_is_still_verified(self):
  (e.ROOT/'settings.json').write_text('{}');self.config(mode='auto');self.assertTrue(e.Manager().tick(self.svc,self.main));self.svc.plane.probe.assert_called_once();self.assertTrue(e.read(e.APPLIED)['connected'])
 def test_same_endpoint_verification_failure_restores_and_checks_original(self):
  self.config(addr='162.159.197.2:8443');self.svc.plane.probe.side_effect=['1.1.1.1','8.8.8.8'];self.assertTrue(e.Manager().tick(self.svc,self.main));self.assertEqual(e.override(),'162.159.197.2:8443');self.assertTrue(e.read(e.APPLIED)['rollback_confirmed'])
 def test_manual_withdraws_before_mutation_and_verifies_exit(self):
  self.config();self.svc.suspend_capture.side_effect=lambda:self.calls.append(('withdraw',)) or True
  self.assertTrue(e.Manager().tick(self.svc,self.main));self.assertEqual(self.calls[0],('withdraw',));self.assertEqual(e.override(),'162.159.197.2:443');self.assertTrue(e.read(e.APPLIED)['exit_verified']);self.svc.plane.probe.assert_called_once()
 def test_unknown_override_never_writes(self):
  self.config();(e.ROOT/'settings.json').unlink()
  self.assertFalse(e.Manager().tick(self.svc,self.main))
  self.assertEqual(self.calls,[])
 def test_cleanup_failure_never_mutates_client(self):
  self.config();self.svc.suspend_capture.return_value=False
  with self.assertRaises(ValueError):e.Manager().tick(self.svc,self.main)
  self.assertEqual(self.calls,[])
 def test_exit_mismatch_rolls_back_and_does_not_retry(self):
  self.config();self.svc.plane.probe.side_effect=['1.1.1.1','8.8.8.8'];m=e.Manager();self.assertTrue(m.tick(self.svc,self.main));self.assertEqual(e.override(),'162.159.197.2:8443');self.assertTrue(e.read(e.APPLIED)['rollback_confirmed']);before=list(self.calls);self.assertFalse(m.tick(self.svc,self.main));self.assertEqual(self.calls,before)
 def test_interrupted_journal_blocks_blind_reconnect(self):
  self.config();e.APPLIED.write_text('{"revision":"one","state":"applying"}')
  with self.assertRaises(ValueError):e.Manager().tick(self.svc,self.main)
  self.assertEqual(self.calls,[])
 def test_auto_only_resets_once(self):
  self.config(mode='auto');m=e.Manager();self.assertTrue(m.tick(self.svc,self.main));self.assertFalse(m.tick(self.svc,self.main));self.assertEqual(self.calls.count(('tunnel','endpoint','reset')),1)
 def test_worker_does_not_apply_main_entry(self):
  self.config();self.svc.backend='worker';self.assertFalse(e.Manager().tick(self.svc,self.main));self.assertEqual(self.calls,[])
 def test_narrow_save_only_own_settings(self):
  e.INPUT.write_text(json.dumps({'mode':'preserve','hours':12}));result=self.save_input();self.assertTrue(result['saved']);self.assertEqual(self.calls,[]);self.assertTrue(e.INPUT.exists())
 def test_periodic_read_obeys_interval(self):
  m=e.Manager();e.STATE.write_text(json.dumps({'checked_at':__import__('time').time()}))
  with patch.object(e,'refresh') as r:m.poll();r.assert_not_called()
 def test_failed_rollback_blocks_reconnect(self):
  self.config();self.svc.plane.probe.side_effect=RuntimeError('failure')
  def bad(*args,**kw):
   if args==('tunnel','endpoint','set','162.159.197.2:8443'):raise RuntimeError()
   return self.cli(*args,**kw)
  with patch.object(mplane,'cli',side_effect=bad),self.assertRaises(ValueError):e.Manager().tick(self.svc,self.main)
  with self.assertRaises(ValueError):e.Manager().tick(self.svc,self.main)
 def test_unsupported_client_cannot_mutate(self):
  self.config()
  with patch.object(e,'capability',return_value={'supported':False}):self.assertFalse(e.Manager().tick(self.svc,self.main))
  self.assertEqual(self.calls,[])
 def test_oversized_and_symlink_input_never_save(self):
  e.INPUT.write_text('x'*2049)
  with self.assertRaises(ValueError):self.save_input()
  self.assertFalse(e.CONFIG.exists())
  target=self.p/'sensitive';target.write_text('{}');e.INPUT.unlink();e.INPUT.symlink_to(target)
  with self.assertRaises(ValueError):self.save_input()
  self.assertTrue(target.exists());self.assertFalse(e.CONFIG.exists())
 def test_damaged_history_and_settings_preserve_healthy_line(self):
  for path,content in [(e.APPLIED,'{bad'),(e.APPLIED,'[]'),(e.CONFIG,'{bad')]:
   path.write_text(content)
   self.assertFalse(e.Manager().tick(self.svc,self.main));self.svc.suspend_capture.assert_not_called();self.assertEqual(self.calls,[])
   path.unlink()
 def test_corrupt_candidate_cache_never_breaks_generation_or_health(self):
  e.CACHE.write_text('bad');self.assertEqual(e.generation(),'');self.assertTrue(e.info()['warnings']);self.assertFalse(e.Manager().tick(self.svc,self.main))
 def test_missing_expected_uses_fresh_baseline_and_rejects_changed_exit(self):
  self.config();self.main.pop('expected_usa');self.svc.plane.probe.side_effect=['8.8.8.8','1.1.1.1','8.8.8.8']
  with patch.object(mplane,'cli',side_effect=lambda *a,**kw: SimpleNamespace(stdout='Status update: Connected',returncode=0) if a==('status',) else self.cli(*a,**kw)):
   self.assertTrue(e.Manager().tick(self.svc,self.main))
  r=e.read(e.APPLIED);self.assertEqual(r['state'],'failed');self.assertTrue(r['rollback_confirmed']);self.assertEqual(r['baseline_source'],'fresh_probe')
 def test_missing_baseline_rejects_before_withdrawal(self):
  self.config();self.main.pop('expected_usa');self.assertFalse(e.Manager().tick(self.svc,self.main));self.svc.suspend_capture.assert_not_called();self.assertEqual(e.read(e.APPLIED)['state'],'rejected')
 def test_fresh_probe_disagrees_with_last_exit_rejects(self):
  self.config();self.main.pop('expected_usa');self.svc.plane.probe.return_value='1.1.1.1'
  with patch.object(mplane,'cli',return_value=SimpleNamespace(stdout='Status update: Connected')):self.assertFalse(e.Manager().tick(self.svc,self.main))
  self.svc.suspend_capture.assert_not_called()
 def test_rollback_must_verify_connection_not_only_setting(self):
  self.config();self.svc.plane.probe.side_effect=['1.1.1.1','9.9.9.9']
  with self.assertRaises(ValueError):e.Manager().tick(self.svc,self.main)
  self.assertEqual(e.read(e.APPLIED)['state'],'unknown');self.assertEqual(e.read(e.TRANSACTION)['phase'],'unknown')
  calls=list(self.calls)
  with self.assertRaises(ValueError):e.Manager().tick(self.svc,self.main)
  self.assertEqual(calls,self.calls)
 def test_delayed_readback_is_waited_for(self):
  with patch.object(e,'override',side_effect=['162.159.197.2:8443','162.159.197.2:443']),patch.object(e.time,'sleep') as sleep:e.wait_override('162.159.197.2:443');sleep.assert_called_once()
 def test_readback_timeout_is_bounded(self):
  with patch.object(e,'override',return_value='162.159.197.2:8443'),patch.object(e.time,'monotonic',side_effect=[0,6]),self.assertRaises(ValueError):e.wait_override('162.159.197.2:443')
 def test_durable_transaction_recovers_previous_path(self):
  self.config();r={'revision':'one','previous':'162.159.197.2:8443','target':'162.159.197.2:443','baseline':'8.8.8.8','vnet':self.main['vnet_usa'],'phase':'apply'};e.TRANSACTION.write_text(json.dumps(r));e.APPLIED.write_text('{bad')
  self.assertTrue(e.Manager().tick(self.svc,self.main));self.assertFalse(e.TRANSACTION.exists());self.assertTrue(e.read(e.APPLIED)['rollback_confirmed'])
 def test_damaged_critical_transaction_is_not_ignored(self):
  e.TRANSACTION.write_text('bad')
  with self.assertRaises(ValueError):e.Manager().tick(self.svc,self.main)
  self.assertEqual(self.calls,[])
 def test_imported_preferences_never_apply_until_saved(self):
  self.config();d=e.read(e.CONFIG);d['staged']=True;e.CONFIG.write_text(json.dumps(d));self.assertFalse(e.Manager().tick(self.svc,self.main));self.assertEqual(self.calls,[])
 def test_save_cannot_replace_unfinished_transaction(self):
  e.TRANSACTION.write_text('{}');e.INPUT.write_text('{"mode":"preserve"}')
  with self.assertRaises(ValueError):self.save_input()
  self.assertFalse(e.CONFIG.exists())
 def test_save_preserves_damaged_history_evidence(self):
  e.APPLIED.write_text('bad');e.INPUT.write_text('{"mode":"preserve"}')
  with self.assertRaises(ValueError):self.save_input()
  self.assertEqual(e.APPLIED.read_text(),'bad');self.assertEqual(self.calls,[])
if __name__=='__main__':unittest.main()
