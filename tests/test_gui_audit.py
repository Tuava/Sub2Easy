from copy import deepcopy
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from sub2easy.gui import create_app, DEFAULT_PROFILE
from sub2easy.intake import parse_batch
from sub2easy.lifecycle import OAuthIdentity
from sub2easy.nvtokens import parse_response
from sub2easy.monitor import config_fingerprint
from sub2easy.vault import VaultError
import json


class GUIAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=create_app(self.tmp.name,token='TEST',start_worker=False)
        self.c=TestClient(self.app,base_url='http://127.0.0.1:8765',headers={'x-local-token':'TEST'})
        self.c.__enter__();self.c.post('/api/unlock',json={'password':'synthetic-master-password','setup':True})
        self.s=self.app.state.service;self.v=self.app.state.vault

    def tearDown(self):
        self.c.__exit__(None,None,None);self.tmp.cleanup()

    def test_local_import_does_not_require_site_or_profile_selection(self):
        r=self.c.post('/api/import',json={'text':'local@example.invalid----PASSWORD----JBSWY3DPEHPK3PXP'})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['added'],1)

    def test_stale_deployment_dialog_cannot_submit_under_new_key(self):
        self.c.post('/api/settings',json={'sub2api_url':'https://example.invalid','admin_key':'SYNTHETIC'})
        r=self.c.post('/api/deployments',json={'confirm_deploy':True,'connection_revision':'stale'})
        self.assertEqual(r.json()['code'],'DEPLOY_CONNECTION_CHANGED')

    def test_stop_inside_cloud_get_does_not_write_credentials(self):
        self.v.set_setting('connection',{'sub2api_url':'https://example.invalid','admin_key':'SYNTHETIC'})
        self.v.import_materials(parse_batch('local@example.invalid----PASSWORD----JBSWY3DPEHPK3PXP'),DEFAULT_PROFILE)
        aid=self.v.accounts()[0]['id']
        credentials={'email':'local@example.invalid','chatgpt_account_id':'workspace','chatgpt_user_id':'user',
                     'access_token':'SYNTHETIC_ACCESS','refresh_token':'SYNTHETIC_REFRESH','client_id':'client',
                     'expires_at':'2099-01-01T00:00:00Z'}
        auth=parse_response(200,json.dumps({'platform':'openai','type':'oauth','credentials':credentials}).encode(),credentials['email']).authorization
        binding={'cloud_id':42,'instance':'https://example.invalid/api/v1/admin','identity':auth['identity']}
        self.v.update_account(aid,status='authorized',authorization=auth,binding=binding)
        remote={'id':42,'platform':'openai','type':'oauth','credentials':credentials,'updated_at':'v1',
                'status':'error','schedulable':False}
        def read(path):self.s.stop.set();return deepcopy(remote)
        with patch.object(self.s,'cloud_read',side_effect=read),patch.object(self.s,'cloud_write') as write:
            with self.assertRaisesRegex(VaultError,'MONITOR_JOB_STALE'):
                self.s.write_credentials(aid,expected_revision='v1',expected_config=config_fingerprint(remote))
            write.assert_not_called()
        self.assertIsNone(self.v.account(aid).get('write_intent'))

    def test_cancel_api_reports_queued_and_running_separately(self):
        self.v.import_materials(parse_batch('local@example.invalid----PASSWORD----JBSWY3DPEHPK3PXP'),DEFAULT_PROFILE)
        aid=self.v.accounts()[0]['id'];j=self.v.queue([aid])[0]
        r=self.c.post(f'/api/jobs/{j}/cancel',json={});self.assertEqual(r.json()['cancelled'],[j])
        j=self.v.queue([aid])[0];self.v.claim()
        r=self.c.post(f'/api/jobs/{j}/cancel',json={});self.assertEqual(r.json()['cancel_requested'],[j])
        self.assertTrue(self.v.cancellation_requested(j));self.assertEqual(self.v.jobs()[0]['state'],'running')
        self.v.finish(j,'cancelled','USER_CANCELLED')

    def test_batch_authorize_reports_one_failure_without_aborting_other_account(self):
        self.v.set_setting('connection',{'nvt_cookie':'SYNTHETIC.COOKIE'})
        self.v.import_materials(parse_batch('local@example.invalid----PASSWORD----JBSWY3DPEHPK3PXP'),DEFAULT_PROFILE)
        aid=self.v.accounts()[0]['id']
        r=self.c.post('/api/jobs',json={'account_ids':['missing',aid],'confirm_send_to_nvtokens':True})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(len(r.json()['queued']),1)
        self.assertEqual([r['state'] for r in r.json()['results']],['failed','queued'])


if __name__=='__main__':unittest.main()

class UpgradeSessionTests(unittest.TestCase):
    def test_upgrade_reuses_private_matching_launcher_session_only(self):
        from pathlib import Path
        from sub2easy.gui import previous_session
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'launch-url.txt'
            token='synthetic-private-session-token-12345678'
            path.write_text('http://127.0.0.1:8765/#token='+token);path.chmod(0o600)
            self.assertEqual(previous_session(directory,8765),token)
            self.assertIsNone(previous_session(directory,8766))
            path.chmod(0o644);self.assertIsNone(previous_session(directory,8765))

    def test_upgrade_does_not_reuse_foreign_or_malformed_session_url(self):
        from pathlib import Path
        from sub2easy.gui import previous_session
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'launch-url.txt'
            for url in ['http://elsewhere.invalid/#token='+'a'*40,'http://127.0.0.1:8765/#token=short']:
                path.write_text(url);path.chmod(0o600)
                self.assertIsNone(previous_session(directory,8765))
