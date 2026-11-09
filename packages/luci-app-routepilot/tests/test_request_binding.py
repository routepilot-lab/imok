# Synthetic unit-test addresses; no live router, tenant or user configuration.
import hashlib,json,sys,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_endpoints as endpoint_tests
import test_config_backup as backup_tests
import endpoint_control as e
import config_backup as b
import masque as m


def put(path,id,payload):
 text=json.dumps({'request_id':id,'payload':payload});path.write_text(text)
 return id+':'+hashlib.sha256(text.encode()).hexdigest()

class RequestBinding(unittest.TestCase):
 def fixture(self,cls):
  t=cls();t.setUp();self.addCleanup(t.doCleanups);return t
 def test_overwritten_entry_is_rejected_without_consuming_other_request(self):
  t=self.fixture(endpoint_tests.Endpoints);a=put(e.INPUT,'a'*32,{'mode':'manual','address':'162.159.197.2:443'});c=put(e.INPUT,'b'*32,{'mode':'manual','address':'162.159.197.2:500'})
  with self.assertRaises(ValueError):e.save(a)
  self.assertFalse(e.CONFIG.exists());self.assertEqual(e.save(c)['settings']['address'],'162.159.197.2:500');self.assertTrue(e.INPUT.exists())
 def test_later_write_survives_earlier_entry_completion(self):
  t=self.fixture(endpoint_tests.Endpoints);a=put(e.INPUT,'a'*32,{'mode':'preserve','hours':12});tokens=[];validate=e.validate
  def interleave(data):
   tokens.append(put(e.INPUT,'b'*32,{'mode':'preserve','hours':6}));return validate(data)
  with patch.object(e,'validate',side_effect=interleave):first=e.save(a)
  self.assertEqual(first['settings']['hours'],12);self.assertEqual(e.save(tokens[0])['settings']['hours'],6)
 def test_same_id_with_tampered_content_is_rejected(self):
  t=self.fixture(endpoint_tests.Endpoints);a=put(e.INPUT,'a'*32,{'mode':'preserve','hours':12});put(e.INPUT,'a'*32,{'mode':'preserve','hours':6})
  with self.assertRaises(ValueError):e.save(a)
  self.assertFalse(e.CONFIG.exists())
 def test_old_or_unbound_client_is_rejected(self):
  t=self.fixture(endpoint_tests.Endpoints);e.INPUT.write_text('{"mode":"preserve"}')
  for id in [None,'a'*32,'../../invalid']:
   with self.assertRaises(ValueError):e.save(id)
  self.assertFalse(e.CONFIG.exists())
 def test_overwritten_import_cannot_apply_another_backup(self):
  t=self.fixture(backup_tests.ConfigBackup);a=put(b.INPUT,'a'*32,t.doc);other=json.loads(json.dumps(t.doc));other['endpoint']['hours']=6;c=put(b.INPUT,'b'*32,other)
  with patch.object(b,'command') as cmd,self.assertRaises(ValueError):b.import_settings(a)
  cmd.assert_not_called();self.assertEqual(b.UCI.read_text(),'old config\n')
  with patch.object(b,'command',side_effect=t.fake_uci):result=b.import_settings(c)
  self.assertEqual(result['request_id'],'b'*32);self.assertEqual(e.settings()['hours'],6)
 def test_later_import_input_survives_previous_import_completion(self):
  t=self.fixture(backup_tests.ConfigBackup);a=put(b.INPUT,'a'*32,t.doc);tokens=[];validate=b.validate
  def interleave(data):
   other=json.loads(json.dumps(t.doc));other['endpoint']['hours']=6;tokens.append(put(b.INPUT,'b'*32,other));return validate(data)
  with patch.object(b,'validate',side_effect=interleave),patch.object(b,'command',side_effect=t.fake_uci):b.import_settings(a)
  self.assertEqual(e.settings()['hours'],12)
  with patch.object(b,'command',side_effect=t.fake_uci):b.import_settings(tokens[0])
  self.assertEqual(e.settings()['hours'],6)

class SupervisorPause(unittest.TestCase):
 def test_unknown_transaction_stays_paused_across_five_supervisor_iterations(self):
  t=endpoint_tests.Endpoints();t.setUp();self.addCleanup(t.doCleanups);t.config()
  r={'revision':'one','previous':'162.159.197.2:8443','target':'162.159.197.2:443','baseline':'8.8.8.8','vnet':t.main['vnet_usa'],'phase':'unknown','state':'unknown'}
  e.TRANSACTION.write_text(json.dumps(r));e.APPLIED.write_text(json.dumps(r));svc=t.svc;svc.stop=Mock(return_value={'direct':True,'legacy':''});svc.plane.start=Mock();svc.plane.protocol_preference='auto';count=[0]
  def sleep(_):
   count[0]+=1
   if count[0]>=5:m.STOP=True
  cfg={'main':dict(t.main,enabled='1',backend='masque'),'policies':[]}
  with patch.object(m,'RUN',e.RUN),patch.object(m.sys,'argv',['routepilot','run']),patch.object(m,'Service',return_value=svc),patch.object(m,'STOP',False),patch.object(m,'read_config',return_value=cfg),patch.object(m,'selected_lans',return_value=['br-lan']),patch.object(m,'lan_interfaces',return_value=[{'name':'br-lan','up':True,'network':'10.77.50.0/24'}]),patch.object(m.signal,'signal'),patch.object(m.time,'sleep',side_effect=sleep):m.main()
  self.assertEqual(count[0],5);svc.plane.start.assert_not_called();self.assertEqual(svc.stop.call_count,3);self.assertEqual(t.calls,[])
 def test_active_recoverable_transaction_is_not_paused(self):
  t=endpoint_tests.Endpoints();t.setUp();self.addCleanup(t.doCleanups);t.config();r={'revision':'one','previous':'162.159.197.2:8443','target':'162.159.197.2:443','baseline':'8.8.8.8','vnet':t.main['vnet_usa'],'phase':'apply'};e.TRANSACTION.write_text(json.dumps(r));self.assertEqual(e.pause_reason(t.main),'');r['phase']='unknown';e.TRANSACTION.write_text(json.dumps(r));self.assertTrue(e.pause_reason(t.main));e.TRANSACTION.unlink();e.APPLIED.write_text('{"state":"applied"}');self.assertTrue(e.pause_reason(t.main))
 def test_corrupt_or_mismatched_transaction_pauses_before_start(self):
  t=endpoint_tests.Endpoints();t.setUp();self.addCleanup(t.doCleanups);e.TRANSACTION.write_text('bad');self.assertTrue(e.pause_reason(t.main));e.TRANSACTION.unlink();self.assertTrue(e.pause_reason(t.main))
if __name__=='__main__':unittest.main()
