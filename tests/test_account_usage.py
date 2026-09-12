from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from sub2easy.account_usage import (
    MAX_ACCOUNTS, MAX_EXTRA_FIELDS, MAX_RESPONSE_FIELDS, MAX_SAFE_INTEGER,
    UsageError, collect_usage, normalize_account_usage, normalize_today_stats,
)
from sub2easy.preflight import Client, PreflightError


NOW = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
SECRET = "NEVER_RETURN_SYNTHETIC_SECRET"
STATS = {"requests": 3, "tokens": 1204, "cost": 0.025, "standard_cost": 0.01, "user_cost": 0.02}


def account(aid=1, **changes):
    data = {
        "id": aid, "platform": "openai", "type": "oauth",
        "credentials": {"access_token": SECRET, "refresh_token": SECRET},
        "name": SECRET, "error_message": SECRET, "proxy": {"password": SECRET},
        "extra": {
            "codex_5h_used_percent": 25,
            "codex_5h_reset_at": (NOW + timedelta(hours=1)).isoformat(),
            "codex_7d_used_percent": 60.5,
            "codex_7d_reset_after_seconds": 86400,
            "codex_usage_updated_at": (NOW - timedelta(seconds=60)).isoformat(),
            "unrelated_secret": SECRET,
        },
    }
    data.update(changes)
    return data


class FakeClient:
    def __init__(self, replies=None):
        self.replies = replies or {}
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        value = self.replies[path]
        if isinstance(value, Exception):
            raise value
        return value


class NormalizationTests(unittest.TestCase):
    def test_two_windows_and_costs_have_verified_units(self):
        row = normalize_account_usage(account(), STATS, now=NOW)
        self.assertEqual(row["status"], "ok")
        five, seven = row["windows"]
        self.assertEqual(five["key"], "five_hour")
        self.assertEqual(five["used_percent"], 25)
        self.assertEqual(five["remaining"], 75)
        self.assertEqual(five["remaining_unit"], "percent")
        self.assertEqual(five["remaining_seconds"], 3600)
        self.assertEqual(five["freshness"], "recent")
        self.assertEqual(seven["remaining_seconds"], 86400 - 60)
        self.assertEqual(seven["used_percent"], 60.5)
        for key, value in STATS.items():
            self.assertEqual(row["today"][key], value)
        self.assertIsNone(row["today"]["timezone"])
        self.assertIsNone(row["today"]["updated_at"])
        self.assertEqual(row["today"]["freshness"], "unknown")
        self.assertEqual(row["snapshot_origin"], "unknown")

    def test_whitelist_and_purity(self):
        raw, stats = account(), {**STATS, "error": SECRET, "credentials": SECRET}
        before = deepcopy((raw, stats))
        result = normalize_account_usage(raw, stats, now=NOW)
        self.assertEqual((raw, stats), before)
        text = json.dumps(result, allow_nan=False)
        for marker in (SECRET, "credentials", "refresh_token", "proxy", "error_message"):
            self.assertNotIn(marker, text)

    def test_missing_data_is_unknown_not_zero_or_unlimited(self):
        row = normalize_account_usage(account(extra={}), now=NOW)
        self.assertEqual(row["status"], "unknown")
        for window in row["windows"]:
            for key in ("used_percent", "remaining", "reset_at", "remaining_seconds", "updated_at"):
                self.assertIsNone(window[key])
            self.assertEqual(window["freshness"], "unknown")
        for key in STATS:
            self.assertIsNone(row["today"][key])

    def test_observed_zero_and_overage_not_missing_or_inverted(self):
        raw = account()
        raw["extra"].update(codex_5h_used_percent=0, codex_7d_used_percent=150)
        row = normalize_account_usage(raw, {key: 0 for key in STATS}, now=NOW)
        five, seven = row["windows"]
        self.assertEqual((five["used_percent"], five["remaining"]), (0, 100))
        self.assertEqual((seven["used_percent"], seven["remaining"]), (150, 0))
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["today"]["requests"], 0)

    def test_expired_window_does_not_claim_reset_to_zero(self):
        for delta in (0, -1, -3600):
            raw = account()
            raw["extra"]["codex_5h_reset_at"] = (NOW + timedelta(seconds=delta)).isoformat()
            window = normalize_account_usage(raw, STATS, now=NOW)["windows"][0]
            self.assertEqual(window["freshness"], "expired")
            self.assertEqual(window["sampled_used_percent"], 25)
            self.assertIsNone(window["used_percent"])
            self.assertIsNone(window["remaining"])
            self.assertEqual(window["remaining_seconds"], 0)

    def test_sample_age_not_fetch_time(self):
        for age, freshness in ((599, "recent"), (600, "stale"), (3600, "stale"), (-1, "unknown")):
            with self.subTest(age=age):
                raw = account()
                raw["extra"]["codex_usage_updated_at"] = (NOW - timedelta(seconds=age)).isoformat()
                row = normalize_account_usage(raw, STATS, now=NOW)
                self.assertEqual(row["windows"][0]["freshness"], freshness)
                self.assertEqual(row["windows"][0]["used_percent"], 25)
                self.assertEqual(row["status"], "ok" if freshness == "recent" else "partial")

    def test_relative_reset_requires_sample_timestamp_and_does_not_slide(self):
        raw = account()
        del raw["extra"]["codex_usage_updated_at"]
        window = normalize_account_usage(raw, STATS, now=NOW)["windows"][1]
        self.assertIsNone(window["reset_at"])
        self.assertIsNone(window["remaining_seconds"])
        raw = account()
        first = normalize_account_usage(raw, STATS, now=NOW)["windows"][1]
        later = normalize_account_usage(raw, STATS, now=NOW + timedelta(seconds=30))["windows"][1]
        self.assertEqual(first["reset_at"], later["reset_at"])
        self.assertEqual(first["remaining_seconds"] - later["remaining_seconds"], 30)

    def test_invalid_values_are_null_and_json_safe(self):
        for value in (True, False, -1, float("nan"), float("inf"), "25", SECRET, {}, [], 10**400):
            with self.subTest(kind=type(value).__name__):
                raw = account()
                raw["extra"].update(codex_5h_used_percent=value, codex_usage_updated_at=value)
                row = normalize_account_usage(raw, {key: value for key in STATS}, now=NOW)
                self.assertIsNone(row["windows"][0]["used_percent"])
                for key in STATS:
                    self.assertIsNone(row["today"][key])
                self.assertNotIn(SECRET, json.dumps(row, allow_nan=False))

    def test_invalid_or_huge_reset_never_raises_or_uses_now(self):
        for value in ("bad", "2026-09-12T12:00:00", "0001-01-01T00:00:00Z", SECRET * 100):
            raw = account()
            raw["extra"]["codex_7d_reset_at"] = value
            raw["extra"]["codex_7d_reset_after_seconds"] = MAX_SAFE_INTEGER
            self.assertIsNone(normalize_account_usage(raw, STATS, now=NOW)["windows"][1]["reset_at"])

    def test_reset_offset_is_canonicalized_and_zero_interval_expires(self):
        raw = account()
        raw["extra"]["codex_5h_reset_at"] = "2026-09-12T21:00:00+08:00"
        raw["extra"]["codex_7d_reset_after_seconds"] = 0
        windows = normalize_account_usage(raw, STATS, now=NOW)["windows"]
        self.assertEqual(windows[0]["reset_at"], "2026-09-12T13:00:00+00:00")
        self.assertEqual(windows[1]["freshness"], "expired")

    def test_non_openai_and_non_oauth_have_stats_not_fake_windows(self):
        for platform, kind in (("anthropic", "oauth"), ("gemini", "oauth"), ("openai", "apikey"), (SECRET, SECRET), ([], {})):
            row = normalize_account_usage(account(platform=platform, type=kind), STATS, now=NOW)
            self.assertEqual(row["windows"], [])
            self.assertEqual(row["status"], "partial")
            self.assertEqual(row["today"]["tokens"], 1204)
            self.assertNotIn(SECRET, json.dumps(row))

    def test_partial_stats_do_not_copy_extras_or_fill_defaults(self):
        stats = normalize_today_stats({"requests": 4, "tokens": 4.5, "cost": 0, "extra": SECRET})
        self.assertEqual(stats["requests"], 4)
        self.assertIsNone(stats["tokens"])
        self.assertEqual(stats["cost"], 0)
        self.assertIsNone(stats["standard_cost"])
        self.assertIsNone(stats["user_cost"])

    def test_malformed_and_oversize_extra_does_not_block_good_today(self):
        for extra in ([], SECRET, {str(i): SECRET for i in range(MAX_EXTRA_FIELDS + 1)}):
            row = normalize_account_usage(account(extra=extra), STATS, now=NOW)
            self.assertEqual(row["today"]["requests"], 3)
            self.assertEqual(row["status"], "partial")
            self.assertTrue(all(w["used_percent"] is None for w in row["windows"]))


class CollectionTests(unittest.TestCase):
    def make_client(self, ids=(1,)):
        replies = {}
        for aid in ids:
            replies[f"/accounts/{aid}"] = account(aid)
            replies[f"/accounts/{aid}/today-stats"] = {**STATS, "error": SECRET}
        return FakeClient(replies)

    def test_exact_read_only_endpoints_order_and_deduplication(self):
        fake = self.make_client((1, 2))
        result = collect_usage(fake, [2, 1, 2], now=NOW)
        self.assertEqual(fake.calls, [("/accounts/2", None), ("/accounts/2/today-stats", None),
                                      ("/accounts/1", None), ("/accounts/1/today-stats", None)])
        self.assertEqual([a["account_id"] for a in result["accounts"]], [2, 1])
        self.assertEqual(result["request_count"], 4)
        self.assertEqual(result["request_attempts_upper_bound"], 12)
        self.assertEqual(result["active_provider_requests"], 0)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_invalid_inputs_fail_before_any_calls(self):
        for ids in ([True], [0], [-1], [1.0], ["1"], [MAX_SAFE_INTEGER + 1], {}, None,
                    (i for i in range(2)), [1] * (MAX_ACCOUNTS + 1)):
            fake = self.make_client()
            with self.assertRaises(UsageError):
                collect_usage(fake, ids, now=NOW)
            self.assertEqual(fake.calls, [])
        for workers in (0, -1, 5, True, 1.0, "2"):
            fake = self.make_client()
            with self.assertRaises(UsageError):
                collect_usage(fake, [1], max_workers=workers, now=NOW)
            self.assertEqual(fake.calls, [])
        fake = self.make_client()
        with self.assertRaises(UsageError):
            collect_usage(fake, [1], now=datetime(2026, 1, 1))
        self.assertEqual(fake.calls, [])

    def test_empty_selection_makes_no_calls(self):
        fake = self.make_client()
        result = collect_usage(fake, [], now=NOW)
        self.assertEqual(result["accounts"], [])
        self.assertEqual(result["request_count"], 0)
        self.assertEqual(result["max_workers"], 0)
        self.assertEqual(fake.calls, [])

    def test_account_failure_isolated_and_skips_misleading_zero_stats(self):
        fake = self.make_client((1, 2))
        fake.replies["/accounts/1"] = PreflightError(SECRET, http_status=404)
        result = collect_usage(fake, [1, 2], now=NOW)
        first, second = result["accounts"]
        self.assertEqual(first["status"], "unknown")
        self.assertIsNone(first["today"]["requests"])
        self.assertEqual(first["errors"], [{"scope": "account", "code": "USAGE_READ_FAILED", "http_status": 404}])
        self.assertEqual(second["status"], "ok")
        self.assertEqual(result["request_count"], 3)
        self.assertNotIn(("/accounts/1/today-stats", None), fake.calls)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_today_failure_does_not_lose_window_and_is_never_retried_here(self):
        for error in (RuntimeError(SECRET), PreflightError(SECRET, http_status=503, retryable=True)):
            fake = self.make_client()
            fake.replies["/accounts/1/today-stats"] = error
            result = collect_usage(fake, [1], now=NOW)
            row = result["accounts"][0]
            self.assertEqual(row["windows"][0]["used_percent"], 25)
            self.assertEqual(row["status"], "partial")
            self.assertIsNone(row["today"]["cost"])
            self.assertEqual(row["errors"][0]["scope"], "today")
            self.assertEqual(len(fake.calls), 2)
            self.assertNotIn(SECRET, json.dumps(result))

    def test_business_errors_and_bad_schema_never_return_bodies(self):
        for payload in ([], None, SECRET, {"code": 1, "message": SECRET, "data": account()},
                        {"code": 0}, {str(i): SECRET for i in range(MAX_RESPONSE_FIELDS + 1)}):
            fake = self.make_client()
            fake.replies["/accounts/1"] = payload
            result = collect_usage(fake, [1], now=NOW)
            self.assertEqual(result["accounts"][0]["status"], "unknown")
            self.assertNotIn(SECRET, json.dumps(result))
            self.assertEqual(len(fake.calls), 1)

    def test_identity_mismatch_and_boolean_id_are_not_accepted(self):
        for aid in (2, True, None):
            fake = self.make_client()
            fake.replies["/accounts/1"] = account(aid)
            row = collect_usage(fake, [1], now=NOW)["accounts"][0]
            self.assertEqual(row["account_id"], 1)
            self.assertEqual(row["errors"][0]["code"], "USAGE_ACCOUNT_ID_MISMATCH")
            self.assertEqual(len(fake.calls), 1)

    def test_envelope_and_malformed_stats(self):
        fake = self.make_client()
        fake.replies["/accounts/1"] = {"code": 0, "data": account(), "message": SECRET}
        fake.replies["/accounts/1/today-stats"] = {"code": 0, "data": STATS}
        self.assertEqual(collect_usage(fake, [1], now=NOW)["accounts"][0]["status"], "ok")
        fake.replies["/accounts/1/today-stats"] = [SECRET]
        row = collect_usage(fake, [1], now=NOW)["accounts"][0]
        self.assertEqual(row["status"], "partial")
        self.assertEqual(row["errors"][0]["code"], "USAGE_INVALID_RESPONSE")

    def test_concurrency_is_bounded_and_result_order_stable(self):
        class SlowClient(FakeClient):
            def __init__(self, replies):
                super().__init__(replies)
                self.lock = threading.Lock()
                self.active = self.peak = 0

            def get(self, path, params=None):
                with self.lock:
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                try:
                    time.sleep(0.008)
                    return super().get(path, params)
                finally:
                    with self.lock:
                        self.active -= 1

        ids = [6, 3, 4, 1, 2, 5]
        fake = SlowClient(self.make_client(ids).replies)
        result = collect_usage(fake, ids, max_workers=3, now=NOW)
        self.assertGreater(fake.peak, 1)
        self.assertLessEqual(fake.peak, 3)
        self.assertEqual([row["account_id"] for row in result["accounts"]], ids)
        self.assertEqual(result["request_count"], 12)

    def test_max_batch_has_bounded_requests_and_output(self):
        ids = list(range(1, MAX_ACCOUNTS + 1))
        result = collect_usage(self.make_client(ids), ids, now=NOW)
        self.assertEqual(result["request_count"], 2 * MAX_ACCOUNTS)
        self.assertEqual(result["request_attempts_upper_bound"], 6 * MAX_ACCOUNTS)
        self.assertLess(len(json.dumps(result).encode()), 128 * 1024)


class ExistingClientTests(unittest.TestCase):
    """Exercise the real Client with a fake opener. No listening sockets/network."""

    def test_uses_existing_auth_path_timeout_and_no_force_or_usage_api(self):
        client = Client("https://synthetic.invalid/panel", "SYNTHETIC_ADMIN", timeout=2)
        calls = []

        def open_fake(request, timeout):
            calls.append(request)
            self.assertEqual(timeout, 2)
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("X-api-key"), "SYNTHETIC_ADMIN")
            payload = STATS if request.full_url.endswith("/today-stats") else account()
            return BytesIO(json.dumps({"code": 0, "data": payload}).encode())

        with patch.object(client.opener, "open", side_effect=open_fake):
            result = collect_usage(client, [1], now=NOW)
        self.assertEqual([r.full_url for r in calls], ["https://synthetic.invalid/panel/api/v1/admin/accounts/1",
                                                        "https://synthetic.invalid/panel/api/v1/admin/accounts/1/today-stats"])
        self.assertNotIn("SYNTHETIC_ADMIN", json.dumps(result))
        self.assertEqual(result["accounts"][0]["status"], "ok")

    def test_existing_retry_limit_in_report_and_error_body_never_read(self):
        class UnreadableBody(BytesIO):
            def read(self, *args):
                raise AssertionError("must not read upstream HTTP error body")

        client = Client("https://synthetic.invalid", "SYNTHETIC_ADMIN")
        count = 0

        def fail(request, timeout):
            nonlocal count
            count += 1
            raise HTTPError(request.full_url, 503, SECRET, {}, UnreadableBody(SECRET.encode()))

        with patch.object(client.opener, "open", side_effect=fail), patch("sub2easy.preflight.time.sleep"):
            result = collect_usage(client, [1], now=NOW)
        self.assertEqual(count, 3)
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["request_attempts_upper_bound"], 3)
        self.assertEqual(result["accounts"][0]["errors"][0]["http_status"], 503)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_existing_response_byte_limit_is_enforced(self):
        client = Client("https://synthetic.invalid", "SYNTHETIC_ADMIN")
        body = BytesIO(b"x" * (8 * 1024 * 1024 + 1))
        with patch.object(client.opener, "open", return_value=body) as opener:
            result = collect_usage(client, [1], now=NOW)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(result["max_response_bytes"], 8 * 1024 * 1024)
        self.assertEqual(result["accounts"][0]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
