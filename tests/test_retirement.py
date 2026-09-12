from copy import deepcopy
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from sub2easy.gui import create_app, DEFAULT_PROFILE
from sub2easy.intake import parse_batch
from sub2easy.retirement import TEAM_LOST
from sub2easy.vault import VaultError
from tests.test_monitor import cloud, EMAIL


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=create_app(self.tmp.name,token='TEST',start_worker=False)
        self.c=TestClient(self.app,base_url='http://127.0.0.1:8765',headers={'x-local-token':'TEST'})
        self.c.__enter__();self.v=self.app.state.vault;self.s=self.app.state.service
        self.v.unlock('synthetic-master-password',setup=True)
        self.setting={'sub2api_url':'https://example.invalid','admin_key':'ADMIN','cloud_revision':'v1'}
        self.v.set_setting('connection',self.setting)
        self.v.set_setting('retirement_config',{'enabled':True,'group_id':None,
                                                'instance':'https://example.invalid/api/v1/admin'})
        self.v.import_materials(parse_batch(EMAIL+'----PASSWORD----JBSWY3DPEHPK3PXP'),DEFAULT_PROFILE)
        self.aid=self.v.accounts()[0]['id'];self.remote=cloud();self.writes=[]
        self.binding={'instance':'https://example.invalid/api/v1/admin','cloud_id':42,'identity':{
            'account_email':EMAIL,'chatgpt_account_id':'workspace','chatgpt_user_id':'user'}}
        self.v.update_account(self.aid,status='review',binding=self.binding,result_code=TEAM_LOST,
                              raw_result={'kept':'personal-credentials'},monitor={'enabled':True,'blocked':True,'last_code':TEAM_LOST,
                              'owned_pause':{'state':'confirmed'},'automatic_reauth':{'stale':True}})
        self.patches=[]
        def mock(obj,name,**kw):
            p=patch.object(obj,name,**kw);self.patches.append(p);return p.start()
        self.options=mock(self.s,'options',return_value={'groups':[{'id':90,'name':'测试组'}],'proxies':[]})
        self.read=mock(self.s,'cloud_read',side_effect=lambda path:deepcopy(self.remote))
        self.post=mock(self.s,'cloud_write',side_effect=self.pause)
        self.put=mock(self.s,'cloud_put',side_effect=self.move)
        self.auth=mock(self.s.connector,'authorize')
        self.probe=mock(self.s.monitor,'probe')
    def tearDown(self):
        for p in self.patches:p.stop()
        self.c.__exit__(None,None,None);self.tmp.cleanup()
    def pause(self,path,body):
        self.writes.append((path,body));self.remote.update(body,updated_at=self.remote['updated_at']+'x')
        return deepcopy(self.remote)
    def move(self,path,body):return self.pause(path,body)
    def run_retire(self):
        self.s.retirement.scan(True);self.s.run_one()
    def test_terminal_cleanup_moves_to_only_test_group_never_login_apply_probe_enable(self):
        before=deepcopy(self.remote);self.run_retire()
        self.assertEqual(self.writes,[('/accounts/42/schedulable',{'schedulable':False}),('/accounts/42',{'group_ids':[90]})])
        self.assertEqual(self.remote['credentials'],before['credentials'])
        for key in ('extra','proxy_id','concurrency','priority','rate_multiplier'):self.assertEqual(self.remote[key],before[key])
        a=self.v.account(self.aid);self.assertEqual(a['status'],'retired');self.assertEqual(a['binding'],self.binding)
        self.assertFalse(a['monitor']['enabled']);self.assertFalse(a['monitor']['blocked']);self.assertFalse(a['monitor']['auth_401'])
        self.assertIsNone(a['monitor']['automatic_reauth']);self.assertIsNone(a['monitor']['owned_pause'])
        self.assertEqual(a['raw_result'],{'kept':'personal-credentials'})
        self.assertEqual(self.v.jobs()[0]['state'],'succeeded');self.assertEqual(self.v.jobs()[0]['kind'],'retire')
        self.auth.assert_not_called();self.probe.assert_not_called()
        self.s.retirement.scan(True);self.assertEqual(len(self.v.jobs()),1)
        with self.assertRaisesRegex(VaultError,'ACCOUNT_RETIRED'):self.v.queue([self.aid])
        with self.assertRaisesRegex(VaultError,'ACCOUNT_RETIRED'):self.s.monitor.set_accounts([self.aid],True)
    def test_already_paused_skips_pause_post(self):
        self.remote['schedulable']=False;self.run_retire()
        self.assertEqual(self.writes,[('/accounts/42',{'group_ids':[90]})])
    def test_already_in_test_group_completes_without_mutations(self):
        self.remote.update(schedulable=False,group_ids=[90]);self.run_retire()
        self.assertEqual(self.writes,[]);self.assertEqual(self.v.account(self.aid)['status'],'retired')
    def test_other_workspace_error_is_not_terminal_cleanup(self):
        self.v.update_account(self.aid,result_code='REAUTH_WORKSPACE_CHANGED',monitor={'enabled':True,'last_code':'REAUTH_WORKSPACE_CHANGED'})
        self.run_retire();self.assertEqual(self.v.jobs(),[]);self.assertTrue(self.v.account(self.aid)['monitor']['enabled'])
    def test_missing_or_ambiguous_test_group_never_uses_staging(self):
        for groups in [[{'id':24,'name':'隔离组'}],[{'id':1,'name':'测试组'},{'id':2,'name':'测试组'}]]:
            self.options.return_value={'groups':groups,'proxies':[]};self.s.retirement.scan(True)
            self.assertEqual(self.v.jobs(),[]);self.assertEqual(self.writes,[])
            self.assertFalse(self.v.account(self.aid)['monitor']['enabled'])
            self.assertEqual(self.s.retirement.view()['runtime']['code'],'RETIREMENT_GROUP_REQUIRED')
    def test_explicit_group_dropdown_config(self):
        self.options.return_value={'groups':[{'id':91,'name':'我的测试'}]}
        response=self.c.post('/api/retirement/config',json={'enabled':True,'group_id':91,
                             'confirm_transfer':True,'connection_revision':'v1'})
        self.assertEqual(response.status_code,200,response.text);self.run_retire()
        self.assertEqual(self.remote['group_ids'],[91])
    def test_identity_conflict_never_touches_cloud(self):
        self.remote['credentials']['chatgpt_account_id']='changed';self.run_retire()
        self.assertEqual(self.writes,[]);self.assertEqual(self.v.account(self.aid)['retirement']['state'],'failed')
    def test_configuration_change_during_precheck_no_write(self):
        calls=0
        def read(path):
            nonlocal calls
            calls+=1
            if calls==2:self.remote['group_ids']=[999]
            return deepcopy(self.remote)
        self.read.side_effect=read;self.run_retire()
        self.assertEqual(self.writes,[]);self.assertEqual(self.v.jobs()[0]['code'],'RETIREMENT_CLOUD_CHANGED')
    def test_ambiguous_move_is_not_replayed(self):
        self.remote['schedulable']=False
        def fail(path,body):self.move(path,body);raise VaultError('CLOUD_WRITE_RESULT_UNKNOWN')
        self.put.side_effect=fail;self.run_retire()
        self.assertEqual(self.v.jobs()[0]['state'],'unknown')
        self.assertEqual(self.v.account(self.aid)['retirement']['state'],'unknown')
        self.s.retirement.scan(True);self.s.run_one();self.assertEqual(len(self.writes),1)
        self.assertNotEqual(self.v.account(self.aid)['status'],'retired')
    def test_unknown_original_write_cannot_be_cleaned(self):
        self.v.update_account(self.aid,write_intent={'state':'unknown'})
        self.run_retire();self.assertEqual(self.writes,[]);self.assertEqual(self.v.jobs(),[])
    def test_running_account_not_enqueued(self):
        self.s.tasks.active['running']={('local',self.aid)}
        self.s.retirement.scan(True);self.assertEqual(self.v.jobs(),[])
        self.s.tasks.active.clear()
    def test_group_deleted_after_queue_no_mutation(self):
        self.s.retirement.scan(True);self.options.return_value={'groups':[]}
        self.s.run_one();self.assertEqual(self.writes,[])
    def test_cancellation_before_run_no_mutations(self):
        self.s.retirement.scan(True);self.v.cancel_jobs();self.s.run_one();self.assertEqual(self.writes,[])
    def test_cleanup_runs_without_nvt_cookie_and_global_monitor(self):
        self.assertFalse(self.s.monitor.config()['enabled']);self.assertNotIn('nvt_cookie',self.setting)
        self.run_retire();self.assertEqual(self.v.account(self.aid)['status'],'retired')
    def test_does_not_reclassify_old_diagnostic_on_recovered_account(self):
        self.v.update_account(self.aid,status='active',monitor={'enabled':True,'last_code':TEAM_LOST})
        self.run_retire();self.assertEqual(self.v.jobs(),[])
    def test_another_site_binding_is_not_touched(self):
        self.v.update_account(self.aid,binding={**self.binding,'instance':'https://other.invalid/api/v1/admin'})
        self.run_retire();self.assertEqual(self.v.jobs(),[]);self.assertTrue(self.v.account(self.aid)['monitor']['enabled'])
    def test_claim_rechecks_exact_reason(self):
        self.s.retirement.scan(True)
        self.v.update_account(self.aid,result_code='OTHER',monitor={'last_code':'OTHER'})
        self.s.run_one();self.assertEqual(self.writes,[]);self.assertEqual(self.v.jobs()[0]['state'],'failed')

    def test_new_install_never_cleans_without_configuration(self):
        self.v.set_setting('retirement_config',{})
        self.run_retire();self.assertEqual(self.v.jobs(),[]);self.assertEqual(self.writes,[])
        self.assertFalse(self.s.retirement.config()['enabled'])
        self.assertTrue(self.v.account(self.aid)['monitor']['enabled'])

    def test_enabling_requires_confirmation_and_current_site_revision(self):
        before=self.s.retirement.config()
        r=self.c.post('/api/retirement/config',json={'enabled':True,'group_id':90,'connection_revision':'v1'})
        self.assertEqual(r.json()['code'],'CONFIRM_RETIREMENT_TRANSFER')
        r=self.c.post('/api/retirement/config',json={'enabled':True,'group_id':90,'confirm_transfer':True,
                                                  'connection_revision':'obsolete'})
        self.assertEqual(r.json()['code'],'RETIREMENT_SITE_CHANGED')
        self.assertEqual(self.s.retirement.config(),before)


if __name__=='__main__':unittest.main()
