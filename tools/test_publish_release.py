"""Release protection tests use a fake API and temporary files, never a live repository."""
import hashlib,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import publish_release as release
class ReleaseProtection(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.file=Path(self.folder.name)/'example.ipk';self.file.write_bytes(b'reviewed package')
        self.asset={'name':self.file.name,'size':self.file.stat().st_size,'state':'uploaded','digest':'sha256:'+release.digest(self.file)}
        self.head='a'*40
    def test_changed_attachment_is_rejected(self):
        release.verify_assets({'assets':[self.asset]},[self.file]);self.file.write_bytes(b'changed')
        with self.assertRaises(AssertionError):release.verify_assets({'assets':[self.asset]},[self.file])
    def test_existing_tag_is_never_rewritten(self):
        with patch.object(release,'api',return_value={'object':{'type':'commit','sha':'b'*40}}) as api:
            with self.assertRaises(AssertionError):release.publish('0.2.35-6',[self.file],self.head,'notes')
            self.assertEqual(api.call_count,1)
    def test_upload_failure_leaves_draft_unpublished(self):
        calls=[]
        def api(method,path,payload=None,**kwargs):
            calls.append((method,path,payload))
            if method=='GET':return None
            if path=='/releases':
                self.assertTrue(payload['draft']);return {'id':1,'assets':[],'upload_url':'https://uploads.github.com/repos/routepilot-lab/imok/releases/1/assets{?name}'}
            raise RuntimeError('simulated upload failure')
        with patch.object(release,'api',side_effect=api):
            with self.assertRaises(RuntimeError):release.publish('0.2.35-6',[self.file],self.head,'notes')
        self.assertFalse(any(method=='PATCH' for method,_,_ in calls))
    def test_existing_public_release_is_only_verified(self):
        public={'draft':False,'prerelease':False,'assets':[self.asset]}
        with patch.object(release,'api',side_effect=[{'object':{'type':'commit','sha':self.head}},public]) as api:
            self.assertEqual(release.publish('0.2.35-6',[self.file],self.head,'notes'),public)
            self.assertTrue(all(c.args[0]=='GET' for c in api.call_args_list))
if __name__=='__main__':unittest.main()
