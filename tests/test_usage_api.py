import tempfile
import threading
import unittest
import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient

from sub2easy.gui import create_app
from sub2easy.usage_cache import UsageCache


def report(ids):
    return {'accounts':[{'account_id':i,'windows':[],'today':{'requests':0,'tokens':0,'cost':0},
                         'errors':[],'collected_at':'2026-09-12T01:00:00Z'} for i in ids]}


class UsageCacheTests(unittest.TestCase):
    def test_cache_singleflight_ttl_and_per_account_selection(self):
        entered=threading.Event();release=threading.Event();calls=[];now=[0]
        def collect(client,ids,**kwargs):
            calls.append((ids,kwargs));entered.set();release.wait(2);return report(ids)
        cache=UsageCache(collect,lambda u,k:None,lambda:now[0])
        try:
            r=cache.request(('site','rev'),'url','key',[1,2]);self.assertTrue(r['pending'])
            self.assertTrue(entered.wait(2))
            cache.request(('site','rev'),'url','key',[1,2]);self.assertEqual(len(calls),1)
            release.set();cache.future.result(2)
            self.assertEqual(len(cache.snapshot(('site','rev'),[1])['accounts']),1)
            cache.request(('site','rev'),'url','key',[1]);self.assertEqual(len(calls),1)
            now[0]=61;cache.request(('site','rev'),'url','key',[1]);cache.future.result(2)
            self.assertEqual(len(calls),2);self.assertEqual(calls[1][0],[1])
        finally:release.set();cache.close()

    def test_lock_discards_inflight_result_and_erases_existing_rows(self):
        entered=threading.Event();release=threading.Event()
        def collect(client,ids,**kwargs):entered.set();release.wait(2);return report(ids)
        cache=UsageCache(collect,lambda u,k:None)
        try:
            cache.request(('site','rev'),'url','key',[1]);entered.wait(2)
            future=cache.future;cache.invalidate();release.set();future.result(2)
            self.assertEqual(cache.snapshot(('site','rev'),[1])['accounts'],[])
        finally:release.set();cache.close()

    def test_site_change_cannot_reuse_same_id_usage(self):
        cache=UsageCache(lambda c,ids,**kw:report(ids),lambda u,k:None)
        try:
            cache.request(('site-a','rev'),'url','key',[1]);cache.future.result(2)
            self.assertTrue(cache.snapshot(('site-a','rev'),[1])['accounts'])
            self.assertEqual(cache.snapshot(('site-b','rev'),[1])['accounts'],[])
        finally:cache.close()


class UsageAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=create_app(self.tmp.name,token='TEST',start_worker=False)
        self.client=TestClient(self.app,base_url='http://127.0.0.1:8765',headers={'x-local-token':'TEST'})
        self.client.__enter__()
        self.client.post('/api/unlock',json={'password':'test-master-password','setup':True})
        self.client.post('/api/settings',json={'sub2api_url':'https://example.invalid','admin_key':'SYNTHETIC'})
        self.app.state.service.usage.close()
        self.calls=[]
        def collect(client,ids,**kwargs):self.calls.append(ids);return report(ids)
        self.cache=UsageCache(collect,lambda u,k:None);self.app.state.service.usage=self.cache

    def tearDown(self):
        self.client.__exit__(None,None,None);self.tmp.cleanup()

    def test_async_endpoint_and_cache_query_are_read_only(self):
        r=self.client.post('/api/usage',json={'account_ids':[42]});self.assertEqual(r.status_code,200,r.text)
        self.cache.future.result(2)
        r=self.client.get('/api/usage?ids=42');self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['accounts'][0]['today']['requests'],0)
        self.assertNotIn('SYNTHETIC',r.text);self.assertEqual(self.calls,[[42]])

    def test_empty_ids_no_requests_and_bad_ids_rejected(self):
        r=self.client.post('/api/usage',json={'account_ids':[]});self.assertEqual(r.status_code,200)
        self.assertEqual(self.calls,[])
        for ids in [[True],[0],list(range(1,52)),['42']]:
            r=self.client.post('/api/usage',json={'account_ids':ids});self.assertEqual(r.status_code,400)
        self.assertEqual(self.calls,[])

    def test_lock_and_site_change_clear_cache(self):
        self.client.post('/api/usage',json={'account_ids':[42]});self.cache.future.result(2)
        self.client.post('/api/settings',json={'sub2api_url':'https://other.invalid'})
        self.assertEqual(self.client.get('/api/usage?ids=42').json()['accounts'],[])
        self.client.post('/api/lock',json={})
        self.assertEqual(self.client.get('/api/usage?ids=42').json()['code'],'VAULT_LOCKED')

    def test_usage_collection_does_not_take_mutation_lock(self):
        service=self.app.state.service;service.operation.acquire()
        try:
            r=self.client.post('/api/usage',json={'account_ids':[42]})
            self.assertEqual(r.status_code,200)
        finally:service.operation.release()


class RealHTTPUsageTests(unittest.TestCase):
    def test_loopback_usage_api_reads_only_account_and_today_stats(self):
        calls=[];now=datetime.now(timezone.utc)
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(('GET',self.path))
                if self.path=='/api/v1/admin/accounts/42':
                    data={'id':42,'platform':'openai','type':'oauth','extra':{
                        'codex_5h_used_percent':25,'codex_7d_used_percent':60,
                        'codex_usage_updated_at':now.isoformat(),
                        'codex_5h_reset_at':(now+timedelta(hours=1)).isoformat(),
                        'codex_7d_reset_at':(now+timedelta(days=2)).isoformat()},
                        'credentials':{'access_token':'NEVER_EXPOSE_THIS_TOKEN'}}
                elif self.path=='/api/v1/admin/accounts/42/today-stats':
                    data={'requests':12,'tokens':2048,'cost':0.25,'standard_cost':0.5,'user_cost':0.75}
                else:self.send_error(404);return
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
                self.wfile.write(json.dumps({'code':0,'data':data}).encode())
            def do_POST(self):calls.append(('POST',self.path));self.send_error(405)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as d:
                app=create_app(d,token='TEST',start_worker=False)
                with TestClient(app,base_url='http://127.0.0.1:8765',headers={'x-local-token':'TEST'}) as c:
                    c.post('/api/unlock',json={'setup':True,'password':'synthetic-master-password'})
                    c.post('/api/settings',json={'sub2api_url':f'http://127.0.0.1:{server.server_port}',
                                               'admin_key':'SYNTHETIC'})
                    r=c.post('/api/usage',json={'account_ids':[42]});self.assertEqual(r.status_code,200)
                    app.state.service.usage.future.result(5)
                    r=c.get('/api/usage?ids=42');row=r.json()['accounts'][0]
                    self.assertEqual(row['windows'][0]['used_percent'],25)
                    self.assertEqual(row['windows'][0]['remaining'],75)
                    self.assertEqual(row['today']['requests'],12);self.assertEqual(row['today']['tokens'],2048)
                    self.assertEqual(row['today']['cost'],0.25)
                    self.assertNotIn('NEVER_EXPOSE',r.text)
            self.assertEqual(calls,[('GET','/api/v1/admin/accounts/42'),('GET','/api/v1/admin/accounts/42/today-stats')])
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
