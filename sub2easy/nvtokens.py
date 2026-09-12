"""NVT account-reauthorize connector. No retries, redirects or remote downloads."""

import base64
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import json
import re

import httpx

from sub2easy.lifecycle import ContractError, email, normalize_sub2json


ENDPOINT = "https://nvtokens.com/api/workspace/tools/account-reauthorize"
MAX_RESULT = 4 * 1024 * 1024
KNOWN_CODES = {
    "INCORRECT_CODE", "INVALID_PASSWORD", "ACCOUNT_DISABLED", "RATE_LIMITED",
    "UNAUTHORIZED", "SESSION_EXPIRED", "INVALID_CREDENTIALS", "LOGIN_FAILED",
}


class ConnectorError(ValueError):
    def __init__(self, code, stage="", ambiguous=False):
        super().__init__(code)
        self.code, self.stage, self.ambiguous = code, stage, ambiguous


def session_value(raw):
    if not isinstance(raw, str):
        raise ConnectorError("INVALID_SESSION_COOKIE")
    raw = raw.strip().replace("scm\\_session=", "scm_session=")
    if raw.startswith("scm_session="):
        raw = raw[len("scm_session="):]
    # Accept only the named cookie/value, not a pasted curl or cookie header with other cookies.
    if not re.fullmatch(r"[A-Za-z0-9_.~-]{8,4096}", raw):
        raise ConnectorError("INVALID_SESSION_COOKIE")
    return raw


def request_payload(login):
    return {"mailbox_credential": "----".join(login[k] for k in ("account", "password", "totp_secret")),
            "output_format": "sub2api"}


@dataclass(frozen=True)
class ParsedResult:
    raw: object = field(repr=False)
    authorization: dict | None = field(repr=False)
    review_code: str = ""


def workspace_review_code(candidate, expected_identity, fallback):
    """Distinguish a personal fallback from a failed password or dead request.

    This is a diagnostic of the returned JSON, not proof that team membership
    was removed. Never rewrite the original workspace to make validation pass.
    """
    if fallback != 'AUTH_SUBJECT_OR_WORKSPACE_MISMATCH' or expected_identity is None:
        return fallback
    if not isinstance(candidate, dict):
        return fallback
    items = candidate.get('accounts', [candidate])
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return fallback
    c = items[0].get('credentials')
    if not isinstance(c, dict):
        return fallback
    try:
        same_email = email(c.get('email')) == email(expected_identity.account_email)
    except ContractError:
        return fallback
    same_user = bool(expected_identity.chatgpt_user_id) and c.get('chatgpt_user_id') == expected_identity.chatgpt_user_id
    if same_email and same_user and c.get('chatgpt_account_id') != expected_identity.chatgpt_account_id:
        if c.get('workspace_selection_kind') == 'personal' and c.get('plan_type') == 'free':
            return 'EXPECTED_WORKSPACE_NOT_RETURNED'
        return 'REAUTH_WORKSPACE_CHANGED'
    return fallback


def export_document(payload):
    """Unwrap the documented export body, without trusting filenames or URLs."""
    candidate = deepcopy(payload)
    for _ in range(3):
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except ValueError:
                break
        elif isinstance(candidate, dict) and "credentials" not in candidate and "accounts" not in candidate:
            keys = [k for k in ("account_json", "sub2json", "sub2api", "data") if k in candidate]
            if len(keys) != 1:
                break
            candidate = candidate[keys[0]]
        else:
            break
    return candidate


def _access_token_expiry(token):
    """Read exp as metadata only; this does NOT verify JWT signatures or identity."""
    if not isinstance(token, str) or len(token) > 32768:
        raise ContractError("ACCESS_TOKEN_EXPIRY_UNAVAILABLE")
    parts = token.split(".")
    if len(parts) != 3 or not all(re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts):
        raise ContractError("ACCESS_TOKEN_EXPIRY_UNAVAILABLE")
    try:
        raw = base64.b64decode(parts[1] + "=" * (-len(parts[1]) % 4), altchars=b"-_", validate=True)
        claims = json.loads(raw)
        exp = claims.get("exp") if isinstance(claims, dict) else None
        if type(exp) is not int or exp <= 0:
            raise ValueError
        return str(exp)
    except (ValueError, UnicodeError):
        raise ContractError("ACCESS_TOKEN_EXPIRY_UNAVAILABLE") from None


def _prepare_document(candidate):
    if not isinstance(candidate, dict):
        return
    items = candidate.get("accounts", [candidate])
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("credentials"), dict):
            continue
        c = item["credentials"]
        if c.get("disabled") is True or c.get("expired") is True:
            raise ContractError("PROVIDER_ACCOUNT_DISABLED_OR_EXPIRED")
        # Never substitute id_token.exp or last_refresh + a guessed TTL.
        if "expires_at" not in c and isinstance(c.get("access_token"), str) and c["access_token"].count(".") == 2:
            c["expires_at"] = _access_token_expiry(c["access_token"])


def _validate_summary(payload, result):
    if not isinstance(payload, dict):
        return
    if "account_json" not in payload:
        return
    summary = payload.get("summary")
    if summary is not None:
        if not isinstance(summary, dict):
            raise ContractError("INVALID_PROVIDER_SUMMARY")
        if "identity_verified" in summary and summary["identity_verified"] is not True:
            raise ContractError("PROVIDER_IDENTITY_NOT_VERIFIED")
        if "output_format" in summary and summary["output_format"] != "sub2api":
            raise ContractError("PROVIDER_OUTPUT_FORMAT_MISMATCH")
        if "account_email" in summary and email(summary["account_email"]) != result.identity.account_email:
            raise ContractError("PROVIDER_SUMMARY_IDENTITY_MISMATCH")
        if "selected_workspace_id" in summary and summary["selected_workspace_id"] != result.identity.chatgpt_account_id:
            raise ContractError("PROVIDER_SUMMARY_WORKSPACE_MISMATCH")
    # These aliases in NVT's export represent the same selected workspace/user.
    document = export_document(payload)
    items = document.get("accounts", [document])
    if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict):
        c = items[0].get("credentials", {})
        for alias in ("account_id", "workspace_id"):
            if alias in c and c[alias] != result.identity.chatgpt_account_id:
                raise ContractError("PROVIDER_WORKSPACE_ALIAS_MISMATCH")
        if "user_id" in c and c.get("chatgpt_user_id") != c["user_id"]:
            raise ContractError("PROVIDER_USER_ALIAS_MISMATCH")


def parse_response(status, body, expected_email, expected_identity=None, client_id=""):
    if len(body) > MAX_RESULT:
        raise ConnectorError("RESULT_TOO_LARGE", ambiguous=True)
    if status == 429:
        raise ConnectorError("CONNECTOR_RATE_LIMITED")
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        if status in {401, 403}:
            raise ConnectorError("CONNECTOR_SESSION_EXPIRED") from None
        raise ConnectorError("NON_JSON_RESPONSE", ambiguous=status >= 200) from None
    # Failure body can be returned with HTTP 200. Never display arbitrary server error strings.
    if isinstance(payload, dict) and ("error" in payload or (isinstance(payload.get("code"), str) and payload["code"] in KNOWN_CODES)):
        code = payload.get("code")
        code = code if isinstance(code, str) and code in KNOWN_CODES else "PROVIDER_REJECTED"
        stage = payload.get("stage")
        stage = stage if isinstance(stage, str) and stage in {"protocol_login", "oauth", "export", "login", "reauthorize"} else ""
        if status in {401, 403} and code in {"UNAUTHORIZED", "SESSION_EXPIRED", "PROVIDER_REJECTED"}:
            code = "CONNECTOR_SESSION_EXPIRED"
        raise ConnectorError(code, stage)
    if status in {401, 403}:
        raise ConnectorError("CONNECTOR_SESSION_EXPIRED")
    if not 200 <= status < 300:
        raise ConnectorError("PROVIDER_HTTP_ERROR", ambiguous=status >= 500)
    # File attachment body is the JSON itself. Do not trust or use attachment filenames.
    # Recognize bounded, explicit envelope keys only. Never follow download_url automatically.
    candidate = export_document(payload)
    # Optional explicit OAuth-client default for export formats which omit client_id.
    if client_id and isinstance(candidate, dict):
        candidate = json.loads(json.dumps(candidate))
        items = candidate.get("accounts", [candidate])
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("credentials"), dict):
                    item["credentials"].setdefault("client_id", client_id)
    try:
        _prepare_document(candidate)
        result = normalize_sub2json(candidate, expected_email, expected_identity)
        _validate_summary(payload, result)
        return ParsedResult(payload, asdict(result))
    except ContractError as exc:
        # Preserve encrypted response for explicit local export/review, not an automatic rerun.
        return ParsedResult(payload, None, workspace_review_code(candidate, expected_identity, str(exc)))
    except (TypeError, AttributeError, OverflowError):
        return ParsedResult(payload, None, "INVALID_SUB2JSON")


class NVTConnector:
    def __init__(self, client=None):
        self.client = client

    def authorize(self, cookie, login, expected_identity=None, client_id=""):
        headers = {
            "Accept": "application/json, application/octet-stream;q=0.9, */*;q=0.5",
            "Content-Type": "application/json",
            "Cookie": "scm_session=" + session_value(cookie),
            "Origin": "https://nvtokens.com",
            "Referer": "https://nvtokens.com/workspace/account-reauthorize",
            "User-Agent": "Sub2Easy/0.2 (local account manager)",
        }
        client = self.client or httpx.Client(timeout=httpx.Timeout(180, connect=15), follow_redirects=False, trust_env=False)
        try:
            with client.stream("POST", ENDPOINT, headers=headers, json=request_payload(login), follow_redirects=False) as response:
                if 300 <= response.status_code < 400:
                    raise ConnectorError("REDIRECT_BLOCKED", ambiguous=True)
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESULT:
                        raise ConnectorError("RESULT_TOO_LARGE", ambiguous=True)
                return parse_response(response.status_code, bytes(raw), login["account"], expected_identity, client_id)
        except httpx.HTTPError:
            # May have been accepted upstream; repeating automatically can rotate sessions twice.
            raise ConnectorError("NETWORK_RESULT_UNKNOWN", ambiguous=True) from None
        finally:
            if self.client is None:
                client.close()
