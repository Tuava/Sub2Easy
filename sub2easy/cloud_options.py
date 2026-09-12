"""Read dropdown choices from the configured sub2api. No secret fields leave here."""

from datetime import datetime, timezone

from sub2easy.preflight import PreflightError, timestamp


def _rows(payload):
    # /groups/all and /proxies/all are unpaginated arrays, not first-page lists.
    if not isinstance(payload, list) or len(payload) > 10000:
        raise PreflightError("分组或代理列表格式不兼容")
    seen = set()
    for row in payload:
        if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0
                or row["id"] in seen or not isinstance(row.get("name"), str)
                or not row["name"].strip() or not isinstance(row.get("status"), str)):
            raise PreflightError("分组或代理列表包含无效数据")
        seen.add(row["id"])
    return payload


def fetch_options(client, now=None):
    now = now or datetime.now(timezone.utc)
    groups, proxies = [], []
    for row in _rows(client.get("/groups/all", {"platform": "openai"})):
        # Filter again locally even if a server ignores the platform query.
        if row.get("platform") == "openai" and row["status"] == "active":
            groups.append({"id": row["id"], "name": row["name"], "platform": "openai"})
    for row in _rows(client.get("/proxies/all")):
        if row["status"] != "active":
            continue
        try:
            expires = timestamp(row.get("expires_at"))
        except (ValueError, OverflowError, OSError):
            raise PreflightError("代理到期时间格式不兼容") from None
        if expires is not None and expires <= now:
            continue
        # Do not serialize upstream proxy objects: older servers may return passwords.
        proxies.append({"id": row["id"], "name": row["name"]})
    key = lambda row: (row["name"].casefold(), row["id"])
    return {"groups": sorted(groups, key=key), "proxies": sorted(proxies, key=key)}
