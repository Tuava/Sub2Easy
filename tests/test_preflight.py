from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from sub2easy.preflight import (
    Client, PreflightError, admin_url, classify, main, summarize, timestamp, unwrap,
)


NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)


def account(account_id=1, **updates):
    data = {
        "id": account_id, "platform": "openai", "group_ids": [1],
        "status": "active", "schedulable": True,
        "expires_at": None, "auto_pause_on_expired": True,
        "rate_limit_reset_at": None, "overload_until": None,
        "temp_unschedulable_until": None,
    }
    data.update(updates)
    return data


@contextmanager
def server(callback):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, headers, body = callback(self)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def json_response(data):
    return 200, {"Content-Type": "application/json"}, json.dumps(data).encode()


class ModelTests(unittest.TestCase):
    def test_candidate_is_not_health_claim(self):
        self.assertEqual(classify(account(), NOW), ("candidate", []))

    def test_each_cooldown(self):
        for field, reason in [("rate_limit_reset_at", "rate_limited"),
                              ("overload_until", "overloaded"),
                              ("temp_unschedulable_until", "temp_unschedulable")]:
            with self.subTest(field=field):
                state, blockers = classify(account(**{field: "2026-09-12T00:00:00Z"}), NOW)
                self.assertEqual(state, "blocked")
                self.assertIn(reason, blockers)

    def test_cooldown_ends_at_boundary(self):
        self.assertEqual(classify(account(rate_limit_reset_at=NOW.isoformat()), NOW)[0], "candidate")

    def test_expiry_with_auto_pause(self):
        for pause, expected in [(True, "blocked"), (False, "candidate")]:
            with self.subTest(pause=pause):
                self.assertEqual(classify(account(expires_at=NOW.timestamp(),
                                                  auto_pause_on_expired=pause), NOW)[0], expected)

    def test_disabled_and_error_not_candidates(self):
        for updates in [{"status": "inactive"}, {"status": "error"}, {"schedulable": False}]:
            with self.subTest(updates=updates):
                self.assertEqual(classify(account(**updates), NOW)[0], "blocked")

    def test_missing_runtime_field_is_unknown(self):
        for field in ["status", "schedulable", "expires_at", "overload_until",
                      "rate_limit_reset_at", "temp_unschedulable_until"]:
            data = account()
            del data[field]
            with self.subTest(field=field):
                self.assertEqual(classify(data, NOW)[0], "unknown")

    def test_unrecognized_status_is_unknown(self):
        self.assertEqual(classify(account(status="new-version-status"), NOW)[0], "unknown")

    def test_bad_runtime_date_is_unknown(self):
        for value in ["tomorrow", "2026-09-12T00:00:00", {}, True, 1e300, float("nan")]:
            with self.subTest(value=value):
                self.assertEqual(classify(account(overload_until=value), NOW)[0], "unknown")

    def test_timestamp_formats(self):
        self.assertEqual(timestamp(NOW.timestamp()), NOW)
        self.assertEqual(timestamp("2026-09-11T08:00:00+08:00"), NOW)
        self.assertIsNone(timestamp(None))

    def test_whitelist_does_not_leak_account_details(self):
        data = account(name="SECRET_NAME", error_message="SECRET_ERROR", proxy={"password": "SECRET_PROXY"},
                       credentials={"refresh_token": "SECRET_TOKEN"}, notes="SECRET_NOTES")
        report = json.dumps(summarize([data], NOW))
        self.assertNotIn("SECRET", report)
        self.assertNotIn("credentials", report)

    def test_group_counts_do_not_duplicate_global_total(self):
        report = summarize([account(group_ids=[1, 2, 2]), account(2, group_ids=[])], NOW)
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["by_group"]["2"]["candidate"], 1)
        self.assertEqual(report["by_group"]["ungrouped"]["candidate"], 1)

    def test_missing_groups_not_misreported_as_ungrouped(self):
        data = account()
        del data["group_ids"]
        report = summarize([data], NOW)
        self.assertIn("unknown", report["by_group"])
        self.assertFalse(report["accounts"][0]["group_scope_known"])

    def test_duplicate_or_invalid_id_rejected(self):
        for data in [[account(), account()], [account(True)], [account(-1)], [None]]:
            with self.subTest(data=data):
                with self.assertRaises(PreflightError):
                    summarize(data, NOW)

    def test_enveloped_and_raw_responses(self):
        self.assertEqual(unwrap({"code": 0, "data": [1]}), [1])
        self.assertEqual(unwrap([1]), [1])
        self.assertEqual(unwrap({"id": 1}), {"id": 1})
        with self.assertRaises(PreflightError) as caught:
            unwrap({"code": 401, "message": "SECRET_ERROR"})
        self.assertNotIn("SECRET", str(caught.exception))

    def test_url_normalization(self):
        for base in ["https://example.test/panel", "https://example.test/panel/api/v1/",
                     "https://example.test/panel/api/v1/admin/"]:
            self.assertEqual(admin_url(base), "https://example.test/panel/api/v1/admin")

    def test_invalid_base_url(self):
        for base in ["file:///etc/passwd", "https://u:p@example.test", "https://example.test?key=x", ""]:
            with self.subTest(base=base):
                with self.assertRaises(PreflightError):
                    admin_url(base)


class HTTPTests(unittest.TestCase):
    def test_transient_get_retried_with_bounded_attempts(self):
        client=Client('https://example.invalid','SYNTHETIC')
        with patch.object(client,'_get_once',side_effect=[PreflightError('network',retryable=True),
                PreflightError('server',503,True),{'ok':True}]) as get, patch('sub2easy.preflight.time.sleep') as sleep:
            self.assertEqual(client.get('/accounts'),{'ok':True});self.assertEqual(get.call_count,3)
            self.assertEqual(sleep.call_count,2)

    def test_auth_and_schema_errors_not_retried(self):
        for error in [PreflightError('auth',401),PreflightError('schema')]:
            client=Client('https://example.invalid','SYNTHETIC')
            with patch.object(client,'_get_once',side_effect=error) as get, self.assertRaises(PreflightError):
                client.get('/accounts')
            self.assertEqual(get.call_count,1)

    def test_pagination_auth_and_filters_over_real_http(self):
        requests = []

        def respond(req):
            requests.append((req.path, req.headers.get("x-api-key"), req.command))
            page = int(parse_qs(urlsplit(req.path).query)["page"][0])
            # Deliberately smaller than requested page_size: follow total, not page length.
            return json_response({"code": 0, "data": {"items": [account(page)], "total": 2}})

        with server(respond) as base:
            result = Client(base, "test-admin-key").accounts("openai", 123)
        self.assertEqual([a["id"] for a in result], [1, 2])
        self.assertEqual(len(requests), 2)
        for path, key, method in requests:
            self.assertTrue(path.startswith("/api/v1/admin/accounts?"))
            self.assertEqual(key, "test-admin-key")
            self.assertEqual(method, "GET")
            self.assertEqual(parse_qs(urlsplit(path).query)["group"], ["123"])

    def test_empty_pool(self):
        with server(lambda req: json_response({"items": [], "total": 0})) as base:
            self.assertEqual(Client(base, "test").accounts(), [])

    def test_repeated_page_rejected(self):
        with server(lambda req: json_response({"items": [account()], "total": 2})) as base:
            with self.assertRaisesRegex(PreflightError, "重复"):
                Client(base, "test").accounts()

    def test_total_changes_during_pagination(self):
        def respond(req):
            page = int(parse_qs(urlsplit(req.path).query)["page"][0])
            return json_response({"items": [account(page)], "total": page + 1})

        with server(respond) as base:
            with self.assertRaisesRegex(PreflightError, "总数变化"):
                Client(base, "test").accounts()

    def test_incomplete_pagination_rejected(self):
        with server(lambda req: json_response({"items": [], "total": 1})) as base:
            with self.assertRaisesRegex(PreflightError, "分页不完整"):
                Client(base, "test").accounts()

    def test_error_body_is_not_exposed(self):
        with server(lambda req: (401, {}, b'SECRET_UPSTREAM_BODY')) as base:
            with self.assertRaises(PreflightError) as caught:
                Client(base, "test").accounts()
        self.assertIn("401", str(caught.exception))
        self.assertNotIn("SECRET", str(caught.exception))

    def test_redirect_not_followed(self):
        paths = []

        def respond(req):
            paths.append(req.path)
            return 302, {"Location": "/must-not-receive-key"}, b""

        with server(respond) as base:
            with self.assertRaisesRegex(PreflightError, "302"):
                Client(base, "test").accounts()
        self.assertEqual(len(paths), 1)

    def test_html_login_page_not_accepted_as_api_success(self):
        with server(lambda req: (200, {}, b"<html>Login</html>")) as base:
            with self.assertRaisesRegex(PreflightError, "JSON"):
                Client(base, "test").accounts()

    def test_wrong_list_schema_rejected(self):
        with server(lambda req: json_response({"code": 0, "data": []})) as base:
            with self.assertRaisesRegex(PreflightError, "不兼容"):
                Client(base, "test").accounts()

    def test_truncated_http_body_is_not_a_snapshot(self):
        def respond(req):
            return 200, {"Content-Length": "10000"}, b'{"code": 0'

        with server(respond) as base:
            with self.assertRaises(PreflightError):
                Client(base, "test").accounts()


class CLITests(unittest.TestCase):
    def test_demo_fixture(self):
        fixture = Path(__file__).resolve().parents[1] / "examples/accounts.json"
        out = io.StringIO()
        with redirect_stdout(out):
            result = main(["--fixture", str(fixture), "--platform", "openai"])
        report = json.loads(out.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["counts"], {"candidate": 1, "blocked": 1})

    def test_bad_timeout(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(main(["--timeout", "nan"]), 2)
        self.assertIn("timeout", err.getvalue())


if __name__ == "__main__":
    unittest.main()
