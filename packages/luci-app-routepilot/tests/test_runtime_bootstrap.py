"""Automatic setup uses official-shaped synthetic archives and fake child processes."""
import hashlib
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

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'root/usr/lib/routepilot'))
import runtime_maintenance as m
import runtime_bootstrap as b
from public_errors import ControlError


class Bootstrap(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.base=self.root/'owned';self.private=self.root/'private';self.run=self.root/'run'
        for p in (self.base,self.private,self.run):p.mkdir(mode=0o700)
        self.cfg={'main':{'enabled':'0','preparing':'0','apply_nonce':'SYNTHETIC'}}
        self.calls=[];self.warp=b'SYNTHETIC WARP PACKAGE'.ljust(2048,b'X');self.dbus=b'SYNTHETIC DBUS PACKAGE';self.corrupt=False;self.register=False
        self.archive=self.archive_bytes()
        for name,value in [('BASE',self.base),('PRIVATE',self.private),('RUN',self.run),('read_config',lambda:self.cfg),('supported',lambda:True),('inactive',lambda:None),('run',self.command),('download',self.download),('test_runtime',lambda root,version=None:m.package_info(root))]:
            p=patch.object(m,name,value);p.start();self.addCleanup(p.stop)
        pin={**b.ROOTFS,'size':len(self.archive),'sha256':hashlib.sha256(self.archive).hexdigest()}
        p=patch.object(b,'ROOTFS',pin);p.start();self.addCleanup(p.stop)
        p=patch.object(b,'detect',return_value={'can_prepare':True,'issues':[]});self.real_detect=p.get_original()[0];p.start();self.addCleanup(p.stop)
        p=patch.object(b,'configure_apt',side_effect=lambda _,root:(root/'var/cache/apt/archives').mkdir(parents=True,exist_ok=True));self.real_configure=p.get_original()[0];p.start();self.addCleanup(p.stop)

    def archive_bytes(self,arch=183):
        data={
          'usr/lib/os-release':b'VERSION_CODENAME=bookworm\n',
          'usr/bin/apt-get':b'SYNTHETIC APT', 'usr/bin/gpgv':b'SYNTHETIC GPG',
          'usr/share/keyrings/debian-archive-keyring.gpg':b'SYNTHETIC PUBLIC KEYRING',
          'var/lib/dpkg/status':b'Package: base-files\nStatus: install ok installed\nArchitecture: arm64\nVersion: 12\n',
        }
        header=bytearray(20);header[:6]=b'\x7fELF\x02\x01';header[18:20]=arch.to_bytes(2,'little');data['usr/bin/dpkg']=bytes(header)
        out=io.BytesIO()
        with tarfile.open(fileobj=out,mode='w:gz') as tf:
            for name,raw in data.items():
                item=tarfile.TarInfo(name);item.size=len(raw);tf.addfile(item,io.BytesIO(raw))
        return out.getvalue()

    def download(self,relative,dest,limit):
        if relative.endswith('/Packages'):
            return ('Package: cloudflare-warp\nVersion: 2026.8.2115.0\nArchitecture: arm64\nSize: '+str(len(self.warp))+'\nSHA256: '+hashlib.sha256(self.warp).hexdigest()+'\nFilename: pool/bookworm/main/c/cloudflare-warp/cloudflare-warp_2026.8.2115.0_arm64.deb\n').encode()
        raise AssertionError('Unexpected network access')

    def command(self,args,*unused,**kw):
        self.calls.append((args,kw))
        if args[0]=='curl':Path(args[args.index('--output')+1]).write_bytes(self.archive)
        elif args[0]=='chroot':
            root=Path(args[1]);cache=root/'var/cache/apt/archives'
            if '--download-only' in args:
                (cache/'cloudflare-warp_2026.8.2115.0_arm64.deb').write_bytes(b'BAD' if self.corrupt else self.warp)
                (cache/'dbus_1_arm64.deb').write_bytes(self.dbus)
            elif '--no-download' in args:
                for name in ['warp-cli','warp-svc']:(root/'usr/bin'/name).write_text('SYNTHETIC BINARY')
                with (root/'var/lib/dpkg/status').open('a') as f:f.write('\nPackage: cloudflare-warp\nStatus: install ok installed\nArchitecture: arm64\nVersion: 2026.8.2115.0\n')
                if self.register:
                    reg=root/'var/lib/cloudflare-warp';reg.mkdir();(reg/'reg.json').write_text('SYNTHETIC UNEXPECTED IDENTITY')
            elif args[-1]!='update':raise AssertionError(args)
        else:raise AssertionError(args)
        return ''

    def plan(self):
        with patch.object(m.shutil,'disk_usage',return_value=type('Disk',(),{'free':10**11})()):return m.make_plan({'operation':'online-install'})

    def test_detection_is_local_and_does_not_create_or_install(self):
        with patch.object(m,'supported',return_value=False):d=self.real_detect(m)
        self.assertFalse(d['can_prepare']);self.assertEqual(self.calls,[]);self.assertEqual(list(self.base.iterdir()),[])

    def test_low_disk_is_detected_before_download(self):
        with patch.object(b.os,'geteuid',return_value=0),patch.object(b.shutil,'disk_usage',return_value=type('Disk',(),{'free':0})()):d=self.real_detect(m)
        self.assertFalse(d['can_prepare']);self.assertTrue(any('空间不足' in x for x in d['issues']))

    def test_prepare_downloads_and_verifies_but_never_installs(self):
        b.prepare(m);r=b.ready(m,verify=True)
        self.assertEqual(r['version'],'2026.8.2115.0');self.assertEqual(b.summary(m)['state'],'ready');self.assertFalse((self.base/'runtime').exists())
        self.assertFalse(any('--no-download' in a for a,_ in self.calls));self.assertTrue(any('--download-only' in a for a,_ in self.calls));self.assertEqual(self.cfg['main']['enabled'],'0')

    def test_wrong_base_hash_is_rejected_before_apt(self):
        self.archive=b'NOT AN ARCHIVE'.ljust(len(self.archive),b'X')
        with self.assertRaises(ControlError):b.prepare(m)
        self.assertFalse(any(a[0]=='chroot' for a,_ in self.calls));self.assertEqual(b.summary(m)['state'],'failed');self.assertFalse(list(self.base.glob('.runtime-prepare-*')))

    def test_download_failure_has_actionable_safe_message_and_no_install(self):
        with patch.object(m,'run',side_effect=ControlError('SYNTHETIC INTERNAL COMMAND')):
            with self.assertRaisesRegex(ControlError,'官方基础环境下载失败'):b.prepare(m)
        state=b.summary(m);self.assertEqual(state['state'],'failed');self.assertNotIn('SYNTHETIC INTERNAL',state['message'])
        self.assertFalse((self.base/'runtime').exists());self.assertFalse(list(self.base.glob('.runtime-prepare-*')))

    def test_wrong_base_architecture_is_rejected(self):
        self.archive=self.archive_bytes(62)
        with patch.object(b,'ROOTFS',{**b.ROOTFS,'size':len(self.archive),'sha256':hashlib.sha256(self.archive).hexdigest()}):
            with self.assertRaises(ControlError):b.prepare(m)
        self.assertFalse(any(a[0]=='chroot' for a,_ in self.calls))

    def test_existing_runtime_is_never_overwritten_by_prepare(self):
        (self.base/'runtime').mkdir();(self.base/'runtime/identity').write_text('SYNTHETIC KEEP')
        with self.assertRaises(ControlError):b.prepare(m)
        self.assertEqual(self.calls,[]);self.assertEqual((self.base/'runtime/identity').read_text(),'SYNTHETIC KEEP')

    def test_wrong_package_hash_prevents_prepared_state(self):
        self.corrupt=True
        with self.assertRaises(ControlError):b.prepare(m)
        self.assertIsNone(b.ready(m));self.assertFalse((self.base/'runtime').exists());self.assertFalse(any('--no-download' in a for a,_ in self.calls))

    def test_install_plan_requires_prepared_files(self):
        with self.assertRaises(ControlError):self.plan()

    def test_prepare_summary_does_not_rehash_large_files_on_poll(self):
        b.prepare(m)
        with patch.object(m,'fingerprint',side_effect=AssertionError('No hash work on poll')):self.assertEqual(b.summary(m)['state'],'ready')

    def test_missing_ready_cache_shows_retry_message_instead_of_breaking_status(self):
        b.prepare(m);r=b.ready(m)
        import shutil
        shutil.rmtree(b.stage_path(m,r['id']))
        self.assertEqual(b.summary(m)['state'],'failed')
        with self.assertRaises(ControlError):self.plan()
        b.prepare(m);self.assertEqual(b.summary(m)['state'],'ready')

    def test_tampered_prepared_tree_invalidates_confirmation_plan(self):
        b.prepare(m);r=b.ready(m);(b.stage_path(m,r['id'])/'usr/bin/apt-get').write_text('CHANGED')
        with self.assertRaises(ControlError):self.plan()

    def test_expired_preparation_requires_download_again(self):
        b.prepare(m);r=m.document(self.private/'online-ready.json');r['prepared_at']-=b.READY_AGE+1;m.save(self.private/'online-ready.json',r)
        self.assertIsNone(b.ready(m));self.assertEqual(b.summary(m)['state'],'expired')

    def test_install_only_after_confirmation_and_in_offline_child(self):
        b.prepare(m);p=self.plan()
        with self.assertRaises(ControlError):m.execute(p['id'],'NO')
        self.assertFalse((self.base/'runtime').exists());m.execute(p['id'],'CONFIRM')
        self.assertEqual(m.package_info(self.base/'runtime')['version'],'2026.8.2115.0');self.assertEqual(self.cfg['main']['enabled'],'0')
        installs=[kw for a,kw in self.calls if '--no-download' in a];self.assertEqual(installs,[{'isolated':True}]);self.assertEqual(b.summary(m)['state'],'installed')
        with self.assertRaises(ControlError):m.execute(p['id'],'CONFIRM')

    def test_package_tampering_after_confirmation_is_rejected(self):
        b.prepare(m);p=self.plan();r=b.ready(m);(b.stage_path(m,r['id'])/'var/cache/apt/archives/dbus_1_arm64.deb').write_text('CHANGED')
        with self.assertRaises(ControlError):m.execute(p['id'],'CONFIRM')
        self.assertFalse((self.base/'runtime').exists());self.assertFalse(any('--no-download' in a for a,_ in self.calls))

    def test_unexpected_registration_during_first_install_is_not_committed(self):
        b.prepare(m);p=self.plan();self.register=True
        with self.assertRaises(ControlError):m.execute(p['id'],'CONFIRM')
        self.assertFalse((self.base/'runtime').exists());self.assertEqual(b.summary(m)['state'],'failed')

    def test_unknown_signing_key_is_rejected_before_apt_configuration(self):
        root=self.root/'stage';root.mkdir()
        with patch.object(m,'download',return_value=b'SYNTHETIC WRONG KEY'):
            with self.assertRaisesRegex(ControlError,'公钥已变化'):self.real_configure(m,root)
        self.assertEqual(list(root.iterdir()),[]);self.assertEqual(self.calls,[])

    def test_download_sources_keep_tls_signature_and_service_start_guards(self):
        root=self.root/'stage';root.mkdir();(root/'dev').mkdir()
        key=b'SYNTHETIC PUBLIC KEY';read_text=Path.read_text
        def public_file(p,*a,**kw):
            if str(p)=='/etc/ssl/certs/ca-certificates.crt':return '-----BEGIN CERTIFICATE-----\nSYNTHETIC PUBLIC CERTIFICATE\n-----END CERTIFICATE-----\n'
            if str(p)=='/etc/resolv.conf':return 'nameserver 192.0.2.1\n'
            return read_text(p,*a,**kw)
        with patch.object(m,'download',return_value=key),patch.object(m,'KEY_SHA256',hashlib.sha256(key).hexdigest()),patch.object(Path,'read_text',public_file),patch.object(b.os,'mknod',side_effect=lambda p,*a:p.touch()):
            self.real_configure(m,root)
        sources=(root/'etc/apt/sources.list.d/imok-warp.list').read_text()
        self.assertEqual(sources.count('signed-by='),3);self.assertEqual(sources.count('https://'),3);self.assertNotIn('trusted=yes',sources)
        self.assertEqual((root/'usr/sbin/policy-rc.d').read_text(),'#!/bin/sh\nexit 101\n')
        args=b.apt_args(root);self.assertIn('Acquire::https::CaInfo=/etc/ssl/certs/ca-certificates.crt',args);self.assertNotIn('Acquire::https::Verify-Peer=false',args)
        self.assertEqual(self.calls,[],'Configuring sources must not launch package scripts or services')

    def test_new_download_discards_only_our_unused_ready_cache(self):
        b.prepare(m);old=b.stage_path(m,b.ready(m)['id']);b.prepare(m);self.assertFalse(old.exists());self.assertIsNotNone(b.ready(m))

    def test_unknown_cache_owner_is_not_deleted(self):
        m.save(self.private/'online-ready.json',{'owner':'another-tool','id':'a'*32})
        with self.assertRaises(ControlError):b.cleanup_ready(m)
        self.assertTrue((self.private/'online-ready.json').exists())

if __name__=='__main__':unittest.main()
