import contextlib,hashlib,json,os,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'root/usr/lib/routepilot'))
from request_input import claim_request,prepare_requests,reap_requests
from input_fixture import stage

class ClaimedInputs(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.base=self.root/'cloud-input'
 def slot(self,token):return self.root/'requests'/('cloud-input-'+token.split(':')[0]+'.json')
 def test_distinct_callers_never_consume_each_others_inputs(self):
  a=stage(self.base,{'token':'A'});b=stage(self.base,{'token':'B'})
  with claim_request(self.base,a,1024) as value:self.assertEqual(value,{'token':'A'});self.assertTrue(self.slot(b).exists())
  with claim_request(self.base,b,1024) as value:self.assertEqual(value,{'token':'B'})
  self.assertEqual(list((self.root/'claimed').iterdir()),[])
 def test_new_write_after_claim_survives_earlier_cleanup(self):
  a=stage(self.base,{'token':'A'},identity='a'*32)
  with claim_request(self.base,a,1024):b=stage(self.base,{'token':'B'},identity='a'*32)
  with claim_request(self.base,b,1024) as value:self.assertEqual(value['token'],'B')
 def test_tampered_content_is_rejected_and_owned_claim_cleaned(self):
  a=stage(self.base,{'token':'A'});self.slot(a).write_text('{}')
  with self.assertRaises(ValueError),claim_request(self.base,a,1024):pass
  self.assertEqual(list((self.root/'claimed').iterdir()),[])
 def test_invalid_ids_do_not_consume_any_slot(self):
  a=stage(self.base,{'token':'A'})
  for token in [None,'../bad','a'*32]:
   with self.assertRaises(ValueError),claim_request(self.base,token,1024):pass
  self.assertTrue(self.slot(a).exists())
 def test_cleanup_on_handler_failure_does_not_leave_credential(self):
  a=stage(self.base,{'token':'SECRET'})
  with self.assertRaises(RuntimeError),claim_request(self.base,a,1024):raise RuntimeError('synthetic')
  self.assertFalse(self.slot(a).exists());self.assertEqual(list((self.root/'claimed').iterdir()),[])
 def test_symlink_fifo_and_permissive_mode_are_rejected_without_following(self):
  for kind in ['symlink','fifo','mode']:
   with self.subTest(kind=kind):
    a=stage(self.base,{'token':'A'});slot=self.slot(a)
    if kind=='symlink':
     target=self.root/'target';target.write_text('keep');slot.unlink();slot.symlink_to(target)
    elif kind=='fifo':slot.unlink();os.mkfifo(slot,0o600)
    else:slot.chmod(0o644)
    with self.assertRaises(ValueError),claim_request(self.base,a,1024):pass
    if kind=='symlink':self.assertEqual(target.read_text(),'keep')
 def test_size_and_expiry_are_bounded(self):
  a=stage(self.base,{'token':'x'*2000})
  with self.assertRaises(ValueError),claim_request(self.base,a,1024):pass
  a=stage(self.base,{'token':'A'});os.utime(self.slot(a),(1,1))
  with self.assertRaises(ValueError),claim_request(self.base,a,1024):pass
 def test_abandoned_slot_expires_but_recent_slot_remains(self):
  a=stage(self.base,{'token':'A'});b=stage(self.base,{'token':'B'});os.utime(self.slot(a),(1,1));reap_requests(self.root)
  self.assertFalse(self.slot(a).exists());self.assertTrue(self.slot(b).exists())
 def test_active_claim_is_not_reaped_but_crash_remnant_is(self):
  a=stage(self.base,{'token':'A'})
  with claim_request(self.base,a,1024):
   claimed=next((self.root/'claimed').iterdir());os.utime(claimed,(1,1));reap_requests(self.root);self.assertTrue(claimed.exists())
  crashed=self.root/'claimed'/('f'*32);crashed.write_text('old secret');crashed.chmod(0o600);os.utime(crashed,(1,1));reap_requests(self.root);self.assertFalse(crashed.exists())
 def test_clock_rollback_does_not_keep_abandoned_secret_forever(self):
  a=stage(self.base,{'token':'A'});future=time.time()+3600;os.utime(self.slot(a),(future,future));reap_requests(self.root);self.assertFalse(self.slot(a).exists())
 def test_private_claim_directory_is_not_aliased(self):
  other=self.root/'other';other.mkdir();(self.root/'claimed').symlink_to(other)
  with self.assertRaises(ValueError):prepare_requests(self.root)

class EndpointConsumers(unittest.TestCase):
 def fixture(self,cls):
  t=cls();t.setUp();self.addCleanup(t.doCleanups);return t
 def slot(self,base,token):return base.parent/'requests'/(base.name+'-'+token.split(':')[0]+'.json')
 def test_cloud_save_uses_its_own_token_and_ack(self):
  import test_cloud_worker as fixture,cloud_worker as c,worker_backend as w
  t=self.fixture(fixture.CloudTest);a=stage(c.INPUT,{**fixture.CFG,'script':'routepilot-a'});b=stage(c.INPUT,{**fixture.CFG,'script':'routepilot-b'})
  with patch.object(w,'guard',return_value=contextlib.nullcontext()),patch.object(c,'api',side_effect=t.fake_api):out=c.save(a)
  self.assertEqual(c.load_settings()['script'],'routepilot-a');self.assertEqual(out['request_id'],a.split(':')[0]);self.assertTrue(self.slot(c.INPUT,b).exists())
 def test_backup_import_uses_its_own_node_without_cloud_calls(self):
  import worker_backend as w
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);base=root/'backup-input';run=root/'run';run.mkdir()
   node=dict(uuid='11111111-1111-4111-8111-111111111111',address='104.21.39.117',port=443,sni='worker.example.com',path='/tunnel')
   uri='vless://11111111-1111-4111-8111-111111111111@104.21.39.117:443?security=tls&type=ws&sni=worker.example.com&path=%2Ftunnel#';a=stage(base,{'source':'uri','uri':uri+'A'});b=stage(base,{'source':'uri','uri':uri+'B'})
   with patch.object(w,'INPUT',base),patch.object(w,'NODE',root/'private/node'),patch.object(w,'RUN',run),patch.object(w,'guard',return_value=contextlib.nullcontext()),patch.object(w,'read_config',return_value={'main':{'backend':'masque'}}):
    out=w.import_node(a);self.assertEqual(w.load()['name'],'A')
   self.assertEqual(out['request_id'],a.split(':')[0]);self.assertTrue(self.slot(base,b).exists())
 def test_relay_save_isolated_from_later_slot(self):
  import test_relay_pool as fixture,relay_pool as r
  t=self.fixture(fixture.RelayTest);a=stage(r.INPUT,{'candidates':fixture.ROWS});b=stage(r.INPUT,{'candidates':[fixture.ROWS[1]]});out=r.save(a)
  self.assertEqual(len(r.load()),2);self.assertEqual(out['request_id'],a.split(':')[0]);self.assertTrue(self.slot(r.INPUT,b).exists())
 def test_organization_save_isolated_and_narrow(self):
  import warp_control as w,runtime,worker_backend
  with tempfile.TemporaryDirectory() as folder:
   base=Path(folder)/'organization-input';a=stage(base,{'organization':'team-a'});b=stage(base,{'organization':'team-b'})
   with patch.object(w,'ORGANIZATION_INPUT',base),patch.object(w,'read_config',return_value={'main':{'enabled':'0','organization':'old','apply_nonce':'n'}}),patch.object(worker_backend,'guard',return_value=contextlib.nullcontext()),patch.object(runtime,'command') as cmd:out=w.save_organization(a)
   self.assertEqual(out['organization'],'team-a');self.assertTrue(self.slot(base,b).exists());self.assertIn('routepilot.main.organization=team-a',cmd.call_args_list[0].args[0])
 def test_registration_never_consumes_other_pages_login_link(self):
  import test_stability as fixture,auth_control as auth
  t=self.fixture(fixture.AuthTest)
  with patch.object(auth,'cli') as cli:
   auth.login();base=auth.RUN/'auth-input';a=stage(base,{'token':'com.cloudflare.warp://test-team.cloudflareaccess.com/auth?token=caller-a'});b=stage(base,{'token':'com.cloudflare.warp://test-team.cloudflareaccess.com/auth?token=caller-b'});out=auth.register(a)
   self.assertEqual(cli.call_args.args[-1],'com.cloudflare.warp://test-team.cloudflareaccess.com/auth?token=caller-a');self.assertEqual(out['request_id'],a.split(':')[0]);self.assertTrue(self.slot(base,b).exists())
 def test_all_consumers_refuse_unbound_calls(self):
  import auth_control,worker_backend,cloud_worker,relay_pool,warp_control
  # Patch outer locks to avoid touching real lock paths; no consumer reaches any operation.
  with patch.object(auth_control,'guard',return_value=contextlib.nullcontext()),patch.object(worker_backend,'guard',return_value=contextlib.nullcontext()),patch.object(cloud_worker,'idle_job',return_value=contextlib.nullcontext()):
   for fn in [auth_control.register,worker_backend.import_node,cloud_worker.save,relay_pool.save,warp_control.save_organization]:
    with self.subTest(fn=fn.__name__),self.assertRaises(ValueError):fn()
