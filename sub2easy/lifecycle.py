"""Pure lifecycle contracts and planners. No HTTP, secret persistence or auto-writes.

OpenAI OAuth is the first concrete adapter. A provider transport must correlate the
result to its job and verify token identity before execution of the returned plan.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math

from sub2easy.intake import normalize_email, IntakeError
from sub2easy.preflight import timestamp


class ContractError(ValueError):
    """Fixed, redacted contract error code."""


def email(value):
    try:
        return normalize_email(value)
    except IntakeError:
        raise ContractError("INVALID_IDENTITY_EMAIL") from None


def positive_id(value):
    return type(value) is int and value > 0


@dataclass(frozen=True)
class ImportProfile:
    profile_id: str
    revision: int
    instance_id: str
    staging_group_id: int
    target_group_ids: tuple[int, ...]
    proxy_id: int | None = None
    concurrency: int = 1
    priority: int = 50
    rate_multiplier: float = 1.0
    fingerprint_mode: str = "off"
    account_expires_at: int | None = None
    auto_pause_on_expired: bool = True
    platform: str = "openai"
    account_type: str = "oauth"

    def __post_init__(self):
        if (not isinstance(self.profile_id, str) or not self.profile_id
                or not isinstance(self.instance_id, str) or not self.instance_id
                or not positive_id(self.revision)):
            raise ContractError("INVALID_PROFILE_IDENTITY")
        if self.platform != "openai" or self.account_type != "oauth":
            raise ContractError("UNSUPPORTED_PROFILE_ADAPTER")
        if (not positive_id(self.staging_group_id) or not isinstance(self.target_group_ids, tuple)
                or not self.target_group_ids or any(not positive_id(g) for g in self.target_group_ids)
                or len(set(self.target_group_ids)) != len(self.target_group_ids)
                or self.staging_group_id in self.target_group_ids):
            raise ContractError("INVALID_PROFILE_GROUPS")
        if self.proxy_id is not None and not positive_id(self.proxy_id):
            raise ContractError("INVALID_PROFILE_PROXY")
        if not positive_id(self.concurrency) or type(self.priority) is not int or self.priority < 0:
            raise ContractError("INVALID_PROFILE_SCHEDULING")
        if (type(self.rate_multiplier) not in {int, float} or not math.isfinite(self.rate_multiplier)
                or self.rate_multiplier < 0):
            raise ContractError("INVALID_PROFILE_RATE")
        if self.fingerprint_mode not in {"off", "device", "session", "full"}:
            raise ContractError("INVALID_FINGERPRINT_MODE")
        if self.account_expires_at is not None and not positive_id(self.account_expires_at):
            raise ContractError("INVALID_ACCOUNT_EXPIRY")
        if type(self.auto_pause_on_expired) is not bool:
            raise ContractError("INVALID_AUTO_PAUSE")

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ContractError("INVALID_PROFILE")
        values = deepcopy(data)
        if isinstance(values.get("target_group_ids"), list):
            values["target_group_ids"] = tuple(values["target_group_ids"])
        try:
            return cls(**values)
        except TypeError:
            raise ContractError("INVALID_PROFILE_FIELDS") from None


@dataclass(frozen=True)
class OAuthIdentity:
    account_email: str = field(repr=False)
    chatgpt_account_id: str = field(repr=False)
    chatgpt_user_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class AuthorizationResult:
    identity: OAuthIdentity
    credentials: dict = field(repr=False)


# Only credential-identity/token metadata can come from a reauthorization service.
# base_url, model_mapping, routing, quotas, proxy settings and fingerprint seeds cannot.
AUTH_KEYS = frozenset({
    "access_token", "refresh_token", "id_token", "token_type", "expires_at",
    "email", "chatgpt_account_id", "chatgpt_user_id", "organization_id",
    "client_id", "plan_type", "subscription_expires_at",
})
REQUIRED_AUTH_KEYS = frozenset({
    "access_token", "refresh_token", "expires_at", "email", "chatgpt_account_id", "client_id",
})
SENSITIVE_CLOUD_KEYS = frozenset({
    "access_token", "refresh_token", "id_token", "agent_private_key", "api_key", "session_key",
    "cookie", "password", "sso_token", "sso", "sso-rw", "clearTextPassword",
    "aws_secret_access_key", "aws_session_token", "service_account_json", "service_account", "private_key",
    "totp_secret", "totp", "2fa",
})


def normalize_sub2json(payload, expected_email, expected_identity=None, now=None):
    """Accept one account or a one-account sub2api-data/bundle, not arbitrary wrappers.

    This is structural validation, not JWT signature/issuer/audience verification.
    The external API wrapper and identity-verification transport remain to be wired.
    """
    if not isinstance(payload, dict):
        raise ContractError("INVALID_SUB2JSON")
    if "accounts" in payload:
        if (payload.get("type") not in {"sub2api-data", "sub2api-bundle"}
                or type(payload.get("version")) is not int or payload["version"] != 1):
            raise ContractError("UNSUPPORTED_SUB2JSON_VERSION")
        if not isinstance(payload["accounts"], list) or len(payload["accounts"]) != 1:
            raise ContractError("EXPECTED_EXACTLY_ONE_ACCOUNT")
        item = payload["accounts"][0]
    else:
        item = payload
    if not isinstance(item, dict) or item.get("platform") != "openai" or item.get("type") != "oauth":
        raise ContractError("UNSUPPORTED_AUTH_ACCOUNT")
    source = item.get("credentials")
    if not isinstance(source, dict) or not REQUIRED_AUTH_KEYS <= source.keys():
        raise ContractError("INCOMPLETE_AUTH_CREDENTIALS")
    credentials = {k: deepcopy(v) for k, v in source.items() if k in AUTH_KEYS}
    for key, value in credentials.items():
        if key == "expires_at":
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > 32768:
            raise ContractError("INVALID_AUTH_FIELD")
        if key in {"access_token", "refresh_token", "id_token", "chatgpt_account_id", "chatgpt_user_id", "client_id"}:
            if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
                raise ContractError("INVALID_AUTH_FIELD")
    # Vendor templates sometimes contain literal redaction placeholders.
    for key in ("access_token", "refresh_token"):
        if credentials[key] in {"***", "REDACTED", "[REDACTED]"}:
            raise ContractError("REDACTED_AUTH_CREDENTIALS")
    credentials["email"] = email(credentials["email"])
    if credentials["email"] != email(expected_email):
        raise ContractError("AUTH_EMAIL_MISMATCH")
    identity = OAuthIdentity(credentials["email"], credentials["chatgpt_account_id"],
                             credentials.get("chatgpt_user_id"))
    if expected_identity is not None:
        if (email(expected_identity.account_email) != identity.account_email
                or expected_identity.chatgpt_account_id != identity.chatgpt_account_id
                or (expected_identity.chatgpt_user_id is not None
                    and expected_identity.chatgpt_user_id != identity.chatgpt_user_id)):
            raise ContractError("AUTH_SUBJECT_OR_WORKSPACE_MISMATCH")
    now = now or datetime.now(timezone.utc)
    try:
        expiry_value = credentials["expires_at"]
        # Some exporters use digit strings instead of RFC3339/Unix numbers.
        if isinstance(expiry_value, str) and expiry_value.isascii() and expiry_value.isdigit():
            expiry_value = int(expiry_value)
        expires = timestamp(expiry_value)
    except (ValueError, OverflowError, OSError):
        raise ContractError("INVALID_TOKEN_EXPIRY") from None
    if expires is None or (expires - now).total_seconds() < 60:
        raise ContractError("TOKEN_EXPIRED_OR_TOO_CLOSE")
    credentials["expires_at"] = str(int(expires.timestamp()))
    return AuthorizationResult(identity, credentials)


@dataclass(frozen=True)
class PlannedRequest:
    method: str
    path: str
    body: dict = field(repr=False)
    preconditions: tuple[str, ...]


def plan_create(result, profile, local_identity_id):
    if not isinstance(local_identity_id, str) or not local_identity_id or len(local_identity_id) > 80:
        raise ContractError("INVALID_LOCAL_ID")
    normalized = normalize_sub2json(
        {"platform": "openai", "type": "oauth", "credentials": result.credentials},
        result.identity.account_email, result.identity,
    )
    body = {
        "name": "s2e-" + local_identity_id,
        "platform": profile.platform, "type": profile.account_type,
        "credentials": deepcopy(normalized.credentials),
        "group_ids": [profile.staging_group_id], "proxy_id": profile.proxy_id,
        "concurrency": profile.concurrency, "priority": profile.priority,
        "rate_multiplier": profile.rate_multiplier,
        "expires_at": profile.account_expires_at,
        "auto_pause_on_expired": profile.auto_pause_on_expired,
        "extra": {"codex_fingerprint_mode": profile.fingerprint_mode},
    }
    return PlannedRequest("POST", "/api/v1/admin/accounts", body, (
        "verified_provider_job_and_token_identity", "verified_staging_route_isolation",
        "persisted_create_intent_and_idempotency_key", "no_existing_binding_or_ambiguous_creation",
        "disable_scheduling_after_create_before_promotion",
    ))


def plan_reauthorize(cloud_account, result, expected_account_id, expected_identity):
    if not isinstance(cloud_account, dict) or not positive_id(expected_account_id):
        raise ContractError("INVALID_CLOUD_ACCOUNT")
    if cloud_account.get("id") != expected_account_id:
        raise ContractError("CLOUD_BINDING_MISMATCH")
    if cloud_account.get("platform") != "openai" or cloud_account.get("type") != "oauth":
        raise ContractError("UNSUPPORTED_CLOUD_ACCOUNT")
    if cloud_account.get("parent_account_id") is not None:
        raise ContractError("REAUTHORIZE_CREDENTIAL_OWNER_NOT_SHADOW")
    if cloud_account.get("status") not in {"active", "error"}:
        raise ContractError("CLOUD_ACCOUNT_ON_HOLD")
    if cloud_account.get("schedulable") is not False:
        raise ContractError("ACCOUNT_MUST_BE_QUIESCED")
    old = cloud_account.get("credentials")
    if not isinstance(old, dict):
        raise ContractError("CLOUD_CREDENTIAL_METADATA_MISSING")
    if (email(old.get("email")) != email(expected_identity.account_email)
            or old.get("chatgpt_account_id") != expected_identity.chatgpt_account_id
            or (expected_identity.chatgpt_user_id is not None
                and old.get("chatgpt_user_id") != expected_identity.chatgpt_user_id)):
        raise ContractError("CLOUD_SUBJECT_OR_WORKSPACE_MISMATCH")
    # Validate result again at the planner boundary (dataclasses may be constructed by callers).
    normalized = normalize_sub2json(
        {"platform": "openai", "type": "oauth", "credentials": result.credentials},
        expected_identity.account_email, expected_identity,
    )
    # Upstream replaces NON-sensitive credentials rather than merging all keys.
    # Use a fresh cloud read as base, retain model_mapping/base_url/client_id, etc.
    # Never copy old secrets from a stale local token snapshot back to the cloud.
    merged = {k: deepcopy(v) for k, v in old.items() if k not in SENSITIVE_CLOUD_KEYS}
    merged.update(deepcopy(normalized.credentials))
    return PlannedRequest("POST", f"/api/v1/admin/accounts/{expected_account_id}/apply-oauth-credentials",
                          {"type": "oauth", "credentials": merged}, (
        "verified_provider_job_and_token_identity", "credential_owner_lease_and_current_generation",
        "automation_owns_quiescence_not_manual_hold", "fresh_cloud_read_no_concurrent_edits",
        "readback_then_successful_model_probe_before_resuming",
    ))


@dataclass(frozen=True)
class AuthIncident:
    source: str  # upstream / admin_api / reauth_api
    http_status: int
    refresh_state: str = "not_attempted"  # not_attempted / pending / failed / revoked
    managed: bool = False
    has_login_material: bool = False
    manual_hold: bool = False
    job_inflight: bool = False
    fresh: bool = True
    provider_degraded: bool = False
    reauth_attempts_in_window: int = 0


def decide_auth_incident(incident, max_reauth_attempts=2):
    if incident.http_status != 401:
        return "NOT_AN_AUTH_401"
    if incident.source == "admin_api":
        return "PAUSE_INSTANCE_WRITES"
    if incident.source == "reauth_api":
        return "PAUSE_REAUTH_CONNECTOR"
    if incident.source != "upstream":
        return "UNKNOWN_SOURCE"
    if not incident.fresh:
        return "IGNORE_STALE_EVIDENCE"
    if not incident.managed or incident.manual_hold:
        return "OBSERVE_ONLY"
    if incident.job_inflight:
        return "JOIN_EXISTING_JOB"
    if incident.provider_degraded:
        return "WAIT_PROVIDER_RECOVERY"
    if incident.refresh_state in {"not_attempted", "pending"}:
        return "WAIT_NATIVE_REFRESH"
    if incident.refresh_state not in {"failed", "revoked"}:
        return "RECONCILE_REFRESH_RESULT"
    if not incident.has_login_material:
        return "NEEDS_LOGIN_MATERIAL"
    if incident.reauth_attempts_in_window >= max_reauth_attempts:
        return "NEEDS_OPERATOR"
    return "QUEUE_REAUTH"


def configuration_drift(last_applied, observed, desired):
    """Three-way diff of explicitly managed fields; no automatic write is returned.

    Flatten owned fields first, e.g. 'extra.codex_fingerprint_mode'. Never pass
    whole extra/credentials dictionaries or runtime/token fields into this function.
    """
    changes = []
    missing = object()
    for key, want in desired.items():
        before, actual = last_applied.get(key, missing), observed.get(key, missing)
        if before is missing or actual is missing:
            changes.append({"field": key, "kind": "unknown"})
        elif actual == want:
            continue
        elif actual != before:
            changes.append({"field": key, "kind": "external_change_conflict"})
        else:
            changes.append({"field": key, "kind": "template_change_pending"})
    return changes
