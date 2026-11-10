"""Synthetic runtime trees only; never execute chroot, apt or a real WARP binary."""
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'root/usr/lib/routepilot'
sys.path.insert(0, str(LIB))
import runtime_maintenance as m
import runtime_guard as guard
from public_errors import ControlError
REAL_RUN = m.run


class Maintenance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name);self.base = self.folder / 'owned';self.private = self.folder / 'private';self.run = self.folder / 'run'
        for p in (self.base,self.private,self.run):p.mkdir(mode=0o700)
        self.config = {'main': {'enabled': '0', 'preparing': '0', 'apply_nonce': 'synthetic'}}
        for name, value in [('BASE',self.base),('PRIVATE',self.private),('RUN',self.run)]:
            p=patch.object(m,name,value);p.start();self.addCleanup(p.stop)
        for name, value in [('read_config',lambda:self.config),('supported',lambda:True)]:
            p=patch.object(m,name,value);p.start();self.addCleanup(p.stop)
        p=patch.object(m,'run',side_effect=AssertionError('A real process must not run in tests'));p.start();self.addCleanup(p.stop)

    def runtime(self, root=None, version='2026.8.2115.0', identity=True):
        root=root or self.base/'runtime'
        for rel in ['usr/bin','usr/lib','var/lib/dpkg','var/lib/cloudflare-warp','etc']:(root/rel).mkdir(parents=True,exist_ok=True)
        for rel in ['usr/bin/warp-cli','usr/bin/warp-svc','usr/bin/apt-get']:(root/rel).write_text('SYNTHETIC executable placeholder')
        (root/'var/lib/dpkg/status').write_text('Package: cloudflare-warp\nStatus: install ok installed\nArchitecture: arm64\nVersion: '+version+'\n')
        (root/'usr/lib/os-release').write_text('VERSION_CODENAME=bookworm\n')
        if identity:(root/'var/lib/cloudflare-warp/reg.json').write_text('SYNTHETIC identity, never a key')
        return root

    def catalog(self, version='2026.9.1.0', **kw):
        d={'Package':'cloudflare-warp','Architecture':'arm64','Version':version,'SHA256':'a'*64,'Size':'4096','Filename':'pool/bookworm/main/c/cloudflare-warp/cloudflare-warp_'+version+'_arm64.deb',**kw}
        return '\n'.join(k+': '+v for k,v in d.items())+'\n'

    def fresh(self):
        m.save(self.private/'check.json',{'state':'checked','checked_at':time.time(),'last_attempt_at':time.time(),'candidate':m.candidate(self.catalog())})

    def plan(self, operation='upgrade', **kw):
        with patch.object(m,'inactive'),patch.object(m.shutil,'disk_usage',return_value=type('Disk',(),{'free':10**11})()):
            return m.make_plan({'operation':operation,**kw})

    def archive(self, members):
        p=self.folder/('upload-'+os.urandom(3).hex()+'.tgz')
        with tarfile.open(p,'w:gz') as tf:
            for name,kind,body in members:
                info=tarfile.TarInfo(name)
                if kind=='link':info.type=tarfile.SYMTYPE;info.linkname=body;tf.addfile(info)
                elif kind=='hard':info.type=tarfile.LNKTYPE;info.linkname=body;tf.addfile(info)
                elif kind=='device':info.type=tarfile.CHRTYPE;tf.addfile(info)
                else:info.size=len(body);tf.addfile(info,io.BytesIO(body))
        return p

    def test_metadata_requires_exact_owned_package_and_numeric_version(self):
        root=self.runtime();self.assertEqual(m.package_info(root)['version'],'2026.8.2115.0')
        (root/'usr/bin/warp-cli').unlink();self.assertIsNone(m.package_info(root))
        with self.assertRaises(ControlError):m.version_key('1; reboot')

    def test_official_catalog_rejects_wrong_path_hash_size_and_arch(self):
        self.assertEqual(m.candidate(self.catalog())['version'],'2026.9.1.0')
        for kw in [{'Filename':'https://evil.invalid/pkg.deb'},{'Filename':'../../private'},{'SHA256':'not-a-hash'},{'Size':'999999999999'},{'Version':'bad'},{'Architecture':'amd64'}]:
            with self.assertRaises(ControlError):m.candidate(self.catalog(**kw))

    def test_failed_check_does_not_report_historical_update_as_current(self):
        self.runtime();self.fresh()
        with patch.object(m,'download',side_effect=ControlError('模拟网络不可用')):m.check()
        s=m.status();self.assertEqual(s['check']['state'],'failed');self.assertIsNone(s['latest_version']);self.assertFalse(s['update_available']);self.assertTrue(s['check']['checked_at'])

    def test_successful_check_never_calls_install_or_changes_identity(self):
        root=self.runtime();before=m.fingerprint(root)
        with patch.object(m,'download',return_value=self.catalog().encode()),patch.object(m,'execute',side_effect=AssertionError('No install')):m.check()
        self.assertTrue(m.status()['update_available']);self.assertEqual(before,m.fingerprint(root))

    def test_schedule_off_interval_clock_backwards_and_state_survive(self):
        m.save(self.private/'settings.json',{'hours':0});self.assertFalse(m.due())
        m.save(self.private/'settings.json',{'hours':24});m.save(self.private/'check.json',{'last_attempt_at':100000})
        self.assertFalse(m.due(100001));self.assertTrue(m.due(200000));self.assertTrue(m.due(90000))

    def test_corrupt_settings_and_symlink_record_fail_closed(self):
        (self.private/'settings.json').write_text('null');(self.private/'settings.json').chmod(0o600)
        with self.assertRaises(ControlError):m.settings()
        (self.private/'settings.json').unlink();(self.private/'settings.json').symlink_to(self.folder/'elsewhere')
        with self.assertRaises(ControlError):m.settings()

    def test_read_status_creates_no_directories_or_processes(self):
        m.status();self.assertEqual(list(self.base.iterdir()),[]);self.assertEqual(list(self.private.iterdir()),[])

    def test_inactive_guard_rejects_master_on_and_preparing(self):
        for k in ['enabled','preparing']:
            self.config['main'][k]='1'
            with self.assertRaises(ControlError):m.inactive()
            self.config['main'][k]='0'
        (self.run/'masque-owner.json').write_text('{}')
        with self.assertRaises(ControlError):m.inactive()

    def test_shared_runtime_lease_prevents_replacement_and_reverse(self):
        shared=guard.open_lock(self.run,fcntl.LOCK_SH)
        try:
            with self.assertRaises(RuntimeError):
                with guard.maintenance(self.run,self.base):pass
        finally:os.close(shared)
        with guard.maintenance(self.run,self.base):
            with self.assertRaises(RuntimeError):guard.open_lock(self.run,fcntl.LOCK_SH)

    def test_context_changes_after_identity_or_config_edit(self):
        root=self.runtime();a=m.context();(root/'var/lib/cloudflare-warp/reg.json').write_text('SYNTHETIC identity changed');self.assertNotEqual(a,m.context())
        a=m.context();self.config['main']['apply_nonce']='different';self.assertNotEqual(a,m.context())

    def test_confirm_plan_is_required_expires_and_binds_context(self):
        self.runtime();self.fresh();p=self.plan()
        with self.assertRaises(ControlError):m.execute(p['id'],'NO')
        plan=m.document(self.private/'plan.json');plan['created_at']-=301;m.save(self.private/'plan.json',plan)
        with self.assertRaises(ControlError):m.execute(p['id'],'CONFIRM')
        p=self.plan();self.config['main']['apply_nonce']='changed'
        with self.assertRaises(ControlError):m.execute(p['id'],'CONFIRM')

    def test_low_space_and_no_new_version_do_not_make_plan(self):
        self.runtime()
        with self.assertRaises(ControlError):self.plan()
        self.fresh()
        with patch.object(m,'inactive'),patch.object(m.shutil,'disk_usage',return_value=type('Disk',(),{'free':0})()):
            with self.assertRaises(ControlError):m.make_plan({'operation':'upgrade'})
        self.assertFalse((self.private/'plan.json').exists())

    def test_first_install_refuses_existing_runtime_and_wrong_upload_checksum(self):
        self.runtime()
        with self.assertRaises(ControlError):self.plan('install',sha256='0'*64)
        import shutil;shutil.rmtree(self.base/'runtime')
        (self.base/'runtime-uploads').mkdir();(self.base/'runtime-uploads/runtime.tgz').write_bytes(b'SYNTHETIC')
        with self.assertRaises(ControlError):self.plan('install',sha256='0'*64)

    def test_archive_rejects_escapes_devices_identity_and_duplicate_paths(self):
        cases=[[('proc','link','/proc')],[('../escape','file',b'x')],[('/escape','file',b'x')],[('x','device',b'')],[('var/lib/cloudflare-warp/reg.json','file',b'private')],[('etc/machine-id','file',b'machine')],[('root/.ssh/id_rsa','file',b'private')],[('x','file',b'a'),('./x','file',b'b')],[('outside','link','/'),('outside/escape','file',b'x')],[('hard','hard','../../escape')]]
        for members in cases:
            with self.subTest(members=members):
                target=self.folder/('extract-'+os.urandom(4).hex());target.mkdir()
                with self.assertRaises(ControlError):m.unpack(self.archive(members),target)
        self.assertFalse((self.folder/'escape').exists())

    def test_archive_accepts_chroot_layout_without_following_links(self):
        target=self.folder/'extract';target.mkdir();p=self.archive([('usr/lib/example','file',b'ok'),('lib','link','usr/lib'),('etc/machine-id','file',b'')]);m.unpack(p,target)
        self.assertEqual((target/'usr/lib/example').read_bytes(),b'ok');self.assertTrue((target/'lib').is_symlink())

    def test_upload_snapshot_is_bound_to_exact_bytes(self):
        (self.base/'runtime-uploads').mkdir();p=self.base/'runtime-uploads/runtime.tgz';p.write_bytes(b'SYNTHETIC')
        target=self.private/'claimed.tgz';m.claim_upload(hashlib.sha256(b'SYNTHETIC').hexdigest(),target);p.write_bytes(b'CHANGED');self.assertEqual(target.read_bytes(),b'SYNTHETIC')
        with self.assertRaises(ControlError):m.claim_upload('0'*64,self.private/'bad.tgz')

    def test_internal_write_does_not_follow_host_symlink(self):
        root=self.folder/'stage';(root/'etc').mkdir(parents=True);outside=self.folder/'outside';outside.write_text('untouched');(root/'etc/resolv.conf').symlink_to(outside)
        m.internal(root,'etc/resolv.conf','nameserver example\n');self.assertEqual(outside.read_text(),'untouched');self.assertFalse((root/'etc/resolv.conf').is_symlink())
        (root/'linked').symlink_to(self.folder,target_is_directory=True)
        with self.assertRaises(ControlError):m.internal(root,'linked/pwn','x')

    def test_upgrade_preserves_identity_and_retains_original_for_rollback(self):
        root=self.runtime();self.fresh();old=m.fingerprint(root);p=self.plan()
        def upgrade(stage,plan):
            q=stage/'var/lib/dpkg/status';q.write_text(q.read_text().replace('2026.8.2115.0',plan['version']))
        with patch.object(m,'inactive'),patch.object(m,'upgrade_copy',side_effect=upgrade),patch.object(m,'test_runtime',side_effect=lambda r,v=None:m.package_info(r)):
            m.execute(p['id'],'CONFIRM')
        self.assertEqual(m.package_info(root)['version'],'2026.9.1.0');self.assertEqual((root/'var/lib/cloudflare-warp/reg.json').read_text(),'SYNTHETIC identity, never a key')
        backup=m.backups()[0];self.assertEqual(m.fingerprint(self.base/'runtime-backups'/backup['id']/'runtime'),old);self.assertFalse((self.base/'.runtime-transaction.json').exists())
        with self.assertRaises(ControlError):m.execute(p['id'],'CONFIRM')

    def test_upgrade_identity_change_aborts_before_swap(self):
        root=self.runtime();before=m.fingerprint(root);self.fresh();p=self.plan()
        def bad(stage,plan):(stage/'var/lib/cloudflare-warp/reg.json').write_text('CHANGED')
        with patch.object(m,'inactive'),patch.object(m,'upgrade_copy',side_effect=bad),patch.object(m,'test_runtime',return_value={'version':'2026.9.1.0'}):
            with self.assertRaises(ControlError):m.execute(p['id'],'CONFIRM')
        self.assertEqual(before,m.fingerprint(root));self.assertEqual(m.backups(),[])

    def test_swap_failure_restores_original_and_no_automatic_retry(self):
        root=self.runtime();before=m.fingerprint(root);stage=self.runtime(self.base/'stage','2026.9.1.0')
        with patch.object(m,'test_runtime',side_effect=ControlError('模拟自检失败')):
            with self.assertRaises(ControlError):m.swap(stage,'2026.9.1.0')
        self.assertEqual(before,m.fingerprint(root));self.assertFalse((self.base/'.runtime-transaction.json').exists())

    def test_prepared_interruption_can_be_verified_without_replacing_files(self):
        root=self.runtime();before=m.fingerprint(root);ident='b'*32;b=self.base/'runtime-backups'/ident;b.mkdir(parents=True)
        m.save(b/'meta.json',{'id':ident,'fingerprint':before,'version':'2026.8.2115.0','created_at':time.time()});m.save(self.base/'.runtime-transaction.json',{'id':ident,'phase':'prepared'})
        p=self.plan('recover')
        with patch.object(m,'inactive'):m.execute(p['id'],'CONFIRM')
        self.assertEqual(before,m.fingerprint(root));self.assertFalse((self.base/'.runtime-transaction.json').exists())

    def test_rollback_preserves_current_identity_and_verifies_backup(self):
        root=self.runtime();self.fresh();p=self.plan()
        def upgrade(stage,plan):
            f=stage/'var/lib/dpkg/status';f.write_text(f.read_text().replace('2026.8.2115.0',plan['version']))
        with patch.object(m,'inactive'),patch.object(m,'upgrade_copy',side_effect=upgrade),patch.object(m,'test_runtime',side_effect=lambda r,v=None:m.package_info(r)):m.execute(p['id'],'CONFIRM')
        backup=m.backups()[0];(root/'var/lib/cloudflare-warp/reg.json').write_text('CURRENT SYNTHETIC identity')
        p=self.plan('rollback',backup_id=backup['id'])
        with patch.object(m,'inactive'),patch.object(m,'test_runtime',side_effect=lambda r,v=None:m.package_info(r)):m.execute(p['id'],'CONFIRM')
        self.assertEqual(m.package_info(root)['version'],'2026.8.2115.0');self.assertEqual((root/'var/lib/cloudflare-warp/reg.json').read_text(),'CURRENT SYNTHETIC identity')

    def test_download_only_uses_constant_https_official_source(self):
        with self.assertRaises(ControlError):m.download('https://evil.invalid',self.folder/'out',1)

    def test_legacy_unmanaged_runtime_is_detected_without_guessing_version(self):
        root=self.runtime();(root/'var/lib/dpkg/status').write_text('Package: base-files\nStatus: install ok installed\nVersion: 12\n')
        header=bytearray(20);header[:6]=b'\x7fELF\x02\x01';header[18:20]=(183).to_bytes(2,'little');(root/'usr/bin/warp-cli').write_bytes(header)
        with patch.object(m,'run',return_value='warp-cli 2026.8.2115.0\n') as cmd:
            self.assertEqual(m.package_info(root),{'version':'2026.8.2115.0','architecture':'arm64','package_managed':False})
            self.assertTrue(cmd.call_args.kwargs['isolated'])
        with patch.object(m,'run',side_effect=AssertionError('Never execute during a job')):
            self.assertEqual(m.package_info(root,probe=False)['version'],None)
            m.save(self.private/'job.json',{'state':'running','updated_at':time.time(),'local_version':'2026.8.2115.0'})
            self.assertEqual(m.status()['local_version'],'2026.8.2115.0')

    def test_changed_signing_key_aborts_before_apt(self):
        root=self.runtime(self.base/'stage');(root/'dev').mkdir()
        with patch.object(m,'download',return_value=b'not the pinned signing key'):
            with self.assertRaises(ControlError):m.upgrade_copy(root,{'version':'2026.9.1.0'})
        m.run.assert_not_called()

    def test_package_hash_mismatch_never_reaches_install(self):
        root=self.runtime(self.base/'stage');(root/'dev').mkdir();cache=root/'var/cache/apt/archives';cache.mkdir(parents=True)
        commands=[]
        def execute(args,*a,**kw):
            commands.append((args,kw))
            if '--download-only' in args:(cache/'cloudflare-warp_2026.9.1.0_arm64.deb').write_bytes(b'BAD')
            return ''
        key=b'SYNTHETIC public signing key'
        with patch.object(m,'download',return_value=key),patch.object(m,'KEY_SHA256',hashlib.sha256(key).hexdigest()),patch.object(m.os,'mknod'),patch.object(m,'run',side_effect=execute):
            with self.assertRaises(ControlError):m.upgrade_copy(root,{'version':'2026.9.1.0','candidate':{'size':3,'sha256':'0'*64}})
        self.assertFalse(any('--no-download' in args for args,_ in commands))
        source=(root/'etc/apt/sources.list.d/imok-warp.list').read_text();self.assertIn('signed-by=',source);self.assertIn('https://deb.debian.org/',source);self.assertNotIn('trusted=yes',source)

    def test_verified_package_installs_only_in_child_offline_namespace(self):
        root=self.runtime(self.base/'stage');(root/'dev').mkdir();cache=root/'var/cache/apt/archives';cache.mkdir(parents=True)
        commands=[]
        def execute(args,*a,**kw):
            commands.append((args,kw))
            if '--download-only' in args:(cache/'cloudflare-warp_2026.9.1.0_arm64.deb').write_bytes(b'OK')
            return ''
        key=b'SYNTHETIC public signing key'
        with patch.object(m,'download',return_value=key),patch.object(m,'KEY_SHA256',hashlib.sha256(key).hexdigest()),patch.object(m.os,'mknod'),patch.object(m,'run',side_effect=execute):
            m.upgrade_copy(root,{'version':'2026.9.1.0','candidate':{'size':2,'sha256':hashlib.sha256(b'OK').hexdigest()}})
        install=[kw for args,kw in commands if '--no-download' in args];self.assertEqual(install,[{'isolated':True}])

    def test_first_install_stages_and_never_starts_a_service(self):
        source=self.runtime(self.folder/'source',identity=False);p=self.folder/'fresh.tgz'
        with tarfile.open(p,'w:gz') as tf:
            for f in sorted(source.rglob('*')):tf.add(f,arcname=str(f.relative_to(source)),recursive=False)
        (self.base/'runtime-uploads').mkdir();(self.base/'runtime-uploads/runtime.tgz').write_bytes(p.read_bytes())
        plan=self.plan('install',sha256=m.file_hash(p))
        with patch.object(m,'inactive'),patch.object(m,'test_runtime',side_effect=lambda r,v=None:m.package_info(r)):
            m.execute(plan['id'],'CONFIRM')
        self.assertTrue(m.status()['installed']);self.assertEqual(m.backups(),[]);m.run.assert_not_called()

    def test_corrupted_backup_is_not_used_for_rollback(self):
        root=self.runtime();stage=self.runtime(self.base/'stage','2026.9.1.0')
        with patch.object(m,'test_runtime',side_effect=lambda r,v=None:m.package_info(r)):m.swap(stage,'2026.9.1.0')
        b=m.backups()[0];(self.base/'runtime-backups'/b['id']/'runtime/usr/bin/warp-cli').write_text('CORRUPTED')
        before=m.fingerprint(root);p=self.plan('rollback',backup_id=b['id'])
        with patch.object(m,'inactive'):
            with self.assertRaises(ControlError):m.execute(p['id'],'CONFIRM')
        self.assertEqual(before,m.fingerprint(root))

    def test_crash_between_renames_is_recoverable_from_original(self):
        root=self.runtime();before=m.fingerprint(root);stage=self.runtime(self.base/'stage','2026.9.1.0');rename=os.rename
        def crash(a,b):
            rename(a,b)
            if a==root:raise KeyboardInterrupt('simulated power loss')
        with patch.object(m.os,'rename',side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):m.swap(stage,'2026.9.1.0')
        self.assertFalse(root.exists());self.assertTrue((self.base/'.runtime-transaction.json').exists());backup=m.backups()[0]
        p=self.plan('rollback',backup_id=backup['id'])
        with patch.object(m,'inactive'),patch.object(m,'test_runtime',side_effect=lambda r,v=None:m.package_info(r)):m.execute(p['id'],'CONFIRM')
        self.assertEqual(before,m.fingerprint(root));self.assertFalse((self.base/'.runtime-transaction.json').exists())

    def test_queued_request_launches_after_lock_release_and_cannot_replay(self):
        from request_input import prepare_requests
        prepare_requests(self.run)
        ident='a'*32;raw=json.dumps({'request_id':ident,'payload':{}});token=ident+':'+hashlib.sha256(raw.encode()).hexdigest();slot=self.run/'requests'/('runtime-input-'+ident+'.json');slot.write_text(raw);slot.chmod(0o600)
        called=[]
        def launch(action,jobtoken):
            with m.job_lock():called.append((action,jobtoken))
        with patch.object(m,'launch',side_effect=launch):r=m.dispatch('check',token)
        self.assertEqual(r['request_id'],ident);self.assertEqual(called[0][0],'check')
        with patch.object(m,'check',return_value={'state':'checked','message':'模拟检查完成'}):m.job(*called[0])
        with self.assertRaises(ControlError):m.job(*called[0])

    def test_public_status_has_no_identity_material(self):
        self.runtime();self.assertNotIn('SYNTHETIC identity',json.dumps(m.status()))

    def test_timeout_kills_only_own_command_group_and_never_returns_stderr(self):
        from unittest.mock import Mock
        child=Mock(pid=12345,returncode=-9)
        child.communicate.side_effect=[m.subprocess.TimeoutExpired('owned-maintenance',1),(b'',b'SYNTHETIC private stderr')]
        with patch.object(m.subprocess,'Popen',return_value=child) as popen,patch.object(m.os,'killpg') as kill:
            with self.assertRaises(ControlError):REAL_RUN(['chroot','synthetic','/usr/bin/apt-get'],1,isolated=True)
        kill.assert_called_once_with(12345,m.signal.SIGKILL)
        self.assertTrue(popen.call_args.kwargs['start_new_session']);self.assertIs(popen.call_args.kwargs['preexec_fn'],m.offline_network)

    def test_namespace_lease_covers_plane_lifetime_and_pending_journal_blocks_start(self):
        ns={'RUN':self.run,'ROOT':self.base/'runtime'}
        exec('def start(self):\n self.owned = True\n',ns)
        start=guard.guarded_start(ns['start']);plane=type('Plane',(),{})();start(plane)
        with self.assertRaises(RuntimeError):
            with guard.maintenance(self.run,self.base):pass
        guard.release_plane(plane)
        with guard.maintenance(self.run,self.base):pass
        (self.base/'.runtime-transaction.json').write_text('{}')
        with self.assertRaises(RuntimeError):start(plane)
        self.assertIsNone(plane._runtime_lease)

    def test_configuration_change_during_staging_aborts_without_losing_user_edit(self):
        root=self.runtime();before=m.fingerprint(root);self.fresh();p=self.plan()
        def changed(stage,plan):
            f=stage/'var/lib/dpkg/status';f.write_text(f.read_text().replace('2026.8.2115.0',plan['version']))
            self.config['main']['apply_nonce']='USER EDIT DURING MAINTENANCE'
        with patch.object(m,'inactive'),patch.object(m,'upgrade_copy',side_effect=changed),patch.object(m,'test_runtime',side_effect=lambda r,v=None:m.package_info(r)):
            with self.assertRaises(ControlError):m.execute(p['id'],'CONFIRM')
        self.assertEqual(before,m.fingerprint(root));self.assertEqual(self.config['main']['apply_nonce'],'USER EDIT DURING MAINTENANCE');self.assertEqual(m.backups(),[])

if __name__=='__main__':unittest.main()
