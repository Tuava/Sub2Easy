from copy import deepcopy
from datetime import datetime, timezone
import json
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from sub2easy.binding import candidates, classify, metadata
from sub2easy.gui import create_app, DEFAULT_PROFILE
from sub2easy.intake import parse_batch
from sub2easy.preflight import PreflightError


EMAIL='match@example.invalid'


def cloud(i=42,email=EMAIL,name=EMAIL,workspace='workspace',created='2026-09-11T00:00:00Z',**updates):
    return {'id':i,'name':name,'platform':'openai','type':'oauth','status':'active','schedulable':True,
        'credentials':{'email':email,'chatgpt_account_id':workspace,'chatgpt_user_id':'user',
                       'access_token':'SECRET_ACCESS','refresh_token':'SECRET_REFRESH'},
        'created_at':created,'updated_at':created,'last_used_at':created,'group_ids':[1],
        'groups':[{'id':1,'name':'测试组'}],'proxy_id':7,'proxy':{'name':'出口 A','password':'SECRET_PROXY'},
        'concurrency':3,'priority':50,'extra':{'codex_fingerprint_mode':'device','private_key':'SECRET_PRIVATE'},
        'notes':'SECRET_NOTES','error_message':'SECRET_ERROR','expires_at':None,
        'rate_limit_reset_at':None,'overload_until':None,'temp_unschedulable_until':None,**updates}


def local():
    return {'id':'local-id','login':{'account':EMAIL},'imported_at':datetime(2026,9,11,tzinfo=timezone.utc).timestamp(),
            'authorization':None,'profile':DEFAULT_PROFILE}


class MatcherTests(unittest.TestCase):
    def test_unique_identity_can_auto_bind_without_time(self):
        a=local();a.pop('imported_at')
        rows=candidates(a,[cloud(created=None)],{})
        status,chosen=classify(rows)
        self.assertEqual(status,'ready');self.assertEqual(chosen['id'],42)
        self.assertIsNone(rows[0]['time_delta_seconds'])

    def test_name_time_only_is_candidate_not_identity(self):
        rows=candidates(local(),[cloud(email=None)],{})
        self.assertIn('name',rows[0]['reasons']);self.assertIn('near_time',rows[0]['reasons'])
        self.assertFalse(rows[0]['eligible']);self.assertEqual(classify(rows)[0],'conflict')

    def test_different_email_with_same_name_and_time_is_conflict(self):
        rows=candidates(local(),[cloud(email='other@example.invalid')],{})
        self.assertFalse(rows[0]['eligible']);self.assertIn('CLOUD_IDENTITY_MISMATCH',rows[0]['conflicts'])

    def test_duplicate_names_emails_cannot_use_time_to_auto_choose(self):
        rows=candidates(local(),[cloud(),cloud(43,created='2020-01-01T00:00:00Z')],{})
        self.assertEqual(classify(rows),('ambiguous',None));self.assertEqual(rows[0]['id'],42)

    def test_known_workspace_disambiguates_same_email(self):
        a=local();a['authorization']={'identity':{'account_email':EMAIL,'chatgpt_account_id':'workspace','chatgpt_user_id':'user'}}
        rows=candidates(a,[cloud(),cloud(43,workspace='other')],{})
        status,chosen=classify(rows);self.assertEqual(status,'ready');self.assertEqual(chosen['id'],42)
        self.assertFalse(next(c for c in rows if c['id']==43)['eligible'])

    def test_occupied_duplicate_is_not_silently_eliminated(self):
        rows=candidates(local(),[cloud(),cloud(43)],{42:'other-local'})
        self.assertEqual(classify(rows)[0],'ambiguous')
        self.assertIn('CLOUD_ALREADY_BOUND',next(c for c in rows if c['id']==42)['conflicts'])

    def test_other_platform_and_shadow_not_eligible(self):
        for item in [cloud(platform='anthropic'),cloud(parent_account_id=10),cloud(type='apikey')]:
            self.assertFalse(candidates(local(),[item],{})[0]['eligible'])

    def test_name_from_authorization_export_and_time_reference(self):
        a=local();a['raw_result']={'account_json':{'exported_at':'2026-09-12T00:00:00Z','accounts':[{'name':'Alias'}]}}
        rows=candidates(a,[cloud(email=None,name='Alias',created='2026-09-12T00:10:00Z')],{})
        self.assertEqual(rows[0]['time_reference'],'authorization_export')
        self.assertEqual(rows[0]['time_delta_seconds'],600)
        self.assertFalse(rows[0]['eligible'])

    def test_whitelisted_details_exclude_credentials_and_error(self):
        raw=json.dumps(metadata(cloud()))
        self.assertNotIn('SECRET',raw)
        for key in ['workspace_id','created_at','last_used_at','proxy_name','groups']:
            self.assertIn(key,raw)

    def test_shallow_and_detail_dto_have_same_selection_fingerprint(self):
        a=cloud();b=deepcopy(a);b.pop('groups');b.pop('proxy')
        self.assertEqual(metadata(a)['fingerprint'],metadata(b)['fingerprint'])
        b['last_used_at']='2026-09-12T00:00:00Z'
        self.assertEqual(metadata(a)['fingerprint'],metadata(b)['fingerprint'])
        b['credentials']['chatgpt_account_id']='changed'
        self.assertNotEqual(metadata(a)['fingerprint'],metadata(b)['fingerprint'])


class BindingAPITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.app=create_app(self.temp.name,token='TOKEN',start_worker=False)
        self.c=TestClient(self.app,base_url='http://127.0.0.1:8765',headers={'x-local-token':'TOKEN'});self.c.__enter__()
        self.c.post('/api/unlock',json={'password':'test-vault-password','setup':True})
        self.c.post('/api/settings',json={'sub2api_url':'https://example.invalid','admin_key':'SECRET_ADMIN'})
        self.id=self.add(EMAIL);self.remote=[cloud()]
        self.mock=patch('sub2easy.binding.Client');self.client=self.mock.start()
        self.client.return_value.accounts.side_effect=lambda:deepcopy(self.remote)
        self.read=patch.object(self.app.state.service,'cloud_read',side_effect=lambda path:deepcopy(next(a for a in self.remote if path==f"/accounts/{a['id']}")))
        self.read.start()

    def tearDown(self):
        self.read.stop();self.mock.stop();self.c.__exit__(None,None,None);self.temp.cleanup()

    def add(self,email):
        self.app.state.vault.import_materials(parse_batch(email+'----FAKE_PASSWORD----JBSWY3DPEHPK3PXP'),DEFAULT_PROFILE)
        return next(a['id'] for a in self.app.state.vault.accounts() if self.app.state.vault.account(a['id'])['login']['account']==email)

    def scan(self,ids=None,auto=True):
        r=self.c.post('/api/bindings/scan',json={'account_ids':ids or [self.id],'auto_bind':auto})
        self.assertEqual(r.status_code,200,r.text);return r.json()

    def test_unique_auto_bind_updates_local_only_and_not_monitor(self):
        with patch.object(self.app.state.service,'cloud_write') as write,patch.object(self.app.state.service.connector,'authorize') as nvt:
            report=self.scan()
        self.assertEqual(report['items'][0]['status'],'bound');write.assert_not_called();nvt.assert_not_called()
        a=self.app.state.vault.account(self.id);self.assertEqual(a['binding']['cloud_id'],42)
        self.assertFalse(a.get('monitor',{}).get('enabled'))

    def test_batch_has_success_ambiguous_conflict_not_found_without_abort(self):
        second=self.add('double@example.invalid');third=self.add('missing@example.invalid')
        self.remote=[cloud(),cloud(43,email='double@example.invalid'),cloud(44,email='double@example.invalid')]
        # Disable time candidates for the missing row so no weak matches are found.
        self.app.state.vault.update_account(third,imported_at=1)
        report=self.scan([self.id,second,third]);states={r['account_id']:r['status'] for r in report['items']}
        self.assertEqual(states,{self.id:'bound',second:'ambiguous',third:'not_found'})

    def test_resolve_ambiguity_rechecks_identity_and_fingerprint(self):
        self.remote.append(cloud(43,workspace='other'))
        report=self.scan();row=report['items'][0];self.assertEqual(row['status'],'ambiguous')
        chosen=next(c for c in row['candidates'] if c['id']==43)
        r=self.c.post('/api/bindings/resolve',json={'account_id':self.id,'cloud_id':43,'fingerprint':chosen['fingerprint'],
                'connection_revision':report['connection_revision'],'confirm_selection':True})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['items'][0]['status'],'bound')
        self.assertEqual(self.app.state.vault.account(self.id)['binding']['cloud_id'],43)

    def test_selected_candidate_changed_goes_to_unified_error_list(self):
        report=self.scan(auto=False);chosen=report['items'][0]['candidates'][0]
        self.remote[0]['credentials']['chatgpt_account_id']='changed'
        r=self.c.post('/api/bindings/resolve',json={'account_id':self.id,'cloud_id':42,'fingerprint':chosen['fingerprint'],
                'connection_revision':report['connection_revision'],'confirm_selection':True})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['items'][0]['code'],'CLOUD_CANDIDATE_CHANGED')
        self.assertIsNone(self.app.state.vault.account(self.id)['binding'])

    def test_bound_elsewhere_cannot_be_rebound(self):
        other=self.add('other@example.invalid');self.app.state.vault.update_account(other,binding={
            'cloud_id':42,'instance':'https://example.invalid/api/v1/admin','identity':{'account_email':EMAIL,'chatgpt_account_id':'workspace'}})
        report=self.scan();self.assertEqual(report['items'][0]['status'],'conflict')
        self.assertIsNone(self.app.state.vault.account(self.id)['binding'])

    def test_incomplete_snapshot_no_binding_and_errors_for_all(self):
        second=self.add('second@example.invalid')
        self.client.return_value.accounts.side_effect=PreflightError('SECRET_NETWORK')
        report=self.scan([self.id,second]);self.assertEqual(report['counts'],{'failed':2})
        self.assertNotIn('SECRET',json.dumps(report));self.assertIsNone(self.app.state.vault.account(self.id)['binding'])

    def test_unresolved_rows_retained_when_retrying_single_account(self):
        second=self.add('second@example.invalid');self.scan([self.id,second],auto=False)
        report=self.scan([self.id]);self.assertEqual(len(report['items']),2)

    def test_batch_pending_task_write_intent_and_invalid_id_each_reported(self):
        second=self.add('second@example.invalid');self.app.state.vault.queue([self.id])
        self.app.state.vault.update_account(second,write_intent={'state':'unknown'})
        report=self.scan([self.id,second,'does-not-exist'])
        self.assertEqual(report['counts'],{'conflict':2,'failed':1})

    def test_site_change_clears_catalog_and_report(self):
        report=self.scan(auto=False)
        self.c.post('/api/settings',json={'sub2api_url':'https://other.invalid'})
        self.assertIsNone(self.c.get('/api/bindings/report').json())
        self.assertTrue(self.c.get('/api/cloud/accounts').json()['needs_refresh'])
        r=self.c.post('/api/bindings/resolve',json={'account_id':self.id,'cloud_id':42,'fingerprint':'old',
            'connection_revision':report['connection_revision'],'confirm_selection':True})
        self.assertEqual(r.json()['code'],'BINDING_REPORT_STALE')

    def test_catalog_all_platforms_and_combined_filters_pagination(self):
        self.remote=[cloud(i,email=f'a{i}@example.invalid',name=f'Account {i}',group_ids=[1 if i%2 else 2],
                           platform='openai' if i<30 else 'anthropic',proxy_id=7 if i%2 else None) for i in range(1,61)]
        r=self.c.post('/api/cloud/catalog',json={});self.assertEqual(r.json()['count'],60)
        r=self.c.get('/api/cloud/accounts?page=2&page_size=25&sort=id').json()
        self.assertEqual(r['pages'],3);self.assertEqual(r['items'][0]['id'],26)
        r=self.c.get('/api/cloud/accounts?platform=openai&group=2&proxy=direct&q=Account&sort=id').json()
        self.assertEqual(r['total'],14);self.assertTrue(all(a['id']%2==0 for a in r['items']))
        r=self.c.get('/api/cloud/accounts?since=2000000000').json();self.assertEqual(r['total'],0)
        self.assertNotIn('SECRET',self.c.get('/api/cloud/accounts').text)

    def test_current_bindings_reflected_in_catalog_and_local_state(self):
        self.scan()
        r=self.c.get('/api/cloud/accounts?binding=bound').json();self.assertEqual(r['total'],1)
        self.assertEqual(r['items'][0]['local_id'],self.id)
        r=self.c.get('/api/state');self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['accounts'][0]['cloud_metadata']['id'],42)

    def test_selection_cannot_target_outside_report(self):
        report=self.scan(auto=False)
        r=self.c.post('/api/bindings/resolve',json={'account_id':self.id,'cloud_id':999,'fingerprint':'x',
            'connection_revision':report['connection_revision'],'confirm_selection':True})
        self.assertEqual(r.json()['code'],'CLOUD_MATCH_CONFLICT')


if __name__=='__main__':unittest.main()
