import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from sub2easy.task_pool import TaskPool
from sub2easy.vault import Vault
from sub2easy.intake import parse_batch


LINE = 'one@example.invalid----PASSWORD----JBSWY3DPEHPK3PXP\ntwo@example.invalid----PASSWORD2----JBSWY3DPEHPK3PXP'
PROFILE = {'profile_id':'x','revision':1,'instance_id':'primary','platform':'openai','account_type':'oauth',
           'staging_group_id':1,'target_group_ids':[2],'proxy_id':None,'concurrency':1,'priority':50,
           'rate_multiplier':1.0,'fingerprint_mode':'off','account_expires_at':None,'auto_pause_on_expired':True}


class TaskPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.v=Vault(self.temp.name); self.v.unlock('synthetic-master-password',setup=True)
        self.v.set_setting('connection',{'nvt_cookie':'COOKIE'})
        self.v.import_materials(parse_batch(LINE),PROFILE)
        self.s=SimpleNamespace(vault=self.v,stop=threading.Event(),operation=threading.RLock())
        self.p=TaskPool(self.s)
    def tearDown(self):self.p.close();self.v.close();self.temp.cleanup()
    def test_claim_skips_account_reserved_by_another_worker(self):
        ids=[a['id'] for a in self.v.accounts()]
        jobs=[self.v.queue([i])[0] for i in ids]
        first=self.p.claim();self.assertIsNotNone(first)
        second=self.p.claim();self.assertIsNotNone(second)
        self.assertNotEqual(first[0]['account_id'],second[0]['account_id'])
        self.assertIsNone(self.p.claim())
    def test_dispatch_runs_different_accounts_in_parallel_and_same_account_once(self):
        ids=[a['id'] for a in self.v.accounts()]
        for i in ids:self.v.queue([i])
        entered=[];release=threading.Event();both=threading.Event()
        def run(job,settings):
            entered.append(job['account_id'])
            if len(entered)>=2:both.set()
            release.wait(3)
            self.v.finish(job['id'],'succeeded','OK')
        self.s.run_job=run
        self.p.save({'max_workers':2,'max_authorizations':1})
        self.p.dispatch();self.assertTrue(both.wait(2));self.assertEqual(len(entered),2)
        # There are only two accounts; no duplicate claim is possible while held.
        self.assertEqual(len(set(entered)),2);release.set()
        deadline=time.time()+3
        while self.p.view()['runtime']['active'] and time.time()<deadline:time.sleep(.02)
        self.assertEqual(self.p.view()['runtime']['active'],0)
    def test_authorization_limit_is_configurable_without_reducing_worker_parallelism(self):
        self.assertEqual(self.p.config()['max_workers'],3);self.assertEqual(self.p.config()['max_authorizations'],2)
        self.assertEqual(self.p.save({'max_workers':4,'max_authorizations':1})['runtime']['active'],0)
        with self.assertRaises(ValueError):self.p.save({'max_workers':0})

    def test_discovered_cloud_id_reserved_until_worker_unwinds(self):
        self.p.active={'one':{('local','a')},'two':{('local','b')}}
        binding={'instance':'https://example.invalid','cloud_id':42}
        self.p.reserve_binding('one',binding)
        with self.assertRaisesRegex(ValueError,'OPERATION_RUNNING'):self.p.reserve_binding('two',binding)
        self.p.active.pop('one');self.p.reserve_binding('two',binding)
        self.assertIn(('cloud','https://example.invalid',42),self.p.active['two'])


from copy import deepcopy
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from sub2easy.gui import create_app, DesktopService
from sub2easy.nvtokens import parse_response, ConnectorError
from sub2easy.vault import VaultError


def wait_until(fn, timeout=4):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if fn():return True
        time.sleep(.01)
    return False


class ParallelServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=create_app(self.temp.name,token='TEST',start_worker=False)
        self.c=TestClient(self.app,base_url='http://127.0.0.1:8765',headers={'x-local-token':'TEST'})
        self.c.__enter__();self.v=self.app.state.vault;self.s=self.app.state.service
        self.v.unlock('synthetic-master-password',setup=True)
        self.v.set_setting('connection',{'nvt_cookie':'SYNTHETIC.COOKIE','sub2api_url':'https://example.invalid',
                                         'admin_key':'SYNTHETIC_ADMIN','cloud_revision':'revision'})
        self.v.import_materials(parse_batch(LINE),PROFILE)
        self.ids=[a['id'] for a in self.v.accounts()]
        self.release=threading.Event()
    def tearDown(self):
        self.release.set();self.c.__exit__(None,None,None);self.temp.cleanup()
    def result(self, email):
        return parse_response(200,__import__('json').dumps({'platform':'openai','type':'oauth','credentials':{
            'email':email,'chatgpt_account_id':'workspace','client_id':'client','access_token':'ACCESS',
            'refresh_token':'REFRESH','expires_at':'2099-01-01T00:00:00Z'}}).encode(),email)
    def queued(self):return [self.v.queue([aid])[0] for aid in self.ids]
    def test_real_manual_handlers_overlap_and_read_refresh_queue_stay_responsive(self):
        self.queued();entered=[];both=threading.Event();mtx=threading.Lock()
        def auth(cookie,login,*args):
            with mtx:
                entered.append(login['account'])
                if len(entered)==2:both.set()
            self.release.wait(4)
            return self.result(login['account'])
        with patch.object(self.s.connector,'authorize',side_effect=auth):
            self.s.tasks.dispatch();self.assertTrue(both.wait(2))
            r=self.c.get('/api/state');self.assertEqual(r.status_code,200)
            self.assertEqual(r.json()['task_pool']['runtime']['authorizing'],2)
            self.assertEqual(self.c.post('/api/monitor/check',json={}).status_code,200)
            r=self.c.post('/api/import',json={'text':LINE.replace('one@','three@').splitlines()[0]})
            self.assertEqual(r.status_code,200,r.text)
            self.assertEqual(self.c.post('/api/settings',json={'admin_key':'NEW'}).json()['code'],'TASKS_RUNNING_CONFIG_LOCKED')
            self.assertEqual(self.c.post('/api/lock',json={}).json()['code'],'TASKS_RUNNING_CONFIG_LOCKED')
            self.release.set();self.assertTrue(wait_until(lambda:not self.s.tasks.view()['runtime']['active']))
        self.assertEqual(self.v.job_counts()['succeeded'],2)
    def test_nvt_cap_wait_is_cancellable_without_second_login(self):
        jobs=self.queued();entered=[];first=threading.Event()
        self.s.tasks.save({'max_workers':2,'max_authorizations':1})
        def auth(cookie,login,*args):
            entered.append(login['account']);first.set();self.release.wait(4)
            return self.result(login['account'])
        with patch.object(self.s.connector,'authorize',side_effect=auth):
            self.s.tasks.dispatch();self.assertTrue(first.wait(2))
            self.assertTrue(wait_until(lambda:self.s.tasks.view()['runtime']['active']==2))
            running_aid=next(a['id'] for a in self.v.accounts() if a['label']==entered[0])
            waiting_job=next(j for j in self.v.jobs() if j['account_id']!=running_aid)
            self.v.cancel_jobs([waiting_job['id']])
            self.assertTrue(wait_until(lambda:self.s.tasks.view()['runtime']['active']==1))
            self.assertEqual(len(entered),1);self.release.set()
            self.assertTrue(wait_until(lambda:not self.s.tasks.view()['runtime']['active']))
        self.assertEqual(next(j for j in self.v.jobs() if j['id']==waiting_job['id'])['state'],'cancelled')
    def test_pause_and_lower_cap_drain_without_cancelling_running(self):
        self.queued();entered=threading.Event()
        self.s.tasks.save({'max_workers':1})
        def auth(cookie,login,*args):
            entered.set();self.release.wait(4);return self.result(login['account'])
        with patch.object(self.s.connector,'authorize',side_effect=auth):
            self.s.tasks.dispatch();self.assertTrue(entered.wait(2))
            self.s.tasks.save({'paused':True});self.s.tasks.dispatch()
            self.assertEqual(self.v.job_counts()['queued'],1)
            self.release.set();self.assertTrue(wait_until(lambda:not self.s.tasks.view()['runtime']['active']))
            self.s.tasks.dispatch();self.assertEqual(self.v.job_counts()['queued'],1)
            self.s.tasks.save({'paused':False});self.s.tasks.dispatch()
            self.assertTrue(wait_until(lambda:self.v.job_counts().get('succeeded')==2))
    def test_same_remote_binding_and_login_owner_do_not_overlap(self):
        b={'instance':'https://example.invalid/api/v1/admin','cloud_id':42,'identity':{'account_email':'a@example.invalid'}}
        for aid in self.ids:self.v.update_account(aid,binding=b)
        self.queued();first=self.s.tasks.claim();self.assertIsNotNone(first)
        self.assertIsNone(self.s.tasks.claim())
        self.v.finish(first[0]['id'],'succeeded','OK')
        # A terminal DB row does not release the owner until the handler has returned.
        self.assertIsNone(self.s.tasks.claim())
        with self.s.tasks.mutex:self.s.tasks.active.clear()
        second=self.s.tasks.claim();self.assertIsNotNone(second)
        self.v.finish(second[0]['id'],'cancelled','OK')
        self.s.tasks.active.clear()
    def test_worker_exception_does_not_starve_other_jobs(self):
        self.queued()
        def run(job,settings):
            if job['account_id']==self.ids[0]:raise RuntimeError('SECRET')
            self.v.finish(job['id'],'succeeded','OK')
        with patch.object(self.s,'run_job',side_effect=run):
            self.s.tasks.dispatch();self.assertTrue(wait_until(lambda:not self.s.tasks.view()['runtime']['active']))
        self.assertEqual(self.v.job_counts(),{'unknown':1,'succeeded':1})
        self.assertEqual(self.s.tasks.view()['runtime']['last_error'],'TASK_WORKER_FAILED')
    def test_late_cookie_failure_never_restores_old_key_or_pauses_new_cookie(self):
        old=self.v.get_setting('connection');new={**old,'nvt_cookie':'NEW.COOKIE','admin_key':'NEW_KEY'}
        self.v.set_setting('connection',new);self.s.pause_connector(old)
        self.assertEqual(self.v.get_setting('connection'),new)
    def test_duplicate_refresh_is_coalesced_not_operation_running(self):
        with self.s.monitor.poll_lock:
            self.assertEqual(self.c.post('/api/monitor/check',json={}).status_code,200)
    def test_task_settings_persist_and_validate(self):
        for key in ('max_workers','max_authorizations'):
            for v in (0,9,True,'2'):
                r=self.c.post('/api/tasks/config',json={key:v})
                self.assertEqual(r.json()['code'],'INVALID_TASK_CONCURRENCY')
        r=self.c.post('/api/tasks/config',json={'max_workers':4,'max_authorizations':1})
        self.assertEqual(r.json()['config']['max_workers'],4)
        self.v.lock();self.v.unlock('synthetic-master-password')
        self.assertEqual(self.s.tasks.config()['max_authorizations'],1)
    def test_shutdown_joins_dispatched_workers_before_vault_close(self):
        self.queued();entered=threading.Event();done=threading.Event()
        def auth(cookie,login,*args):
            entered.set();self.release.wait(4);return self.result(login['account'])
        with patch.object(self.s.connector,'authorize',side_effect=auth):
            self.s.tasks.dispatch();self.assertTrue(entered.wait(2));self.s.stop.set()
            t=threading.Thread(target=lambda:(self.s.tasks.close(),done.set()));t.start()
            self.assertFalse(done.wait(.15));self.release.set();self.assertTrue(done.wait(3));t.join()
        self.assertEqual(self.s.tasks.view()['runtime']['active'],0)

    def test_background_monitor_keeps_running_while_authorizations_block(self):
        self.queued();auth_entered=threading.Event();monitor_after_auth=threading.Event()
        def auth(cookie,login,*args):
            auth_entered.set();self.release.wait(4);return self.result(login['account'])
        def poll(*args,**kwargs):
            if auth_entered.is_set():monitor_after_auth.set()
        with patch.object(self.s.connector,'authorize',side_effect=auth),patch.object(self.s.monitor,'poll',side_effect=poll):
            self.s.start()
            self.assertTrue(auth_entered.wait(2))
            self.assertTrue(monitor_after_auth.wait(2))
            self.assertEqual(self.s.tasks.view()['runtime']['authorizing'],2)
            self.s.stop.set();self.release.set()
            self.s.thread.join(3);self.s.monitor_thread.join(3)
            self.assertFalse(self.s.thread.is_alive());self.assertFalse(self.s.monitor_thread.is_alive())

    def test_stale_401_snapshot_cannot_overwrite_new_worker_state(self):
        from tests.test_monitor import cloud
        aid=self.ids[0];email=self.v.account(aid)['login']['account'];remote=cloud()
        remote['credentials']['email']=email
        binding={'instance':'https://example.invalid/api/v1/admin','cloud_id':42,'identity':{
            'account_email':email,'chatgpt_account_id':'workspace','chatgpt_user_id':'user'}}
        self.v.update_account(aid,binding=binding,monitor={'enabled':True,'state':'refresh_grace','first_seen':1})
        self.s.monitor.save_config({'enabled':True,'model_id':'model','confirm_auto_reauth':True})
        entered=threading.Event()
        def accounts(**kwargs):
            entered.set();self.release.wait(4);return [remote]
        with patch('sub2easy.monitor.Client') as client:
            client.return_value.accounts.side_effect=accounts
            with ThreadPoolExecutor(1) as pool:
                future=pool.submit(self.s.monitor.poll,True)
                self.assertTrue(entered.wait(2))
                self.v.update_account(aid,status='active',monitor={'enabled':True,'state':'recovered','auth_401':False})
                self.release.set();future.result(3)
        m=self.v.account(aid)['monitor']
        self.assertEqual(m['state'],'recovered');self.assertFalse(m['auth_401']);self.assertEqual(self.v.jobs(),[])


if __name__=='__main__':unittest.main()
