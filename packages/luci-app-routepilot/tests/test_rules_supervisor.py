"""Exercise the real supervisor loop with local fakes; no router or network calls."""
import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'root/usr/lib/routepilot'))
import masque as m
import endpoint_control as e
import rules_schedule as rs


class SupervisorRulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.run = self.root/'run'; self.rules = self.root/'rules'
        self.run.mkdir(); self.rules.mkdir(); (self.rules/'china.txt').write_text('1.0.1.0/24\n')
        self.cfg = {'main': {'enabled':'1','backend':'masque','scope':'devices','warp_protocol':'auto','apply_nonce':'n','update_hours':'168'}, 'policies':[]}
        self.compiled = {'dns':{}, 'devices':[], 'exit':'usa', 'scope':'devices','vnet':'fixture'}
        self.svc = SimpleNamespace(backend='masque', update_job=None, recovery_started=None, worker_failed_nonce=None,
                                  plane=SimpleNamespace(owned=False, protocol_preference='auto',protocol='MASQUE',start=Mock()),
                                  compiled=None, exit_ip='1.0.1.1',runtime_id='fixture', health=Mock())
        self.svc.stop = Mock(return_value={'direct':True,'legacy':''})
        self.svc.apply = Mock(side_effect=lambda cfg,c: setattr(self.svc,'compiled',c))
        self.events=[]

    def run_loop(self, iterations=3, on_tick=None):
        manager=SimpleNamespace(poll=Mock(), recover_requested=Mock(return_value=False), pause_reason=Mock(return_value=''), tick=Mock(return_value=False))
        count=[0]
        def sleep(_):
            count[0]+=1
            if on_tick is not None:on_tick(count[0])
            if count[0]>=iterations:m.STOP=True
        with contextlib.ExitStack() as stack:
            for obj,key,value in [(m,'RUN',self.run),(m,'RULES',self.rules),(m.sys,'argv',['routepilot','run']),
                                  (m,'STOP',False),(m,'Service',Mock(return_value=self.svc)),(m,'read_config',Mock(return_value=self.cfg)),
                                  (m,'lan_interfaces',Mock(return_value=[])),(m,'selected_lans',Mock(return_value=[])),
                                  (m,'preflight',Mock()),(m,'compile_config',Mock(side_effect=lambda *a:dict(self.compiled,dns={}))),
                                  (m,'rule_snapshot',Mock(side_effect=lambda:contextlib.nullcontext(self.rules))),
                                  (m,'output',Mock(side_effect=lambda state,message,*a,**kw:self.events.append(dict(state=state,message=message,**kw)))),
                                  (m.signal,'signal',Mock()),(m.time,'sleep',Mock(side_effect=sleep)),
                                  (e,'Manager',Mock(return_value=manager)),(e,'stage_restored_preferences',Mock())]:
                stack.enter_context(patch.object(obj,key,value))
            m.main()

    def test_schedule_write_failure_never_tears_down_healthy_tunnel(self):
        with patch.object(rs,'atomic',side_effect=PermissionError('read-only disk')),patch.object(m.subprocess,'Popen') as spawn:
            self.run_loop()
        spawn.assert_not_called()
        self.assertEqual(self.svc.stop.call_count,2,'only startup/final shutdown may stop service')
        running=[e for e in self.events if e['state']=='running']
        self.assertEqual(len(running),3)
        self.assertTrue(all('更新计划无法持久保存' in e['message'] for e in running))

    def test_failed_background_worker_schedules_retry_without_stopping_tunnel(self):
        job=SimpleNamespace(poll=lambda:1,returncode=1)
        with patch.object(m.subprocess,'Popen',return_value=job) as spawn:
            self.run_loop()
        spawn.assert_called_once()
        schedule=rs.Schedule(self.rules,self.run).plan(self.cfg['main'])
        self.assertEqual(schedule['state'],'retry')
        self.assertTrue(schedule['retry_pending'])
        self.assertEqual(self.svc.stop.call_count,2)
        self.assertEqual(len([e for e in self.events if e['state']=='running']),3)

    def test_persisted_success_prevents_startup_redownload(self):
        schedule=rs.Schedule(self.rules,self.run);schedule.start(self.cfg['main'],'ok');schedule.finish(self.cfg['main'],True)
        with patch.object(m.subprocess,'Popen') as spawn:self.run_loop()
        spawn.assert_not_called()
        self.assertEqual(len([e for e in self.events if e['state']=='running']),3)

    def test_interrupted_blocked_worker_is_not_automatically_retried(self):
        schedule=rs.Schedule(self.rules,self.run);schedule.start(self.cfg['main'],'drop')
        (self.run/'update.json').write_text(json.dumps(dict(request_id='drop',state='blocked')))
        with patch.object(m.subprocess,'Popen') as spawn:self.run_loop()
        spawn.assert_not_called()
        plan=rs.Schedule(self.rules,self.run).plan(self.cfg['main'])
        self.assertEqual(plan['state'],'awaiting_confirmation')
        self.assertEqual(plan['next_due_at'],0)

    def test_running_status_reports_compiled_rules_revision(self):
        from rules_catalog import directory_revision
        schedule=rs.Schedule(self.rules,self.run);schedule.start(self.cfg['main'],'ok');schedule.finish(self.cfg['main'],True)
        self.run_loop(1)
        running=next(e for e in self.events if e['state']=='running')
        self.assertEqual(running['rules_revision'],directory_revision(self.rules))

    def test_resolved_candidate_is_not_overwritten_by_late_worker_completion(self):
        # The child released its operation lock; an administrator accepted the
        # candidate before the supervisor collected that child's exit status.
        def completed_after_accept():
            rs.Schedule(self.rules,self.run).resolve()
            (self.run/'update.json').write_text(json.dumps(dict(request_id='accepted-candidate',state='success')))
            return 1
        job=SimpleNamespace(poll=completed_after_accept,returncode=1)
        with patch.object(m.subprocess,'Popen',return_value=job):self.run_loop(2)
        plan=rs.Schedule(self.rules,self.run).plan(self.cfg['main'])
        self.assertEqual(plan['state'],'scheduled', 'late failed worker must not undo an explicit candidate resolution')
        self.assertFalse(plan['retry_pending'])

    def assert_preparing_failure_feedback(self, fail_resolve):
        request_id='manual-preparing-failed'
        self.cfg['main'].update(enabled='0',preparing='1',update_nonce=request_id)
        self.svc.plane.connect=Mock(side_effect=None if fail_resolve else RuntimeError('fixture connection unavailable'))
        resolver=Mock(side_effect=ValueError('fixture exit unavailable')) if fail_resolve else Mock(return_value='fixture')
        with patch.object(m,'resolve_exit',resolver),patch.object(m,'update_rules') as update:
            self.run_loop()
        update.assert_not_called()
        resolver.assert_called_once()
        if not fail_resolve:self.svc.plane.connect.assert_called_once_with('fixture')
        self.assertTrue((self.run/'update.json').exists(),'manual failure must publish request-correlated feedback')
        feedback=json.loads((self.run/'update.json').read_text())
        self.assertEqual(feedback['state'],'failed')
        self.assertEqual(feedback['request_id'],request_id)
        self.assertGreater(feedback['finished_at'],0)
        persisted=rs.Schedule(self.rules,self.run)
        self.assertGreater(persisted.data['last_attempt_at'],0,'a failed pre-download attempt still counts as an attempt')
        self.assertEqual(persisted.data['last_nonce'],request_id)
        self.assertFalse(persisted.data.get('running'))
        self.assertEqual(persisted.plan(self.cfg['main'])['state'],'paused')

    def test_preparing_manual_update_exit_resolution_failure_reports_request(self):
        self.assert_preparing_failure_feedback(True)

    def test_preparing_manual_update_connection_failure_reports_request(self):
        self.assert_preparing_failure_feedback(False)

    def test_close_then_reopen_reconciles_cancelled_worker_instead_of_staying_running(self):
        process=SimpleNamespace(poll=lambda:None,returncode=None)
        def stop(**kwargs):
            self.svc.update_job=None
            self.svc.compiled=None
            return {'direct':True,'legacy':''}
        self.svc.stop=Mock(side_effect=stop)
        def steer(tick):
            if tick==1:self.cfg['main']['enabled']='0'
            if tick==2:self.cfg['main']['enabled']='1'
        with patch.object(m.subprocess,'Popen',return_value=process) as spawn:
            self.run_loop(3,on_tick=steer)
        spawn.assert_called_once()
        persisted=rs.Schedule(self.rules,self.run)
        self.assertFalse(persisted.data.get('running'),'cancelled child must be reconciled when service comes back')
        self.assertEqual(persisted.plan(self.cfg['main'])['state'],'retry')
        states=[event['state'] for event in self.events]
        self.assertEqual(states[:3],['running','disabled','running'])
