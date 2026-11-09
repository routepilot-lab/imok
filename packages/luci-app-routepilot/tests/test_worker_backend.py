# Synthetic unit-test addresses; no live router, tenant or user configuration.
from input_fixture import stage
import copy,json,tempfile,time,unittest,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'root/usr/lib/routepilot'))
import worker_backend as w
import masque_config

NODE=dict(name='example backup',uuid='11111111-1111-4111-8111-111111111111',address='edge.example.com',port=443,sni='worker.example.com',path='/?ed=2560')
class WorkerTest(unittest.TestCase):
 def test_strict_tls_uri_and_ip_override_keep_certificate_identity(self):
  uri='vless://'+NODE['uuid']+'@edge.example.com:443?type=ws&security=tls&sni=worker.example.com&host=worker.example.com&path=%2F%3Fed%3D2560#backup'
  n=w.parse_input({'source':'uri','uri':uri,'entry':'ip','ip':'104.16.1.1'})
  self.assertEqual(n['address'],'104.16.1.1');self.assertEqual(n['sni'],'worker.example.com');self.assertEqual(n['path'],'/?ed=2560')
  for bad in [uri.replace('security=tls','security=none'),uri+'&allowInsecure=1',uri.replace('host=worker.example.com','host=other.example.com')]:
   if bad.endswith('&allowInsecure=1'):bad=bad.replace('#backup','')
   with self.assertRaises(ValueError):w.parse_input({'source':'uri','uri':bad})
 def test_credential_not_in_metadata_and_no_plaintext_outbound(self):
  m=w.metadata(NODE);self.assertNotIn('uuid',m);self.assertNotIn('path',m)
  c=w.config(NODE,15000,tun=True)
  tls=c['outbounds'][0]['streamSettings']['tlsSettings'];self.assertFalse(tls['allowInsecure'])
  self.assertEqual({o['protocol'] for o in c['outbounds']},{'vless','blackhole'})
  self.assertEqual(c['inbounds'][0]['listen'],'127.0.0.1');self.assertFalse(c['inbounds'][0]['settings']['udp'])
  self.assertNotIn('autoSystemRoutingTable',c['inbounds'][1]['settings'])
  self.assertEqual(c['routing']['rules'][-1]['outboundTag'],'backup-block')
 def test_invalid_private_addresses_and_paths_rejected(self):
  for change in [{'address':'127.0.0.1'},{'address':'192.168.1.1'},{'address':'host.local'},{'sni':'bad name'},{'port':0},{'path':'/x\ny'}]:
   with self.assertRaises(ValueError):w.validate({**NODE,**change})
 def test_dns_rebinding_is_pinned_and_private_results_refused(self):
  answer=lambda ip:(2,1,6,'',(ip,443))
  with patch.object(w.socket,'getaddrinfo',return_value=[answer('104.16.1.1')]):self.assertEqual(w.pinned(NODE)['address'],'104.16.1.1')
  with patch.object(w.socket,'getaddrinfo',return_value=[answer('127.0.0.1')]):
   with self.assertRaises(ValueError):w.pinned(NODE)
 def test_entry_resolution_retries_once_without_changing_the_node(self):
  before=copy.deepcopy(NODE);reply=(2,1,6,'',('104.16.1.1',443))
  with patch.object(w.socket,'getaddrinfo',side_effect=[w.socket.gaierror(w.socket.EAI_AGAIN,'temporary'),[reply]]) as resolver,patch.object(w.time,'sleep') as sleep:
   self.assertEqual(w.pinned(NODE)['address'],'104.16.1.1');self.assertEqual(resolver.call_count,2);sleep.assert_called_once_with(0.2)
  self.assertEqual(NODE,before)
 def test_resolution_failure_and_private_answers_never_use_another_entry(self):
  with patch.object(w.socket,'getaddrinfo',side_effect=w.socket.gaierror(w.socket.EAI_NONAME,'missing')) as resolver,patch.object(w.time,'sleep'):
   with self.assertRaisesRegex(ValueError,'解析未完成'):w.pinned(NODE)
   self.assertEqual(resolver.call_count,2)
  with patch.object(w.socket,'getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]) as resolver,patch.object(w.time,'sleep') as sleep:
   with self.assertRaises(ValueError):w.pinned(NODE)
   self.assertEqual(resolver.call_count,1);sleep.assert_not_called()
 def test_manual_enable_requires_current_complete_recent_proof(self):
  proof=dict(boot='test-boot',entry_ip='104.16.1.1',state='done',finished_at=time.time(),finished_uptime=100,fingerprint=w.fingerprint(NODE),results=[{'id':k,'state':'ok'} for k in ['trace','google','github','dns','download']])
  with tempfile.TemporaryDirectory() as temp:
   p=Path(temp)/'proof'
   with patch.object(w,'TEST',p),patch.object(w,'load',return_value=NODE),patch.object(w,'boot_id',return_value='test-boot'),patch.object(w,'uptime',return_value=101),patch.object(w,'verified_endpoint',return_value={**NODE,'address':'104.16.1.1'}):
    p.write_text(json.dumps(proof));self.assertEqual(w.require_test(),NODE)
    for change in [{'boot':'previous-boot'},{'finished_uptime':-201},{'fingerprint':'old'},{'results':proof['results'][:-1]},{'state':'running'}]:
     p.write_text(json.dumps({**proof,**change}))
     with self.assertRaises(ValueError):w.require_test()
 def test_curl_always_uses_loopback_socks_without_environment_bypass(self):
  with patch.object(w,'command') as c:
   w.curl(['curl','https://example.com'],12345)
   args=c.call_args.args[0];self.assertEqual(args[2:6],['--noproxy','','--proxy','socks5h://127.0.0.1:12345'])
 def test_import_does_not_change_uci_and_input_is_removed(self):
  with tempfile.TemporaryDirectory() as temp:
   d=Path(temp);i=d/'input';i.write_text('{}')
   with patch.object(w,'INPUT',i),patch.object(w,'NODE',d/'node'),patch.object(w,'RUN',d),patch.object(w,'read_config',return_value={'main':{}}),patch.object(w,'parse_input',return_value=NODE),patch.object(w,'command') as command:
    w.import_node(stage(w.INPUT));self.assertFalse(i.exists());command.assert_not_called();self.assertEqual((d/'node').stat().st_mode&0o777,0o600)
 def test_provider_failure_threshold_does_not_switch_to_other_exit(self):
  p=w.WorkerPlane();p.child=type('P',(),{'poll':lambda self:None})();p.probe=lambda: (_ for _ in ()).throw(ValueError('fail'))
  with patch.object(w,'command',side_effect=lambda args,**kw:type('R',(),{'stdout':json.dumps([{'flags':['UP']}] if 'link' in args else [{'dev':w.TUN,'dst':'default'}])})()),patch.object(w,'curl',side_effect=RuntimeError('offline')),patch.object(w.time,'monotonic',return_value=100):p.optimize();self.assertEqual(p.failures,1)
  with patch.object(w,'command',side_effect=lambda args,**kw:type('R',(),{'stdout':json.dumps([{'flags':['UP']}] if 'link' in args else [{'dev':w.TUN,'dst':'default'}])})()),patch.object(w,'curl',side_effect=RuntimeError('offline')),patch.object(w.time,'monotonic',return_value=131):
   with self.assertRaises(RuntimeError):p.optimize()


class WorkerIntegration(unittest.TestCase):
 def test_backup_compiles_without_a_cloudflare_virtual_network(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'china.txt').write_text('1.0.1.0/24\n')
   cfg={'main':{'backend':'worker','exit':'not-a-vnet'},'policies':[{'id':'test','enabled':'1','ip':'10.77.50.4','mac':'02:11:22:33:44:55','mode':'bypass_cn'}]}
   out=masque_config.compile_config(cfg,p);self.assertEqual(out['exit'],'worker');self.assertEqual(out['vnet'],'');self.assertEqual(cfg['main']['exit'],'not-a-vnet')
 def test_manual_control_changes_only_backend_and_submission_nonce(self):
  import masque,io
  with tempfile.TemporaryDirectory() as d,patch.object(w,'RUN',Path(d)),patch.object(w,'require_test',return_value=NODE),patch.object(masque,'read_config',return_value={'main':{'enabled':'1'}}),patch.object(masque,'command') as cmd,patch('sys.stdout',new_callable=io.StringIO):
   masque.control('backup-enable')
   calls=[c.args[0] for c in cmd.call_args_list];self.assertEqual(calls[0],['uci','set','routepilot.main.backend=worker']);self.assertEqual(len(calls),3)
   self.assertFalse(any('.exit=' in str(c) or 'enabled=' in str(c) or 'firewall' in str(c) for c in calls))
 def test_firewall_forwarding_uses_only_backup_and_current_wan(self):
  import mplane
  result=type('R',(),{'stdout':'[{"dev":"eth1"}]'})()
  row={'id':'lan','scope':'lan','mode':'global','lan_interface':'br-test','ip':'192.168.22.0/24','mac':''}
  with patch.object(mplane,'fw4_remove'),patch.object(mplane,'command',return_value=result) as c:
   mplane.fw4_install([row],proxy='rp-backup',control=False)
   rules=c.call_args.kwargs['data'];self.assertIn('"rp-backup"',rules);self.assertNotIn('"rp-host"',rules);self.assertNotIn('masquerade',rules);self.assertNotIn('flow offload',rules)

class WorkerRegression(unittest.TestCase):
 def test_429_is_a_site_limit_not_an_unusable_line(self):
  rows=[{'id':k,'state':'ok'} for k in ['trace','google','github','dns','download']]
  rows[-1].update(state='restricted',http=429)
  self.assertTrue(w.proof_rows(rows))
  rows[-1]['http']=503;self.assertTrue(w.proof_rows(rows))
  rows[-1]['http']=429;rows[3]['state']='failed';self.assertFalse(w.proof_rows(rows))
 def test_health_accepts_target_throttling_without_switching_backend(self):
  plane=w.WorkerPlane();plane.child=type('P',(),{'poll':lambda self:None})()
  result=type('R',(),{'returncode':0,'stdout':'429'})()
  with patch.object(w,'command',side_effect=lambda args,**kw:type('R',(),{'stdout':json.dumps([{'flags':['UP']}] if 'link' in args else [{'dev':w.TUN,'dst':'default'}])})()),patch.object(w,'curl',return_value=result),patch.object(w.time,'monotonic',return_value=100):
   plane.optimize();self.assertEqual(plane.failures,0)
 def test_expired_boot_cannot_reuse_verified_entry(self):
  with tempfile.TemporaryDirectory() as d:
   file=Path(d)/'node';(file.parent/'last-entry.json').write_text(json.dumps({'fingerprint':w.fingerprint(NODE),'ip':'104.16.1.1','boot':'old'}))
   with patch.object(w,'NODE',file),patch.object(w,'boot_id',return_value='new'):
    with self.assertRaises(ValueError):w.verified_endpoint(NODE)
 def test_encrypted_dns_is_bounded_and_always_uses_backup(self):
  import native
  result=type('R',(),{'returncode':0,'stdout':b'x'*12})()
  with patch.object(native.subprocess,'run',return_value=result) as run:
   native.backup_doh(b'query',15000)
   args=run.call_args.args[0]
   self.assertIn('socks5h://127.0.0.1:15000',args);self.assertIn('--noproxy',args)
   self.assertNotIn('-k',args);self.assertEqual(run.call_args.kwargs['input'],b'query')
   self.assertLessEqual(run.call_args.kwargs['timeout'],7)
 def test_proxy_dns_uses_doh_but_domestic_remains_original_transport(self):
  import native
  from dns_diagnostics import make_query
  q=make_query('www.google.com');response=q[:2]+b'\x81\x80'+q[4:]
  policy={'mark':123,'mode':'global','dns_foreign':'1.1.1.1','dns_domestic':'223.5.5.5','proxy_dns_override':'https://dns.google/dns-query'}
  with patch.object(native,'backup_doh',return_value=response) as doh,patch.object(native,'populate_sets'),patch.object(native.socket,'socket') as sock:
   native.upstream(q,policy);doh.assert_called_once();sock.assert_not_called()
  policy.update(mode='bypass_cn',china_domains=frozenset({'google.com'}))
  socket=sock.return_value.__enter__.return_value
  with patch.object(native,'backup_doh') as doh,patch.object(native,'populate_sets'),patch.object(native.socket,'socket') as sock:
   sock.return_value.__enter__.return_value.recv.return_value=response
   native.upstream(q,policy);doh.assert_not_called();sock.return_value.__enter__.return_value.connect.assert_called_once_with(('223.5.5.5',53))



class WorkerSafety(unittest.TestCase):
 def test_cleanup_only_deletes_the_selected_backend_firewall_handles(self):
  import mplane
  listing='ip accept comment "routepilot-device-out" # handle 10\nip accept comment "routepilot-worker-device-out" # handle 20\n'
  r=type('R',(),{'stdout':listing})()
  with patch.object(mplane,'command',return_value=r) as command:
   mplane.fw4_remove('worker')
   deletions=[c.args[0] for c in command.call_args_list if 'delete' in c.args[0]]
   self.assertEqual([row[-1] for row in deletions],['20','20'])
  with patch.object(mplane,'command',return_value=r) as command:
   mplane.fw4_remove()
   self.assertEqual([c.args[0][-1] for c in command.call_args_list if 'delete' in c.args[0]],['10','10'])
 def test_unconfigured_worker_cleanup_never_touches_the_main_plane(self):
  with tempfile.TemporaryDirectory() as d,patch.object(w,'OWNER',Path(d)/'absent'),patch.object(w,'command') as cmd,patch('mplane.fw4_remove') as remove:
   w.WorkerPlane().stop();cmd.assert_not_called();remove.assert_not_called()
 def test_unknown_process_is_not_killed_but_backup_route_is_withdrawn_first(self):
  import mplane
  with tempfile.TemporaryDirectory() as d:
   owner=Path(d)/'owner';owner.write_text(json.dumps({'pid':123,'start':1,'boot':'test'}))
   with patch.object(w,'OWNER',owner),patch.object(w.Path,'read_text',autospec=True,side_effect=lambda *args,**kw:json.dumps({'pid':123,'start':1,'boot':'test'}) if args[0]==owner else 'test'),patch.object(w,'owned_process',return_value=False),patch.object(w,'command') as command,patch.object(mplane,'fw4_remove') as remove,patch.object(w.os,'kill') as kill:
    with self.assertRaises(RuntimeError):w.WorkerPlane().stop()
    kill.assert_not_called();remove.assert_called_once_with('worker')
    self.assertEqual(command.call_args.args[0],['ip','-4','route','del','default','dev',w.TUN,'table','51990'])
 def test_dedicated_tun_never_sets_system_routes_or_hidden_dns(self):
  c=w.config(NODE,15000,True)
  self.assertNotIn('dns',c);self.assertNotIn('gateway',c['inbounds'][1]['settings'])
  self.assertNotIn('autoSystemRoutingTable',json.dumps(c));self.assertNotIn('sockopt',json.dumps(c))



class WorkerReviewClosure(unittest.TestCase):
 def test_failure_latches_same_activation_even_after_supervisor_restart(self):
  with tempfile.TemporaryDirectory() as d,patch.object(w,'RUN',Path(d)),patch.object(w,'boot_id',return_value='boot'):
   w.mark_failed('attempt-1')
   for _ in range(2):
    with self.assertRaises(ValueError):w.check_retry('attempt-1')
   w.check_retry('user-new-attempt')
 def test_start_errors_remove_secrets_only_after_confirmed_cleanup(self):
  import mplane
  for phase in ('schema','ready','cleanup'):
   with self.subTest(phase=phase),tempfile.TemporaryDirectory() as d:
    folder=Path(d);owner=folder/'owner'
    def command(args,**kwargs):
     if args[:2]==['xray','version']:return type('R',(),{'stdout':'Xray 26.10.21','returncode':0})()
     if '-test' in args and phase=='schema':raise RuntimeError('固定配置校验失败')
     present=phase=='cleanup' and owner.exists()
     return type('R',(),{'stdout':'[]','returncode':0 if present or 'link' not in args else 1})()
    process=type('P',(),{'pid':123})()
    with patch.object(w,'RUN',folder),patch.object(w,'OWNER',owner),patch.object(w,'require_test',return_value=NODE),patch.object(w,'verified_endpoint',return_value=NODE),patch.object(w,'boot_id',return_value='boot'),patch.object(w,'proc_key',return_value=1),patch.object(w,'command',side_effect=command),patch.object(w,'ready',side_effect=RuntimeError('核心未就绪')),patch.object(w,'spawn',return_value=process),patch.object(w,'stop_process'),patch.object(w.socket,'socket'),patch.object(w.time,'sleep'),patch.object(mplane,'fw4_remove'):
     with self.assertRaises(RuntimeError):w.WorkerPlane()._start()
     self.assertEqual(owner.exists(),phase=='cleanup')
     self.assertEqual((folder/'backup-core.json').exists(),phase=='cleanup')
 def test_submission_transfers_held_lock_and_invalidates_old_ui_proof(self):
  with tempfile.TemporaryDirectory() as d,patch.object(w,'RUN',Path(d)),patch.object(w,'TEST',Path(d)/'test'),patch.object(w,'load',return_value=NODE),patch.object(w.subprocess,'Popen') as popen:
   self.assertEqual(w.start_test()['state'],'submitted')
   self.assertTrue(popen.call_args.kwargs['pass_fds'])
   self.assertEqual(json.loads(w.TEST.read_text())['state'],'queued')
   self.assertFalse(json.loads(w.TEST.read_text())['usable'])



class GithubProbeTests(unittest.TestCase):
 def test_transient_handshake_timeout_is_recorded_and_retried_once(self):
  failed=type('R',(),{'returncode':28,'stdout':json.dumps({'http_code':0,'time_total':6,'time_appconnect':0})})()
  passed=type('R',(),{'returncode':0,'stdout':json.dumps({'http_code':200,'time_total':2,'time_appconnect':1})})()
  with tempfile.TemporaryDirectory() as d,patch.object(w,'curl',side_effect=[failed,passed]) as curl:
   row=w.https_result('github','https://github.com/robots.txt',Path(d)/'body',15000)
   self.assertEqual(row['state'],'ok');self.assertEqual(row['attempt_count'],2)
   self.assertIn('握手超时',row['attempts'][0]['message']);self.assertEqual(curl.call_count,2)
 def test_invalid_certificate_is_never_retried_or_reported_as_connected(self):
  failed=type('R',(),{'returncode':60,'stdout':json.dumps({'http_code':0,'time_total':1})})()
  with tempfile.TemporaryDirectory() as d,patch.object(w,'curl',return_value=failed) as curl:
   row=w.https_result('github','https://github.com/robots.txt',Path(d)/'body',15000)
   self.assertEqual(row['state'],'failed');self.assertEqual(row['message'],'证书验证失败');self.assertEqual(curl.call_count,1)

if __name__=='__main__':unittest.main()
