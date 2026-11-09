# Synthetic unit-test addresses; no live router, tenant or user configuration.
from input_fixture import stage
import contextlib,io,json,tempfile,unittest,sys,hashlib
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'root/usr/lib/routepilot'))
import cloud_worker as c
import worker_backend as w
import worker_scan as scan

CFG=dict(token='t'*40,account_id='a'*32,script='routepilot-test',hostname='backup.example.com',zone_id='b'*32,proxyip='proxyip.example.com:443')
ZONE=dict(id='b'*32,name='example.com',account={'id':'a'*32})
NODE=dict(name='backup',uuid='11111111-1111-4111-8111-111111111111',address='104.21.39.117',port=443,sni='worker.example.com',path='/tunnel')

class CloudTest(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
  for module,key,file in [(c,'RUN','run'),(c,'PRIVATE','private'),(c,'SETTINGS','private/cloud'),(c,'JOURNAL','private/journal'),(c,'PENDING','private/pending'),(c,'INPUT','input'),(c,'STATE','state')]:self.stack.enter_context(patch.object(module,key,self.root/file))
  c.RUN.mkdir();c.PRIVATE.mkdir(mode=0o700)
  self.stack.enter_context(patch.object(c,'read_config',return_value={'main':{'backend':'masque'}}))
 def fake_api(self,cfg,method,url,payload=None,**kwargs):
  if url.startswith('/zones?'):return [ZONE]
  if url.endswith('/settings'):return None
  if method=='GET' and url.split('?')[0].endswith('/domains'):return []
  if '/dns_records?' in url:return []
  if method=='PUT' and url.split('?')[0].endswith('/domains'):return dict(id='domain-id',hostname=cfg['hostname'],service=cfg['script'])
  if method=='PUT':return dict(id=cfg['script'])
  raise AssertionError((method,url))
 def test_only_cloud_fields_and_scoped_names_are_accepted(self):
  self.assertEqual(c.valid_settings(CFG),CFG)
  for change in [{'script':'existing-main'},{'hostname':'127.0.0.1'},{'account_id':'bad'},{'token':'secret\nX:bad'},{'network':'main'},{'proxyip':'127.0.0.1:443'}]:
   with self.subTest(change=change),self.assertRaises(ValueError):c.valid_settings({**CFG,**change})
 def test_domains_can_be_discovered_before_a_hostname_is_chosen(self):
  cfg=c.valid_settings({**CFG,'hostname':'','zone_id':''})
  with patch.object(c,'api',side_effect=self.fake_api) as api:
   out=c.verify(cfg)
  self.assertEqual(out['zones'],[{'id':ZONE['id'],'name':ZONE['name']}])
  self.assertTrue(all(x.args[1]=='GET' for x in api.call_args_list))
  with self.assertRaises(c.CloudError):c.zone_for(cfg,out['zones'])
 def test_zone_listing_ignores_other_accounts_and_paginates(self):
  other={**ZONE,'account':{'id':'c'*32}}
  with patch.object(c,'api',side_effect=[[ZONE]*49+[other],[]]) as api:
   out=c.zones(CFG)
  self.assertEqual(len(out),49);self.assertEqual(api.call_count,2)
  self.assertIn('status=active',api.call_args_list[0].args[2]);self.assertIn('page=2',api.call_args_list[1].args[2])
 def test_directory_survives_reload_and_lists_all_authorized_accounts(self):
  other=dict(id='d'*32,name='other.example',account={'id':'c'*32,'name':'Other account'})
  c.atomic(c.SETTINGS,json.dumps(CFG));before=c.SETTINGS.read_bytes()
  with patch.object(c,'api',return_value=[ZONE,other]) as api:out=c.verify(CFG)
  self.assertEqual(len(out['accounts']),2);self.assertEqual(len(out['all_zones']),2)
  self.assertEqual(len(out['zones']),1);self.assertNotIn('account.id',api.call_args.args[2])
  self.assertTrue(all(x.args[1]=='GET' for x in api.call_args_list))
  self.assertEqual(c.SETTINGS.read_bytes(),before)
  self.assertEqual(c.metadata()['directory']['all_zones'],out['all_zones'])
  self.assertEqual((c.PRIVATE/'cloud-directory.json').stat().st_mode&0o777,0o600)
  self.assertNotIn('credential',json.dumps(c.metadata()));self.assertNotIn(CFG['token'],json.dumps(c.metadata()))
 def test_directory_refresh_does_not_overwrite_deployment_readiness(self):
  c.atomic(c.SETTINGS,json.dumps(CFG));c.public(dict(state='ready',identity=c.identity(CFG)))
  before=c.STATE.read_bytes();fd=__import__('os').open(c.RUN/'lock',__import__('os').O_CREAT|__import__('os').O_RDWR,0o600)
  with patch.object(c,'api',side_effect=self.fake_api):c.job('verify',fd)
  self.assertEqual(c.STATE.read_bytes(),before);self.assertEqual(json.loads(c.directory_state().read_text())['state'],'verified')
 def test_same_settings_save_preserves_readiness_and_token_change_invalidates_directory(self):
  c.atomic(c.SETTINGS,json.dumps(CFG));c.public(dict(state='ready',identity=c.identity(CFG)))
  with patch.object(c,'api',side_effect=self.fake_api):c.verify(CFG)
  before=c.STATE.read_bytes();c.INPUT.write_text(json.dumps({**CFG,'token':''}))
  with patch.object(w,'guard',return_value=contextlib.nullcontext()):c.save(stage(c.INPUT))
  self.assertEqual(c.STATE.read_bytes(),before);self.assertIsNotNone(c.metadata()['directory'])
  c.INPUT.write_text(json.dumps({**CFG,'token':'x'*40}))
  with patch.object(w,'guard',return_value=contextlib.nullcontext()):c.save(stage(c.INPUT))
  self.assertIsNone(c.metadata()['directory']);self.assertFalse((c.PRIVATE/'cloud-directory.json').exists())
 def test_corrupt_or_old_credential_directory_is_not_projected(self):
  path=c.PRIVATE/'cloud-directory.json';path.write_text('{broken')
  self.assertIsNone(c.directory_metadata(CFG))
  with patch.object(c,'api',side_effect=self.fake_api):c.verify(CFG)
  self.assertIsNone(c.directory_metadata({**CFG,'token':'x'*40}))
  with patch.object(c.time,'time',return_value=0):self.assertTrue(c.directory_metadata(CFG)['stale'])
 def test_created_projects_are_visible_without_exposing_node_or_owner_secrets(self):
  record=dict(uuid=NODE['uuid'],owner='private-owner-secret',hostname=CFG['hostname'],uploaded=True,domain_id='private-domain-id')
  c.atomic(c.JOURNAL,json.dumps({c.own_key(CFG):record}));before=c.JOURNAL.read_bytes()
  with patch.object(w,'load',return_value={**NODE,'sni':CFG['hostname']}):projects=c.metadata(CFG)['projects']
  self.assertEqual(len(projects),1);self.assertTrue(projects[0]['saved_as_backup']);self.assertTrue(projects[0]['bound']);self.assertTrue(projects[0]['selected'])
  encoded=json.dumps(projects)
  for secret in [NODE['uuid'],record['owner'],record['domain_id'],CFG['token']]:self.assertNotIn(secret,encoded)
  self.assertEqual(c.JOURNAL.read_bytes(),before)
 def test_pending_creation_is_not_presented_as_uploaded_or_saved(self):
  c.atomic(c.JOURNAL,json.dumps({c.own_key(CFG):dict(uuid=NODE['uuid'],hostname=CFG['hostname'])}))
  with patch.object(w,'load',side_effect=FileNotFoundError):row=c.project_metadata(CFG)[0]
  self.assertFalse(row['uploaded']);self.assertFalse(row['bound']);self.assertFalse(row['saved_as_backup'])
 def test_forgetting_api_credentials_keeps_created_project_inventory(self):
  c.atomic(c.JOURNAL,json.dumps({c.own_key(CFG):dict(hostname=CFG['hostname'],uploaded=True)}))
  self.assertFalse(c.metadata()['configured']);self.assertEqual(len(c.metadata()['projects']),1)
 def test_target_check_is_readonly_and_reports_dns_collision(self):
  with patch.object(c,'api',side_effect=self.fake_api) as api:
   self.assertEqual(c.check_target(CFG)['state'],'available')
  self.assertTrue(all(x.args[1]=='GET' for x in api.call_args_list))
  def fake(*args,**kw):return [{'id':'existing'}] if '/dns_records?' in args[2] else self.fake_api(*args,**kw)
  with patch.object(c,'api',side_effect=fake):self.assertEqual(c.check_target(CFG)['state'],'conflict')
 def test_multiple_relays_preserve_priority_and_deduplicate(self):
  cfg=c.valid_settings({**CFG,'proxyip':'a.example.com:443\nb.example.com:443,a.example.com:443'})
  self.assertEqual(cfg['proxyip'],'a.example.com:443,b.example.com:443')
 def test_account_discovery_without_manual_id_uses_only_authorized_zones(self):
  cfg=c.valid_settings({**CFG,'account_id':'','hostname':'','zone_id':''})
  with patch.object(c,'api',side_effect=self.fake_api) as api:out=c.verify(cfg)
  self.assertEqual(out['accounts'][0]['id'],CFG['account_id']);self.assertEqual(out['discovered_account_id'],CFG['account_id']);self.assertNotIn('account.id',api.call_args.args[2])
 def test_pool_edits_do_not_invalidate_cloud_project_identity(self):
  self.assertEqual(c.identity(CFG),c.identity({**CFG,'proxyip':'other.example.com:443'}))
 def test_saving_credentials_does_not_change_main_or_node_and_never_projects_token(self):
  c.INPUT.write_text(json.dumps(CFG))
  with patch.object(w,'guard',return_value=contextlib.nullcontext()),patch.object(w,'atomic') as node_writer:
   c.save(stage(c.INPUT));node_writer.assert_not_called()
  self.assertFalse(c.INPUT.exists());self.assertEqual(c.SETTINGS.stat().st_mode&0o777,0o600)
  self.assertNotIn(CFG['token'],c.STATE.read_text());self.assertNotIn('token',c.metadata())
 def test_unmanaged_existing_worker_is_never_uploaded_or_replaced(self):
  def fake(*args,**kw):return {} if args[2].endswith('/settings') else self.fake_api(*args,**kw)
  with patch.object(c,'api',side_effect=fake) as api:
   with self.assertRaises(c.CloudError):c.deploy(CFG,lambda *a,**k:None)
   self.assertFalse(any(x.args[1]=='PUT' for x in api.call_args_list))
 def test_dns_collision_never_causes_a_delete_or_deployment(self):
  def fake(*args,**kw):return [{'id':'existing-dns'}] if '/dns_records?' in args[2] else self.fake_api(*args,**kw)
  with patch.object(c,'api',side_effect=fake) as api:
   with self.assertRaises(c.CloudError):c.deploy(CFG,lambda *a,**k:None)
   self.assertFalse(any(x.args[1] in ('PUT','DELETE','PATCH') for x in api.call_args_list))
 def test_other_service_custom_domain_is_not_overwritten(self):
  def fake(*args,**kw):return [{'hostname':CFG['hostname'],'service':'unrelated'}] if args[1]=='GET' and args[2].split('?')[0].endswith('/domains') else self.fake_api(*args,**kw)
  with patch.object(c,'api',side_effect=fake) as api:
   with self.assertRaises(c.CloudError):c.deploy(CFG,lambda *a,**k:None)
   self.assertFalse(any(x.args[1]=='PUT' for x in api.call_args_list))
 def test_root_or_wrong_zone_cannot_be_bound(self):
  for hostname in ['example.com','backup.other.com']:
   with self.assertRaises(c.CloudError):c.zone_for({**CFG,'hostname':hostname},[ZONE])
 def test_deploy_separates_created_binding_from_tls_ready_and_keeps_original_node(self):
  with patch.object(c,'api',side_effect=self.fake_api),patch.object(c,'health',return_value=False),patch.object(w,'atomic') as node_writer:
   out=c.deploy(CFG,lambda *a,**k:None);self.assertEqual(out['state'],'waiting_dns');node_writer.assert_not_called()
  data=json.loads(c.PENDING.read_text());self.assertEqual(data['node']['path'],'/tunnel');self.assertEqual(c.PENDING.stat().st_mode&0o777,0o600)
  self.assertNotIn(data['node']['uuid'],json.dumps(out))
 def test_upload_retry_keeps_authentication_identity_stable(self):
  payloads=[]
  def fake(*args,**kw):
   if args[1]=='PUT' and '/scripts/' in args[2]:
    payloads.append(args[3])
    if len(payloads)==1:raise c.CloudError('network failed')
   return self.fake_api(*args,**kw)
  with patch.object(c,'api',side_effect=fake),patch.object(c,'health',return_value=False):
   with self.assertRaises(c.CloudError):c.deploy(CFG,lambda *a,**k:None)
   c.deploy(CFG,lambda *a,**k:None)
  self.assertEqual(payloads[0]['bindings'],payloads[1]['bindings'])
 def test_api_auth_is_not_in_argv_and_redirects_cannot_receive_it(self):
  def fake_run(args,**kwargs):
   self.assertFalse(any(CFG['token'] in x for x in args));self.assertIn(CFG['token'],kwargs['input']);self.assertNotIn('-L',args)
   Path(args[args.index('-o')+1]).write_text(json.dumps({'success':True,'result':[]}))
   return type('R',(),{'returncode':0,'stdout':'200'})()
  with patch.object(c.subprocess,'run',side_effect=fake_run):self.assertEqual(c.api(CFG,'GET','/zones'),[])
  with self.assertRaises(c.CloudError):c.api(CFG,'GET','https://untrusted.invalid')
 def test_public_state_does_not_echo_remote_api_errors_or_tokens(self):
  c.atomic(c.SETTINGS,json.dumps(CFG));lock=c.RUN/'lock';fd=__import__('os').open(lock,__import__('os').O_CREAT|__import__('os').O_RDWR,0o600)
  with patch.object(c,'verify',side_effect=RuntimeError('secret '+CFG['token'])):c.job('verify',fd)
  self.assertNotIn(CFG['token'],c.directory_state().read_text());self.assertEqual(json.loads(c.directory_state().read_text())['state'],'failed');self.assertFalse(c.STATE.exists())
 def test_active_backup_blocks_configuration_and_deployment(self):
  with patch.object(c,'read_config',return_value={'main':{'backend':'worker'}}),patch.object(c,'api') as api:
   with self.assertRaises(c.CloudError):c.deploy(CFG,lambda *a,**k:None)
   api.assert_not_called()

class EntryScanTest(unittest.TestCase):
 def test_only_cf_public_ipv4_is_eligible(self):
  for value in ['127.0.0.1','10.77.50.1','1.1.1.1','not-an-ip','::1']:self.assertFalse(scan.cf_ip(value))
  self.assertTrue(scan.cf_ip('104.21.39.117'))
 def test_candidates_are_bounded_and_do_not_trust_private_dns(self):
  with patch.object(scan.socket,'getaddrinfo',return_value=[(2,1,6,'',('10.77.50.1',443))]):
   rows=scan.candidates(NODE);self.assertLessEqual(len(rows),8);self.assertNotIn('10.77.50.1',rows);self.assertEqual(rows[0],NODE['address'])
 def test_no_recommendation_without_both_real_sites(self):
  rows=[{'ip':'104.16.0.1','checks':[{'ok':True,'ms':1},{'ok':False,'ms':1}]},{'ip':'104.17.0.1','checks':[{'ok':True,'ms':20},{'ok':True,'ms':20}]}]
  self.assertEqual(scan.rank(rows)[0]['ip'],'104.17.0.1')
 def test_apply_changes_only_backup_address_and_invalidates_previous_proof(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);state=root/'scan';node=root/'node';last=root/'last-entry.json';last.write_text('old')
   proof=dict(state='done',fingerprint=w.fingerprint(NODE),boot='boot',finished_uptime=10,best_ip='104.16.0.1',rows=[dict(ip='104.16.0.1',checks=[dict(ok=True,ms=1),dict(ok=True,ms=1)])]);state.write_text(json.dumps(proof))
   with patch.object(scan,'STATE',state),patch.object(scan,'read_config',return_value={'main':{}}),patch.object(w,'RUN',root),patch.object(w,'NODE',node),patch.object(w,'load',return_value=NODE),patch.object(w,'guard',return_value=contextlib.nullcontext()),patch.object(w,'boot_id',return_value='boot'),patch.object(w,'uptime',return_value=20):scan.apply_best()
   changed=json.loads(node.read_text());self.assertEqual(changed,{**NODE,'address':'104.16.0.1'});self.assertFalse(last.exists());self.assertEqual(json.loads(state.read_text())['applied_fingerprint'],w.fingerprint(changed))

if __name__=='__main__':unittest.main()
