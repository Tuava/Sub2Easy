from copy import deepcopy
import json
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from sub2easy.gui import create_app, DEFAULT_PROFILE
from sub2easy.intake import parse_batch


PROFILE={**DEFAULT_PROFILE,'instance_id':'https://example.invalid/api/v1/admin'}
TEXT='inline@example.invalid----SYNTHETIC_PASSWORD----JBSWY3DPEHPK3PXP'


def oauth(email='inline@example.invalid',workspace='workspace'):
    return {'platform':'openai','type':'oauth','credentials':{
        'email':email,'chatgpt_account_id':workspace,'client_id':'client',
        'access_token':'SYNTHETIC_ACCESS','refresh_token':'SYNTHETIC_REFRESH','expires_at':'2099-01-01T00:00:00Z'}}


class InlineImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=create_app(self.tmp.name,token='TEST',start_worker=False)
        self.c=TestClient(self.app,base_url='http://127.0.0.1:8765',headers={'x-local-token':'TEST'});self.c.__enter__()
        self.c.post('/api/unlock',json={'setup':True,'password':'synthetic-master-password'})
        self.c.post('/api/settings',json={'sub2api_url':'https://example.invalid','admin_key':'SYNTHETIC_ADMIN'})
        self.v=self.app.state.vault
        self.revision=self.c.get('/api/state').json()['settings']['cloud_revision']
        self.body={'request_id':'synthetic-request-1234','format':'login','text':TEXT,'profile':PROFILE,
                   'connection_revision':self.revision,'model_id':'test-model','confirm_deploy':True,'staging_verified':True}

    def tearDown(self):
        self.c.__exit__(None,None,None);self.tmp.cleanup()

    def submit(self,**updates):
        r=self.c.post('/api/import/deploy',json={**self.body,**updates})
        self.assertEqual(r.status_code,200,r.text);return r.json()

    def test_text_single_request_saves_and_queues_exact_input_not_other_inventory(self):
        self.v.import_materials(parse_batch(TEXT.replace('inline@','other@')),PROFILE)
        other=self.v.accounts()[0]['id']
        with patch.object(self.app.state.service,'cloud_write') as write,patch.object(self.app.state.service.connector,'authorize') as auth:
            batch=self.submit()
        self.assertEqual(batch['counts'],{'queued':1});self.assertEqual(len(self.v.accounts()),2)
        self.assertNotEqual(batch['items'][0]['account_id'],other)
        self.assertEqual(len(self.v.jobs()),1);self.assertEqual(self.v.jobs()[0]['kind'],'server_deploy')
        write.assert_not_called();auth.assert_not_called()

    def test_identical_submission_replays_and_reports_current_job_status(self):
        batch=self.submit();job_id=batch['items'][0]['job_id']
        self.v.finish(job_id,'failed','PROBE_FAILED','verify')
        replay=self.submit()
        self.assertEqual(replay['items'][0]['job_id'],job_id)
        self.assertEqual(replay['items'][0]['state'],'failed');self.assertEqual(len(self.v.jobs()),1)
        self.assertEqual(self.c.get('/api/import/batch').json()['items'][0]['code'],'PROBE_FAILED')

    def test_reused_request_id_with_changed_material_is_rejected(self):
        self.submit()
        r=self.c.post('/api/import/deploy',json={**self.body,'text':TEXT.replace('inline@','changed@')})
        self.assertEqual(r.json()['code'],'IMPORT_REQUEST_CONFLICT');self.assertEqual(len(self.v.accounts()),1)

    def test_batch_response_does_not_return_secrets_or_digest(self):
        batch=self.submit()
        output=json.dumps(batch)+self.c.get('/api/import/batch').text
        self.assertEqual(batch['items'][0]['label'],'inline@example.invalid')
        for value in ['SYNTHETIC_PASSWORD','JBSWY3DPEHPK3PXP','SYNTHETIC_ADMIN','digest']:
            self.assertNotIn(value,output)

    def test_local_batch_survives_reload_and_deploys_exact_rows_without_materials(self):
        report=self.c.post('/api/import',json={'text':TEXT}).json()
        batch=report['batch'];self.assertEqual(batch['counts'],{'local_saved':1})
        self.assertEqual(batch['items'][0]['label'],'inline@example.invalid')
        self.assertEqual(self.v.jobs(),[])
        self.v.import_materials(parse_batch(TEXT.replace('inline@','other@')),PROFILE)
        self.assertEqual(self.c.get('/api/import/batch').json()['id'],batch['id'])
        body={k:v for k,v in self.body.items() if k not in {'text','format','request_id'}}
        path='/api/import/batch/'+batch['id']+'/deploy'
        r=self.c.post(path,json=body);self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['counts'],{'queued':1})
        self.assertEqual(self.v.jobs()[0]['account_id'],batch['items'][0]['account_id'])
        again=self.c.post(path,json=body);self.assertEqual(again.json()['items'],r.json()['items'])
        self.assertEqual(len(self.v.jobs()),1)

    def test_local_batch_history_offline_and_locked(self):
        self.v.set_setting('connection',{})
        a=self.c.post('/api/import',json={'text':TEXT}).json()['batch']
        b=self.c.post('/api/import',json={'format':'sub2','text':json.dumps(oauth('two@example.invalid'))}).json()['batch']
        self.assertEqual([r['id'] for r in self.c.get('/api/import/history').json()],[b['id'],a['id']])
        self.assertEqual(self.c.get('/api/import/batch?request_id='+a['id']).json()['items'][0]['label'],'inline@example.invalid')
        self.assertEqual(self.c.post('/api/lock',json={}).status_code,200)
        self.assertEqual(self.c.get('/api/import/history').json()['code'],'VAULT_LOCKED')

    def test_local_conflict_cannot_upload_existing_password(self):
        self.v.import_materials(parse_batch(TEXT.replace('SYNTHETIC_PASSWORD','different')),PROFILE)
        batch=self.c.post('/api/import',json={'text':TEXT}).json()['batch']
        r=self.c.post('/api/import/batch/'+batch['id']+'/deploy',json=self.body)
        self.assertEqual(r.status_code,200);self.assertEqual(r.json()['counts'],{'failed':1})
        self.assertEqual(self.v.jobs(),[])

    def test_list_full_email_and_order_do_not_change_when_monitor_updates(self):
        self.c.post('/api/import',json={'text':TEXT})
        first=self.v.accounts()[0]['id']
        self.c.post('/api/import',json={'text':TEXT.replace('inline@','newest@')})
        before=[a['id'] for a in self.v.accounts()]
        self.v.update_account(first,monitor={'last_check':1234})
        self.assertEqual(before,[a['id'] for a in self.v.accounts()])
        self.assertEqual(self.v.accounts()[1]['label'],'inline@example.invalid')

    def test_existing_text_record_is_reused(self):
        self.v.import_materials(parse_batch(TEXT),PROFILE);aid=self.v.accounts()[0]['id']
        batch=self.submit();self.assertEqual(batch['items'][0]['account_id'],aid)
        self.assertEqual(batch['items'][0]['intake_state'],'duplicate');self.assertEqual(len(self.v.accounts()),1)

    def test_text_conflict_does_not_deploy_old_password(self):
        self.v.import_materials(parse_batch(TEXT.replace('SYNTHETIC_PASSWORD','DIFFERENT_PASSWORD')),PROFILE)
        batch=self.submit();self.assertEqual(batch['counts'],{'failed':1});self.assertEqual(self.v.jobs(),[])
        self.assertEqual(batch['items'][0]['code'],'CONFLICTING_LOGIN_MATERIAL')

    def test_malformed_text_row_does_not_drop_valid_rows(self):
        batch=self.submit(text=TEXT+'\nBAD_INPUT\n'+TEXT)
        self.assertEqual(batch['counts'],{'queued':1,'failed':1,'skipped':1})
        self.assertEqual(len(self.v.jobs()),1)

    def test_json_without_login_materials_goes_directly_to_server_job(self):
        batch=self.submit(format='sub2',text=json.dumps({'accounts':[oauth()]}))
        aid=batch['items'][0]['account_id']
        self.assertEqual(batch['counts'],{'queued':1});self.assertIsNotNone(self.v.account(aid)['authorization'])
        self.assertNotIn('password',self.v.account(aid)['login'])

    def test_json_conflict_never_deploys_existing_identity(self):
        self.c.post('/api/import',json={'format':'sub2','text':json.dumps(oauth(workspace='other')),'profile':PROFILE})
        batch=self.submit(format='sub2',text=json.dumps(oauth()))
        self.assertEqual(batch['counts'],{'failed':1});self.assertEqual(self.v.jobs(),[])

    def test_validation_precedes_saving_materials(self):
        for changed,code in [({'model_id':''},'DEPLOY_MODEL_REQUIRED'),({'confirm_deploy':False},'CONFIRM_SERVER_DEPLOYMENT'),
                             ({'connection_revision':'changed'},'DEPLOY_CONNECTION_CHANGED')]:
            r=self.c.post('/api/import/deploy',json={**self.body,**changed})
            self.assertEqual(r.json()['code'],code);self.assertEqual(self.v.accounts(),[])

    def test_failed_new_isolation_keeps_material_with_same_page_error(self):
        batch=self.submit(staging_verified=False)
        self.assertEqual(len(self.v.accounts()),1);self.assertEqual(self.v.jobs(),[])
        self.assertEqual(batch['items'][0]['code'],'STAGING_ISOLATION_CONFIRMATION_REQUIRED')

    def test_queue_exception_rolls_back_materials_and_batch(self):
        with patch.object(self.app.state.service.deployments,'queue',side_effect=RuntimeError('synthetic fail')):
            r=self.c.post('/api/import/deploy',json=self.body)
        self.assertEqual(r.status_code,500);self.assertEqual(self.v.accounts(),[]);self.assertEqual(self.v.jobs(),[])
        self.assertIsNone(self.c.get('/api/import/batch').json())

    def test_same_page_retry_retains_snapshot_and_changes_job(self):
        batch=self.submit();old=batch['items'][0]['job_id']
        self.v.finish(old,'failed','DEPLOY_READ_FAILED','precheck')
        r=self.c.post('/api/import/batch/'+batch['id']+'/retry',json={'indices':[1]})
        self.assertEqual(r.status_code,200,r.text)
        self.assertNotEqual(r.json()['items'][0]['job_id'],old);self.assertEqual(r.json()['items'][0]['state'],'queued')
        self.assertEqual(r.json()['profile'],PROFILE)

    def test_site_change_hides_prior_batch(self):
        self.submit();self.c.post('/api/settings',json={'sub2api_url':'https://other.invalid'})
        self.assertIsNone(self.c.get('/api/import/batch').json())

    def test_former_budget_failure_resumes_once_without_reimport(self):
        service=self.app.state.service
        batch=self.submit();job=batch['items'][0]['job_id']
        self.v.finish(job,'failed','RECOVERY_CONTINUE_BUDGET','precheck')
        service.import_workflow.resume_budget_blocked()
        current=self.c.get('/api/import/batch').json()
        self.assertEqual(current['items'][0]['state'],'queued')
        self.assertNotEqual(current['items'][0]['job_id'],job)
        self.assertEqual(len(self.v.accounts()),1)
        service.import_workflow.resume_budget_blocked()
        self.assertEqual(len(self.v.jobs()),2)

    def test_stale_precheck_migrates_to_batch_profile_not_global_profile_once(self):
        batch=self.submit();row=batch['items'][0];aid=row['account_id'];job=row['job_id']
        dep=self.v.account(aid)['deployment']
        dep.update(state='failed',step='precheck',code='SELECTED_GROUP_UNAVAILABLE',
                   profile={**PROFILE,'revision':3,'target_group_ids':[18]},history=[])
        self.v.update_account(aid,status='failed',deployment=dep)
        self.v.finish(job,'failed','SELECTED_GROUP_UNAVAILABLE','precheck')
        self.v.set_setting('profile',{**PROFILE,'revision':99,'target_group_ids':[888]})
        workflow=self.app.state.service.import_workflow
        workflow.resume_stale_precheck()
        updated=self.c.get('/api/import/batch').json()['items'][0]
        self.assertEqual(updated['state'],'queued');self.assertNotEqual(updated['job_id'],job)
        self.assertEqual(updated['execution_profile']['target_group_ids'],PROFILE['target_group_ids'])
        self.assertEqual(self.v.account(aid)['deployment']['profile'],PROFILE)
        workflow.resume_stale_precheck();self.assertEqual(len(self.v.jobs()),2)


if __name__=='__main__':unittest.main()
