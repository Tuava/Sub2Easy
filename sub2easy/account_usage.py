"""Bounded, read-only account usage collection; no provider requests or refreshes.

Verified against sub2api 98d86915becae9fe9491a91ffc6defd5235c8d2b:
* routes/admin.go:394-397 and handler/admin/account_handler.go:2519-2541:
  GET /accounts/:id/usage?source=passive only supports Anthropic OAuth/SetupToken
  (service/account_usage_service.go:588-627). It is NOT an OpenAI passive API.
  UsageInfo has source, updated_at, five_hour, seven_day, seven_day_fable;
  UsageProgress has utilization (already 0..100+), resets_at, remaining_seconds,
  optional window_stats/used_requests/limit_requests (:139-195). updated_at is
  passive_usage_sampled_at, not read time. Five-hour values can be estimates or
  artificial zero when absent (:1707-1771); attached local stats cache lasts 60s.
* POST /accounts/usage/batch {account_ids, force} returns {usage, errors}, but
  service/account_usage_service.go:514-585 dispatches non-Anthropic accounts to
  active code even with force=false. OpenAI can POST a model probe, persist Extra,
  notify auto-reset, and clear account errors (:711-949, :1610-1633). Never use it.
* POST /accounts/today-stats/batch {account_ids} returns {stats: {"ID": WindowStats}}.
  It only queries local logs and caches the result for 30s (account_today_stats_cache.go:9),
  but can silently replace failed queries with zeros (account_usage_service.go:1412-1471).
  The existing preflight.Client has no POST API. Instead, GET /accounts/:id/today-stats
  (:1396-1408) is read-only, uncached here, and propagates SQL failures.
* GET /accounts/:id reads the repository (handler/admin/account_handler.go:927-946;
  service/admin_account.go:55-57). Only existing Extra codex_5h/7d_* values are read.
  openai_gateway_usage.go:1045-1106 writes normalized snapshot fields; their origin
  may be normal traffic OR an earlier active probe. Reading them does not sample.
* repository/usage_log_repo_stats.go:276-302 defines today from upstream's configured
  timezone, sums input+output+cache_creation+cache_read tokens, and computes cost as
  SUM(COALESCE(account_stats_cost,total_cost)*COALESCE(account_rate_multiplier,1));
  standard_cost=SUM(total_cost), user_cost=SUM(actual_cost). These are local billing
  records, NOT subscription balance/provider invoice or full account-wide traffic.

Contract: collect_usage(client, [cloud_id, ...], max_workers=1, now=None) -> JSON-safe
{accounts: [{account_id, platform, account_type, status, windows, today, errors, ...}],
 request_count, request_attempts_upper_bound, ...}. IDs are positive JS-safe integers,
 deduplicated in input order, with at most 50 input entries. Empty input makes no calls.
 Client is preflight.Client or a fake implementing get(path, params=None), returning
 its already-unwrapped JSON. No account listing, mutation, model call, extra retries,
 disk reads/writes, logs, raw dumps, or raw exception messages occur here.

At most 2*N logical GETs; Client retries up to 3 attempts, each with its own timeout
and an 8-MiB response limit. Thus at most 6*N wire attempts, not 2*N. There is no
whole-call deadline; caller configures Client.timeout and serializes polling runs.
max_workers is 1..4; >1 requires a concurrency-capable client. Output has at most two
OpenAI windows per account. normalize_account_usage / normalize_today_stats are pure.

Window remaining is a PERCENTAGE (remaining_unit=percent), never tokens/requests.
used_percent is the last sample, NOT a live promise; freshness labels are mandatory
for UI. Row status is data completeness, never account health, and old/unverifiable
samples make it partial. A 600s age threshold is a local display heuristic, not a freshness guarantee
or refresh trigger. Expired windows have null current usage/remaining and retain
sampled_used_percent; missing fields remain null/unknown. Relative resets require a
valid snapshot timestamp: never anchor an old interval to fetch time. Other platforms
still receive today stats, but quota windows remain unsupported until verified.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import math

from sub2easy.preflight import PreflightError, unwrap


MAX_ACCOUNTS = 50
MAX_WORKERS = 4
MAX_SAFE_INTEGER = 2**53 - 1
MAX_RESPONSE_FIELDS = 128
MAX_EXTRA_FIELDS = 512
STALE_AFTER_SECONDS = 600

_PLATFORMS = frozenset({"openai", "anthropic", "gemini", "antigravity", "grok", "ollama"})
_ACCOUNT_TYPES = frozenset({"oauth", "setup-token", "apikey", "bedrock"})
_METRICS = ("requests", "tokens", "cost", "standard_cost", "user_cost")


class UsageError(PreflightError):
    """Fixed codes only; invalid input is rejected before making any requests."""


def _now(value):
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise UsageError("USAGE_INVALID_NOW")
    return value.astimezone(timezone.utc)


def _id(value):
    return type(value) is int and 0 < value <= MAX_SAFE_INTEGER


def _number(value, *, integer=False):
    # Never coerce bools, numeric strings, NaN, infinity, or arbitrary objects.
    if type(value) not in (int, float) or not 0 <= value <= MAX_SAFE_INTEGER:
        return None
    if not math.isfinite(value) or (integer and (type(value) is not int)):
        return None
    return value


def _time(value):
    if not isinstance(value, str) or not 10 <= len(value) <= 64:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None or result.year < 1970:
            return None
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _iso(value):
    return value.isoformat() if value is not None else None


def _object(value, limit=MAX_RESPONSE_FIELDS):
    if not isinstance(value, dict) or len(value) > limit:
        raise UsageError("USAGE_INVALID_RESPONSE")
    return value


def _error(scope, code, status=None):
    # Scope/code are locally defined, never upstream text (even PreflightError text).
    return {"scope": scope, "code": code, "http_status": status}


def _read(client, path, scope):
    try:
        # Fake clients can also return the upstream envelope. unwrap checks only
        # code/data and never incorporates message/detail/error bodies in output.
        return _object(unwrap(_object(client.get(path)))), None
    except Exception as exc:
        status = getattr(exc, "http_status", None) if isinstance(exc, PreflightError) else None
        if type(status) is not int or not 100 <= status <= 599:
            status = None
        code = "USAGE_INVALID_RESPONSE" if isinstance(exc, UsageError) else "USAGE_READ_FAILED"
        return None, _error(scope, code, status)


def normalize_today_stats(payload=None):
    """Pure whitelist. Missing/invalid metrics are null; explicit SQL zeros survive.

    No updated_at or timezone is provided by this endpoint. collected_at is not
    the time of the underlying log data; async billing/log persistence may lag.
    """
    data = {} if payload is None else _object(payload)
    return {
        **{key: _number(data.get(key), integer=key in {"requests", "tokens"}) for key in _METRICS},
        "source": "sub2api_usage_logs",
        "period": "upstream_today",
        "timezone": None,
        "updated_at": None,
        "freshness": "unknown",
    }


def _openai_windows(extra, now):
    updated = _time(extra.get("codex_usage_updated_at"))
    age = (now - updated).total_seconds() if updated is not None else None
    windows = []
    for key, prefix in (("five_hour", "codex_5h"), ("seven_day", "codex_7d")):
        sample = _number(extra.get(prefix + "_used_percent"))
        reset = _time(extra.get(prefix + "_reset_at"))
        if reset is None and updated is not None:
            after = _number(extra.get(prefix + "_reset_after_seconds"), integer=True)
            if after is not None:
                try:
                    reset = updated + timedelta(seconds=after)
                except OverflowError:
                    pass
        expired = reset is not None and reset <= now
        if expired:
            freshness = "expired"
        elif sample is None or age is None or age < 0:
            freshness = "unknown"
        else:
            freshness = "stale" if age >= STALE_AFTER_SECONDS else "recent"
        # Do not fabricate a zero-utilization new window merely because the old
        # reset passed. Also do not hide >100% overages by clamping the sample.
        used = None if expired else sample
        windows.append({
            "key": key,
            "used_percent": used,
            "remaining": max(0, 100 - used) if used is not None else None,
            "remaining_unit": "percent",
            "reset_at": _iso(reset),
            "remaining_seconds": max(0, int((reset - now).total_seconds())) if reset is not None else None,
            "sampled_used_percent": sample,
            "updated_at": _iso(updated),
            "freshness": freshness,
        })
    return windows


# Extend with verified, pure snapshot readers, not provider/API refresh methods.
_WINDOW_READERS = {("openai", "oauth"): _openai_windows}


def _finish(row):
    known = any(w["used_percent"] is not None for w in row["windows"])
    known = known or any(row["today"][key] is not None for key in _METRICS)
    row["status"] = "unknown" if not known else "partial" if row["errors"] else "ok"
    return row


def normalize_account_usage(account, today_stats=None, *, now=None):
    """Pure account -> sanitized row; account_id is the sub2api/cloud numeric ID.

    Only id/platform/type/extra's explicitly named numeric/time fields are read.
    Never returns names, credentials, email, notes, proxy, links or upstream errors.
    No input objects or nested dictionaries are modified or copied into output.
    """
    now = _now(now)
    account = _object(account)
    if not _id(account.get("id")):
        raise UsageError("USAGE_INVALID_RESPONSE")
    platform, kind = account.get("platform"), account.get("type")
    platform = platform if isinstance(platform, str) and platform in _PLATFORMS else "unknown"
    kind = kind if isinstance(kind, str) and kind in _ACCOUNT_TYPES else "unknown"
    reader = _WINDOW_READERS.get((platform, kind))
    errors = []
    if reader is None:
        windows = []
        errors.append(_error("windows", "USAGE_WINDOWS_UNSUPPORTED"))
    else:
        extra = account.get("extra")
        if extra is None:
            extra = {}
        elif not isinstance(extra, dict) or len(extra) > MAX_EXTRA_FIELDS:
            extra = {}
            errors.append(_error("windows", "USAGE_INVALID_RESPONSE"))
        windows = reader(extra, now)
        if any(w["used_percent"] is None or w["reset_at"] is None for w in windows):
            errors.append(_error("windows", "USAGE_WINDOW_DATA_UNKNOWN"))
        if any(w["freshness"] == "stale" for w in windows):
            errors.append(_error("windows", "USAGE_WINDOW_SNAPSHOT_STALE"))
        if any(w["used_percent"] is not None and w["freshness"] == "unknown" for w in windows):
            errors.append(_error("windows", "USAGE_WINDOW_FRESHNESS_UNKNOWN"))
    today = normalize_today_stats(today_stats)
    if any(today[key] is None for key in _METRICS):
        errors.append(_error("today", "USAGE_TODAY_DATA_UNKNOWN"))
    return _finish({
        "account_id": account["id"],
        "platform": platform,
        "account_type": kind,
        "mode": "read_only",
        "window_source": "persisted_snapshot" if reader else "unknown",
        "snapshot_origin": "unknown",
        "collected_at": _iso(now),
        "windows": windows,
        "today": today,
        "errors": errors,
    })


def _collect_one(client, account_id, now):
    account, error = _read(client, f"/accounts/{account_id}", "account")
    if error is None and (not _id(account.get("id")) or account["id"] != account_id):
        error = _error("account", "USAGE_ACCOUNT_ID_MISMATCH")
    if error is not None:
        # No today request on account failure: SQL stats alone can return zero
        # for an ID that does not exist, which must not look like an unused account.
        row = normalize_account_usage({"id": account_id}, now=now)
        row["errors"] = [error]
        return _finish(row), 1

    stats, error = _read(client, f"/accounts/{account_id}/today-stats", "today")
    row = normalize_account_usage(account, stats, now=now)
    if error is not None:
        row["errors"] = [e for e in row["errors"] if e["scope"] != "today"] + [error]
    return _finish(row), 2


def collect_usage(client, account_ids, *, max_workers=1, now=None):
    """Read just the selected IDs using an existing Client or injected fake.

    Raises UsageError (a PreflightError) only for invalid caller input; ordinary
    upstream errors are fixed-code per-account errors, with no error body/text.
    max_workers bounds concurrency within this call, not across overlapping calls.
    """
    if (not isinstance(account_ids, (list, tuple)) or len(account_ids) > MAX_ACCOUNTS
            or any(not _id(value) for value in account_ids)):
        raise UsageError("USAGE_INVALID_ACCOUNT_IDS")
    if type(max_workers) is not int or not 1 <= max_workers <= MAX_WORKERS:
        raise UsageError("USAGE_INVALID_MAX_WORKERS")
    if now is not None:
        now = _now(now)
    ids = list(dict.fromkeys(account_ids))
    if max_workers == 1 or len(ids) < 2:
        results = [_collect_one(client, aid, now) for aid in ids]
    else:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(ids))) as pool:
            results = list(pool.map(lambda aid: _collect_one(client, aid, now), ids))
    requests = sum(count for _, count in results)
    return {
        "mode": "read_only",
        "collected_at": _iso(_now(now)),
        "accounts": [row for row, _ in results],
        "request_count": requests,
        "request_attempts_upper_bound": requests * 3,
        "max_response_bytes": 8 * 1024 * 1024,
        "max_workers": min(max_workers, len(ids)) if ids else 0,
        "active_provider_requests": 0,
        "limitations": [
            "仅读取已有用量快照与本站日志；不刷新、不探测模型、不修改账号。",
            "remaining 单位为百分比，不是剩余 token/请求数；历史快照不保证实时余额。",
            "600 秒仅为展示过期阈值；已到重置时间的窗口返回未知，不推断配额已恢复。",
            "当日按 sub2api 服务端时区统计，时区及日志新鲜度未由接口提供。",
            "tokens 包含输入、输出、缓存创建和缓存读取；不包含未经过本站的使用。",
            "cost 为账号口径费用，standard_cost 为无倍率费用，user_cost 为用户口径费用；均非订阅余额。",
            "现有 Client 每次 GET 最多尝试 3 次、单次响应上限 8 MiB；不提供整批超时。",
        ],
    }
