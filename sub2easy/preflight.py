"""Read-only sub2api integration check. Never tests or mutates accounts."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class PreflightError(Exception):
    """Only fixed, non-sensitive messages should reach the CLI."""

    def __init__(self, message, http_status=None, retryable=False):
        super().__init__(message)
        self.http_status = http_status
        self.retryable = retryable


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def admin_url(base):
    parts = urlsplit(base.strip())
    if (parts.scheme not in {"https", "http"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment):
        raise PreflightError("站点地址必须是无用户名、密码、查询参数和片段的 HTTP(S) URL")
    path = parts.path.rstrip("/")
    if path.endswith("/api/v1/admin"):
        pass
    elif path.endswith("/api/v1"):
        path += "/admin"
    else:
        path += "/api/v1/admin"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def unwrap(payload):
    # Most endpoints use the envelope; scheduled-test APIs return raw JSON.
    if isinstance(payload, dict) and "code" in payload:
        if type(payload["code"]) is not int or payload["code"] != 0:
            raise PreflightError("管理接口返回业务错误；请在 sub2api 管理日志中检查")
        if "data" not in payload:
            raise PreflightError("管理接口缺少 data 字段")
        return payload["data"]
    return payload


class Client:
    def __init__(self, base, key, timeout=20):
        self.base = admin_url(base)
        if not key.strip() or "\n" in key or "\r" in key:
            raise PreflightError("缺少或无效的 SUB2API_ADMIN_KEY")
        self.key = key.strip()
        self.timeout = timeout
        self.opener = build_opener(NoRedirect())

    def get(self, path, params=None):
        # GET only. Authentication/schema errors are not retried; mutating POSTs
        # live in other clients and never inherit this retry policy.
        for attempt in range(3):
            try:
                return self._get_once(path, params)
            except PreflightError as exc:
                if not exc.retryable or attempt == 2:
                    raise
                time.sleep((0.3, 1.0)[attempt])

    def _get_once(self, path, params=None):
        url = self.base + path
        if params:
            url += "?" + urlencode(params)
        request = Request(url, headers={
            "x-api-key": self.key,
            "Accept": "application/json",
            "User-Agent": "Sub2Easy-preflight/0.1",
        }, method="GET")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                # Bound memory even if a proxy responds with an unexpected body.
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise PreflightError("管理接口响应超过 8 MiB；请核对接口兼容性")
                return unwrap(json.loads(raw))
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise PreflightError(
                f"管理接口 HTTP {status}；未读取错误正文、未跟随重定向", http_status=status,
                retryable=status in {408,429,500,502,503,504}
            ) from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise PreflightError("无法连接管理接口或请求超时；未生成账号快照", retryable=True) from None
        except (ValueError, UnicodeError):
            raise PreflightError("管理接口未返回有效 JSON；请核对地址和版本") from None

    def accounts(self, platform=None, group=None):
        items, seen = [], set()
        initial_total = None
        for page in range(1, 10001):
            params = {"page": page, "page_size": 100, "lite": "true",
                      "sort_by": "id", "sort_order": "asc"}
            if platform:
                params["platform"] = platform
            if group is not None:
                params["group"] = group
            data = self.get("/accounts", params)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise PreflightError("账号列表结构不兼容；预期 data.items 数组")
            total = data.get("total")
            if type(total) is not int or total < 0:
                raise PreflightError("账号列表缺少有效 total")
            if initial_total is None:
                initial_total = total
            if total != initial_total:
                raise PreflightError("分页期间账号总数变化；请重新预检")
            batch = data["items"]
            for item in batch:
                if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0:
                    raise PreflightError("账号列表包含无效 ID")
                if item["id"] in seen:
                    raise PreflightError("分页结果重复；请检查版本或重试完整快照")
                seen.add(item["id"])
            items.extend(batch)
            if len(items) == total:
                return items
            if not batch or len(items) > total:
                raise PreflightError("账号分页不完整；未输出误导性统计")
        raise PreflightError("账号分页超过上限")


def timestamp(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("invalid timestamp")
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("invalid timestamp")
        # Account expires_at uses Unix seconds. Runtime cooldowns use RFC3339.
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("timezone required")
        return dt.astimezone(timezone.utc)
    raise ValueError("invalid timestamp")


def classify(account, now):
    blockers = []
    status = account.get("status")
    if status == "inactive":
        blockers.append("inactive")
    elif status == "error":
        blockers.append("error")
    elif status != "active":
        blockers.append("unknown")
    if account.get("schedulable") is False:
        blockers.append("unschedulable")
    elif account.get("schedulable") is not True:
        blockers.append("unknown")

    for field, reason in (
        ("rate_limit_reset_at", "rate_limited"),
        ("overload_until", "overloaded"),
        ("temp_unschedulable_until", "temp_unschedulable"),
    ):
        if field not in account:
            blockers.append("unknown")
            continue
        try:
            until = timestamp(account[field])
            if until is not None and until > now:
                blockers.append(reason)
        except (ValueError, OverflowError, OSError):
            blockers.append("unknown")

    try:
        if "expires_at" not in account:
            blockers.append("unknown")
        expires = timestamp(account.get("expires_at"))
        pause = account.get("auto_pause_on_expired")
        if expires is not None:
            if type(pause) is not bool:
                blockers.append("unknown")
            elif pause and expires <= now:
                blockers.append("expired")
    except (ValueError, OverflowError, OSError):
        blockers.append("unknown")

    blockers = sorted(set(blockers))
    # Unknown schemas are never advertised as healthy.
    state = "unknown" if "unknown" in blockers else "blocked" if blockers else "candidate"
    return state, blockers


def summarize(accounts, now=None):
    now = now or datetime.now(timezone.utc)
    counts, reasons = Counter(), Counter()
    platforms, groups = defaultdict(Counter), defaultdict(Counter)
    rows, seen = [], set()
    for account in accounts:
        if (not isinstance(account, dict) or type(account.get("id")) is not int
                or account["id"] <= 0 or account["id"] in seen):
            raise PreflightError("账号快照包含无效或重复 ID")
        seen.add(account["id"])
        state, blockers = classify(account, now)
        platform = account.get("platform")
        if not isinstance(platform, str) or not platform:
            platform = "unknown"
            state = "unknown"
            blockers = sorted(set(blockers + ["unknown"]))
        group_ids = account.get("group_ids")
        if (not isinstance(group_ids, list)
                or any(type(g) is not int or g <= 0 for g in group_ids)):
            group_ids = []
            group_keys = ["unknown"]
        else:
            group_ids = sorted(set(group_ids))
            group_keys = [str(g) for g in group_ids] or ["ungrouped"]
        counts[state] += 1
        reasons.update(blockers)
        platforms[platform][state] += 1
        for group in group_keys:
            groups[group][state] += 1
        # Explicit whitelist: do not dump the upstream account object.
        rows.append({"id": account["id"], "platform": platform,
                     "group_ids": group_ids, "group_scope_known": group_keys != ["unknown"],
                     "state": state, "blockers": blockers})
    return {
        "mode": "read_only_preflight",
        "collected_at": now.isoformat(),
        "total": len(rows), "counts": dict(counts), "blockers": dict(reasons),
        "by_platform": {k: dict(v) for k, v in platforms.items()},
        "by_group": {k: dict(v) for k, v in groups.items()},
        "accounts": rows,
        "limitations": [
            "candidate 仅表示未发现列表级阻塞，未验证模型、真实调度、配额或代理连通性",
            "分组统计不可相加：一个账号可以同时属于多个分组",
            "分页读取不是数据库事务快照；同总数的并发替换或状态变化仍可能发生",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="sub2api 号池只读接入预检")
    parser.add_argument("--fixture", type=Path, help="读取本地虚构账号快照，不访问网络")
    parser.add_argument("--platform")
    parser.add_argument("--group", type=int)
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args(argv)
    try:
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise PreflightError("timeout 必须为正数")
        if args.group is not None and args.group <= 0:
            raise PreflightError("group 必须为正整数")
        if args.fixture:
            payload = unwrap(json.loads(args.fixture.read_text()))
            accounts = payload.get("items") if isinstance(payload, dict) else payload
            if not isinstance(accounts, list) or any(not isinstance(a, dict) for a in accounts):
                raise PreflightError("本地快照必须为账号数组或包含 items 的对象")
            if args.platform:
                accounts = [a for a in accounts if a.get("platform") == args.platform]
            if args.group is not None:
                accounts = [a for a in accounts if args.group in (a.get("group_ids") or [])]
        else:
            base = os.environ.get("SUB2API_BASE_URL", "")
            if not base:
                raise PreflightError("请设置 SUB2API_BASE_URL 和 SUB2API_ADMIN_KEY，或使用 --fixture")
            client = Client(base, os.environ.get("SUB2API_ADMIN_KEY", ""), args.timeout)
            accounts = client.accounts(args.platform, args.group)
        print(json.dumps(summarize(accounts), ensure_ascii=False, indent=2))
        return 0
    except PreflightError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError):
        print("输入格式或本地文件无效；未输出原始内容", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
