from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest

from sub2easy.lifecycle import (
    AuthIncident, AuthorizationResult, ContractError, ImportProfile, OAuthIdentity,
    configuration_drift, decide_auth_incident, normalize_sub2json, plan_create, plan_reauthorize,
)


IDENTITY = OAuthIdentity("demo@example.invalid", "workspace-demo", "user-demo")


def auth_item(**extra_fields):
    return {
        "platform": "openai", "type": "oauth",
        "credentials": {
            "access_token": "SYNTHETIC-ACCESS", "refresh_token": "SYNTHETIC-REFRESH",
            "id_token": "SYNTHETIC-IDTOKEN", "client_id": "SYNTHETIC-CLIENT",
            "email": IDENTITY.account_email, "chatgpt_account_id": IDENTITY.chatgpt_account_id,
            "chatgpt_user_id": IDENTITY.chatgpt_user_id,
            "expires_at": "2099-01-01T00:00:00Z", **extra_fields,
        },
        "extra": {"codex_fingerprint_mode": "full", "codex_fingerprint_seed": "UNTRUSTED-SEED"},
        "group_ids": [999], "proxy_id": 999, "concurrency": 999,
    }


def cloud_account(**updates):
    return {
        "id": 42, "platform": "openai", "type": "oauth", "status": "error", "schedulable": False,
        "credentials": {
            "email": IDENTITY.account_email, "chatgpt_account_id": IDENTITY.chatgpt_account_id,
            "chatgpt_user_id": IDENTITY.chatgpt_user_id,
            "model_mapping": {"request-model": "actual-model"},
            "base_url": "https://example.invalid", "client_id": "OLD-CLIENT",
            "access_token": "DO-NOT-REPLAY-OLD-TOKEN",
            "password": "DO-NOT-SEND-PASSWORD", "totp_secret": "DO-NOT-SEND-TOTP",
        },
        "group_ids": [1001], "proxy_id": 7, "concurrency": 4, "rate_multiplier": 0.8,
        "extra": {"codex_fingerprint_mode": "device", "quota_limit": 100}, **updates,
    }


def result(item=None):
    return normalize_sub2json(item or auth_item(), IDENTITY.account_email, IDENTITY)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "examples/import-profile.json"
        self.data = json.loads(path.read_text())

    def test_load_revisioned_profile(self):
        profile = ImportProfile.from_dict(self.data)
        self.assertEqual(profile.target_group_ids, (1001,))
        self.assertEqual(profile.fingerprint_mode, "off")

    def test_all_supported_modes(self):
        for mode in ["off", "device", "session", "full"]:
            profile = ImportProfile.from_dict({**self.data, "fingerprint_mode": mode})
            plan = plan_create(result(), profile, "uuid-local")
            self.assertEqual(plan.body["extra"], {"codex_fingerprint_mode": mode})
            self.assertNotIn("codex_fingerprint_seed", plan.body["extra"])

    def test_profile_rejects_incompatible_or_ambiguous_config(self):
        for update in [
            {"fingerprint_mode": "random"}, {"platform": "anthropic"},
            {"staging_group_id": 1001}, {"target_group_ids": []}, {"concurrency": True},
            {"rate_multiplier": float("nan")}, {"revision": 0}, {"unknown": True},
        ]:
            with self.subTest(update=update), self.assertRaises(ContractError):
                ImportProfile.from_dict({**self.data, **update})

    def test_create_uses_profile_not_vendor_settings_and_not_production_group(self):
        profile = ImportProfile.from_dict(self.data)
        request = plan_create(result(), profile, "uuid-local")
        self.assertEqual(request.body["group_ids"], [9001])
        self.assertEqual(request.body["concurrency"], 1)
        self.assertIsNone(request.body["proxy_id"])
        self.assertNotIn("schedulable", request.body)  # Unsupported create parameter.
        self.assertIn("verified_staging_route_isolation", request.preconditions)


class AuthorizationTests(unittest.TestCase):
    def test_single_account_bundle(self):
        bundle = {"type": "sub2api-data", "version": 1, "accounts": [auth_item()], "proxies": []}
        normalized = result(bundle)
        self.assertEqual(normalized.identity, IDENTITY)
        self.assertTrue(normalized.credentials["expires_at"].isdigit())

    def test_nested_unconfigured_envelope_not_guessed(self):
        with self.assertRaises(ContractError):
            result({"data": auth_item()})

    def test_reject_multi_account_or_wrong_version(self):
        for update in [{"accounts": [auth_item(), auth_item()]}, {"version": 2}, {"accounts": []}]:
            bundle = {"type": "sub2api-data", "version": 1, "accounts": [auth_item()], **update}
            with self.subTest(update=update), self.assertRaises(ContractError):
                result(bundle)

    def test_identity_mismatch_email_workspace_or_user(self):
        for update in [{"email": "other@example.invalid"}, {"chatgpt_account_id": "other-workspace"},
                       {"chatgpt_user_id": "other-user"}]:
            with self.subTest(update=update), self.assertRaises(ContractError):
                result(auth_item(**update))

    def test_auth_service_cannot_override_configuration_or_send_login_secrets(self):
        normalized = result(auth_item(password="DO-NOT-SEND", totp_secret="DO-NOT-SEND",
                                      base_url="https://vendor.invalid", model_mapping={"a": "b"}))
        for key in ["password", "totp_secret", "base_url", "model_mapping", "extra"]:
            self.assertNotIn(key, normalized.credentials)

    def test_missing_or_empty_required_fields(self):
        for key in ["access_token", "refresh_token", "client_id", "expires_at", "chatgpt_account_id", "email"]:
            item = auth_item()
            del item["credentials"][key]
            with self.subTest(key=key), self.assertRaises(ContractError):
                result(item)
        for update in [{"access_token": ""}, {"refresh_token": "***"}, {"client_id": "with space"}]:
            with self.subTest(update=update), self.assertRaises(ContractError):
                result(auth_item(**update))

    def test_token_expiry_not_account_expiry(self):
        item = auth_item(expires_at="2099-01-01T00:00:00Z")
        item["expires_at"] = 1  # Top-level account retirement time is NOT token expiry.
        self.assertIn("access_token", result(item).credentials)
        with self.assertRaisesRegex(ContractError, "TOKEN_EXPIRED"):
            result(auth_item(expires_at="2020-01-01T00:00:00Z"))

    def test_close_to_expiry_and_epoch_string(self):
        now = datetime(2026, 9, 11, tzinfo=timezone.utc)
        with self.assertRaises(ContractError):
            normalize_sub2json(auth_item(expires_at=str(int(now.timestamp()) + 59)),
                               IDENTITY.account_email, IDENTITY, now)
        valid = normalize_sub2json(auth_item(expires_at=str(int(now.timestamp()) + 3600)),
                                  IDENTITY.account_email, IDENTITY, now)
        self.assertEqual(valid.credentials["expires_at"], str(int(now.timestamp()) + 3600))

    def test_reauthorize_updates_original_id_only_and_preserves_cloud_credential_config(self):
        cloud = cloud_account()
        snapshot = deepcopy(cloud)
        request = plan_reauthorize(cloud, result(), 42, IDENTITY)
        self.assertEqual(request.path, "/api/v1/admin/accounts/42/apply-oauth-credentials")
        self.assertEqual(set(request.body), {"type", "credentials"})
        self.assertEqual(request.body["credentials"]["model_mapping"], {"request-model": "actual-model"})
        self.assertEqual(request.body["credentials"]["base_url"], "https://example.invalid")
        self.assertEqual(request.body["credentials"]["client_id"], "SYNTHETIC-CLIENT")
        self.assertNotIn("DO-NOT", json.dumps(request.body))
        self.assertEqual(cloud, snapshot)

    def test_reauthorize_requires_matching_id_quiescence_and_not_manual_inactive_or_shadow(self):
        for updates in [{"id": 43}, {"schedulable": True}, {"status": "inactive"},
                        {"parent_account_id": 1}, {"platform": "anthropic"}, {"credentials": {}}]:
            with self.subTest(updates=updates), self.assertRaises(ContractError):
                plan_reauthorize(cloud_account(**updates), result(), 42, IDENTITY)

    def test_wrong_result_cannot_be_assigned_by_batch_order(self):
        item = auth_item(chatgpt_account_id="workspace-other")
        other = normalize_sub2json(item, IDENTITY.account_email)
        with self.assertRaises(ContractError):
            plan_reauthorize(cloud_account(), other, 42, IDENTITY)

    def test_constructed_result_is_revalidated(self):
        invalid = AuthorizationResult(IDENTITY, {"access_token": "fake"})
        with self.assertRaises(ContractError):
            plan_reauthorize(cloud_account(), invalid, 42, IDENTITY)

    def test_repr_does_not_expose_identity_tokens_or_body(self):
        normalized = result()
        request = plan_reauthorize(cloud_account(), normalized, 42, IDENTITY)
        output = repr(normalized) + repr(request)
        for value in [IDENTITY.account_email, IDENTITY.chatgpt_account_id, "SYNTHETIC-ACCESS"]:
            self.assertNotIn(value, output)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.incident = AuthIncident("upstream", 401, "revoked", managed=True, has_login_material=True)

    def test_upstream_401_can_queue_reauth(self):
        self.assertEqual(decide_auth_incident(self.incident), "QUEUE_REAUTH")

    def test_three_401_sources_have_distinct_actions(self):
        self.assertEqual(decide_auth_incident(replace(self.incident, source="admin_api")), "PAUSE_INSTANCE_WRITES")
        self.assertEqual(decide_auth_incident(replace(self.incident, source="reauth_api")), "PAUSE_REAUTH_CONNECTOR")

    def test_inflight_and_stale_and_native_refresh(self):
        for update, expected in [
            ({"job_inflight": True}, "JOIN_EXISTING_JOB"),
            ({"fresh": False}, "IGNORE_STALE_EVIDENCE"),
            ({"refresh_state": "pending"}, "WAIT_NATIVE_REFRESH"),
            ({"provider_degraded": True}, "WAIT_PROVIDER_RECOVERY"),
        ]:
            with self.subTest(update=update):
                self.assertEqual(decide_auth_incident(replace(self.incident, **update)), expected)

    def test_missing_material_or_manual_hold_or_retry_budget(self):
        for update, expected in [
            ({"has_login_material": False}, "NEEDS_LOGIN_MATERIAL"),
            ({"manual_hold": True}, "OBSERVE_ONLY"),
            ({"managed": False}, "OBSERVE_ONLY"),
            ({"reauth_attempts_in_window": 2}, "NEEDS_OPERATOR"),
            ({"http_status": 429}, "NOT_AN_AUTH_401"),
        ]:
            with self.subTest(update=update):
                self.assertEqual(decide_auth_incident(replace(self.incident, **update)), expected)

    def test_three_way_configuration_diff(self):
        baseline = {"priority": 50, "concurrency": 1, "rate_multiplier": 1}
        observed = {"priority": 10, "concurrency": 1, "rate_multiplier": 0.8}
        desired = {"priority": 50, "concurrency": 3, "rate_multiplier": 0.8, "new_field": 5}
        self.assertEqual(configuration_drift(baseline, observed, desired), [
            {"field": "priority", "kind": "external_change_conflict"},
            {"field": "concurrency", "kind": "template_change_pending"},
            {"field": "new_field", "kind": "unknown"},
        ])


if __name__ == "__main__":
    unittest.main()
