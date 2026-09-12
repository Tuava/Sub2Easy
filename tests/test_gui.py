from dataclasses import asdict
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx

from sub2easy.gui import create_app, DEFAULT_PROFILE, DesktopService
from sub2easy.intake import parse_batch
from sub2easy.nvtokens import ConnectorError, NVTConnector, export_document, parse_response, session_value
from sub2easy.lifecycle import OAuthIdentity
from sub2easy.vault import Vault, VaultError


LOGIN = {"account": "test@example.invalid", "password": "SYNTHETIC_PASSWORD", "totp_secret": "JBSWY3DPEHPK3PXP"}
LINE = "----".join(LOGIN.values())
PASSWORD = "synthetic-vault-password"


def exported():
    return {"type": "sub2api-data", "version": 1, "accounts": [{
        "platform": "openai", "type": "oauth", "credentials": {
            "access_token": "SYNTHETIC_ACCESS", "refresh_token": "SYNTHETIC_REFRESH",
            "expires_at": "2099-01-01T00:00:00Z", "email": LOGIN["account"],
            "chatgpt_account_id": "synthetic-workspace", "client_id": "synthetic-client",
        }, "extra": {"codex_fingerprint_mode": "full"},
    }]}


def synthetic_jwt(exp):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    return encode({'alg': 'RS256', 'typ': 'JWT'}) + '.' + encode({'exp': exp}) + '.SYNTHETIC_SIGNATURE'


def nvt_success():
    """Observed NVT envelope/keys; no copied user identifiers or tokens."""
    document = exported()
    c = document['accounts'][0]['credentials']
    del c['expires_at']
    c.update({
        'access_token': synthetic_jwt(4070908800), 'id_token': synthetic_jwt(4070905200),
        'account_id': 'synthetic-workspace', 'workspace_id': 'synthetic-workspace',
        'user_id': 'synthetic-user', 'chatgpt_user_id': 'synthetic-user',
        'disabled': False, 'expired': False, 'last_refresh': 1,
        'oai_password': '', 'outlook_email': LOGIN['account'],
    })
    return {
        'filename': 'test@example.invalid-reauthorized.sub2api.json',
        'account_json': document,
        'summary': {
            'account_email': LOGIN['account'], 'credential_format': 'mailbox_credential',
            'output_format': 'sub2api', 'identity_verified': True, 'refresh_token_updated': True,
            'workspace_fallback_used': False, 'workspace_selection_kind': 'organization',
            'selected_workspace_id': 'synthetic-workspace',
        },
    }


def cloud(cloud_id=42):
    return {"id": cloud_id, "platform": "openai", "type": "oauth", "status": "error", "schedulable": False,
            "credentials": {"email": LOGIN["account"], "chatgpt_account_id": "synthetic-workspace",
                            "model_mapping": {"a": "b"}}, "extra": {"codex_fingerprint_mode": "device"}}


class ConnectorTests(unittest.TestCase):
    def test_personal_free_fallback_does_not_replace_team_workspace(self):
        payload=nvt_success()
        payload['account_json']['accounts'][0]['credentials'].update(
            chatgpt_account_id='personal-workspace',account_id='personal-workspace',workspace_id='personal-workspace',
            workspace_selection_kind='personal',plan_type='free')
        payload['summary']['selected_workspace_id']='personal-workspace'
        result=parse_response(200,json.dumps(payload).encode(),LOGIN['account'],
                              OAuthIdentity(LOGIN['account'],'original-team','synthetic-user'))
        self.assertIsNone(result.authorization)
        self.assertEqual(result.review_code,'EXPECTED_WORKSPACE_NOT_RETURNED')
        self.assertEqual(result.raw,payload)

    def test_other_team_same_user_is_classified_but_not_accepted(self):
        result=parse_response(200,json.dumps(nvt_success()).encode(),LOGIN['account'],
                              OAuthIdentity(LOGIN['account'],'different-team','synthetic-user'))
        self.assertIsNone(result.authorization)
        self.assertEqual(result.review_code,'REAUTH_WORKSPACE_CHANGED')

    def test_different_user_not_labeled_as_personal_fallback(self):
        payload=nvt_success()
        payload['account_json']['accounts'][0]['credentials'].update(workspace_selection_kind='personal',plan_type='free')
        result=parse_response(200,json.dumps(payload).encode(),LOGIN['account'],
                              OAuthIdentity(LOGIN['account'],'different-team','different-user'))
        self.assertIsNone(result.authorization)
        self.assertEqual(result.review_code,'AUTH_SUBJECT_OR_WORKSPACE_MISMATCH')

    def test_observed_nvt_envelope_derives_access_exp_not_id_exp(self):
        payload = nvt_success()
        r = parse_response(200, json.dumps(payload).encode(), LOGIN['account'])
        self.assertIsNotNone(r.authorization, r.review_code)
        self.assertEqual(r.authorization['credentials']['expires_at'], '4070908800')
        self.assertEqual(r.raw, payload)
        self.assertNotIn('expires_at', payload['account_json']['accounts'][0]['credentials'])
        self.assertNotIn('oai_password', r.authorization['credentials'])

    def test_account_json_string_supported_filename_never_identity(self):
        payload = nvt_success()
        document = payload['account_json']
        payload['filename'] = '../../other-user@example.invalid.json'
        payload['account_json'] = json.dumps(document)
        r = parse_response(200, json.dumps(payload).encode(), LOGIN['account'])
        self.assertIsNotNone(r.authorization, r.review_code)
        self.assertEqual(export_document(payload), document)

    def test_summary_rejects_false_or_conflicting_identity(self):
        cases = [
            ({'identity_verified': False}, 'PROVIDER_IDENTITY_NOT_VERIFIED'),
            ({'identity_verified': 'true'}, 'PROVIDER_IDENTITY_NOT_VERIFIED'),
            ({'account_email': 'other@example.invalid'}, 'PROVIDER_SUMMARY_IDENTITY_MISMATCH'),
            ({'selected_workspace_id': 'wrong'}, 'PROVIDER_SUMMARY_WORKSPACE_MISMATCH'),
            ({'output_format': 'other'}, 'PROVIDER_OUTPUT_FORMAT_MISMATCH'),
        ]
        for change, code in cases:
            with self.subTest(code=code):
                p = nvt_success(); p['summary'].update(change)
                r = parse_response(200, json.dumps(p).encode(), LOGIN['account'])
                self.assertIsNone(r.authorization)
                self.assertEqual(r.review_code, code)

    def test_aliases_cannot_disagree(self):
        for alias in ['account_id', 'workspace_id', 'user_id']:
            with self.subTest(alias=alias):
                p = nvt_success(); p['account_json']['accounts'][0]['credentials'][alias] = 'wrong'
                r = parse_response(200, json.dumps(p).encode(), LOGIN['account'])
                self.assertIsNone(r.authorization)
                self.assertIn('ALIAS_MISMATCH', r.review_code)

    def test_malformed_missing_exp_and_non_numeric_exp_stay_review(self):
        for token in ['bad.parts.token', synthetic_jwt(None), synthetic_jwt(True), synthetic_jwt('4070908800')]:
            with self.subTest(token_type=type(token)):
                p = nvt_success(); p['account_json']['accounts'][0]['credentials']['access_token'] = token
                r = parse_response(200, json.dumps(p).encode(), LOGIN['account'])
                self.assertIsNone(r.authorization)
                self.assertEqual(r.review_code, 'ACCESS_TOKEN_EXPIRY_UNAVAILABLE')

    def test_expired_access_not_hidden_by_valid_id_token(self):
        p = nvt_success(); p['account_json']['accounts'][0]['credentials']['access_token'] = synthetic_jwt(1)
        r = parse_response(200, json.dumps(p).encode(), LOGIN['account'])
        self.assertIsNone(r.authorization)
        self.assertEqual(r.review_code, 'TOKEN_EXPIRED_OR_TOO_CLOSE')

    def test_explicit_expiry_is_not_replaced_with_jwt_hint(self):
        p = nvt_success(); p['account_json']['accounts'][0]['credentials']['expires_at'] = 1
        r = parse_response(200, json.dumps(p).encode(), LOGIN['account'])
        self.assertIsNone(r.authorization)
        self.assertEqual(r.review_code, 'TOKEN_EXPIRED_OR_TOO_CLOSE')

    def test_provider_disabled_and_expired_flags(self):
        for flag in ['disabled', 'expired']:
            p = nvt_success(); p['account_json']['accounts'][0]['credentials'][flag] = True
            r = parse_response(200, json.dumps(p).encode(), LOGIN['account'])
            self.assertIsNone(r.authorization)
            self.assertEqual(r.review_code, 'PROVIDER_ACCOUNT_DISABLED_OR_EXPIRED')

    def test_ambiguous_envelope_not_silently_selected(self):
        p = nvt_success(); p['data'] = exported()
        r = parse_response(200, json.dumps(p).encode(), LOGIN['account'])
        self.assertIsNone(r.authorization)

    def test_cookie_accepts_value_or_only_named_cookie(self):
        self.assertEqual(session_value("scm\\_session=SYNTHETIC.COOKIE"), "SYNTHETIC.COOKIE")
        for value in ["Cookie: other=hello", "abc\nheader:x", "scm_session=value; other=1", ""]:
            with self.subTest(value=value), self.assertRaises(ConnectorError):
                session_value(value)

    def test_actual_documented_error_even_http_200(self):
        payload = {"error": "SENSITIVE_RAW_ERROR", "stage": "protocol_login", "code": "INCORRECT_CODE",
                   "request_id": "SYNTHETIC_REQUEST"}
        for status in [200, 400]:
            with self.subTest(status=status), self.assertRaises(ConnectorError) as c:
                parse_response(status, json.dumps(payload).encode(), LOGIN["account"])
            self.assertEqual(c.exception.code, "INCORRECT_CODE")
            self.assertEqual(c.exception.stage, "protocol_login")
            self.assertNotIn("SENSITIVE", str(c.exception))

    def test_unrecognized_error_fields_never_leak(self):
        with self.assertRaises(ConnectorError) as c:
            parse_response(400, b'{"error":"SECRET", "code":{}, "stage":[]}', LOGIN["account"])
        self.assertEqual(c.exception.code, "PROVIDER_REJECTED")

    def test_raw_file_and_explicit_envelopes(self):
        for payload in [exported(), {"data": exported()}, {"sub2json": json.dumps(exported())}]:
            with self.subTest(payload=type(payload)):
                r = parse_response(200, json.dumps(payload).encode(), LOGIN["account"])
                self.assertIsNotNone(r.authorization)
                self.assertNotIn("SYNTHETIC_ACCESS", repr(r))

    def test_unknown_success_preserved_but_not_marked_valid(self):
        payload = {"download_url": "https://example.invalid/secret.json"}
        r = parse_response(200, json.dumps(payload).encode(), LOGIN["account"])
        self.assertIsNone(r.authorization)
        self.assertEqual(r.raw, payload)

    def test_incomplete_missing_client_with_configured_fallback(self):
        payload = exported()
        del payload["accounts"][0]["credentials"]["client_id"]
        raw = json.dumps(payload).encode()
        self.assertIsNone(parse_response(200, raw, LOGIN["account"]).authorization)
        r = parse_response(200, raw, LOGIN["account"], client_id="known-client")
        self.assertEqual(r.authorization["credentials"]["client_id"], "known-client")

    def test_identity_mismatch_does_not_authorize(self):
        r = parse_response(200, json.dumps(exported()).encode(), "other@example.invalid")
        self.assertIsNone(r.authorization)
        self.assertEqual(r.review_code, "AUTH_EMAIL_MISMATCH")

    def test_request_exact_contract_and_no_browser_headers_needed(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=exported(), headers={"Content-Disposition": 'attachment; filename="../../secret.json"'})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            r = NVTConnector(client).authorize("SYNTHETIC.COOKIE", LOGIN)
        self.assertEqual(len(requests), 1)
        req = requests[0]
        self.assertEqual(str(req.url), "https://nvtokens.com/api/workspace/tools/account-reauthorize")
        self.assertEqual(req.headers["Cookie"], "scm_session=SYNTHETIC.COOKIE")
        self.assertEqual(json.loads(req.content), {"mailbox_credential": LINE, "output_format": "sub2api"})
        self.assertIsNotNone(r.authorization)

    def test_redirect_and_timeout_never_retry(self):
        for timeout in [False, True]:
            calls = []
            def handler(request):
                calls.append(request)
                if timeout:
                    raise httpx.ReadTimeout("SECRET", request=request)
                return httpx.Response(302, headers={"Location": "https://other.invalid"})
            with httpx.Client(transport=httpx.MockTransport(handler)) as client, self.assertRaises(ConnectorError) as c:
                NVTConnector(client).authorize("SYNTHETIC.COOKIE", LOGIN)
            self.assertEqual(len(calls), 1)
            self.assertTrue(c.exception.ambiguous)

    def test_session_html_response_and_size(self):
        for status, raw, code in [(401,b'<html>login</html>',"CONNECTOR_SESSION_EXPIRED"),
                                  (200,b'not-json',"NON_JSON_RESPONSE"),
                                  (200,b'x'*(4*1024*1024+1),"RESULT_TOO_LARGE")]:
            with self.subTest(code=code), self.assertRaises(ConnectorError) as c:
                parse_response(status, raw, LOGIN["account"])
            self.assertEqual(c.exception.code, code)

    def test_rate_limit_html_or_json_always_pauses_connector(self):
        for body in [b'<html>Too many</html>', b'{"error":"busy","code":"RATE_LIMITED"}']:
            with self.assertRaises(ConnectorError) as c:
                parse_response(429, body, LOGIN["account"])
            self.assertEqual(c.exception.code, "CONNECTOR_RATE_LIMITED")


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Vault(self.temp.name)
        self.vault.unlock(PASSWORD, setup=True)

    def tearDown(self):
        self.vault.close()
        self.temp.cleanup()

    def add(self):
        self.vault.import_materials(parse_batch(LINE), DEFAULT_PROFILE)
        return self.vault.accounts()[0]["id"]

    def test_roundtrip_restart_and_no_plaintext_on_disk(self):
        account_id = self.add()
        self.vault.set_setting("connection", {"nvt_cookie": "SYNTHETIC.COOKIE"})
        self.vault.lock()
        with self.assertRaises(VaultError):
            self.vault.account(account_id)
        with self.assertRaises(VaultError):
            self.vault.unlock("wrong-password-long")
        self.vault.unlock(PASSWORD)
        self.assertEqual(self.vault.account(account_id)["login"], LOGIN)
        for path in Path(self.temp.name).glob('vault.sqlite3*'):
            raw = path.read_bytes()
            for secret in [PASSWORD, *LOGIN.values(), "SYNTHETIC.COOKIE"]:
                self.assertNotIn(secret.encode(), raw)

    def test_dedupe_conflicts_not_overwritten(self):
        account_id = self.add()
        r = self.vault.import_materials(parse_batch(LINE), DEFAULT_PROFILE)
        self.assertEqual(r["added"], 0)
        self.assertEqual(r["duplicate_lines"], [1])
        r = self.vault.import_materials(parse_batch(LINE.replace("SYNTHETIC_PASSWORD", "CHANGED")), DEFAULT_PROFILE)
        self.assertEqual(r["conflict_lines"], [1])
        self.assertEqual(self.vault.account(account_id)["login"]["password"], LOGIN["password"])

    def test_queue_unique_and_budget_and_unknown(self):
        account_id = self.add()
        self.assertEqual(len(self.vault.queue([account_id])), 1)
        self.assertEqual(self.vault.queue([account_id]), [])
        first = self.vault.claim()
        with self.assertRaises(VaultError):
            self.vault.lock()
        self.vault.finish(first["id"], "failed", "INCORRECT_CODE")
        self.vault.queue([account_id])
        second = self.vault.claim()
        self.vault.finish(second["id"], "failed", "INCORRECT_CODE")
        self.assertEqual(len(self.vault.queue([account_id])), 1)

    def test_interrupted_job_becomes_unknown_not_replayed(self):
        account_id = self.add()
        self.vault.queue([account_id])
        self.vault.claim()
        self.vault.close()
        self.vault = Vault(self.temp.name)
        self.vault.unlock(PASSWORD)
        self.assertEqual(self.vault.jobs()[0]["state"], "unknown")
        self.assertIsNone(self.vault.claim())
        self.assertEqual(self.vault.account(account_id)["status"], "unknown")
        with self.assertRaisesRegex(VaultError, "REVIEW"):
            self.vault.queue([account_id])

    def test_aead_binds_record_id(self):
        account_id = self.add()
        payload = self.vault.db.execute("SELECT payload FROM accounts WHERE id=?", (account_id,)).fetchone()[0]
        with self.assertRaises(VaultError):
            self.vault._open("account:different-id", payload)

    def test_pending_write_disallows_new_authorization(self):
        account_id = self.add()
        self.vault.update_account(account_id, write_intent={"state":"unknown"})
        with self.assertRaisesRegex(VaultError, "RECONCILIATION"):
            self.vault.queue([account_id])


class GUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, token="SYNTHETIC-LOCAL-TOKEN", start_worker=False)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8765", headers={"x-local-token":"SYNTHETIC-LOCAL-TOKEN"})
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None,None,None)
        self.temp.cleanup()

    def unlock(self):
        r = self.client.post('/api/unlock',json={"password":PASSWORD,"setup":True})
        self.assertEqual(r.status_code,200,r.text)

    def add(self):
        r=self.client.post('/api/import',json={"text":LINE,"profile":DEFAULT_PROFILE})
        self.assertEqual(r.status_code,200,r.text)
        return self.client.get('/api/state').json()["accounts"][0]["id"]

    def test_loopback_csrf_token_and_lock_boundaries(self):
        self.assertEqual(self.client.get('/').status_code,200)
        self.assertNotIn('SYNTHETIC-LOCAL-TOKEN',self.client.get('/').text)
        self.assertEqual(self.client.get('/api/status',headers={"x-local-token":"bad"}).status_code,401)
        self.assertEqual(self.client.get('/api/status',headers={"Host":"attacker.invalid"}).status_code,403)
        self.assertEqual(self.client.get('/api/status',headers={"Origin":"https://attacker.invalid"}).status_code,403)
        self.assertEqual(self.client.post('/api/unlock',content='{}',headers={"Content-Type":"text/plain"}).status_code,415)
        self.assertEqual(self.client.get('/api/state').status_code,400)

    def test_import_settings_profile_api_without_network(self):
        self.unlock()
        self.client.post('/api/settings',json={"nvt_cookie":"SYNTHETIC.COOKIE","admin_key":"SYNTHETIC-ADMIN"})
        account_id=self.add()
        response=self.client.get('/api/state')
        self.assertEqual(response.status_code,200)
        for secret in [PASSWORD,LOGIN['password'],LOGIN['totp_secret'],'SYNTHETIC.COOKIE','SYNTHETIC-ADMIN']:
            self.assertNotIn(secret,response.text)
        self.assertEqual(response.json()["accounts"][0]["id"],account_id)
        self.assertEqual(response.json()["accounts"][0]["label"],LOGIN["account"])
        self.assertTrue(response.json()["settings"]["has_cookie"])
        self.assertIn("frame-ancestors 'none'",response.headers['content-security-policy'])

    def test_save_cookie_only_preserves_other_connection_fields(self):
        self.unlock()
        r = self.client.post('/api/settings', json={'admin_key': 'SYNTHETIC-ADMIN',
                'sub2api_url': 'https://example.invalid', 'oauth_client_id': 'known-client'})
        self.assertEqual(r.status_code, 200)
        r = self.client.post('/api/settings', json={'nvt_cookie': 'SYNTHETIC.COOKIE'})
        self.assertEqual(r.status_code, 200)
        s = self.client.get('/api/state').json()['settings']
        self.assertEqual(s['oauth_client_id'], 'known-client')
        self.assertEqual(s['sub2api_url'], 'https://example.invalid')
        self.assertTrue(s['has_admin_key'] and s['has_cookie'])

    def test_export_unwraps_account_json_for_sub2api_import(self):
        self.unlock(); account_id = self.add()
        payload = nvt_success()
        self.app.state.vault.update_account(account_id, status='review', raw_result=payload)
        r = self.client.get(f'/api/accounts/{account_id}/export')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), payload['account_json'])
        self.assertNotIn('filename', r.json())
        self.assertNotIn('summary', r.json())

    def test_revalidate_previously_stored_nvt_envelope_without_login(self):
        self.unlock(); account_id = self.add()
        self.app.state.vault.update_account(account_id, status='review', raw_result=nvt_success())
        with patch.object(self.app.state.service.connector, 'authorize') as call:
            r = self.client.post(f'/api/accounts/{account_id}/revalidate', json={})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()['validated'], r.text)
            call.assert_not_called()

    def test_queue_requires_explicit_send_confirmation(self):
        self.unlock(); account_id=self.add()
        self.client.post('/api/settings',json={"nvt_cookie":"SYNTHETIC.COOKIE"})
        response=self.client.post('/api/jobs',json={"account_ids":[account_id]})
        self.assertEqual(response.status_code,400)
        response=self.client.post('/api/jobs',json={"account_ids":[account_id],"confirm_send_to_nvtokens":True})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(response.json()['queued']),1)

    def test_revalidate_saved_result_without_new_login(self):
        self.unlock(); account_id=self.add();v=self.app.state.vault
        raw=exported();del raw['accounts'][0]['credentials']['client_id']
        v.update_account(account_id,status='review',raw_result=raw)
        self.client.post('/api/settings',json={'oauth_client_id':'known-client'})
        with patch.object(self.app.state.service.connector,'authorize') as call:
            r=self.client.post(f'/api/accounts/{account_id}/revalidate',json={})
            self.assertEqual(r.status_code,200,r.text)
            self.assertTrue(r.json()['validated'])
            call.assert_not_called()
        self.assertEqual(v.account(account_id)['status'],'authorized')

    def test_reunlock_cannot_clear_active_master_key(self):
        self.unlock()
        r=self.client.post('/api/unlock',json={'password':'wrong-but-long-enough'})
        self.assertEqual(r.json()['code'],'VAULT_ALREADY_UNLOCKED')
        self.assertEqual(self.client.get('/api/state').status_code,200)

    def test_worker_success_only_stages_result_no_cloud_write(self):
        self.unlock(); account_id=self.add()
        self.client.post('/api/settings',json={"nvt_cookie":"SYNTHETIC.COOKIE"})
        self.app.state.vault.queue([account_id])
        result=parse_response(200,json.dumps(exported()).encode(),LOGIN['account'])
        with patch.object(self.app.state.service.connector,'authorize',return_value=result) as call:
            self.assertTrue(self.app.state.service.run_one())
            call.assert_called_once()
        a=self.app.state.vault.account(account_id)
        self.assertEqual(a['status'],'authorized')
        self.assertIsNone(a['binding'])
        response=self.client.get('/api/state')
        self.assertNotIn('SYNTHETIC_ACCESS',response.text)
        response=self.client.get(f'/api/accounts/{account_id}/export')
        self.assertIn('SYNTHETIC_ACCESS',response.text)

    def test_worker_documented_code_failure_and_connector_pause(self):
        self.unlock(); account_id=self.add()
        self.client.post('/api/settings',json={"nvt_cookie":"SYNTHETIC.COOKIE"})
        self.app.state.vault.queue([account_id])
        with patch.object(self.app.state.service.connector,'authorize',side_effect=ConnectorError('INCORRECT_CODE','protocol_login')):
            self.app.state.service.run_one()
        self.assertEqual(self.app.state.vault.jobs()[0]['code'],'INCORRECT_CODE')
        self.assertIsNone(self.app.state.vault.claim())
        self.app.state.vault.queue([account_id])
        with patch.object(self.app.state.service.connector,'authorize',side_effect=ConnectorError('CONNECTOR_SESSION_EXPIRED')):
            self.app.state.service.run_one()
        self.assertTrue(self.app.state.vault.get_setting('connection')['connector_paused'])

    def test_cloud_create_stops_in_staging_and_saves_id_before_pause(self):
        self.unlock(); account_id=self.add()
        v=self.app.state.vault;service=self.app.state.service
        v.set_setting('connection',{'sub2api_url':'https://example.invalid','admin_key':'fake'})
        auth=parse_response(200,json.dumps(exported()).encode(),LOGIN['account']).authorization
        v.update_account(account_id,status='authorized',authorization=auth)
        calls=[]
        def write(path,body,key=None):
            calls.append((path,body,key))
            if path=='/accounts':
                return {'id':42}
            self.assertEqual(v.account(account_id)['binding']['cloud_id'],42)
            return cloud()
        with patch.object(service,'cloud_read',return_value=cloud()),patch.object(service,'cloud_write',side_effect=write):
            service.write_credentials(account_id,True)
        self.assertEqual(calls[0][1]['group_ids'],[9001])
        self.assertEqual(calls[0][1]['extra'],{'codex_fingerprint_mode':'off'})
        self.assertTrue(calls[0][2])
        self.assertEqual(calls[1][1],{'schedulable':False})
        self.assertEqual(v.account(account_id)['status'],'cloud_paused')

    def test_cloud_reauth_never_recreates_or_changes_extra(self):
        self.unlock();account_id=self.add();v=self.app.state.vault;s=self.app.state.service
        v.set_setting('connection',{'sub2api_url':'https://example.invalid','admin_key':'fake'})
        auth=parse_response(200,json.dumps(exported()).encode(),LOGIN['account']).authorization
        v.update_account(account_id,status='authorized',authorization=auth,binding={
            'cloud_id':42,'instance':'https://example.invalid/api/v1/admin','identity':auth['identity']})
        with patch.object(s,'cloud_read',return_value=cloud()),patch.object(s,'cloud_write',return_value=cloud()) as write:
            s.write_credentials(account_id)
        self.assertEqual(write.call_args.args[0],'/accounts/42/apply-oauth-credentials')
        body=write.call_args.args[1]
        self.assertEqual(set(body),{'type','credentials'})
        self.assertEqual(body['credentials']['model_mapping'],{'a':'b'})

    def test_ambiguous_cloud_write_not_replayed(self):
        self.unlock();account_id=self.add();v=self.app.state.vault;s=self.app.state.service
        v.set_setting('connection',{'sub2api_url':'https://example.invalid','admin_key':'fake'})
        v.update_account(account_id,status='authorized',authorization=parse_response(200,json.dumps(exported()).encode(),LOGIN['account']).authorization)
        with patch.object(s,'cloud_read',return_value=cloud()),patch.object(s,'cloud_write',side_effect=VaultError('CLOUD_WRITE_RESULT_UNKNOWN')):
            with self.assertRaises(VaultError):s.write_credentials(account_id,True)
            with self.assertRaisesRegex(VaultError,'RECONCILIATION'):s.write_credentials(account_id,True)
        self.assertEqual(v.account(account_id)['status'],'write_unknown')


if __name__=='__main__':
    unittest.main()
