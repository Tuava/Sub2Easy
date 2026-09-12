from copy import deepcopy
from datetime import datetime, timezone
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from sub2easy.cloud_options import fetch_options
from sub2easy.gui import create_app, DEFAULT_PROFILE
from sub2easy.preflight import PreflightError


GROUPS = [
    {"id": 11, "name": "OpenAI 隔离", "platform": "openai", "status": "active"},
    {"id": 22, "name": "OpenAI 生产 A", "platform": "openai", "status": "active"},
    {"id": 33, "name": "OpenAI 生产 B", "platform": "openai", "status": "active"},
    {"id": 44, "name": "停用", "platform": "openai", "status": "inactive"},
    {"id": 55, "name": "Claude", "platform": "anthropic", "status": "active"},
]
PROXIES = [
    {"id": 7, "name": "出口 A", "status": "active", "expires_at": None,
     "password": "SECRET_PROXY_PASSWORD", "username": "SECRET_USER", "host": "PRIVATE_HOST"},
    {"id": 8, "name": "停用代理", "status": "inactive"},
    {"id": 9, "name": "已过期", "status": "active", "expires_at": "2020-01-01T00:00:00Z"},
    {"id": 10, "name": "出口 B", "status": "active", "expires_at": "2099-01-01T00:00:00Z"},
]


class OptionsTests(unittest.TestCase):
    def test_verified_all_endpoints_and_whitelist(self):
        client = Mock();client.get.side_effect = [GROUPS, PROXIES]
        result = fetch_options(client, datetime(2026, 9, 11, tzinfo=timezone.utc))
        self.assertEqual(client.get.call_args_list[0].args, ("/groups/all", {"platform": "openai"}))
        self.assertEqual(client.get.call_args_list[1].args, ("/proxies/all",))
        self.assertEqual({g['id'] for g in result['groups']}, {11, 22, 33})
        self.assertEqual({p['id'] for p in result['proxies']}, {7, 10})
        for value in ['SECRET', 'PRIVATE_HOST', 'password', 'username']:
            self.assertNotIn(value, json.dumps(result))

    def test_empty_lists_are_valid_not_fake_defaults(self):
        client = Mock();client.get.side_effect = [[], []]
        self.assertEqual(fetch_options(client), {'groups': [], 'proxies': []})

    def test_unpaginated_all_endpoint_does_not_accept_partial_page(self):
        client = Mock();client.get.return_value = {'items': GROUPS, 'total': 999}
        with self.assertRaises(PreflightError):fetch_options(client)

    def test_invalid_or_repeated_rows_rejected(self):
        for rows in [[GROUPS[0],GROUPS[0]], [{'id':True,'name':'x','status':'active'}],
                     [{'id':1,'name':{},'status':'active'}], [{'id':1,'name':'missing status'}]]:
            with self.subTest(rows=rows):
                client = Mock();client.get.side_effect = [rows, []]
                with self.assertRaises(PreflightError):fetch_options(client)

    def test_one_endpoint_failure_does_not_return_partial_choices(self):
        client = Mock();client.get.side_effect = [GROUPS, PreflightError('failure')]
        with self.assertRaises(PreflightError):fetch_options(client)


class OptionsAPITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=create_app(self.temp.name,token='TEST-LOCAL-TOKEN',start_worker=False)
        self.client=TestClient(self.app,base_url='http://127.0.0.1:8765',headers={'x-local-token':'TEST-LOCAL-TOKEN'})
        self.client.__enter__()
        self.client.post('/api/unlock',json={'password':'test-vault-password','setup':True})

    def tearDown(self):
        self.client.__exit__(None,None,None);self.temp.cleanup()

    def configure(self):
        r=self.client.post('/api/settings',json={'sub2api_url':'https://site-a.invalid','admin_key':'SYNTHETIC-ADMIN'})
        self.assertEqual(r.status_code,200)
        return self.client.get('/api/state').json()['settings']['cloud_revision']

    def profile(self, revision):
        return {**DEFAULT_PROFILE,'instance_id':'https://site-a.invalid/api/v1/admin',
                'staging_group_id':11,'target_group_ids':[22,33],'proxy_id':7,'connection_revision':revision}

    def test_requires_config_and_unlocked_session(self):
        self.assertEqual(self.client.post('/api/cloud/options',json={}).json()['code'],'SUB2API_NOT_CONFIGURED')
        self.client.post('/api/lock',json={})
        self.assertEqual(self.client.post('/api/cloud/options',json={}).json()['code'],'VAULT_LOCKED')

    def test_choices_read_saved_credentials_no_external_mutations(self):
        revision=self.configure()
        with patch('sub2easy.gui.Client') as Client:
            Client.return_value.get.side_effect=[GROUPS,PROXIES]
            r=self.client.post('/api/cloud/options',json={})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['connection_revision'],revision)
        self.assertEqual(r.json()['instance_id'],'https://site-a.invalid/api/v1/admin')
        Client.assert_called_once_with('https://site-a.invalid','SYNTHETIC-ADMIN')
        self.assertNotIn('SYNTHETIC-ADMIN',r.text)
        self.assertNotIn('SECRET_PROXY',r.text)

    def test_profile_saves_multiple_groups_and_enforces_server_revision(self):
        revision=self.configure();data=self.profile(revision);data['revision']=999
        with patch('sub2easy.gui.Client') as Client:
            Client.return_value.get.side_effect=[GROUPS,PROXIES]
            r=self.client.post('/api/profile',json=data)
        self.assertEqual(r.status_code,200,r.text)
        p=self.client.get('/api/state').json()['profile']
        self.assertEqual(p['target_group_ids'],[22,33])
        self.assertEqual(p['proxy_id'],7)
        self.assertEqual(p['revision'],2)
        self.assertEqual(p['instance_id'],'https://site-a.invalid/api/v1/admin')
        self.assertNotIn('connection_revision',p)

    def test_direct_connection_is_valid_choice(self):
        revision=self.configure();data=self.profile(revision);data['proxy_id']=None
        with patch('sub2easy.gui.Client') as Client:
            Client.return_value.get.side_effect=[GROUPS,[]]
            r=self.client.post('/api/profile',json=data)
        self.assertEqual(r.status_code,200,r.text)
        self.assertIsNone(r.json()['profile']['proxy_id'])

    def test_deleted_wrong_platform_or_inactive_group_not_saved(self):
        revision=self.configure()
        for target in [999,44,55]:
            data=self.profile(revision);data['target_group_ids']=[target]
            with self.subTest(target=target),patch('sub2easy.gui.Client') as Client:
                Client.return_value.get.side_effect=[GROUPS,PROXIES]
                r=self.client.post('/api/profile',json=data)
            self.assertEqual(r.json()['code'],'SELECTED_GROUP_UNAVAILABLE')

    def test_expired_proxy_not_silently_downgraded_to_direct(self):
        revision=self.configure();data=self.profile(revision);data['proxy_id']=9
        with patch('sub2easy.gui.Client') as Client:
            Client.return_value.get.side_effect=[GROUPS,PROXIES]
            r=self.client.post('/api/profile',json=data)
        self.assertEqual(r.json()['code'],'SELECTED_PROXY_UNAVAILABLE')

    def test_same_group_cannot_be_staging_and_production(self):
        revision=self.configure();data=self.profile(revision);data['target_group_ids']=[11]
        r=self.client.post('/api/profile',json=data)
        self.assertEqual(r.json()['code'],'INVALID_PROFILE_GROUPS')

    def test_site_switch_and_key_change_invalidate_old_choices(self):
        revision=self.configure();data=self.profile(revision)
        self.client.post('/api/settings',json={'admin_key':'DIFFERENT-ADMIN'})
        self.assertEqual(self.client.post('/api/profile',json=data).json()['code'],'CLOUD_CHOICES_STALE')
        self.client.post('/api/settings',json={'sub2api_url':'https://site-b.invalid'})
        self.assertEqual(self.client.post('/api/profile',json=data).json()['code'],'PROFILE_INSTANCE_MISMATCH')

    def test_cookie_only_save_does_not_invalidate_cloud_choices(self):
        revision=self.configure()
        self.client.post('/api/settings',json={'nvt_cookie':'SYNTHETIC.COOKIE'})
        self.assertEqual(self.client.get('/api/state').json()['settings']['cloud_revision'],revision)

    def test_api_failure_has_no_secrets_and_does_not_replace_profile(self):
        revision=self.configure()
        before=deepcopy(self.client.get('/api/state').json()['profile'])
        with patch('sub2easy.gui.Client') as Client:
            Client.return_value.get.side_effect=PreflightError('failure')
            r=self.client.post('/api/profile',json=self.profile(revision))
        self.assertEqual(r.json()['code'],'SUB2API_CONNECTION_FAILED')
        self.assertEqual(self.client.get('/api/state').json()['profile'],before)


if __name__=='__main__':unittest.main()
