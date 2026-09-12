from copy import deepcopy
from datetime import datetime, timezone
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx

from sub2easy.gui import create_app, DesktopService, DEFAULT_PROFILE
from sub2easy.intake import parse_batch
from sub2easy.monitor import auth_signal, parse_test_sse, require_fresh_checkpoint
from sub2easy.nvtokens import ConnectorError, parse_response
from sub2easy.preflight import PreflightError
from sub2easy.vault import Vault, VaultError


EMAIL='monitor@example.invalid'
PASSWORD='synthetic-master-password'
LINE=EMAIL+'----SYNTHETIC_PASSWORD----JBSWY3DPEHPK3PXP'


def cloud():
    return {'id':42,'name':'test','platform':'openai','type':'oauth','status':'error','schedulable':True,
            'error_message':'Token revoked (401): synthetic reason','updated_at':'revision-1',
            'credentials':{'email':EMAIL,'chatgpt_account_id':'workspace','chatgpt_user_id':'user',
                           'model_mapping':{'model':'target'},'base_url':'https://example.invalid'},
            'group_ids':[12,13],'proxy_id':7,'priority':50,'concurrency':3,'rate_multiplier':0.8,
            'expires_at':None,'auto_pause_on_expired':True,'rate_limit_reset_at':None,'overload_until':None,
            'temp_unschedulable_until':None,'extra':{'codex_fingerprint_mode':'device','quota_limit':100}}


def auth_result():
    return parse_response(200,json.dumps({'platform':'openai','type':'oauth','credentials':{
        'email':EMAIL,'chatgpt_account_id':'workspace','chatgpt_user_id':'user','client_id':'client',
        'access_token':'SYNTHETIC_ACCESS','refresh_token':'SYNTHETIC_REFRESH','expires_at':'2099-01-01T00:00:00Z',
    }}).encode(),EMAIL)


class SignalAndSSETests(unittest.TestCase):
    def test_upstream_test_errors_have_distinct_fixed_codes(self):
        for status,code in [('401','PROBE_AUTH_401'),('403','PROBE_ACCESS_DENIED'),
                            ('429','PROBE_RATE_LIMITED'),('503','PROBE_UPSTREAM_UNAVAILABLE')]:
            with self.subTest(status=status),self.assertRaisesRegex(VaultError,code) as error:
                parse_test_sse(['data: '+json.dumps({'type':'error','error':f'API returned {status}: SECRET'}),''])
            self.assertNotIn('SECRET',str(error.exception))
        with self.assertRaisesRegex(VaultError,'^PROBE_FAILED$'):
            parse_test_sse(['data: '+json.dumps({'type':'error','error':'proxy port 401 failed'}),''])

    def test_only_current_known_account_401_patterns(self):
        c=cloud();self.assertIsNotNone(auth_signal(c))
        for message in ['HTTP 429: try 401 seconds later','HTTP 403: blocked','proxy port 401 failed','Unauthorized','']:
            c['error_message']=message;self.assertIsNone(auth_signal(c))

    def test_stale_cooldown_and_inactive_and_shadow_do_not_trigger(self):
        c=cloud();c.update(status='active',temp_unschedulable_reason='OAuth 401: expired',temp_unschedulable_until='2020-01-01T00:00:00Z')
        self.assertIsNone(auth_signal(c));c['temp_unschedulable_until']='2099-01-01T00:00:00Z'
        self.assertIsNotNone(auth_signal(c))
        c['status']='inactive';self.assertIsNone(auth_signal(c))
        c=cloud();c['parent_account_id']=1;self.assertIsNone(auth_signal(c))

    def test_sse_success_requires_complete_frame(self):
        self.assertTrue(parse_test_sse([': heartbeat','','data: {"type":"test_start"}','','data: {"type":"test_complete","success":true}','']))
        for lines in [[],['data: [DONE]',''],['data: {"type":"test_complete","success":true}'],
                      ['data: {"type":"test_complete","success":false}',''],
                      ['data: {"type":"error","error":"SECRET"}',''],
                      ['data: {"type":"test_complete","success":true}','','data: {"type":"error"}','']]:
            with self.subTest(lines=lines),self.assertRaises(VaultError) as caught:parse_test_sse(lines)
            self.assertNotIn('SECRET',str(caught.exception))

    def test_multiline_and_size_bound(self):
        self.assertTrue(parse_test_sse(['data: {"type":"test_complete",','data: "success":true}','']))
        with self.assertRaisesRegex(VaultError,'TOO_LARGE'):parse_test_sse(['x'*(1024*1024+1)])

    def test_checkpoint_exact_expiry_and_invalid_timestamp_types(self):
        with patch('sub2easy.monitor.time.time',return_value=100000):
            for created in (100000,13600):require_fresh_checkpoint({'created':created})
            for created in (13599.99,100000.01,True,False,'100000',None,float('inf'),float('-inf'),float('nan')):
                with self.subTest(created=created),self.assertRaisesRegex(VaultError,'NO_SAFE_RECOVERY_CHECKPOINT'):
                    require_fresh_checkpoint({'created':created})


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.v=Vault(self.temp.name);self.v.unlock(PASSWORD,setup=True)
        self.s=DesktopService(self.v);self.m=self.s.monitor;self.remote=cloud();self.writes=[];self.tests=[];self.rev=1
        self.v.set_setting('connection',{'sub2api_url':'https://example.invalid','admin_key':'ADMIN','nvt_cookie':'SYNTHETIC.COOKIE'})
        self.v.import_materials(parse_batch(LINE),DEFAULT_PROFILE);self.id=self.v.accounts()[0]['id']
        self.binding={'cloud_id':42,'instance':'https://example.invalid/api/v1/admin','identity':{
            'account_email':EMAIL,'chatgpt_account_id':'workspace','chatgpt_user_id':'user'}}
        self.v.update_account(self.id,binding=self.binding)
        self.read_patch=patch.object(self.s,'cloud_read',side_effect=self.read);self.read_patch.start()
        self.write_patch=patch.object(self.s,'cloud_write',side_effect=self.write);self.write_patch.start()
        self.probe_patch=patch.object(self.m,'probe',side_effect=self.probe);self.probe_mock=self.probe_patch.start()
        self.connector_patch=patch.object(self.s.connector,'authorize',return_value=auth_result());self.connector=self.connector_patch.start()
        self.client_patch=patch('sub2easy.monitor.Client');self.client=self.client_patch.start()
        self.client.return_value.accounts.side_effect=lambda **kwargs:[deepcopy(self.remote)]
        self.m.set_accounts([self.id],True)
        self.cfg=self.m.save_config({'enabled':True,'model_id':'test-model','interval_seconds':30,'grace_seconds':60,'confirm_auto_reauth':True})
        self.now=time.time()

    def tearDown(self):
        self.client_patch.stop();self.connector_patch.stop();self.probe_patch.stop();self.write_patch.stop();self.read_patch.stop()
        self.v.close();self.temp.cleanup()

    def read(self,path):
        self.assertEqual(path,'/accounts/42');return deepcopy(self.remote)

    def write(self,path,body,idempotency_key=None):
        self.writes.append((path,deepcopy(body)))
        self.rev+=1;self.remote['updated_at']=f'revision-{self.rev}'
        if path.endswith('/schedulable'):self.remote['schedulable']=body['schedulable']
        elif path.endswith('/apply-oauth-credentials'):
            self.remote['credentials'].update(body['credentials']);self.remote['status']='active';self.remote['error_message']=''
            self.remote['temp_unschedulable_until']=None
        else:self.fail('Unexpected cloud write '+path)
        return deepcopy(self.remote)

    def probe(self,cloud_id,model_id):
        self.tests.append((cloud_id,model_id));return True

    def queue_auto(self):
        self.m.poll(force=True,now=self.now)
        self.m.poll(force=True,now=self.now+61)
        self.assertEqual(len(self.v.jobs()),1)

    def test_grace_and_complete_auto_reauth_original_id(self):
        before=deepcopy(self.remote)
        self.m.poll(force=True,now=self.now);self.assertEqual(self.v.jobs(),[])
        self.assertEqual(self.v.account(self.id)['monitor']['state'],'refresh_grace')
        self.m.poll(force=True,now=self.now+61);self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['state'],'succeeded');self.assertEqual(self.v.jobs()[0]['kind'],'auto_reauth')
        self.assertEqual([path for path,body in self.writes],[
            '/accounts/42/schedulable','/accounts/42/apply-oauth-credentials','/accounts/42/schedulable'])
        self.assertEqual(self.writes[0][1],{'schedulable':False});self.assertEqual(self.writes[2][1],{'schedulable':True})
        self.assertEqual(self.tests,[(42,'test-model')]);self.connector.assert_called_once()
        self.assertIn('totp_secret',self.connector.call_args.args[1])
        self.assertNotIn('extra',self.writes[1][1])
        for key in ['extra','group_ids','proxy_id','concurrency','priority','rate_multiplier']:
            self.assertEqual(self.remote[key],before[key])
        self.assertEqual(self.remote['credentials']['model_mapping'],{'model':'target'})
        self.assertEqual(self.v.account(self.id)['status'],'active')
        self.assertIsNone(self.v.account(self.id)['monitor']['owned_pause'])

    def test_native_refresh_clears_error_without_reauth(self):
        self.m.poll(force=True,now=self.now);self.remote.update(status='active',error_message='')
        self.m.poll(force=True,now=self.now+61);self.assertEqual(self.v.jobs(),[]);self.connector.assert_not_called()

    def test_duplicate_polls_same_error_single_job(self):
        self.queue_auto();self.m.poll(force=True,now=self.now+100);self.m.poll(force=True,now=self.now+200)
        self.assertEqual(len(self.v.jobs()),1)

    def test_changing_error_details_do_not_reset_grace_forever(self):
        self.m.poll(force=True,now=self.now)
        self.remote['error_message']='Token revoked (401): a different request id'
        self.m.poll(force=True,now=self.now+61)
        self.assertEqual(len(self.v.jobs()),1)
        self.s.run_one();self.assertEqual(self.v.jobs()[0]['state'],'succeeded')

    def test_a_new_incident_after_recovery_can_queue_again(self):
        self.queue_auto();self.s.run_one()
        self.remote['error_message']='Token revoked (401): synthetic reason';self.remote['status']='error'
        self.m.poll(force=True,now=self.now+100)
        self.m.poll(force=True,now=self.now+161)
        self.assertEqual(len(self.v.jobs()),2)

    def test_cloud_clears_error_after_queue_no_mutation(self):
        self.queue_auto();self.remote.update(status='active',error_message='');self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['state'],'cancelled');self.assertEqual(self.writes,[]);self.connector.assert_not_called()

    def test_disabled_unknown_or_paused_without_401_not_reauthorized(self):
        for updates,expected in [({'status':'inactive'},'account_disabled'),
                                  ({'schedulable':False,'status':'active','error_message':''},'paused_unknown'),
                                  ({'schedulable':None},'state_unknown')]:
            self.remote=cloud();self.remote.update(updates)
            self.m.poll(force=True,now=self.now);self.m.poll(force=True,now=self.now+61)
            self.assertEqual(self.v.jobs(),[]);self.assertEqual(self.v.account(self.id)['monitor']['state'],expected)

    def test_paused_401_is_visible_reauthorized_and_enabled_by_default(self):
        self.remote['schedulable']=False
        self.m.poll(force=True,now=self.now)
        m=self.v.account(self.id)['monitor'];self.assertTrue(m['auth_401']);self.assertFalse(m['cloud_schedulable'])
        self.assertEqual(m['state'],'refresh_grace')
        self.m.poll(force=True,now=self.now+61);self.s.run_one()
        self.connector.assert_called_once()
        self.assertEqual([path for path,body in self.writes],['/accounts/42/apply-oauth-credentials','/accounts/42/schedulable'])
        self.assertEqual(self.tests,[(42,'test-model')]);self.assertTrue(self.remote['schedulable'])
        m=self.v.account(self.id)['monitor'];self.assertFalse(m['auth_401']);self.assertEqual(m['state'],'recovered')
        self.assertEqual(self.v.jobs()[0]['code'],'AUTO_REAUTH_RECOVERED')
        self.m.poll(force=True,now=self.now+200)
        self.assertEqual(self.v.account(self.id)['monitor']['state'],'watching')
        self.assertTrue(self.remote['schedulable'])
        self.assertIsNotNone(self.v.account(self.id)['monitor']['last_recovered'])
        self.assertEqual(len(self.v.jobs()),1)

    def test_opt_in_paused_401_repairs_tests_and_enables(self):
        self.m.save_config({'enabled':True,'resume_paused_401':True,'confirm_auto_reauth':True})
        self.remote['schedulable']=False;self.queue_auto();self.s.run_one()
        self.assertEqual([p for p,b in self.writes],['/accounts/42/apply-oauth-credentials','/accounts/42/schedulable'])
        self.assertTrue(self.remote['schedulable']);self.assertEqual(self.tests,[(42,'test-model')])

    def test_already_repaired_paused_account_continue_only_tests_then_enables(self):
        self.m.save_config({'enabled':True,'resume_paused_401':False,'confirm_auto_reauth':True})
        self.remote['schedulable']=False;self.queue_auto();self.s.run_one()
        self.writes.clear();self.tests.clear();self.connector.reset_mock()
        self.m.queue_continuation(self.id,True);self.s.run_one()
        self.connector.assert_not_called()
        self.assertEqual(self.writes,[('/accounts/42/schedulable',{'schedulable':True})])
        self.assertEqual(self.tests,[(42,'test-model')]);self.assertEqual(self.v.jobs()[0]['state'],'succeeded')

    def test_pre_apply_fetch_failure_continue_reuses_saved_authorization(self):
        self.queue_auto()
        def interrupted(*args):
            self.read_patch.stop()
            self.read_patch=patch.object(self.s,'cloud_read',side_effect=PreflightError('redacted',retryable=True))
            self.read_patch.start()
            return auth_result()
        self.connector.side_effect=interrupted;self.s.run_one()
        a=self.v.account(self.id)
        self.assertIsNotNone(a['authorization']);self.assertEqual(a['monitor']['continuation']['step'],'apply')
        self.assertEqual(a['monitor']['state'],'retry_wait');self.assertIsNone(a.get('write_intent'))
        self.read_patch.stop();self.read_patch=patch.object(self.s,'cloud_read',side_effect=self.read);self.read_patch.start()
        self.connector.side_effect=None;self.connector.reset_mock()
        self.m.queue_continuation(self.id,True);self.s.run_one()
        self.connector.assert_not_called();self.assertTrue(self.remote['schedulable'])
        self.assertEqual(self.v.jobs()[0]['state'],'succeeded')

    def test_pending_retry_polled_into_continuation_without_login(self):
        from sub2easy.monitor import config_fingerprint
        self.remote['schedulable']=False
        cp={'step':'apply','base':config_fingerprint(self.remote),'revision':self.remote['updated_at'],
            'resume':True,'created':time.time()}
        self.v.update_account(self.id,status='failed',authorization=auth_result().authorization)
        self.m.record(self.id,blocked=True,continuation=cp,next_retry=time.time()-1,
                      owned_pause={'state':'confirmed'},last_code='MONITOR_FETCH_FAILED')
        self.m.poll(force=True)
        self.assertEqual(self.v.jobs()[0]['kind'],'recovery_continue')
        self.s.run_one();self.connector.assert_not_called();self.assertTrue(self.remote['schedulable'])

    def test_no_automatic_retry_for_auth_failure_or_unknown_write(self):
        self.m.record(self.id,continuation={'step':'apply','resume':True,'created':time.time()})
        self.assertFalse(self.m._save_retry(self.id,PreflightError('auth',401),'applying',{}))
        self.v.update_account(self.id,write_intent={'state':'unknown'})
        self.assertFalse(self.m._save_retry(self.id,PreflightError('network',retryable=True),'applying',{}))

    def test_legacy_read_failure_has_migratable_checkpoint(self):
        from sub2easy.monitor import config_fingerprint
        self.remote['schedulable']=False
        self.v.update_account(self.id,status='failed',authorization=auth_result().authorization)
        job_id=self.v.queue([self.id],kind='auto_reauth')[0]
        self.v.finish(job_id,'failed','MONITOR_FETCH_FAILED','applying')
        self.m.record(self.id,blocked=True,last_code='MONITOR_FETCH_FAILED',owned_pause={
            'job_id':job_id,'state':'confirmed','baseline':config_fingerprint(self.remote),'revision':self.remote['updated_at']})
        self.m.queue_continuation(self.id,True);self.s.run_one()
        self.connector.assert_not_called();self.assertTrue(self.remote['schedulable'])

    def test_continuation_with_unknown_write_is_rejected(self):
        self.v.update_account(self.id,write_intent={'state':'unknown'})
        with self.assertRaisesRegex(VaultError,'RECONCILIATION'):self.m.queue_continuation(self.id)

    def test_old_apply_marker_requires_original_terminal_job_time(self):
        from sub2easy.monitor import config_fingerprint
        self.remote['schedulable']=False
        self.v.update_account(self.id,status='failed',authorization=auth_result().authorization)
        job_id=self.v.queue([self.id],kind='auto_reauth')[0]
        self.v.finish(job_id,'failed','MONITOR_FETCH_FAILED','applying')
        self.m.record(self.id,blocked=True,last_code='MONITOR_FETCH_FAILED',owned_pause={
            'job_id':job_id,'state':'confirmed','baseline':config_fingerprint(self.remote),
            'revision':self.remote['updated_at']})
        with self.v.db:
            self.v.db.execute('UPDATE jobs SET updated=? WHERE id=?',(time.time()-86401,job_id))
        with self.assertRaisesRegex(VaultError,'NO_SAFE_RECOVERY_CHECKPOINT'):
            self.m.queue_continuation(self.id)
        self.connector.assert_not_called();self.assertEqual(self.writes,[])

    def test_missing_verify_proof_cannot_be_queued_then_fail_later(self):
        self.remote.update(status='active',schedulable=False,error_message='')
        self.m.record(self.id,state='recovered_paused',last_code='AUTO_REAUTH_RECOVERED_PAUSED',
                      last_recovered=time.time())
        with self.assertRaisesRegex(VaultError,'NO_SAFE_RECOVERY_CHECKPOINT'):
            self.m.queue_continuation(self.id)
        self.assertEqual(self.v.jobs(),[])

    def test_missing_or_invalid_checkpoint_time_is_not_replaced_by_now(self):
        from sub2easy.monitor import config_fingerprint
        self.remote.update(status='active',schedulable=False,error_message='')
        self.v.update_account(self.id,write_intent={'state':'confirmed'})
        for created in [None,True,float('nan'),time.time()+500, time.time()-90000]:
            self.m.record(self.id,continuation={'step':'verify','base':config_fingerprint(self.remote),
                                               'created':created,'resume':True})
            with self.subTest(created=created),self.assertRaisesRegex(VaultError,'NO_SAFE_RECOVERY_CHECKPOINT'):
                self.m.queue_continuation(self.id)
        self.assertEqual(self.v.jobs(),[])

    def test_queued_checkpoint_can_expire_before_worker_claim(self):
        self.m.save_config({'enabled':True,'resume_paused_401':False,'confirm_auto_reauth':True})
        self.remote['schedulable']=False;self.queue_auto();self.s.run_one()
        self.m.queue_continuation(self.id);self.writes.clear();self.tests.clear();self.connector.reset_mock()
        with patch('sub2easy.monitor.time.time',return_value=time.time()+90000):self.s.run_one()
        self.assertFalse(self.remote['schedulable']);self.assertEqual(self.writes,[]);self.assertEqual(self.tests,[])
        self.connector.assert_not_called()

    def test_resume_master_switch_prevents_legacy_paused_auto_enable(self):
        self.m.save_config({'enabled':True,'resume_paused_401':True,'resume_after_success':False,
                           'confirm_auto_reauth':True})
        self.remote['schedulable']=False;self.queue_auto();self.s.run_one()
        self.assertFalse(self.remote['schedulable'])
        jobs=len(self.v.jobs());self.m.poll(force=True)
        self.assertEqual(len(self.v.jobs()),jobs)
        with self.assertRaisesRegex(VaultError,'AUTO_RESUME_DISABLED'):
            self.m.queue_continuation(self.id,True,automatic=True)

    def test_resume_migration_checks_prior_config_before_enqueue(self):
        self.m.save_config({'enabled':True,'resume_paused_401':False,'confirm_auto_reauth':True})
        self.remote['schedulable']=False;self.queue_auto();self.s.run_one()
        evidence=self.v.account(self.id)['monitor']['recovery_evidence']
        self.assertTrue(evidence['sse_success']);self.assertEqual(evidence['cloud_id'],42)
        self.remote['extra']['codex_fingerprint_mode']='full'
        self.m.save_config({'enabled':True,'resume_paused_401':True,'confirm_auto_reauth':True})
        self.writes.clear();self.connector.reset_mock();self.m.poll(force=True)
        self.assertEqual(len(self.v.jobs()),1);self.assertEqual(self.writes,[])
        self.assertEqual(self.v.account(self.id)['monitor']['last_code'],'CLOUD_CHANGED_DURING_RECOVERY')

    def test_old_no_baseline_requires_explicit_operator_continuation(self):
        self.m.save_config({'enabled':True,'resume_paused_401':False,'confirm_auto_reauth':True})
        self.remote['schedulable']=False;self.queue_auto();self.s.run_one()
        self.m.record(self.id,recovery_evidence=None)
        self.m.save_config({'enabled':True,'resume_paused_401':True,'confirm_auto_reauth':True})
        with self.assertRaisesRegex(VaultError,'RECOVERY_BASELINE_REVIEW_REQUIRED'):
            self.m.queue_continuation(self.id,True,automatic=True)
        self.m.queue_continuation(self.id,True,automatic=False)
        self.connector.reset_mock();self.s.run_one()
        self.connector.assert_not_called();self.assertTrue(self.remote['schedulable'])

    def test_checkpoint_retries_poll_without_nvt_cookie(self):
        from sub2easy.monitor import config_fingerprint
        self.remote['schedulable']=False
        cp={'step':'apply','base':config_fingerprint(self.remote),'revision':self.remote['updated_at'],
            'resume':True,'created':time.time()}
        self.v.update_account(self.id,status='failed',authorization=auth_result().authorization)
        self.m.record(self.id,blocked=True,continuation=cp,next_retry=time.time()-1,
                      owned_pause={'state':'confirmed'},last_code='MONITOR_FETCH_FAILED')
        setting=self.v.get_setting('connection');setting.pop('nvt_cookie');self.v.set_setting('connection',setting)
        self.m.poll(force=True);self.assertEqual(self.v.jobs()[0]['kind'],'recovery_continue')
        self.s.run_one();self.connector.assert_not_called();self.assertTrue(self.remote['schedulable'])

    def test_continuation_detects_external_edit_no_apply(self):
        self.m.save_config({'enabled':True,'resume_paused_401':False,'confirm_auto_reauth':True})
        self.remote['schedulable']=False;self.queue_auto();self.s.run_one()
        self.m.queue_continuation(self.id)
        self.remote['concurrency']=100
        self.writes.clear();self.s.run_one()
        self.assertFalse(self.remote['schedulable']);self.assertEqual(self.writes,[])
        self.assertEqual(self.v.jobs()[0]['state'],'failed')

    def test_continuation_failing_probe_never_enables(self):
        self.m.save_config({'enabled':True,'resume_paused_401':False,'confirm_auto_reauth':True})
        self.remote['schedulable']=False;self.queue_auto();self.s.run_one()
        self.m.queue_continuation(self.id);self.writes.clear()
        self.probe_mock.side_effect=VaultError('PROBE_FAILED');self.s.run_one()
        self.assertFalse(self.remote['schedulable']);self.assertEqual(self.writes,[])

    def test_continuation_works_without_cookie_but_not_login(self):
        self.m.save_config({'enabled':True,'resume_paused_401':False,'confirm_auto_reauth':True})
        self.remote['schedulable']=False;self.queue_auto();self.s.run_one()
        s=self.v.get_setting('connection');s.pop('nvt_cookie');self.v.set_setting('connection',s)
        self.m.queue_continuation(self.id);self.connector.reset_mock();self.s.run_one()
        self.connector.assert_not_called();self.assertTrue(self.remote['schedulable'])

    def test_human_pauses_after_queue_stops_without_reauthorizing(self):
        self.queue_auto();self.remote['schedulable']=False;self.s.run_one()
        self.connector.assert_not_called();self.assertEqual(self.writes,[])
        self.assertEqual(self.v.jobs()[0]['code'],'CLOUD_CHANGED_DURING_RECOVERY')

    def test_human_enables_previously_paused_job_stops(self):
        self.remote['schedulable']=False;self.queue_auto();self.remote['schedulable']=True;self.s.run_one()
        self.connector.assert_not_called();self.assertEqual(self.writes,[])

    def test_unknown_schedule_still_reports_auth_error_no_write(self):
        del self.remote['schedulable']
        self.m.poll(force=True,now=self.now)
        m=self.v.account(self.id)['monitor'];self.assertTrue(m['auth_401']);self.assertEqual(m['state'],'state_unknown')
        self.assertEqual(self.v.jobs(),[])

    def test_failed_paused_401_repair_stays_paused_and_visible(self):
        self.m.save_config({'enabled':True,'resume_paused_401':False,'confirm_auto_reauth':True})
        self.remote['schedulable']=False;self.queue_auto()
        self.connector.side_effect=ConnectorError('INCORRECT_CODE');self.s.run_one()
        self.m.poll(force=True,now=self.now+500)
        m=self.v.account(self.id)['monitor'];self.assertTrue(m['auth_401']);self.assertTrue(m['blocked'])
        self.assertEqual(self.writes,[]);self.assertFalse(self.remote['schedulable'])

    def test_admin_401_and_incomplete_fetch_no_account_actions(self):
        for error in [PreflightError('redacted',401),PreflightError('incomplete')]:
            self.client.return_value.accounts.side_effect=error
            self.m.poll(force=True,now=self.now)
            self.assertEqual(self.v.jobs(),[]);self.assertEqual(self.writes,[])
            self.assertEqual(self.m.view()['runtime']['state'],'paused')
        self.connector.assert_not_called()

    def test_missing_cloud_binding_and_owner_conflict(self):
        self.client.return_value.accounts.side_effect=lambda **kwargs:[]
        self.m.poll(force=True,now=self.now)
        self.assertEqual(self.v.account(self.id)['monitor']['state'],'remote_missing')
        self.assertEqual(self.v.jobs(),[])

    def test_wrong_cloud_identity_not_modified(self):
        self.remote['credentials']['chatgpt_account_id']='other';self.m.poll(force=True,now=self.now)
        self.assertEqual(self.v.account(self.id)['monitor']['state'],'binding_conflict');self.assertEqual(self.v.jobs(),[])

    def test_hourly_circuit_budget(self):
        self.m.poll(force=True,now=self.now)
        with patch.object(self.v,'auto_jobs_since',return_value=100):self.m.poll(force=True,now=self.now+61)
        self.assertEqual(self.v.jobs(),[]);self.assertEqual(self.v.account(self.id)['monitor']['state'],'rate_budget')

    def test_incorrect_code_blocks_account_and_never_enables(self):
        self.queue_auto();self.connector.side_effect=ConnectorError('INCORRECT_CODE','protocol_login');self.s.run_one()
        self.assertFalse(self.remote['schedulable']);self.assertEqual(len(self.writes),1)
        self.assertTrue(self.v.account(self.id)['monitor']['blocked']);self.assertEqual(self.v.jobs()[0]['code'],'INCORRECT_CODE')
        self.m.poll(force=True,now=self.now+4000);self.assertEqual(len(self.v.jobs()),1)

    def test_cookie_error_pauses_connector(self):
        self.queue_auto();self.connector.side_effect=ConnectorError('CONNECTOR_SESSION_EXPIRED');self.s.run_one()
        self.assertTrue(self.v.get_setting('connection')['connector_paused']);self.assertFalse(self.remote['schedulable'])

    def test_network_unknown_does_not_retry(self):
        self.queue_auto();self.connector.side_effect=ConnectorError('NETWORK_RESULT_UNKNOWN',ambiguous=True);self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['state'],'unknown');self.assertFalse(self.remote['schedulable'])
        self.m.poll(force=True,now=self.now+4000);self.connector.assert_called_once()

    def test_failed_probe_does_not_enable(self):
        self.queue_auto();self.probe_mock.side_effect=VaultError('PROBE_FAILED');self.s.run_one()
        self.assertFalse(self.remote['schedulable']);self.assertEqual(self.v.jobs()[0]['code'],'PROBE_FAILED')
        self.assertEqual(len(self.writes),2)

    def fail_auth_probe(self, cloud_id, model_id):
        self.remote.update(status='error',error_message='Authentication failed (401): synthetic',
                           updated_at='probe-auth-failure')
        raise VaultError('PROBE_AUTH_401')

    def test_confirmed_probe_401_recovers_without_user_click(self):
        self.queue_auto();self.probe_mock.side_effect=self.fail_auth_probe;self.s.run_one()
        m=self.v.account(self.id)['monitor']
        self.assertEqual(m['state'],'reauth_retry_wait');self.assertFalse(m['blocked'])
        self.assertIsNotNone(m['automatic_reauth']);self.assertFalse(self.remote['schedulable'])
        self.probe_mock.side_effect=self.probe
        self.m.poll(force=True,now=m['automatic_reauth']['due']+1)
        self.assertEqual(self.v.jobs()[0]['state'],'queued')
        self.s.run_one()
        self.assertEqual(self.connector.call_count,2)
        self.assertTrue(self.remote['schedulable'])
        self.assertEqual(self.v.jobs()[0]['code'],'AUTO_REAUTH_RECOVERED')

    def test_probe_401_after_second_login_stops_instead_of_looping(self):
        self.queue_auto();self.probe_mock.side_effect=self.fail_auth_probe;self.s.run_one()
        due=self.v.account(self.id)['monitor']['automatic_reauth']['due']
        self.m.poll(force=True,now=due+1);self.s.run_one()
        self.assertEqual(self.connector.call_count,2)
        m=self.v.account(self.id)['monitor']
        self.assertEqual(m['last_code'],'REAUTH_STILL_UNAUTHORIZED');self.assertTrue(m['blocked'])
        self.assertIsNone(m['automatic_reauth']);self.assertFalse(self.remote['schedulable'])
        self.m.poll(force=True,now=due+4000);self.assertEqual(len(self.v.jobs()),2)

    def test_probe_401_retry_survives_cookie_pause(self):
        self.queue_auto();self.probe_mock.side_effect=self.fail_auth_probe;self.s.run_one()
        retry=self.v.account(self.id)['monitor']['automatic_reauth']
        setting=self.v.get_setting('connection');setting['connector_paused']=True;self.v.set_setting('connection',setting)
        self.m.poll(force=True,now=retry['due']+1)
        self.assertEqual(self.v.account(self.id)['monitor']['state'],'waiting_connector')
        self.assertEqual(len(self.v.jobs()),1)
        setting['connector_paused']=False;self.v.set_setting('connection',setting)
        self.probe_mock.side_effect=self.probe
        self.m.poll(force=True,now=retry['due']+100);self.s.run_one()
        self.assertEqual(self.connector.call_count,2);self.assertTrue(self.remote['schedulable'])

    def test_retry_stops_when_human_changes_config(self):
        self.queue_auto();self.probe_mock.side_effect=self.fail_auth_probe;self.s.run_one()
        retry=self.v.account(self.id)['monitor']['automatic_reauth']
        self.remote['proxy_id']=999
        self.m.poll(force=True,now=retry['due']+1)
        self.assertEqual(self.connector.call_count,1)
        self.assertEqual(self.v.account(self.id)['monitor']['last_code'],'CLOUD_CHANGED_DURING_RECOVERY')

    def test_retry_respects_global_task_budget(self):
        self.queue_auto();self.probe_mock.side_effect=self.fail_auth_probe;self.s.run_one()
        retry=self.v.account(self.id)['monitor']['automatic_reauth']
        with patch.object(self.v,'auto_jobs_since',return_value=100):
            self.m.poll(force=True,now=retry['due']+1)
        self.assertEqual(len(self.v.jobs()),1)
        self.assertEqual(self.v.account(self.id)['monitor']['state'],'rate_budget')

    def test_no_auto_login_for_non_401_probe_failure(self):
        for code in ['PROBE_FAILED','PROBE_ACCESS_DENIED','PROBE_RATE_LIMITED','PROBE_RESULT_UNKNOWN']:
            with self.subTest(code=code):
                self.assertFalse(self.m._schedule_probe_reauth(self.id,VaultError(code),'verifying',{'context':{}}))
        self.connector.assert_not_called()

    def test_resume_optional_keeps_paused(self):
        self.m.save_config({'enabled':True,'resume_after_success':False,'confirm_auto_reauth':True});self.queue_auto();self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['state'],'succeeded');self.assertFalse(self.remote['schedulable'])
        self.assertEqual(self.v.account(self.id)['monitor']['state'],'recovered_paused')

    def test_remote_edit_during_authorization_prevents_token_apply(self):
        def changed(*args):
            self.remote['extra']['codex_fingerprint_mode']='full';self.remote['updated_at']='human-edited';return auth_result()
        self.queue_auto();self.connector.side_effect=changed;self.s.run_one()
        self.assertEqual(len(self.writes),1);self.assertEqual(self.v.jobs()[0]['code'],'CLOUD_CHANGED_DURING_RECOVERY')

    def test_remote_token_refresh_race_detected_before_apply(self):
        def changed(*args):
            self.remote['updated_at']='native-refresh';return auth_result()
        self.queue_auto();self.connector.side_effect=changed;self.s.run_one()
        self.assertEqual(len(self.writes),1);self.assertEqual(self.v.jobs()[0]['code'],'CLOUD_CHANGED_DURING_RECOVERY')

    def test_disable_while_authorizing_prevents_later_cloud_write(self):
        def stopped(*args):self.m.save_config({'enabled':False});return auth_result()
        self.queue_auto();self.connector.side_effect=stopped;self.s.run_one()
        self.assertEqual(len(self.writes),1);self.assertFalse(self.remote['schedulable'])
        self.assertEqual(self.v.jobs()[0]['code'],'MONITOR_JOB_STALE')

    def test_disable_cancels_auto_queue_not_manual_queue(self):
        self.queue_auto();self.m.save_config({'enabled':False})
        self.assertEqual(self.v.jobs()[0]['state'],'cancelled');self.s.run_one();self.connector.assert_not_called()

    def test_site_change_invalidates_monitor(self):
        s=self.v.get_setting('connection');s['sub2api_url']='https://other.invalid';self.v.set_setting('connection',s)
        self.m.poll(force=True,now=self.now);self.assertEqual(self.m.view()['runtime']['last_code'],'MONITOR_CONNECTION_CHANGED')
        self.assertEqual(self.v.jobs(),[])

    def test_lock_no_network_poll(self):
        self.v.lock();self.assertFalse(self.m.poll(force=True));self.client.return_value.accounts.assert_not_called()

    def test_no_updated_at_fails_before_pause(self):
        self.queue_auto();del self.remote['updated_at'];self.s.run_one()
        self.assertEqual(self.writes,[]);self.assertEqual(self.v.jobs()[0]['code'],'CLOUD_REVISION_REQUIRED')

    def test_manual_acknowledgement_requires_real_cloud_recovery(self):
        self.queue_auto();self.connector.side_effect=ConnectorError('INCORRECT_CODE');self.s.run_one()
        with self.assertRaises(VaultError):self.m.acknowledge(self.id)
        self.remote.update(status='active',schedulable=True,error_message='')
        self.m.acknowledge(self.id)
        self.assertFalse(self.v.account(self.id)['monitor']['blocked'])
        self.assertIsNone(self.v.account(self.id)['monitor']['owned_pause'])

    def test_probe_transport_requires_sse_and_keeps_error_separate(self):
        real_probe=type(self.m).probe
        for status,headers,body,expected in [
            (200,{'content-type':'text/event-stream'},b'data: {"type":"test_complete","success":true}\n\n',None),
            (200,{'content-type':'text/event-stream'},b'data: {"type":"error","error":"SECRET"}\n\n','PROBE_FAILED'),
            (200,{'content-type':'application/json'},b'{"code":0}','PROBE_INVALID_SSE'),
            (401,{},b'not-account-401','ADMIN_AUTH_FAILED'),
        ]:
            with self.subTest(expected=expected):
                requests=[]
                def handler(request):
                    requests.append(request);return httpx.Response(status,headers=headers,content=body)
                client=httpx.Client(transport=httpx.MockTransport(handler))
                with patch('sub2easy.monitor.httpx.Client',return_value=client):
                    if expected:
                        with self.assertRaisesRegex(VaultError,expected):real_probe(self.m,42,'test-model')
                    else:self.assertTrue(real_probe(self.m,42,'test-model'))
                self.assertEqual(str(requests[0].url),'https://example.invalid/api/v1/admin/accounts/42/test')
                self.assertEqual(json.loads(requests[0].content)['model_id'],'test-model')

    def test_crash_running_auto_job_not_replayed(self):
        self.queue_auto();job=self.v.claim();self.m.record(self.id,owned_pause={'job_id':job['id'],'state':'pending'})
        self.v.close();self.v=Vault(self.temp.name);self.v.unlock(PASSWORD)
        self.s.vault=self.v;self.s.monitor=type(self.m)(self.s);self.m=self.s.monitor
        self.assertEqual(self.v.jobs()[0]['state'],'unknown');self.assertIsNone(self.v.claim())
        self.assertIsNotNone(self.v.account(self.id)['monitor']['owned_pause'])
        self.m.poll(force=True,now=self.now+4000);self.assertEqual(len(self.v.jobs()),1)

    def install_apply_checkpoint(self, **changes):
        from sub2easy.monitor import config_fingerprint
        self.remote['schedulable']=False
        checkpoint={'step':'apply','base':config_fingerprint(self.remote),'revision':self.remote['updated_at'],
                    'resume':True,'created':time.time(),**changes}
        self.v.update_account(self.id,status='failed',authorization=auth_result().authorization)
        self.m.record(self.id,blocked=True,continuation=checkpoint,next_retry=time.time()-1,
                      owned_pause={'state':'confirmed'},last_code='MONITOR_FETCH_FAILED')
        return checkpoint

    def test_expired_auto_checkpoint_reports_current_reason_and_stops(self):
        self.install_apply_checkpoint(created=time.time()-86401)
        self.m.poll(force=True)
        m=self.v.account(self.id)['monitor']
        self.assertEqual(m['last_code'],'NO_SAFE_RECOVERY_CHECKPOINT')
        self.assertTrue(m['blocked']);self.assertIsNone(m['next_retry'])
        self.assertEqual(self.v.jobs(),[]);self.connector.assert_not_called();self.assertEqual(self.writes,[])

    def test_checkpoint_retry_read_failure_preserves_bounded_automatic_resume(self):
        cp=self.install_apply_checkpoint()
        self.read_patch.stop()
        self.read_patch=patch.object(self.s,'cloud_read',side_effect=PreflightError('SECRET',retryable=True))
        self.read_patch.start();self.m.poll(force=True)
        m=self.v.account(self.id)['monitor']
        self.assertEqual(m['state'],'retry_wait');self.assertEqual(m['last_code'],'MONITOR_FETCH_FAILED')
        self.assertEqual(m['continuation']['created'],cp['created']);self.assertIsNotNone(m['next_retry'])
        self.m.poll(force=True,now=m['next_retry']-1)
        self.assertEqual(self.v.account(self.id)['monitor']['state'],'retry_wait')
        self.read_patch.stop();self.read_patch=patch.object(self.s,'cloud_read',side_effect=self.read);self.read_patch.start()
        self.m.poll(force=True,now=m['next_retry']+1)
        self.assertEqual(self.m.view()['runtime']['queued'],1)
        self.s.run_one();self.connector.assert_not_called();self.assertTrue(self.remote['schedulable'])

    def test_checkpoint_admin_auth_error_never_retries_even_if_flagged_retryable(self):
        self.install_apply_checkpoint()
        for status in (401,403):
            with self.subTest(status=status):
                self.assertFalse(self.m._save_retry(self.id,PreflightError('SECRET',status,retryable=True),'applying',{}))
        self.read_patch.stop()
        self.read_patch=patch.object(self.s,'cloud_read',side_effect=PreflightError('SECRET',401,retryable=True))
        self.read_patch.start();self.m.poll(force=True)
        m=self.v.account(self.id)['monitor']
        self.assertEqual(m['last_code'],'ADMIN_AUTH_FAILED');self.assertIsNone(m['next_retry'])
        self.assertEqual(self.v.jobs(),[])

    def test_continuation_budget_waits_and_resumes_without_relogin(self):
        self.install_apply_checkpoint()
        with patch.object(self.v,'queue',side_effect=VaultError('RECOVERY_CONTINUE_BUDGET')):
            self.m.poll(force=True)
        m=self.v.account(self.id)['monitor']
        self.assertEqual(m['state'],'rate_budget');self.assertIsNotNone(m['next_retry'])
        self.m.poll(force=True,now=m['next_retry']-1)
        self.assertEqual(self.v.account(self.id)['monitor']['state'],'rate_budget')
        self.m.poll(force=True,now=m['next_retry']+1);self.s.run_one()
        self.connector.assert_not_called();self.assertTrue(self.remote['schedulable'])

    def test_auto_reauth_budget_is_temporary_not_permanent_block(self):
        self.m.poll(force=True,now=self.now)
        with patch.object(self.v,'queue',side_effect=VaultError('REAUTH_BUDGET_2_PER_30_MIN')):
            self.m.poll(force=True,now=self.now+61)
        m=self.v.account(self.id)['monitor']
        self.assertEqual(m['state'],'rate_budget');self.assertFalse(m['blocked'])
        self.m.poll(force=True,now=self.now+1900);self.s.run_one()
        self.connector.assert_called_once();self.assertTrue(self.remote['schedulable'])

    def test_verify_checkpoint_cannot_invent_missing_baseline(self):
        self.remote.update(status='active',schedulable=False)
        self.v.update_account(self.id,write_intent={'state':'confirmed'})
        self.m.record(self.id,continuation={'step':'verify','created':time.time(),'resume':True})
        for automatic in (True,False):
            with self.subTest(automatic=automatic),self.assertRaisesRegex(VaultError,'NO_SAFE_RECOVERY_CHECKPOINT'):
                self.m.queue_continuation(self.id,True,automatic=automatic)
        self.assertEqual(self.v.jobs(),[])

    def test_continuation_boolean_options_are_not_truthiness_based(self):
        self.install_apply_checkpoint()
        for enable,automatic in [('false',False),(True,'false'),(1,False)]:
            with self.subTest(enable=enable,automatic=automatic),self.assertRaisesRegex(VaultError,'INVALID_MONITOR_SELECTION'):
                self.m.queue_continuation(self.id,enable,automatic)
        self.assertEqual(self.v.jobs(),[])

    def test_stopped_monitor_does_not_poll_or_queue_continuation(self):
        self.install_apply_checkpoint();self.s.stop.set()
        self.assertFalse(self.m.poll(force=True));self.client.return_value.accounts.assert_not_called()
        with self.assertRaisesRegex(VaultError,'MONITOR_JOB_STALE'):self.m.queue_continuation(self.id)
        self.assertEqual(self.v.jobs(),[])

    def test_stop_during_snapshot_does_not_queue(self):
        self.m.poll(force=True,now=self.now)
        def stopped(**kwargs):self.s.stop.set();return [deepcopy(self.remote)]
        self.client.return_value.accounts.side_effect=stopped
        self.m.poll(force=True,now=self.now+61)
        self.assertEqual(self.v.jobs(),[]);self.assertEqual(self.m.view()['runtime']['last_code'],'MONITOR_JOB_STALE')

    def test_stop_during_continuation_read_does_not_enqueue(self):
        self.install_apply_checkpoint()
        def stopped(path):self.s.stop.set();return self.read(path)
        with patch.object(self.s,'cloud_read',side_effect=stopped):
            with self.assertRaisesRegex(VaultError,'MONITOR_JOB_STALE'):self.m.queue_continuation(self.id)
        self.assertEqual(self.v.jobs(),[]);self.assertEqual(self.writes,[])

    def test_stop_during_preprobe_read_prevents_probe(self):
        self.queue_auto()
        def stopped(path):
            if self.v.account(self.id)['monitor'].get('state')=='verifying':self.s.stop.set()
            return self.read(path)
        with patch.object(self.s,'cloud_read',side_effect=stopped):self.s.run_one()
        self.assertEqual(self.tests,[]);self.assertEqual(len(self.writes),2)
        self.assertFalse(self.remote['schedulable']);self.assertNotIn('last_recovered',self.v.account(self.id)['monitor'])

    def test_disable_after_final_resume_read_prevents_enable(self):
        self.queue_auto();reads_after_probe=0
        def disabled(path):
            nonlocal reads_after_probe
            if self.tests:
                reads_after_probe+=1
                if reads_after_probe==2:self.m.save_config({'enabled':False})
            return self.read(path)
        with patch.object(self.s,'cloud_read',side_effect=disabled):self.s.run_one()
        self.assertFalse(self.remote['schedulable']);self.assertEqual(len(self.writes),2)
        self.assertEqual(self.v.jobs()[0]['code'],'MONITOR_JOB_STALE')
        self.assertEqual(self.v.jobs()[0]['state'],'failed')  # No resume POST was sent.

    def test_stop_during_probe_cannot_publish_paused_success(self):
        self.m.save_config({'enabled':True,'resume_after_success':False,'confirm_auto_reauth':True})
        self.queue_auto()
        def stopped(*args):self.s.stop.set();return True
        self.probe_mock.side_effect=stopped;self.s.run_one()
        self.assertFalse(self.remote['schedulable']);self.assertEqual(self.v.jobs()[0]['code'],'MONITOR_JOB_STALE')
        self.assertIsNone(self.v.account(self.id)['monitor'].get('recovery_evidence'))

    def test_cookie_pause_after_authorization_does_not_block_saved_credentials(self):
        self.queue_auto()
        def paused(*args):
            setting=self.v.get_setting('connection');setting['connector_paused']=True
            self.v.set_setting('connection',setting);return auth_result()
        self.connector.side_effect=paused;self.s.run_one()
        self.connector.assert_called_once();self.assertTrue(self.remote['schedulable'])
        self.assertEqual(self.v.jobs()[0]['code'],'AUTO_REAUTH_RECOVERED')
        self.assertTrue(self.v.get_setting('connection')['connector_paused'])

    def test_queued_401_with_changing_request_id_is_one_incident(self):
        self.queue_auto();self.remote['error_message']='OAuth 401: new request identifier'
        self.s.run_one();self.connector.assert_called_once()
        self.assertEqual(len(self.v.jobs()),1);self.assertEqual(self.v.jobs()[0]['code'],'AUTO_REAUTH_RECOVERED')

    def test_new_incident_preapply_failure_does_not_reuse_previous_write_proof(self):
        self.queue_auto();self.s.run_one()
        self.remote.update(status='error',error_message='OAuth 401: next incident')
        self.m.poll(force=True,now=self.now+100);self.m.poll(force=True,now=self.now+161)
        def interrupted(*args):
            self.read_patch.stop()
            self.read_patch=patch.object(self.s,'cloud_read',side_effect=PreflightError('SECRET',retryable=True))
            self.read_patch.start();return auth_result()
        self.connector.side_effect=interrupted;self.s.run_one()
        a=self.v.account(self.id)
        self.assertIsNone(a['write_intent']);self.assertIsNotNone(a['authorization'])
        self.assertEqual(a['monitor']['state'],'retry_wait')
        self.read_patch.stop();self.read_patch=patch.object(self.s,'cloud_read',side_effect=self.read);self.read_patch.start()
        self.connector.reset_mock();self.m.queue_continuation(self.id);self.s.run_one()
        self.connector.assert_not_called();self.assertTrue(self.remote['schedulable'])

    def test_queued_unknown_result_remains_unknown_and_cannot_write(self):
        self.install_apply_checkpoint();self.m.queue_continuation(self.id)
        self.v.update_account(self.id,status='unknown');self.s.run_one()
        self.assertEqual(self.v.account(self.id)['status'],'unknown')
        self.assertEqual(self.v.jobs()[0]['code'],'RESULT_REVIEW_REQUIRED_BEFORE_RETRY')
        self.assertEqual(self.writes,[]);self.connector.assert_not_called()

    def test_probe_retry_checkpoint_expires_during_cookie_pause(self):
        self.queue_auto();self.probe_mock.side_effect=self.fail_auth_probe;self.s.run_one()
        retry=self.v.account(self.id)['monitor']['automatic_reauth'];retry['created']=time.time()-86401
        self.m.record(self.id,automatic_reauth=retry)
        self.m.poll(force=True,now=retry['due']+1)
        m=self.v.account(self.id)['monitor']
        self.assertEqual(m['last_code'],'NO_SAFE_RECOVERY_CHECKPOINT');self.assertIsNone(m['automatic_reauth'])
        self.connector.assert_called_once();self.assertEqual(len(self.v.jobs()),1)

    def test_queued_probe_retry_still_checks_original_config(self):
        self.queue_auto();self.probe_mock.side_effect=self.fail_auth_probe;self.s.run_one()
        retry=self.v.account(self.id)['monitor']['automatic_reauth']
        self.m.poll(force=True,now=retry['due']+1)
        self.remote['group_ids']=[999];self.writes.clear();self.s.run_one()
        self.connector.assert_called_once();self.assertEqual(self.writes,[])
        self.assertEqual(self.v.jobs()[0]['code'],'CLOUD_CHANGED_DURING_RECOVERY')

    def test_queued_probe_retry_expires_before_worker_claim(self):
        self.queue_auto();self.probe_mock.side_effect=self.fail_auth_probe;self.s.run_one()
        retry=self.v.account(self.id)['monitor']['automatic_reauth']
        self.m.poll(force=True,now=retry['due']+1);self.writes.clear()
        with patch('sub2easy.monitor.time.time',return_value=time.time()+86401):self.s.run_one()
        self.connector.assert_called_once();self.assertEqual(self.writes,[])
        self.assertEqual(self.v.jobs()[0]['code'],'NO_SAFE_RECOVERY_CHECKPOINT')

    def test_probe_must_return_literal_success_before_success_marker(self):
        self.queue_auto();self.probe_mock.side_effect=None;self.probe_mock.return_value=None;self.s.run_one()
        self.assertFalse(self.remote['schedulable']);self.assertEqual(self.v.jobs()[0]['code'],'PROBE_FAILED')
        self.assertIsNone(self.v.account(self.id)['monitor'].get('recovery_evidence'))

    def test_post_enable_cooldown_cannot_publish_success(self):
        self.queue_auto()
        def cooled(path,body,idempotency_key=None):
            result=self.write(path,body,idempotency_key)
            if body.get('schedulable') is True:
                self.remote['temp_unschedulable_until']='2099-01-01T00:00:00Z'
                self.remote['temp_unschedulable_reason']='OAuth 401: still failing'
            return result
        with patch.object(self.s,'cloud_write',side_effect=cooled):self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['code'],'RESUME_RESULT_UNKNOWN')
        self.assertEqual(self.v.account(self.id)['status'],'unknown')
        self.assertIsNone(self.v.account(self.id)['monitor'].get('recovery_evidence'))

    def test_probe_media_type_match_is_exact_case_insensitive(self):
        real_probe=type(self.m).probe
        for content_type,success in [('TEXT/EVENT-STREAM; charset=utf-8',True),('text/event-stream-evil',False)]:
            with self.subTest(content_type=content_type):
                transport=httpx.MockTransport(lambda request:httpx.Response(200,headers={'content-type':content_type},
                    content=b'data: {"type":"test_complete","success":true}\n\n'))
                client=httpx.Client(transport=transport)
                with patch('sub2easy.monitor.httpx.Client',return_value=client):
                    if success:self.assertTrue(real_probe(self.m,42,'test-model'))
                    else:
                        with self.assertRaisesRegex(VaultError,'PROBE_INVALID_SSE'):real_probe(self.m,42,'test-model')

    def test_queued_login_shows_cookie_pause_then_resumes_same_job(self):
        self.queue_auto()
        setting=self.v.get_setting('connection');setting['connector_paused']=True
        self.v.set_setting('connection',setting);self.m.poll(force=True)
        self.assertEqual(self.v.account(self.id)['monitor']['state'],'waiting_connector')
        self.assertEqual(self.m.view()['runtime']['last_code'],'CONFIGURE_OR_RENEW_COOKIE')
        self.assertFalse(self.s.run_one());self.connector.assert_not_called()
        setting['connector_paused']=False;self.v.set_setting('connection',setting)
        self.m.poll(force=True);self.s.run_one()
        self.assertEqual(len(self.v.jobs()),1);self.connector.assert_called_once()
        self.assertEqual(self.v.jobs()[0]['code'],'AUTO_REAUTH_RECOVERED')

    def test_reenable_preserves_recovered_paused_migration(self):
        self.m.save_config({'enabled':True,'resume_paused_401':False,'confirm_auto_reauth':True})
        self.remote['schedulable']=False;self.queue_auto();self.s.run_one()
        self.m.set_accounts([self.id],False);self.m.set_accounts([self.id],True)
        self.assertEqual(self.v.account(self.id)['monitor']['state'],'recovered_paused')
        self.m.save_config({'enabled':True,'resume_paused_401':True,'confirm_auto_reauth':True})
        self.connector.reset_mock();self.m.poll(force=True);self.s.run_one()
        self.connector.assert_not_called();self.assertTrue(self.remote['schedulable'])

    def test_manual_acknowledgement_clears_obsolete_checkpoint_not_only_block(self):
        self.install_apply_checkpoint();self.remote.update(status='active',schedulable=True,error_message='')
        self.m.acknowledge(self.id);self.m.poll(force=True)
        m=self.v.account(self.id)['monitor']
        self.assertFalse(m['blocked']);self.assertEqual(m['state'],'watching')
        self.assertIsNone(m['continuation']);self.assertIsNone(m['next_retry']);self.assertEqual(self.v.jobs(),[])

    def test_legacy_checkpoint_get_failure_keeps_original_time_for_retry(self):
        from sub2easy.monitor import config_fingerprint
        self.remote['schedulable']=False
        self.v.update_account(self.id,status='failed',authorization=auth_result().authorization)
        job_id=self.v.queue([self.id],kind='auto_reauth')[0]
        self.v.finish(job_id,'failed','MONITOR_FETCH_FAILED','applying')
        created=self.v.job_checkpoint_time(job_id,self.id)
        self.m.record(self.id,blocked=True,last_code='MONITOR_FETCH_FAILED',owned_pause={
            'job_id':job_id,'state':'confirmed','baseline':config_fingerprint(self.remote),'revision':self.remote['updated_at']})
        with patch.object(self.s,'cloud_read',side_effect=PreflightError('SECRET',retryable=True)):
            self.m.poll(force=True)
        m=self.v.account(self.id)['monitor']
        self.assertEqual(m['continuation']['created'],created);self.assertEqual(m['state'],'retry_wait')
        self.m.poll(force=True,now=m['next_retry']+1);self.s.run_one()
        self.connector.assert_not_called();self.assertTrue(self.remote['schedulable'])

    def test_checkpoint_read_retries_are_bounded_not_perpetual(self):
        self.install_apply_checkpoint()
        with patch.object(self.s,'cloud_read',side_effect=PreflightError('SECRET',retryable=True)):
            for attempt in range(1,5):
                due=self.v.account(self.id)['monitor'].get('next_retry')
                self.m.poll(force=True,now=due+1)
                m=self.v.account(self.id)['monitor']
                self.assertEqual(m['continuation_attempts'],min(attempt,3))
        self.assertIsNone(m['next_retry']);self.assertEqual(m['state'],'needs_attention')
        self.assertEqual(self.v.jobs(),[]);self.connector.assert_not_called()

    def test_stop_during_preapply_read_prevents_credential_write(self):
        self.queue_auto()
        def stopped(path):
            if self.v.account(self.id)['monitor'].get('state')=='applying':self.s.stop.set()
            return self.read(path)
        with patch.object(self.s,'cloud_read',side_effect=stopped):self.s.run_one()
        self.assertEqual(self.writes,[('/accounts/42/schedulable',{'schedulable':False})])
        self.assertIsNotNone(self.v.account(self.id)['authorization'])
        self.assertEqual(self.v.jobs()[0]['code'],'MONITOR_JOB_STALE')

    def test_no_checkpoint_retry_after_ambiguous_cloud_write(self):
        self.queue_auto()
        def ambiguous(path,body,idempotency_key=None):
            result=self.write(path,body,idempotency_key)
            if path.endswith('/apply-oauth-credentials'):raise VaultError('CLOUD_WRITE_RESULT_UNKNOWN')
            return result
        with patch.object(self.s,'cloud_write',side_effect=ambiguous):self.s.run_one()
        a=self.v.account(self.id)
        self.assertEqual(a['status'],'write_unknown');self.assertEqual(a['write_intent']['state'],'unknown')
        self.assertIsNone(a['monitor'].get('next_retry'));self.assertEqual(self.tests,[])
        self.m.poll(force=True,now=self.now+4000);self.assertEqual(len(self.v.jobs()),1)
        with self.assertRaisesRegex(VaultError,'PREVIOUS_WRITE_NEEDS_RECONCILIATION'):
            self.m.queue_continuation(self.id)

    def test_definite_expired_cookie_resumes_after_renewal_without_manual_ack(self):
        self.queue_auto();self.connector.side_effect=ConnectorError('CONNECTOR_SESSION_EXPIRED');self.s.run_one()
        m=self.v.account(self.id)['monitor']
        self.assertEqual(m['state'],'waiting_connector');self.assertFalse(m['blocked'])
        self.assertIsNotNone(m['automatic_reauth']);self.assertFalse(self.remote['schedulable'])
        self.m.poll(force=True);self.assertFalse(self.s.run_one());self.connector.assert_called_once()
        setting=self.v.get_setting('connection');setting.update(connector_paused=False,nvt_cookie='RENEWED.COOKIE')
        self.v.set_setting('connection',setting);self.connector.side_effect=None
        self.m.poll(force=True);self.s.run_one()
        self.assertEqual(self.connector.call_count,2);self.assertTrue(self.remote['schedulable'])
        self.assertEqual(self.connector.call_args.args[0],'RENEWED.COOKIE')
        self.assertEqual(self.v.jobs()[0]['code'],'AUTO_REAUTH_RECOVERED')
        self.assertEqual(sum(path.endswith('/apply-oauth-credentials') for path,body in self.writes),1)

    def test_expired_cookie_marked_ambiguous_is_not_replayed(self):
        self.queue_auto();self.connector.side_effect=ConnectorError('CONNECTOR_SESSION_EXPIRED',ambiguous=True)
        self.s.run_one()
        self.assertEqual(self.v.account(self.id)['status'],'unknown')
        self.assertIsNone(self.v.account(self.id)['monitor'].get('automatic_reauth'))
        setting=self.v.get_setting('connection');setting['connector_paused']=False;self.v.set_setting('connection',setting)
        self.m.poll(force=True,now=self.now+4000);self.connector.assert_called_once()
        self.assertEqual(len(self.v.jobs()),1)

    def test_cookie_retry_does_not_bless_remote_revision_change(self):
        self.queue_auto();self.connector.side_effect=ConnectorError('CONNECTOR_SESSION_EXPIRED');self.s.run_one()
        setting=self.v.get_setting('connection');setting['connector_paused']=False;self.v.set_setting('connection',setting)
        self.remote['updated_at']='external-token-refresh';self.m.poll(force=True)
        m=self.v.account(self.id)['monitor']
        self.assertEqual(m['last_code'],'CLOUD_CHANGED_DURING_RECOVERY');self.assertTrue(m['blocked'])
        self.connector.assert_called_once();self.assertEqual(len(self.v.jobs()),1)

    def test_second_cookie_failure_keeps_original_expiry_and_budget(self):
        self.queue_auto();self.connector.side_effect=ConnectorError('CONNECTOR_SESSION_EXPIRED');self.s.run_one()
        created=self.v.account(self.id)['monitor']['automatic_reauth']['created']
        setting=self.v.get_setting('connection');setting['connector_paused']=False;self.v.set_setting('connection',setting)
        self.m.poll(force=True);self.s.run_one()
        retry=self.v.account(self.id)['monitor']['automatic_reauth']
        self.assertEqual(retry['created'],created);self.assertEqual(self.connector.call_count,2)
        setting['connector_paused']=False;self.v.set_setting('connection',setting);self.m.poll(force=True)
        self.assertEqual(self.v.account(self.id)['monitor']['state'],'rate_budget')
        self.assertEqual(len(self.v.jobs()),2)

    def test_current_verify_evidence_cannot_move_to_another_binding(self):
        self.m.save_config({'enabled':True,'resume_after_success':False,'confirm_auto_reauth':True})
        self.queue_auto();self.s.run_one()
        binding=deepcopy(self.binding);binding['cloud_id']=43
        self.v.update_account(self.id,binding=binding)
        self.writes.clear();self.connector.reset_mock()
        with self.assertRaisesRegex(VaultError,'CLOUD_CHANGED_DURING_RECOVERY'):
            self.m.queue_continuation(self.id)
        self.assertEqual(self.writes,[]);self.connector.assert_not_called();self.assertEqual(len(self.v.jobs()),1)


class MonitorAPITests(unittest.TestCase):
    def test_no_opt_in_by_default_and_explicit_confirmation_and_disable(self):
        with tempfile.TemporaryDirectory() as directory:
            app=create_app(directory,token='TOKEN',start_worker=False)
            with TestClient(app,base_url='http://127.0.0.1:8765',headers={'x-local-token':'TOKEN'}) as c:
                c.post('/api/unlock',json={'password':PASSWORD,'setup':True})
                state=c.get('/api/state').json();self.assertFalse(state['monitor']['config']['enabled'])
                c.post('/api/settings',json={'sub2api_url':'https://example.invalid','admin_key':'ADMIN','nvt_cookie':'SYNTHETIC.COOKIE'})
                r=c.post('/api/monitor/config',json={'enabled':True,'model_id':'test-model'})
                self.assertEqual(r.json()['code'],'CONFIRM_AUTO_REAUTH')
                r=c.post('/api/monitor/config',json={'enabled':True,'model_id':'test-model','confirm_auto_reauth':True})
                self.assertEqual(r.status_code,200,r.text)
                app.state.service.operation.acquire()
                try:
                    r=c.post('/api/monitor/config',json={'enabled':False})
                    self.assertEqual(r.status_code,200,r.text)
                finally:app.state.service.operation.release()
                self.assertFalse(c.get('/api/state').json()['monitor']['config']['enabled'])


if __name__=='__main__':unittest.main()
