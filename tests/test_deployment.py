from copy import deepcopy
from dataclasses import asdict
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx

from sub2easy.gui import create_app, DEFAULT_PROFILE
from sub2easy.intake import parse_batch
from sub2easy.nvtokens import ConnectorError, NVTConnector, parse_response
from sub2easy.preflight import PreflightError
from sub2easy.vault import Vault, VaultError


EMAIL='deploy@example.invalid'
PROFILE={**DEFAULT_PROFILE,'instance_id':'https://example.invalid/api/v1/admin',
         'fingerprint_mode':'device','proxy_id':7,'target_group_ids':[11,12]}


def authorization(email=EMAIL):
    return parse_response(200,json.dumps({'platform':'openai','type':'oauth','credentials':{
        'email':email,'chatgpt_account_id':'workspace','chatgpt_user_id':'user','client_id':'client',
        'expires_at':'2099-01-01T00:00:00Z','access_token':'SYNTHETIC_ACCESS','refresh_token':'SYNTHETIC_REFRESH',
    }}).encode(),email)


def account(i=42, email=EMAIL):
    return {'id':i,'name':'existing','platform':'openai','type':'oauth','status':'active','schedulable':True,
            'credentials':{'email':email,'chatgpt_account_id':'workspace','chatgpt_user_id':'user',
                           'model_mapping':{'model':'target'},'base_url':'https://upstream.invalid'},
            'group_ids':[4,5],'proxy_id':2,'concurrency':8,'priority':1,'rate_multiplier':0.7,
            'extra':{'codex_fingerprint_mode':'session','quota_limit':10},'updated_at':'v1',
            'expires_at':None,'auto_pause_on_expired':True,'rate_limit_reset_at':None,
            'overload_until':None,'temp_unschedulable_until':None}


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=create_app(self.temp.name,token='TEST-TOKEN',start_worker=False)
        self.client=TestClient(self.app,base_url='http://127.0.0.1:8765',headers={'x-local-token':'TEST-TOKEN'})
        self.client.__enter__();self.v=self.app.state.vault;self.s=self.app.state.service
        self.client.post('/api/unlock',json={'setup':True,'password':'synthetic-master-password'})
        self.v.set_setting('connection',{'sub2api_url':'https://example.invalid','admin_key':'SYNTHETIC_ADMIN',
                                        'nvt_cookie':'SYNTHETIC.COOKIE'})
        self.id=self.add()
        self.remote={};self.calls=[];self.rev=0
        self.patches=[]
        def patched(obj,name,**kwargs):
            p=patch.object(obj,name,**kwargs);self.patches.append(p);return p.start()
        self.read=patched(self.s,'cloud_read',side_effect=self.cloud_read)
        self.write=patched(self.s,'cloud_write',side_effect=self.cloud_write)
        self.put=patched(self.s,'cloud_put',side_effect=self.cloud_put)
        self.probe=patched(self.s.monitor,'probe',side_effect=self.probe_ok)
        self.auth=patched(self.s.connector,'authorize',return_value=authorization())
        self.options=patched(self.s,'options',return_value={'groups':[{'id':i} for i in [9001,11,12]],'proxies':[{'id':7}]})
        p=patch('sub2easy.deployment.Client');self.patches.append(p);self.list_client=p.start()
        self.list_client.return_value.accounts.side_effect=lambda **kw:deepcopy(list(self.remote.values()))

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.client.__exit__(None,None,None);self.temp.cleanup()

    def add(self,email=EMAIL):
        self.v.import_materials(parse_batch(email+'----FAKE_PASSWORD----JBSWY3DPEHPK3PXP'),DEFAULT_PROFILE)
        return next(a['id'] for a in self.v.accounts() if self.v.account(a['id'])['login']['account']==email)

    def cached(self):
        r=authorization();self.v.update_account(self.id,status='authorized',authorization=r.authorization,raw_result=r.raw)

    def bind_existing(self):
        self.remote[42]=account()
        self.v.update_account(self.id,binding={'cloud_id':42,'instance':PROFILE['instance_id'],
            'identity':authorization().authorization['identity']})

    def cloud_read(self,path):return deepcopy(self.remote[int(path.split('/')[2])])

    def cloud_write(self,path,body,key=None):
        self.calls.append(('POST',path,deepcopy(body)));self.rev+=1
        if path=='/accounts':
            i=42;self.remote[i]={**account(i),**deepcopy(body),'id':i,'schedulable':True}
        else:
            i=int(path.split('/')[2])
            if path.endswith('/schedulable'):self.remote[i]['schedulable']=body['schedulable']
            elif path.endswith('/apply-oauth-credentials'):
                self.remote[i]['credentials']=deepcopy(body['credentials']);self.remote[i]['status']='active'
            else:raise AssertionError(path)
        self.remote[i]['updated_at']=f'v{self.rev+1}'
        return deepcopy(self.remote[i])

    def cloud_put(self,path,body):
        self.calls.append(('PUT',path,deepcopy(body)));i=int(path.split('/')[2]);self.rev+=1
        self.remote[i].update(deepcopy(body));self.remote[i]['updated_at']=f'v{self.rev+1}'
        return deepcopy(self.remote[i])

    def probe_ok(self,cloud_id,model):
        self.calls.append(('PROBE',cloud_id,model))
        self.assertFalse(self.remote[cloud_id]['schedulable']);return True

    def queue(self,ids=None,**updates):
        body={'account_ids':ids or [self.id],'model_id':'test-model','profile':PROFILE,
              'confirm_deploy':True,'staging_verified':True,**updates}
        r=self.client.post('/api/deployments',json=body)
        self.assertEqual(r.status_code,200,r.text);return r.json()

    def test_cached_new_account_full_pipeline(self):
        self.cached();r=self.queue();self.assertEqual(r['items'][0]['state'],'queued')
        self.s.run_one();self.auth.assert_not_called()
        self.assertEqual([(x[0],x[1]) for x in self.calls],[('POST','/accounts'),('POST','/accounts/42/schedulable'),
            ('PROBE',42),('PUT','/accounts/42'),('PROBE',42),('POST','/accounts/42/schedulable')])
        created=self.calls[0][2];self.assertEqual(created['group_ids'],[9001]);self.assertEqual(created['proxy_id'],7)
        self.assertEqual(created['extra'],{'codex_fingerprint_mode':'device'})
        self.assertTrue(self.remote[42]['schedulable']);self.assertEqual(self.remote[42]['group_ids'],[11,12])
        self.assertEqual(self.v.account(self.id)['deployment']['state'],'complete')
        self.assertEqual(self.v.jobs()[0]['code'],'DEPLOYED_AND_ENABLED')
        self.assertEqual(self.v.account(self.id)['binding']['cloud_id'],42)

    def test_missing_authorization_gets_one_nvt_request_then_deploys(self):
        self.queue();self.s.run_one();self.auth.assert_called_once()
        self.assertEqual(self.v.jobs()[0]['state'],'succeeded')

    def test_success_automatically_enrolls_and_next_401_queues_repair(self):
        self.cached();self.queue();self.s.run_one()
        m=self.v.account(self.id)['monitor']
        self.assertTrue(m['enabled']);self.assertEqual(m['state'],'watching')
        self.assertEqual(m['model_id'],'test-model');self.assertTrue(self.s.monitor.config()['enabled'])
        self.remote[42].update(status='error',error_message='OAuth 401: synthetic invalid token')
        with patch('sub2easy.monitor.Client') as client:
            client.return_value.accounts.return_value=deepcopy(list(self.remote.values()))
            self.s.monitor.poll(force=True,now=1000)
            self.s.monitor.poll(force=True,now=1200)
        auto=[j for j in self.v.jobs() if j['kind']=='auto_reauth']
        self.assertEqual(len(auto),1);self.assertEqual(auto[0]['state'],'queued')
        self.assertEqual(self.v.account(self.id)['monitor']['state'],'queued')

    def test_failed_probe_never_enrolls(self):
        self.cached();self.probe.side_effect=VaultError('PROBE_FAILED');self.queue();self.s.run_one()
        self.assertFalse(self.v.account(self.id).get('monitor',{}).get('enabled',False))

    def test_deployment_monitor_opt_out_keeps_disabled(self):
        self.cached();self.queue(auto_monitor=False);self.s.run_one()
        self.assertFalse(self.s.monitor.config()['enabled'])
        self.assertFalse(self.v.account(self.id).get('monitor',{}).get('enabled',False))

    def test_user_stops_monitor_during_deploy_is_not_overridden(self):
        self.cached();self.queue();self.s.monitor.save_config({'enabled':False});self.s.run_one()
        a=self.v.account(self.id)
        self.assertEqual(a['deployment']['state'],'complete')
        self.assertFalse(self.s.monitor.config()['enabled'])
        self.assertFalse(a.get('monitor',{}).get('enabled',False))
        self.assertEqual(a['monitor']['enrollment_code'],'MONITOR_STOPPED_DURING_DEPLOY')

    def test_enrollment_keeps_existing_monitor_generation_and_limits(self):
        self.s.monitor.save_config({'enabled':True,'model_id':'global-model','max_per_hour':2,
                                   'resume_after_success':False,'confirm_auto_reauth':True})
        cfg=self.s.monitor.config();self.cached();self.queue();self.s.run_one()
        self.assertEqual(self.s.monitor.config(),cfg)
        self.assertEqual(self.v.account(self.id)['monitor']['model_id'],'test-model')

    def test_token_only_without_cookie_still_monitored_but_not_relogged(self):
        self.cached();self.v.update_account(self.id,login={'account':EMAIL})
        s=self.v.get_setting('connection');s.pop('nvt_cookie');self.v.set_setting('connection',s)
        self.queue();self.s.run_one();self.assertTrue(self.v.account(self.id)['monitor']['enabled'])
        self.remote[42].update(status='error',error_message='OAuth 401: synthetic expired')
        with patch('sub2easy.monitor.Client') as client:
            client.return_value.accounts.return_value=deepcopy(list(self.remote.values()))
            self.s.monitor.poll(force=True)
        self.assertEqual(self.v.account(self.id)['monitor']['state'],'login_material_missing')
        self.auth.assert_not_called()

    def test_legacy_success_missing_monitor_is_adopted_without_cloud_write(self):
        self.cached();self.queue();self.s.run_one()
        a=self.v.account(self.id);dep=a['deployment'];dep.pop('auto_monitor');dep.pop('monitor_generation')
        self.v.update_account(self.id,monitor={},deployment=dep)
        self.calls.clear();self.s.monitor.adopt_legacy_deployments(self.s.monitor.config())
        self.assertEqual(self.calls,[])
        self.assertTrue(self.v.account(self.id)['monitor']['enabled'])
        self.assertTrue(self.v.account(self.id)['deployment']['auto_monitor'])

    def test_legacy_adoption_preserves_manual_disable_and_cloud_config(self):
        self.cached();self.queue();self.s.run_one()
        a=self.v.account(self.id);dep=a['deployment'];dep.pop('auto_monitor');dep.pop('monitor_generation')
        self.v.update_account(self.id,monitor={'enabled':False},deployment=dep)
        self.s.monitor.adopt_legacy_deployments(self.s.monitor.config())
        self.assertFalse(self.v.account(self.id)['monitor']['enabled'])
        self.v.update_account(self.id,monitor={})
        self.remote[42]['group_ids']=[999]
        self.s.monitor.adopt_legacy_deployments(self.s.monitor.config())
        self.assertNotIn('enabled',self.v.account(self.id)['monitor'])

    def test_expired_cached_auth_gets_one_new_authorization(self):
        self.cached();a=self.v.account(self.id)['authorization'];a['credentials']['expires_at']='1'
        self.v.update_account(self.id,authorization=a)
        self.queue();self.s.run_one();self.auth.assert_called_once();self.assertTrue(self.remote[42]['schedulable'])

    def test_valid_credentials_work_without_cookie(self):
        self.cached();s=self.v.get_setting('connection');s.pop('nvt_cookie');self.v.set_setting('connection',s)
        self.queue();self.s.run_one();self.auth.assert_not_called();self.assertTrue(self.remote[42]['schedulable'])

    def test_expired_token_only_account_does_not_call_nvt(self):
        self.cached()
        a=self.v.account(self.id);a['authorization']['credentials']['expires_at']='1'
        self.v.update_account(self.id,login={'account':EMAIL},authorization=a['authorization'])
        self.queue();self.s.run_one();self.auth.assert_not_called()
        self.assertEqual(self.v.jobs()[0]['code'],'LOGIN_MATERIAL_MISSING')
        self.assertFalse(self.remote)

    def test_bound_update_preserves_original_config(self):
        self.bind_existing();before=deepcopy(self.remote[42]);self.cached()
        self.queue();self.s.run_one();self.auth.assert_not_called()
        self.assertFalse(any(x[1]=='/accounts' or x[0]=='PUT' for x in self.calls))
        for k in ['group_ids','proxy_id','concurrency','priority','rate_multiplier','extra']:
            self.assertEqual(self.remote[42][k],before[k])
        self.assertEqual(self.remote[42]['credentials']['model_mapping'],{'model':'target'})
        self.assertTrue(self.remote[42]['schedulable'])

    def test_existing_unbound_identity_stops_before_create_and_login(self):
        self.remote[55]=account(55);self.queue();self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['code'],'DEPLOY_BIND_EXISTING_FIRST')
        self.assertEqual(self.calls,[]);self.auth.assert_not_called()

    def test_shared_workspace_other_user_is_not_duplicate(self):
        self.remote[55]=account(55,email='other@example.invalid');self.cached()
        self.queue();self.s.run_one();self.assertEqual(self.v.jobs()[0]['state'],'succeeded')

    def test_failed_probe_no_promote_no_enable_and_retry_no_duplicate(self):
        self.cached();self.probe.side_effect=VaultError('PROBE_FAILED');self.queue();self.s.run_one()
        self.assertFalse(self.remote[42]['schedulable']);self.assertEqual(self.remote[42]['group_ids'],[9001])
        self.assertEqual(self.v.account(self.id)['deployment']['step'],'verify')
        self.probe.side_effect=self.probe_ok;self.queue();self.s.run_one()
        self.assertEqual(sum(x[1]=='/accounts' for x in self.calls),1)
        self.assertTrue(self.remote[42]['schedulable']);self.auth.assert_not_called()

    def test_silently_ignored_create_template_does_not_get_promoted(self):
        self.cached()
        def ignore_proxy(path,body,key=None):
            out=self.cloud_write(path,body,key)
            if path=='/accounts':self.remote[42]['proxy_id']=999
            return out
        self.write.side_effect=ignore_proxy;self.queue();self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['code'],'DEPLOY_TEMPLATE_NOT_APPLIED')
        self.probe.assert_not_called();self.put.assert_not_called()

    def test_missing_runtime_field_does_not_enable(self):
        self.cached()
        def omit(cloud_id,model):self.remote[cloud_id].pop('rate_limit_reset_at');return True
        self.probe.side_effect=omit;self.queue();self.s.run_one()
        self.assertFalse(self.remote[42]['schedulable'])
        self.assertEqual(self.v.jobs()[0]['code'],'DEPLOY_RUNTIME_UNKNOWN')

    def test_read_failure_after_create_retries_from_saved_id(self):
        self.cached();count=0
        def fail_once(path):
            nonlocal count
            count+=1
            if count==1:raise PreflightError('network')
            return self.cloud_read(path)
        self.read.side_effect=fail_once;self.queue();self.s.run_one()
        self.assertEqual(self.v.account(self.id)['binding']['cloud_id'],42)
        self.queue();self.s.run_one()
        self.assertEqual(sum(x[1]=='/accounts' for x in self.calls),1)
        self.assertEqual(self.v.jobs()[0]['state'],'succeeded')

    def test_cannot_rebind_or_advance_to_new_server_on_retry(self):
        self.cached();self.probe.side_effect=VaultError('PROBE_FAILED');self.queue();self.s.run_one()
        setting=self.v.get_setting('connection');setting['cloud_revision']='changed';self.v.set_setting('connection',setting)
        self.assertEqual(self.queue()['items'][0]['code'],'DEPLOY_CONNECTION_CHANGED')

    def test_mutation_is_journaled_before_remote_request(self):
        self.cached()
        def checked(path,body,key=None):
            dep=self.v.account(self.id)['deployment']
            self.assertIsNotNone(dep['mutation']);self.assertIn('started',dep['mutation'])
            return self.cloud_write(path,body,key)
        self.write.side_effect=checked;self.queue();self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['state'],'succeeded')

    def test_second_probe_failure_stays_in_target_groups_but_disabled(self):
        self.cached();self.probe.side_effect=[True,VaultError('PROBE_FAILED')];self.queue();self.s.run_one()
        self.assertEqual(self.remote[42]['group_ids'],[11,12]);self.assertFalse(self.remote[42]['schedulable'])
        self.probe.side_effect=self.probe_ok;self.queue();self.s.run_one()
        self.assertEqual(sum(x[0]=='PUT' for x in self.calls),1);self.assertTrue(self.remote[42]['schedulable'])

    def test_create_response_lost_never_creates_again(self):
        self.cached();self.write.side_effect=VaultError('CLOUD_WRITE_RESULT_UNKNOWN');self.queue();self.s.run_one()
        self.assertEqual(self.v.account(self.id)['deployment']['state'],'unknown')
        r=self.queue();self.assertEqual(r['items'][0]['code'],'DEPLOY_RESULT_NEEDS_REVIEW')
        self.write.assert_called_once()

    def test_new_cloud_id_saved_before_pause_failure(self):
        self.cached()
        def fail_pause(path,body,key=None):
            if path.endswith('/schedulable'):raise VaultError('CLOUD_WRITE_RESULT_UNKNOWN')
            return self.cloud_write(path,body,key)
        self.write.side_effect=fail_pause;self.queue();self.s.run_one()
        self.assertEqual(self.v.account(self.id)['binding']['cloud_id'],42)
        self.assertEqual(self.v.account(self.id)['deployment']['state'],'unknown')
        self.assertEqual(self.queue()['items'][0]['code'],'DEPLOY_RESULT_NEEDS_REVIEW')

    def test_nvt_wrong_workspace_stays_review_no_write(self):
        self.bind_existing();bad=authorization();bad=type(bad)(bad.raw,None,'REAUTH_WORKSPACE_CHANGED')
        self.auth.return_value=bad;self.queue();self.s.run_one()
        self.assertFalse(self.remote[42]['schedulable'])
        self.assertEqual(len(self.calls),1);self.assertEqual(self.v.jobs()[0]['state'],'review')

    def test_nvt_network_result_unknown_not_retried(self):
        self.auth.side_effect=ConnectorError('NETWORK_RESULT_UNKNOWN',ambiguous=True);self.queue();self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['state'],'unknown');self.assertFalse(self.remote)
        self.assertEqual(self.queue()['items'][0]['state'],'failed');self.auth.assert_called_once()

    def test_site_switch_after_queue_does_not_send_anything(self):
        self.queue();s=self.v.get_setting('connection');s['sub2api_url']='https://other.invalid';self.v.set_setting('connection',s)
        self.s.run_one();self.assertEqual(self.calls,[]);self.auth.assert_not_called()
        self.assertEqual(self.v.jobs()[0]['code'],'DEPLOY_CONNECTION_CHANGED')

    def test_old_inactive_account_not_reenabled(self):
        self.bind_existing();self.remote[42]['status']='inactive';self.queue();self.s.run_one()
        self.assertEqual(self.calls,[]);self.auth.assert_not_called()

    def test_config_change_during_nvt_prevents_apply(self):
        self.bind_existing()
        def mutate(*args):self.remote[42]['concurrency']=99;return authorization()
        self.auth.side_effect=mutate;self.queue();self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['code'],'DEPLOY_CONFIG_CHANGED')
        self.assertEqual(len(self.calls),1)

    def test_missing_model_or_confirmation_rejected_before_queue(self):
        for update in [{'model_id':''},{'confirm_deploy':False}]:
            r=self.client.post('/api/deployments',json={'account_ids':[self.id],'model_id':'test-model',
                'profile':PROFILE,'confirm_deploy':True,'staging_verified':True,**update})
            self.assertEqual(r.status_code,400)
        self.assertEqual(self.v.jobs(),[])

    def test_staging_confirmation_required_only_for_new_accounts(self):
        r=self.queue(staging_verified=False);self.assertEqual(r['items'][0]['state'],'failed')
        self.bind_existing();r=self.queue(staging_verified=False);self.assertEqual(r['items'][0]['state'],'queued')

    def test_batch_one_bad_local_id_does_not_prevent_valid_job(self):
        r=self.queue(ids=['missing',self.id]);self.assertEqual([i['state'] for i in r['items']],['failed','queued'])

    def test_completed_account_skipped_and_no_tokens_in_state(self):
        self.cached();self.queue();self.s.run_one();r=self.queue()
        self.assertEqual(r['items'][0]['state'],'already_complete')
        output=self.client.get('/api/state').text
        for secret in ['SYNTHETIC_ACCESS','SYNTHETIC_REFRESH','SYNTHETIC.COOKIE','FAKE_PASSWORD']:
            self.assertNotIn(secret,output)

    def test_monitor_and_manual_jobs_do_not_overlap_incomplete_deployment(self):
        self.cached();self.probe.side_effect=VaultError('PROBE_FAILED');self.queue();self.s.run_one()
        with self.assertRaisesRegex(VaultError,'DEPLOYMENT_INCOMPLETE'):self.v.queue([self.id])

    def test_confirmed_pause_read_failure_retries_without_replaying_pause(self):
        self.bind_existing();self.cached();failed=False
        def fail_readback(path):
            nonlocal failed
            if not self.remote[42]['schedulable'] and not failed:
                failed=True
                raise PreflightError('synthetic read failure')
            return self.cloud_read(path)
        self.read.side_effect=fail_readback;self.queue();self.s.run_one()
        dep=self.v.account(self.id)['deployment']
        self.assertEqual(dep['state'],'failed');self.assertIsNone(dep['mutation'])
        self.assertEqual(dep['step'],'pause_confirm')
        self.assertEqual(self.queue()['items'][0]['state'],'queued');self.s.run_one()
        pauses=[c for c in self.calls if c[1]=='/accounts/42/schedulable' and c[2]=={'schedulable':False}]
        self.assertEqual(len(pauses),1);self.auth.assert_not_called()
        self.assertEqual(self.v.jobs()[0]['state'],'succeeded')

    def defer_pause(self, stale_reads):
        pending=False;reads=0
        def write(path,body,key=None):
            nonlocal pending
            result=self.cloud_write(path,body,key)
            if path.endswith('/schedulable') and body['schedulable'] is False:
                self.remote[42]['schedulable']=True;pending=True
            return result
        def read(path):
            nonlocal pending,reads
            if pending:
                reads+=1
                if reads>stale_reads:
                    self.remote[42]['schedulable']=False;pending=False
            return self.cloud_read(path)
        self.write.side_effect=write;self.read.side_effect=read

    def test_existing_async_pause_is_read_back_before_authorization(self):
        self.bind_existing();self.defer_pause(1)
        def authorize(*args):
            self.assertFalse(self.remote[42]['schedulable'])
            return authorization()
        self.auth.side_effect=authorize;self.queue();self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['state'],'succeeded',self.v.jobs()[0])
        self.auth.assert_called_once()

    def test_new_async_pause_is_read_back_before_probe(self):
        self.cached();self.defer_pause(1);self.queue();self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['state'],'succeeded',self.v.jobs()[0])
        self.assertEqual(self.probe.call_count,2)
        self.assertEqual(sum(c[1]=='/accounts' for c in self.calls),1)

    def test_unconfirmed_pause_is_bounded_and_retry_is_read_only_until_paused(self):
        self.bind_existing();self.cached();self.defer_pause(100)
        self.queue();self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['code'],'DEPLOY_NOT_READY')
        self.assertLess(self.read.call_count,10);self.assertEqual(len(self.calls),1)
        self.auth.assert_not_called();self.probe.assert_not_called()
        self.assertEqual(self.queue()['items'][0]['state'],'queued')
        self.read.side_effect=self.cloud_read;self.remote[42]['schedulable']=False
        self.s.run_one();self.assertEqual(self.v.jobs()[0]['state'],'succeeded')

    def test_definitive_connector_failure_can_be_explicitly_retried(self):
        self.auth.side_effect=ConnectorError('CONNECTOR_SESSION_EXPIRED')
        self.queue();self.s.run_one();self.assertEqual(self.auth.call_count,1)
        self.assertFalse(self.remote)
        settings=self.v.get_setting('connection');settings['connector_paused']=False
        self.v.set_setting('connection',settings);self.auth.side_effect=None
        self.assertEqual(self.queue()['items'][0]['state'],'queued');self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['state'],'succeeded',self.v.jobs()[0])
        self.assertEqual(self.auth.call_count,2)

    def test_expired_credentials_at_create_checkpoint_return_to_authorization(self):
        self.cached();original=self.s.deployments._check_duplicates;checks=0
        def fail_second(a,dep):
            nonlocal checks
            checks+=1
            if checks==2:raise PreflightError('synthetic read failure')
            return original(a,dep)
        with patch.object(self.s.deployments,'_check_duplicates',side_effect=fail_second):
            self.queue();self.s.run_one()
        self.assertEqual(self.v.account(self.id)['deployment']['step'],'create')
        auth=self.v.account(self.id)['authorization'];auth['credentials']['expires_at']='1'
        self.v.update_account(self.id,authorization=auth)
        self.queue();self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['state'],'succeeded',self.v.jobs()[0])
        self.auth.assert_called_once();self.assertEqual(sum(c[1]=='/accounts' for c in self.calls),1)

    def test_apply_read_failure_reuses_credentials_and_preserves_all_old_config(self):
        self.bind_existing();self.remote[42]['extra']['codex_fingerprint_seed']='original-seed'
        self.remote[42]['credentials']['custom_headers']={'X-Test':'original'}
        self.remote[42]['credentials']['opaque_option']={'nested':[1,2]}
        self.remote[42].update(notes='original notes',load_factor=0.5,expires_at=4102444800)
        before=deepcopy(self.remote[42]);failed=False
        def fail_apply(path):
            nonlocal failed
            if self.v.account(self.id)['deployment']['step']=='apply' and not failed:
                failed=True;raise PreflightError('synthetic read failure')
            return self.cloud_read(path)
        self.read.side_effect=fail_apply;self.queue();self.s.run_one()
        self.auth.assert_called_once();self.queue();self.s.run_one();self.auth.assert_called_once()
        self.assertEqual(self.v.jobs()[0]['state'],'succeeded',self.v.jobs()[0])
        for key,value in before.items():
            if key not in {'credentials','updated_at'}:self.assertEqual(self.remote[42][key],value,key)
        for key,value in before['credentials'].items():
            self.assertEqual(self.remote[42]['credentials'][key],value,key)
        self.put.assert_not_called()

    def test_empty_cloud_revision_stops_before_old_account_pause(self):
        self.bind_existing();self.cached();self.remote[42]['updated_at']=''
        self.queue();self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['code'],'CLOUD_REVISION_REQUIRED')
        self.assertEqual(self.calls,[]);self.auth.assert_not_called()

    def test_create_binding_and_checkpoint_are_saved_together(self):
        self.cached();update=self.v.update_account
        def checked(aid,status=None,**updates):
            if updates.get('binding'):
                dep=updates.get('deployment',{})
                self.assertEqual(dep.get('cloud_id'),updates['binding']['cloud_id'])
                self.assertEqual(dep.get('binding'),updates['binding'])
                self.assertEqual(dep.get('step'),'pause_new');self.assertIsNone(dep.get('mutation'))
            return update(aid,status=status,**updates)
        with patch.object(self.v,'update_account',side_effect=checked):
            self.queue();self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['state'],'succeeded',self.v.jobs()[0])

    def test_stop_during_verify_read_prevents_side_effecting_probe(self):
        self.cached()
        def stop_at_verify(path):
            result=self.cloud_read(path)
            if self.v.account(self.id)['deployment']['step']=='verify':self.s.stop.set()
            return result
        self.read.side_effect=stop_at_verify;self.queue();self.s.run_one()
        self.probe.assert_not_called();self.put.assert_not_called()
        self.assertEqual(self.v.jobs()[0]['code'],'DEPLOY_STOPPED')
        self.assertFalse(self.remote[42]['schedulable'])

    def test_stop_during_catalog_read_prevents_duplicate_scan_and_login(self):
        def stop_options():
            self.s.stop.set()
            return {'groups':[{'id':i} for i in [9001,11,12]],'proxies':[{'id':7}]}
        self.options.side_effect=stop_options;self.queue();self.s.run_one()
        self.list_client.assert_not_called();self.auth.assert_not_called()
        self.assertEqual(self.calls,[]);self.assertEqual(self.v.jobs()[0]['code'],'DEPLOY_STOPPED')

    def test_stop_during_mutation_journal_prevents_unsent_create(self):
        self.cached();save=self.s.deployments._save
        def stop_after_save(aid,dep,**updates):
            save(aid,dep,**updates)
            if (updates.get('mutation') or {}).get('stage')=='create':self.s.stop.set()
        with patch.object(self.s.deployments,'_save',side_effect=stop_after_save):
            self.queue();self.s.run_one()
        self.write.assert_not_called();self.assertFalse(self.remote)
        dep=self.v.account(self.id)['deployment']
        self.assertEqual(dep['state'],'failed');self.assertIsNone(dep['mutation'])

    def test_stop_during_authorization_journal_prevents_unsent_login(self):
        save=self.s.deployments._save
        def stop_after_save(aid,dep,**updates):
            save(aid,dep,**updates)
            if (updates.get('mutation') or {}).get('stage')=='authorize':self.s.stop.set()
        with patch.object(self.s.deployments,'_save',side_effect=stop_after_save):
            self.queue();self.s.run_one()
        self.auth.assert_not_called();self.assertEqual(self.calls,[])
        self.assertIsNone(self.v.account(self.id)['deployment']['mutation'])

    def test_monitor_blocked_after_queue_prevents_all_remote_calls(self):
        self.cached();self.queue();self.v.update_account(self.id,monitor={'blocked':True})
        self.s.run_one();self.options.assert_not_called();self.list_client.assert_not_called()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.v.jobs()[0]['code'],'MONITOR_RECOVERY_NEEDS_REVIEW')

    def test_new_account_expiry_readback_accepts_equivalent_iso_timestamp(self):
        self.cached()
        def format_expiry(path,body,key=None):
            result=self.cloud_write(path,body,key)
            if path=='/accounts':self.remote[42]['expires_at']='2099-01-01T00:00:00Z'
            return result
        self.write.side_effect=format_expiry
        self.queue(profile={**PROFILE,'account_expires_at':4070908800});self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['state'],'succeeded',self.v.jobs()[0])

    def test_expired_credentials_at_apply_checkpoint_renew_without_repausing(self):
        self.bind_existing();failed=False
        def fail_apply(path):
            nonlocal failed
            if self.v.account(self.id)['deployment']['step']=='apply' and not failed:
                failed=True;raise PreflightError('synthetic read failure')
            return self.cloud_read(path)
        self.read.side_effect=fail_apply;self.queue();self.s.run_one();self.auth.assert_called_once()
        auth=self.v.account(self.id)['authorization'];auth['credentials']['expires_at']='1'
        self.v.update_account(self.id,authorization=auth)
        self.queue();self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['state'],'succeeded',self.v.jobs()[0])
        self.assertEqual(self.auth.call_count,2)
        self.assertEqual(sum(c[1]=='/accounts/42/schedulable' and c[2]=={'schedulable':False}
                             for c in self.calls),1)
        self.assertFalse(any(c[1]=='/accounts' for c in self.calls))

    def test_expired_apply_retry_checks_remote_pause_before_new_login(self):
        self.bind_existing();self.cached();failed=False
        def fail_apply(path):
            nonlocal failed
            if self.v.account(self.id)['deployment']['step']=='apply' and not failed:
                failed=True;raise PreflightError('synthetic read failure')
            return self.cloud_read(path)
        self.read.side_effect=fail_apply;self.queue();self.s.run_one()
        auth=self.v.account(self.id)['authorization'];auth['credentials']['expires_at']='1'
        self.v.update_account(self.id,authorization=auth)
        self.remote[42].update(schedulable=True,updated_at='human-enable')
        self.queue();self.s.run_one()
        self.auth.assert_not_called();self.assertEqual(len(self.calls),1)
        self.assertEqual(self.v.jobs()[0]['code'],'DEPLOY_CONFIG_CHANGED')

    def test_stop_during_authorization_keeps_result_for_retry_without_login(self):
        def stop_after_login(*args):
            self.s.stop.set();return authorization()
        self.auth.side_effect=stop_after_login;self.queue();self.s.run_one()
        self.assertEqual(self.calls,[]);self.auth.assert_called_once()
        self.assertIsNotNone(self.v.account(self.id)['authorization'])
        self.assertEqual(self.v.jobs()[0]['code'],'DEPLOY_STOPPED')
        self.s.stop.clear();self.queue();self.s.run_one()
        self.auth.assert_called_once();self.assertEqual(self.v.jobs()[0]['state'],'succeeded')

    def test_stop_during_pause_readback_keeps_confirmed_checkpoint(self):
        self.bind_existing();self.cached()
        def stop_at_readback(path):
            result=self.cloud_read(path)
            if not result['schedulable']:self.s.stop.set()
            return result
        self.read.side_effect=stop_at_readback;self.queue();self.s.run_one()
        dep=self.v.account(self.id)['deployment']
        self.assertEqual(dep['state'],'failed');self.assertIsNone(dep['mutation'])
        self.assertEqual(dep['step'],'pause_confirm');self.assertEqual(len(self.calls),1)
        self.auth.assert_not_called();self.probe.assert_not_called()
        self.s.stop.clear();self.read.side_effect=self.cloud_read;self.queue();self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['state'],'succeeded');self.auth.assert_not_called()

    def test_unknown_pause_is_not_cleared_by_stop(self):
        self.bind_existing();self.cached()
        def lost_pause(path,body,key=None):
            self.cloud_write(path,body,key);self.s.stop.set()
            raise VaultError('CLOUD_WRITE_RESULT_UNKNOWN')
        self.write.side_effect=lost_pause;self.queue();self.s.run_one()
        self.assertEqual(self.v.account(self.id)['deployment']['state'],'unknown')
        self.assertIsNotNone(self.v.account(self.id)['deployment']['mutation'])
        self.s.stop.clear()
        self.assertEqual(self.queue()['items'][0]['code'],'DEPLOY_RESULT_NEEDS_REVIEW')
        self.write.assert_called_once();self.auth.assert_not_called();self.probe.assert_not_called()

    def test_duplicate_appearing_during_authorization_prevents_create(self):
        def duplicate(*args):
            self.remote[55]=account(55);return authorization()
        self.auth.side_effect=duplicate;self.queue();self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['code'],'DEPLOY_BIND_EXISTING_FIRST')
        self.write.assert_not_called();self.auth.assert_called_once()
        self.queue();self.s.run_one();self.auth.assert_called_once();self.write.assert_not_called()

    def test_create_response_with_invalid_id_is_never_replayed(self):
        self.cached();self.write.return_value={'id':True};self.write.side_effect=None
        self.queue();self.s.run_one()
        self.assertEqual(self.v.account(self.id)['deployment']['state'],'unknown')
        self.assertEqual(self.queue()['items'][0]['code'],'DEPLOY_RESULT_NEEDS_REVIEW')
        self.assertIsNone(self.v.account(self.id)['binding']);self.write.assert_called_once()

    def test_config_change_during_promotion_catalog_read_stops_before_put(self):
        options=self.options.return_value
        def change_config():
            if self.remote:
                self.remote[42]['concurrency']=99;self.remote[42]['updated_at']='external'
            return options
        self.options.side_effect=change_config;self.cached();self.queue();self.s.run_one()
        self.put.assert_not_called();self.assertEqual(self.v.jobs()[0]['code'],'DEPLOY_CONFIG_CHANGED')
        self.assertFalse(self.remote[42]['schedulable']);self.assertEqual(self.remote[42]['group_ids'],[9001])

    def test_retry_retains_original_profile_snapshot(self):
        self.cached();self.probe.side_effect=VaultError('PROBE_FAILED');self.queue();self.s.run_one()
        self.probe.side_effect=self.probe_ok
        self.queue(profile={**PROFILE,'target_group_ids':[999],'proxy_id':999});self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['state'],'succeeded')
        self.assertEqual(self.remote[42]['group_ids'],[11,12]);self.assertEqual(self.remote[42]['proxy_id'],7)

    def test_new_precheck_retry_adopts_resubmitted_profile_not_stale_groups(self):
        old={**PROFILE,'revision':3,'target_group_ids':[18],'proxy_id':179}
        self.queue(profile=old);self.s.run_one()
        self.auth.assert_not_called();self.write.assert_not_called()
        self.assertEqual(self.v.jobs()[0]['code'],'SELECTED_GROUP_UNAVAILABLE')
        self.assertEqual(self.v.account(self.id)['deployment']['precheck_error']['missing_target_group_ids'],[18])
        self.queue(profile={**PROFILE,'revision':9});self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['state'],'succeeded')
        self.assertEqual(self.remote[42]['group_ids'],[11,12]);self.assertEqual(self.remote[42]['proxy_id'],7)
        self.assertEqual(self.v.account(self.id)['deployment']['previous_profile']['revision'],3)

    def test_precheck_template_change_still_requires_isolation_confirmation(self):
        self.queue(profile={**PROFILE,'target_group_ids':[18]});self.s.run_one()
        result=self.queue(staging_verified=False)
        self.assertEqual(result['items'][0]['code'],'STAGING_ISOLATION_CONFIRMATION_REQUIRED')
        self.assertEqual(self.v.account(self.id)['deployment']['profile']['target_group_ids'],[18])

    def test_precheck_does_not_replace_checkpoint_if_authorization_was_sent(self):
        self.queue(profile={**PROFILE,'target_group_ids':[18]});self.s.run_one()
        dep=self.v.account(self.id)['deployment'];dep['auth_attempts']=1
        self.v.update_account(self.id,deployment=dep)
        self.queue();self.s.run_one()
        self.assertEqual(self.v.account(self.id)['deployment']['profile']['target_group_ids'],[18])
        self.assertEqual(self.v.jobs()[0]['code'],'SELECTED_GROUP_UNAVAILABLE')

    def test_cached_wrong_workspace_is_not_applied_or_silently_reauthorized(self):
        self.bind_existing();self.cached()
        auth=self.v.account(self.id)['authorization'];auth['credentials']['chatgpt_account_id']='other-workspace'
        self.v.update_account(self.id,authorization=auth);self.queue();self.s.run_one()
        self.assertEqual(self.v.jobs()[0]['state'],'review');self.auth.assert_not_called()
        self.assertEqual(len(self.calls),1);self.assertFalse(self.remote[42]['schedulable'])
        self.assertEqual(self.remote[42]['credentials']['chatgpt_account_id'],'workspace')

    def test_concurrent_queue_claim_returns_per_account_error_not_index_error(self):
        with patch.object(self.v,'queue',return_value=[]):
            response=self.queue()
        self.assertEqual(response['items'][0]['code'],'OPERATION_RUNNING')
        self.assertNotIn('deployment',self.v.account(self.id));self.assertEqual(self.v.jobs(),[])


class RealHTTPDeploymentTests(unittest.TestCase):
    def test_real_http_sub2_import_without_password_cookie_or_nvt_then_deploy(self):
        remote={};requests=[];counter=0
        class Handler(BaseHTTPRequestHandler):
            def send_json(self,data):
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
                self.wfile.write(json.dumps({'code':0,'data':data}).encode())

            def do_GET(self):
                requests.append(('GET',self.path))
                if '/groups/all' in self.path:
                    self.send_json([{'id':i,'name':f'Group {i}','platform':'openai','status':'active'} for i in [9001,11,12]])
                elif '/proxies/all' in self.path:
                    self.send_json([{'id':7,'name':'Proxy','status':'active'}])
                elif self.path.startswith('/api/v1/admin/accounts?'):
                    self.send_json({'items':list(remote.values()),'total':len(remote)})
                elif self.path=='/api/v1/admin/accounts/42':self.send_json(remote[42])
                else:self.send_error(404)

            def do_POST(self):
                nonlocal counter
                requests.append(('POST',self.path));body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                counter+=1
                if self.path=='/api/v1/admin/accounts':
                    remote[42]={**account(),**body,'id':42,'schedulable':True,'updated_at':str(counter)}
                    self.send_json(remote[42])
                elif self.path=='/api/v1/admin/accounts/42/schedulable':
                    remote[42].update(body,updated_at=str(counter));self.send_json(remote[42])
                elif self.path=='/api/v1/admin/accounts/42/test':
                    if remote[42]['schedulable']:self.send_error(500);return
                    self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                    self.wfile.write(b'data: {"type":"test_start"}\n\ndata: {"type":"test_complete","success":true}\n\n')
                else:self.send_error(404)

            def do_PUT(self):
                nonlocal counter
                requests.append(('PUT',self.path));body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                counter+=1
                remote[42].update(body,updated_at=str(counter));self.send_json(remote[42])

            def log_message(self,*args):pass

        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as d:
                def no_login(request):raise AssertionError('Cached credentials must not log in again')
                with httpx.Client(transport=httpx.MockTransport(no_login)) as nvt:
                    app=create_app(d,token='TEST-TOKEN',start_worker=False,connector=NVTConnector(nvt))
                    with TestClient(app,base_url='http://127.0.0.1:8765',headers={'x-local-token':'TEST-TOKEN'}) as client:
                        client.post('/api/unlock',json={'setup':True,'password':'synthetic-master-password'})
                        url=f'http://127.0.0.1:{server.server_port}'
                        client.post('/api/settings',json={'sub2api_url':url,'admin_key':'SYNTHETIC_ADMIN'})
                        v=app.state.vault
                        revision=client.get('/api/state').json()['settings']['cloud_revision']
                        response=client.post('/api/import/deploy',json={'format':'sub2',
                            'request_id':'same-page-http-test','text':json.dumps(authorization().raw),
                            'connection_revision':revision,'profile':{
                            **PROFILE,'instance_id':url+'/api/v1/admin'},'model_id':'test-model',
                            'confirm_deploy':True,'staging_verified':True})
                        self.assertEqual(response.status_code,200,response.text)
                        self.assertEqual(response.json()['counts'],{'queued':1})
                        self.assertFalse(v.accounts()[0]['has_login_material'])
                        self.assertTrue(app.state.service.run_one())
                        job=v.jobs()[0];self.assertEqual(job['state'],'succeeded',job)
                        self.assertTrue(remote[42]['schedulable']);self.assertEqual(remote[42]['group_ids'],[11,12])
                        self.assertEqual(remote[42]['extra'],{'codex_fingerprint_mode':'device'})
                        self.assertEqual(client.get('/api/status').json()['version'],'0.7.0')
                        batch=client.get('/api/import/batch').json()
                        self.assertEqual(batch['counts'],{'succeeded':1})
                        self.assertEqual(batch['items'][0]['cloud_id'],42)
                        self.assertNotIn('SYNTHETIC_ACCESS',client.get('/api/state').text)
            writes=[(method,path.removeprefix('/api/v1/admin')) for method,path in requests if method!='GET']
            self.assertEqual(writes,[('POST','/accounts'),('POST','/accounts/42/schedulable'),
                ('POST','/accounts/42/test'),('PUT','/accounts/42'),('POST','/accounts/42/test'),
                ('POST','/accounts/42/schedulable')])
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
