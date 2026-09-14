from copy import deepcopy
from dataclasses import asdict
import json
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from sub2easy.gui import create_app, DEFAULT_PROFILE
from sub2easy.intake import IntakeError, has_login_material, parse_batch
from sub2easy.sub2_import import parse_sub2
from sub2easy.vault import VaultError


EMAIL='json@example.invalid'


def item(email=EMAIL, workspace='workspace', token='SYNTHETIC_ACCESS'):
    return {'name':'JSON demo','platform':'openai','type':'oauth','group_ids':[999],
            'proxy_id':999,'extra':{'codex_fingerprint_mode':'full'},'credentials':{
        'email':email,'chatgpt_account_id':workspace,'chatgpt_user_id':'user','client_id':'client',
        'access_token':token,'refresh_token':'SYNTHETIC_REFRESH','expires_at':'2099-01-01T00:00:00Z',
        'password':'DO_NOT_IMPORT_PASSWORD','totp_secret':'DO_NOT_IMPORT_TOTP'}}


def bundle(*items):
    return {'type':'sub2api-data','version':1,'exported_at':'2026-09-12T00:00:00Z','accounts':list(items),
            'proxies':[{'id':999,'password':'DO_NOT_IMPORT_PROXY'}]}


class ParserTests(unittest.TestCase):
    def test_single_bundle_list_and_multiple_files(self):
        for payload in [item(),bundle(item()),{'type':'sub2api-bundle','version':1,'accounts':[item()]},
                        [item()], {'account_json':bundle(item())}, {'data':bundle(item())},
                        {'accounts':[item()]}]:
            with self.subTest(kind=type(payload)):
                parsed=parse_sub2(json.dumps(payload))
                self.assertEqual(len(parsed.items),1);self.assertEqual(parsed.errors,())
        parsed=parse_sub2(json.dumps([bundle(item()),item('two@example.invalid')]))
        self.assertEqual(parsed.total,2);self.assertEqual(len(parsed.items),2)
        self.assertEqual(parsed.ignored_proxies,1)

    def test_mixed_account_types_and_invalid_items_partial_results(self):
        p=bundle(item(),{'platform':'anthropic','type':'oauth','credentials':{}},None,item('two@example.invalid'))
        result=parse_sub2(json.dumps(p))
        self.assertEqual([i.index for i in result.items],[1,4])
        self.assertEqual([e['index'] for e in result.errors],[2,3])

    def test_same_material_dedup_cross_file(self):
        r=parse_sub2(json.dumps([bundle(item()),bundle(item())]))
        self.assertEqual(len(r.items),1);self.assertEqual(r.duplicates,(2,))

    def test_conflicting_tokens_or_workspaces_no_silent_winner(self):
        for second in [item(workspace='other'),item(token='NEW_ACCESS')]:
            r=parse_sub2(json.dumps(bundle(item(),second)))
            self.assertEqual(r.items,());self.assertEqual(len(r.errors),2)
            self.assertTrue(all(e['code']=='SUB2_CONFLICTING_ACCOUNT' for e in r.errors))

    def test_preview_and_repr_have_no_secrets_or_full_email(self):
        result=parse_sub2(json.dumps(bundle(item())))
        output=json.dumps(result.preview())+repr(result)+repr(result.items[0])
        for v in [EMAIL,'SYNTHETIC_ACCESS','SYNTHETIC_REFRESH','DO_NOT_IMPORT']:
            self.assertNotIn(v,output)
        clean=json.dumps(result.items[0].document)
        self.assertNotIn('DO_NOT_IMPORT',clean)
        self.assertNotIn('group_ids',clean);self.assertNotIn('proxy_id',clean);self.assertNotIn('extra',clean)

    def test_bad_json_version_empty_size_and_depth_limits(self):
        for payload in ['not-json',json.dumps({'accounts':[]}),json.dumps({'version':2,'accounts':[item()]}),
                        'x'*(2*1024*1024+1),json.dumps([item()]*1001)]:
            with self.assertRaises(IntakeError):parse_sub2(payload)

    def test_nvt_summary_not_discarded(self):
        payload={'account_json':bundle(item()),'summary':{'identity_verified':False}}
        result=parse_sub2(json.dumps(payload));self.assertEqual(result.items,())
        self.assertEqual(result.errors[0]['code'],'PROVIDER_IDENTITY_NOT_VERIFIED')
        payload['account_json']=bundle(item(),item('two@example.invalid'))
        with self.assertRaisesRegex(IntakeError,'AMBIGUOUS'):parse_sub2(json.dumps(payload))

    def test_optional_client_default_expiry_and_email_validation(self):
        p=item();del p['credentials']['client_id']
        self.assertFalse(parse_sub2(json.dumps(p)).items)
        self.assertTrue(parse_sub2(json.dumps(p),client_id='explicit-client').items)
        p=item();p['credentials']['expires_at']='1'
        self.assertEqual(parse_sub2(json.dumps(p)).errors[0]['code'],'TOKEN_EXPIRED_OR_TOO_CLOSE')
        p=item();del p['credentials']['email']
        self.assertEqual(parse_sub2(json.dumps(p)).errors[0]['code'],'SUB2_EMAIL_REQUIRED')


class ImportAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=create_app(self.tmp.name,token='TEST',start_worker=False)
        self.c=TestClient(self.app,base_url='http://127.0.0.1:8765',headers={'x-local-token':'TEST'});self.c.__enter__()
        self.c.post('/api/unlock',json={'password':'synthetic-master-password','setup':True})
        self.v=self.app.state.vault

    def tearDown(self):
        self.c.__exit__(None,None,None);self.tmp.cleanup()

    def send(self,payload,update=False):
        r=self.c.post('/api/import',json={'format':'sub2','text':json.dumps(payload),'profile':DEFAULT_PROFILE,
                                         'update_credentials':update})
        self.assertEqual(r.status_code,200,r.text);return r.json()

    def aid(self):return self.v.accounts()[0]['id']

    def test_import_no_cookie_network_and_encrypted_at_rest(self):
        with patch.object(self.app.state.service.connector,'authorize') as nvt,patch.object(self.app.state.service,'cloud_write') as write:
            r=self.send(bundle(item(),item('two@example.invalid')))
            self.assertEqual(r['added'],2);nvt.assert_not_called();write.assert_not_called()
        a=self.v.account(self.aid())
        self.assertEqual(a['status'],'authorized');self.assertFalse(has_login_material(a['login']))
        state=self.c.get('/api/state').text
        for secret in ['SYNTHETIC_ACCESS','SYNTHETIC_REFRESH','DO_NOT_IMPORT_PASSWORD']:
            self.assertNotIn(secret,state)
            for path in __import__('pathlib').Path(self.tmp.name).glob('vault.sqlite3*'):
                self.assertNotIn(secret.encode(),path.read_bytes())
        self.assertFalse(self.v.accounts()[0]['has_login_material'])

    def test_repeat_import_and_explicit_update_preserve_id_and_profile(self):
        self.send(item());aid=self.aid()
        self.assertEqual(self.send(item())['duplicates'],1)
        self.assertEqual(self.send(item(token='NEW'))['failed'],1)
        self.assertEqual(self.send(item(token='NEW'),True)['updated'],1)
        self.assertEqual(self.aid(),aid);self.assertEqual(self.v.account(aid)['revision'],2)
        self.assertEqual(self.v.account(aid)['profile'],DEFAULT_PROFILE)

    def test_workspace_conflict_even_update_checked(self):
        self.send(item());r=self.send(item(workspace='other'),True)
        self.assertEqual(r['results'][0]['code'],'SUB2_IDENTITY_CONFLICT');self.assertEqual(len(self.v.accounts()),1)

    def test_text_first_then_json_attaches_to_same_login_identity(self):
        self.v.import_materials(parse_batch(EMAIL+'----PASSWORD----JBSWY3DPEHPK3PXP'),DEFAULT_PROFILE)
        aid=self.aid();r=self.send(item())
        self.assertEqual(r['updated'],1);self.assertEqual(self.aid(),aid)
        self.assertTrue(self.v.accounts()[0]['has_login_material'])
        self.assertEqual(self.v.account(aid)['login']['password'],'PASSWORD')

    def test_json_first_then_text_supplements_password_and_2fa(self):
        self.send(item());aid=self.aid()
        r=self.v.import_materials(parse_batch(EMAIL+'----PASSWORD----JBSWY3DPEHPK3PXP'),DEFAULT_PROFILE)
        self.assertEqual(r['supplemented_lines'],[1]);self.assertEqual(self.aid(),aid)
        self.assertTrue(self.v.accounts()[0]['has_login_material']);self.assertIsNotNone(self.v.account(aid)['authorization'])

    def test_pending_job_does_not_receive_new_materials_or_tokens(self):
        self.send(item());self.v.queue([self.aid()],kind='server_deploy')
        r=self.send(item(token='NEW'),True);self.assertEqual(r['results'][0]['code'],'SUB2_ACCOUNT_BUSY')
        r=self.v.import_materials(parse_batch(EMAIL+'----PASSWORD----JBSWY3DPEHPK3PXP'),DEFAULT_PROFILE)
        self.assertEqual(r['conflict_lines'],[1])

    def test_token_only_cannot_queue_nvt_login(self):
        self.send(item())
        with self.assertRaisesRegex(VaultError,'LOGIN_MATERIAL_MISSING'):self.v.queue([self.aid()])
        with self.assertRaisesRegex(VaultError,'LOGIN_MATERIAL_MISSING'):self.v.queue([self.aid()],kind='auto_reauth')

    def test_api_preview_reports_item_index_without_credentials(self):
        r=self.c.post('/api/import/preview',json={'format':'sub2','text':json.dumps(bundle(item(),None))})
        self.assertEqual(r.status_code,200);self.assertEqual(r.json()['accepted_indices'],[1])
        self.assertEqual(r.json()['errors'][0]['index'],2)
        self.assertNotIn('SYNTHETIC',r.text)

    def test_monitor_token_only_401_does_not_attempt_login(self):
        self.send(item());aid=self.aid();s=self.app.state.service
        identity=self.v.account(aid)['authorization']['identity']
        self.v.update_account(aid,binding={'cloud_id':42,'instance':'https://example.invalid/api/v1/admin','identity':identity})
        self.v.set_setting('connection',{'sub2api_url':'https://example.invalid','admin_key':'KEY','nvt_cookie':'SYNTHETIC.COOKIE'})
        cloud={'id':42,'platform':'openai','type':'oauth','status':'error','schedulable':True,
               'credentials':item()['credentials'],'error_message':'Token revoked (401): test'}
        with patch.object(s,'cloud_read',return_value=cloud):s.monitor.set_accounts([aid],True)
        s.monitor.save_config({'enabled':True,'model_id':'test','confirm_auto_reauth':True})
        with patch('sub2easy.monitor.Client') as Client,patch.object(s.connector,'authorize') as auth:
            Client.return_value.accounts.return_value=[cloud];s.monitor.poll(force=True)
            self.assertEqual(self.v.account(aid)['monitor']['state'],'credentials_imported');auth.assert_not_called()
        self.assertEqual(self.v.jobs(),[])

    def test_bound_completed_import_needs_explicit_update_permission(self):
        self.send(item());aid=self.aid();a=self.v.account(aid)
        self.v.update_account(aid,status='active',authorization=None,raw_result=None,
                              binding={'cloud_id':42,'instance':'https://example.invalid/api/v1/admin','identity':a['authorization']['identity']},
                              deployment={'state':'complete'})
        self.assertEqual(self.send(item(token='NEW'))['results'][0]['code'],'SUB2_UPDATE_CONFIRM_REQUIRED')
        self.assertEqual(self.send(item(token='NEW'),True)['updated'],1)
        self.assertEqual(self.v.account(aid)['binding']['cloud_id'],42)

    def test_old_applied_token_cannot_silently_skip_new_staged_token(self):
        self.send(item());aid=self.aid();old=self.v.account(aid)['authorization']
        self.v.update_account(aid,last_applied_auth_digest=self.v.authorization_digest(old))
        self.assertEqual(self.send(item(token='NEW'),True)['updated'],1)
        self.assertEqual(self.send(item())['results'][0]['code'],'SUB2_UPDATE_CONFIRM_REQUIRED')

    def test_import_errors_partial_valid_rows_retained(self):
        r=self.send(bundle(item(),{'platform':'grok','type':'oauth','credentials':{}},None))
        self.assertEqual((r['added'],r['failed']),(1,2));self.assertEqual(len(self.v.accounts()),1)


if __name__=='__main__':unittest.main()
