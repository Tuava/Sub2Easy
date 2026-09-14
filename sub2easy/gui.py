"""Loopback-only local GUI. Start with python -m sub2easy.gui."""

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path
import secrets
import threading
import time
import webbrowser
import shlex
import re
from urllib.parse import urlsplit, parse_qs

import httpx
from fastapi import FastAPI, Request, Query
from fastapi.responses import FileResponse, JSONResponse, Response
import uvicorn

from sub2easy.intake import IntakeError, parse_batch, has_login_material
from sub2easy.sub2_import import parse_sub2
from sub2easy.cloud_options import fetch_options
from sub2easy.lifecycle import (
    AuthorizationResult, ContractError, ImportProfile, OAuthIdentity, email,
    plan_create, plan_reauthorize,
)
from sub2easy.nvtokens import ConnectorError, NVTConnector, export_document, parse_response, session_value
from sub2easy.preflight import Client, PreflightError, admin_url, summarize, unwrap
from sub2easy.vault import Vault, VaultError
from sub2easy.monitor import AccountMonitor, config_fingerprint
from sub2easy.binding import BindingCenter, metadata
from sub2easy.deployment import DeploymentService
from sub2easy import __version__
from sub2easy.usage_cache import UsageCache
from sub2easy.account_usage import UsageError
from sub2easy.import_workflow import ImportWorkflow
from sub2easy.task_pool import TaskPool
from sub2easy.retirement import RetirementService
from sub2easy.runtime import default_data_dir, private_lock_file, write_private_launcher
from sub2easy import updater


STATIC = Path(__file__).with_name("static")
DEFAULT_PROFILE = {
    "profile_id": "openai-default", "revision": 1, "instance_id": "primary",
    "platform": "openai", "account_type": "oauth", "staging_group_id": 9001,
    "target_group_ids": [1001], "proxy_id": None, "concurrency": 1, "priority": 50,
    "rate_multiplier": 1.0, "fingerprint_mode": "off", "account_expires_at": None,
    "auto_pause_on_expired": True,
}


class DesktopService:
    def __init__(self, vault, connector=None):
        self.vault = vault
        self.connector = connector or NVTConnector()
        self.stop = threading.Event()
        self.operation = threading.RLock()
        self.thread = None
        self.monitor_thread = None
        self.monitor = AccountMonitor(self)
        self.bindings = BindingCenter(self)
        self.deployments = DeploymentService(self)
        self.usage = UsageCache()
        self.import_workflow = ImportWorkflow(self)
        self.tasks = TaskPool(self)
        self.vault.runtime_busy = self.tasks.busy
        self.retirement = RetirementService(self)

    def start(self):
        self.thread = threading.Thread(target=self._loop, daemon=True, name="sub2easy-dispatch")
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True, name="sub2easy-monitor")
        self.thread.start()
        self.monitor_thread.start()

    def _loop(self):
        while not self.stop.wait(0.5):
            try:
                if self.operation.acquire(blocking=False):
                    try:
                        self.import_workflow.resume_budget_blocked()
                        self.import_workflow.resume_stale_precheck()
                    finally:self.operation.release()
                self.tasks.dispatch()
            except Exception:
                if self.vault.key is not None:
                    self.tasks.last_error = 'TASK_DISPATCH_FAILED'

    def _monitor_loop(self):
        while not self.stop.wait(0.5):
            try:
                self.retirement.scan()
                self.monitor.poll()
            except Exception:
                # Never log exception bodies containing upstream credentials.
                continue

    def run_one(self):
        claimed = self.tasks.claim()
        if claimed is None: return False
        self.tasks.execute(*claimed)
        return True

    def run_job(self, job, settings):
        if job['kind'] == 'retire':
            self.retirement.run(job)
            return True
        if job['kind'] == 'auto_reauth':
            self.monitor.run(job)
            return True
        if job['kind'] == 'recovery_continue':
            self.monitor.run_continuation(job)
            return True
        if job['kind'] == 'server_deploy':
            self.deployments.run(job)
            return True
        try:
            a = self.vault.account(job["account_id"])
            if self.vault.cancellation_requested(job['id']):
                self.vault.finish(job['id'],'cancelled','USER_CANCELLED')
                return True
            if not has_login_material(a['login']):raise VaultError('LOGIN_MATERIAL_MISSING')
            if a["revision"] != job["revision"]:
                self.vault.finish(job["id"], "cancelled", "STALE_MATERIAL")
                return True
            binding = a.get("binding")
            identity = OAuthIdentity(**binding["identity"]) if binding else None
            if binding:
                if binding["instance"] != admin_url(self.cloud()["sub2api_url"]):
                    raise VaultError("CLOUD_INSTANCE_CHANGED")
                cloud = self.cloud_read(f"/accounts/{binding['cloud_id']}")
                metadata = cloud.get("credentials") or {}
                if cloud.get("schedulable") is not False or cloud.get("status") not in {"active", "error"}:
                    raise VaultError("ACCOUNT_MUST_BE_QUIESCED")
                if (cloud.get("platform") != "openai" or cloud.get("type") != "oauth"
                        or cloud.get("parent_account_id") is not None
                        or email(metadata.get("email")) != identity.account_email
                        or metadata.get("chatgpt_account_id") != identity.chatgpt_account_id):
                    raise VaultError("CLOUD_IDENTITY_MISMATCH")
            if self.stop.is_set() or self.vault.cancellation_requested(job['id']):
                self.vault.finish(job['id'],'cancelled','USER_CANCELLED')
                return True
            self.vault.update_account(a["id"], status="authorizing", authorization=None, raw_result=None)
            result = self.authorize(settings, a['login'], identity, job['id'])
            # A new result replaces the previous staged result, but never automatically writes to cloud.
            status = "authorized" if result.authorization else "review"
            self.vault.update_account(a["id"], status=status, authorization=result.authorization,
                                      raw_result=result.raw, result_code=result.review_code)
            if binding and result.authorization is None:
                self.monitor.record(a['id'],state='needs_attention',blocked=True,last_code=result.review_code)
            self.vault.finish(job["id"], "succeeded" if result.authorization else "review",
                              result.review_code or "AUTHORIZATION_READY")
        except ConnectorError as exc:
            self.vault.update_account(job["account_id"], status="unknown" if exc.ambiguous else "failed",
                                      authorization=None, raw_result=None, result_code=exc.code)
            self.vault.finish(job["id"], "unknown" if exc.ambiguous else "failed", exc.code, exc.stage)
            if exc.code in {"CONNECTOR_SESSION_EXPIRED", "CONNECTOR_RATE_LIMITED"}:
                self.pause_connector(settings)
        except (VaultError, ContractError, PreflightError) as exc:
            code = "SUB2API_CONNECTION_FAILED" if isinstance(exc, PreflightError) else str(exc)
            cancelled = code == 'TASK_CANCELLED_BEFORE_AUTH'
            self.vault.update_account(job['account_id'],status='local' if cancelled else 'failed')
            self.vault.finish(job["id"], "cancelled" if cancelled else "failed", code)
        except Exception:
            self.vault.update_account(job["account_id"], status="unknown", authorization=None)
            self.vault.finish(job["id"], "unknown", "INTERNAL_RESULT_UNKNOWN")
        return True

    def authorize(self, settings, login, identity=None, job_id=None, guard=None, before_send=None):
        """Separate NVT cap, interruptible wait, and fresh connector state at dispatch."""
        while True:
            if self.stop.is_set() or (job_id and self.vault.cancellation_requested(job_id)):
                raise VaultError('TASK_CANCELLED_BEFORE_AUTH')
            current = self.vault.get_setting('connection', {})
            if current.get('connector_paused') and current.get('nvt_cookie') == settings.get('nvt_cookie'):
                raise ConnectorError('CONNECTOR_SESSION_EXPIRED', stage='preflight')
            if guard: guard()
            cfg = self.tasks.config()
            with self.tasks.mutex:
                if self.tasks.authorizing < cfg['max_authorizations']:
                    self.tasks.authorizing += 1
                    break
            self.stop.wait(0.1)
        try:
            current = self.vault.get_setting('connection', {})
            if (current.get('nvt_cookie') != settings.get('nvt_cookie')
                    or current.get('cloud_revision', 'legacy') != settings.get('cloud_revision', 'legacy')):
                raise VaultError('TASK_CONNECTION_CHANGED')
            if not current.get('nvt_cookie') or current.get('connector_paused'):
                raise ConnectorError('CONNECTOR_SESSION_EXPIRED', stage='preflight')
            if self.stop.is_set() or (job_id and self.vault.cancellation_requested(job_id)):
                raise VaultError('TASK_CANCELLED_BEFORE_AUTH')
            if guard: guard()
            if before_send: before_send()
            try:
                return self.connector.authorize(current['nvt_cookie'], login, identity,
                                                current.get('oauth_client_id', ''))
            except ConnectorError as exc:
                if exc.code in {'CONNECTOR_SESSION_EXPIRED', 'CONNECTOR_RATE_LIMITED'}:
                    self.pause_connector(current)
                raise
        finally:
            with self.tasks.mutex: self.tasks.authorizing -= 1

    def pause_connector(self, observed):
        with self.vault.transaction():
            current = self.vault.get_setting('connection', {})
            # A late failure on an old Cookie must not overwrite a newly saved Key/Cookie.
            if (current.get('nvt_cookie') == observed.get('nvt_cookie')
                    and current.get('cloud_revision','legacy') == observed.get('cloud_revision','legacy')):
                current['connector_paused'] = True
                self.vault.set_setting('connection', current)

    def require_idle(self):
        if self.monitor.poll_lock.locked(): raise VaultError('OPERATION_RUNNING')
        with self.tasks.mutex:
            if self.tasks.active: raise VaultError('TASKS_RUNNING_CONFIG_LOCKED')

    def require_account_idle(self, aid):
        if self.tasks.busy(self.vault.account(aid)) or self.vault.pending(aid):
            raise VaultError('OPERATION_RUNNING')

    def cloud(self):
        setting = self.vault.get_setting("connection", {})
        if not setting.get("sub2api_url") or not setting.get("admin_key"):
            raise VaultError("SUB2API_NOT_CONFIGURED")
        return setting

    def usage_scope(self):
        setting=self.cloud()
        return (admin_url(setting['sub2api_url']),setting.get('cloud_revision','legacy'))

    def usage_result(self,ids,refresh=False):
        setting=self.cloud();scope=self.usage_scope()
        if refresh:return self.usage.request(scope,setting['sub2api_url'],setting['admin_key'],ids)
        return self.usage.snapshot(scope,ids)

    def cloud_read(self, path):
        setting = self.cloud()
        return Client(setting["sub2api_url"], setting["admin_key"]).get(path)

    def cloud_write(self, path, body, idempotency_key=None):
        return self.cloud_mutate('POST',path,body,idempotency_key)

    def cloud_put(self,path,body):
        return self.cloud_mutate('PUT',path,body)

    def cloud_mutate(self,method,path,body,idempotency_key=None):
        setting = self.cloud()
        headers = {"x-api-key": setting["admin_key"], "Accept": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            with httpx.Client(timeout=40, follow_redirects=False, trust_env=False) as client:
                with client.stream(method, admin_url(setting["sub2api_url"]) + path,
                                   json=body, headers=headers) as response:
                    if not 200 <= response.status_code < 300:
                        # Any non-success mutation can be partially applied by the remote service.
                        raise VaultError("CLOUD_WRITE_RESULT_UNKNOWN")
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 8 * 1024 * 1024:
                            raise VaultError("CLOUD_WRITE_RESULT_UNKNOWN")
                    return unwrap(json.loads(raw))
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, VaultError):
                raise
            raise VaultError("CLOUD_WRITE_RESULT_UNKNOWN") from None

    def sync(self):
        setting = self.cloud()
        accounts = Client(setting["sub2api_url"], setting["admin_key"]).accounts()
        report = summarize(accounts)
        report["synced_at"] = time.time()
        self.vault.set_setting("cloud_snapshot", report)
        self.vault.set_setting('cloud_catalog',{'instance':admin_url(setting['sub2api_url']),
            'connection_revision':setting.get('cloud_revision','legacy'),'fetched_at':report['synced_at'],
            'accounts':[metadata(a) for a in accounts]})
        return report

    def options(self):
        setting = self.cloud()
        result = fetch_options(Client(setting["sub2api_url"], setting["admin_key"]))
        return {**result, "instance_id": admin_url(setting["sub2api_url"]),
                "connection_revision": setting.get("cloud_revision", "legacy"), "fetched_at": time.time()}

    def save_profile(self, data):
        values = dict(data)
        revision = values.pop("connection_revision", None)
        valid = ImportProfile.from_dict(values)
        setting = self.cloud()
        if valid.instance_id != admin_url(setting["sub2api_url"]):
            raise VaultError("PROFILE_INSTANCE_MISMATCH")
        if revision != setting.get("cloud_revision", "legacy"):
            raise VaultError("CLOUD_CHOICES_STALE")
        # Re-read on save, rather than trusting stale/forged dropdown IDs.
        choices = self.options()
        group_ids = {g["id"] for g in choices["groups"]}
        if valid.staging_group_id not in group_ids or not set(valid.target_group_ids) <= group_ids:
            raise VaultError("SELECTED_GROUP_UNAVAILABLE")
        if valid.proxy_id is not None and valid.proxy_id not in {p["id"] for p in choices["proxies"]}:
            raise VaultError("SELECTED_PROXY_UNAVAILABLE")
        old = self.vault.get_setting("profile", DEFAULT_PROFILE)
        saved = asdict(valid)
        saved["revision"] = old["revision"] + 1
        self.vault.set_setting("profile", saved)
        return saved

    def bind(self, account_id, cloud_id, expected_fingerprint=None):
        if type(cloud_id) is not int or cloud_id <= 0:
            raise VaultError("INVALID_CLOUD_ID")
        self.require_account_idle(account_id)
        a = self.vault.account(account_id)
        if self.vault.pending(account_id) or a.get('monitor',{}).get('owned_pause'):
            raise VaultError('OPERATION_RUNNING')
        if a.get("binding") or a.get("write_intent"):
            raise VaultError("BINDING_ALREADY_EXISTS_OR_WRITE_PENDING")
        cloud = self.cloud_read(f"/accounts/{cloud_id}")
        if not isinstance(cloud,dict) or cloud.get('id')!=cloud_id:
            raise VaultError('CLOUD_BINDING_MISMATCH')
        if expected_fingerprint is not None and metadata(cloud)['fingerprint']!=expected_fingerprint:
            raise VaultError('CLOUD_CANDIDATE_CHANGED')
        if (not isinstance(cloud, dict) or cloud.get("platform") != "openai" or cloud.get("type") != "oauth"
                or cloud.get("parent_account_id") is not None):
            raise VaultError("UNSUPPORTED_CLOUD_ACCOUNT")
        c = cloud.get("credentials") or {}
        if not isinstance(c,dict):raise VaultError('CLOUD_IDENTITY_METADATA_MISSING')
        try:cloud_email=email(c.get('email'))
        except ContractError:raise VaultError('CLOUD_IDENTITY_METADATA_MISSING') from None
        if cloud_email != a["login"]["account"] or not isinstance(c.get('chatgpt_account_id'),str) or not c['chatgpt_account_id'].strip():
            raise VaultError("CLOUD_IDENTITY_MISMATCH")
        identity = OAuthIdentity(a["login"]["account"], c["chatgpt_account_id"], c.get("chatgpt_user_id"))
        if a.get("authorization"):
            candidate = OAuthIdentity(**a["authorization"]["identity"])
            if (candidate.account_email!=identity.account_email or candidate.chatgpt_account_id!=identity.chatgpt_account_id
                    or (candidate.chatgpt_user_id and candidate.chatgpt_user_id!=identity.chatgpt_user_id)):
                raise VaultError("CLOUD_IDENTITY_MISMATCH")
        instance = admin_url(self.cloud()["sub2api_url"])
        for item in self.vault.accounts():
            b = item["binding"]
            if b and b["instance"] == instance and b["cloud_id"] == cloud_id:
                raise VaultError("CLOUD_ALREADY_BOUND")
        self.vault.update_account(account_id, binding={"cloud_id": cloud_id, "instance": instance,
                                                      "identity": asdict(identity)})

    def write_credentials(self, account_id, staging_verified=False, expected_revision=None, expected_config=None, before_write=None):
        a = self.vault.account(account_id)
        if a.get('retirement'):raise VaultError('ACCOUNT_RETIRED')
        if a.get('deployment') and a['deployment'].get('state')!='complete':
            raise VaultError('DEPLOYMENT_INCOMPLETE')
        if a.get('monitor',{}).get('owned_pause') and expected_revision is None:
            raise VaultError('MONITOR_RECOVERY_NEEDS_REVIEW')
        if a.get("write_intent") and a["write_intent"].get("state") != "confirmed":
            raise VaultError("PREVIOUS_WRITE_NEEDS_RECONCILIATION")
        if not a.get("authorization") or a["status"] != "authorized":
            raise VaultError("VALIDATED_AUTHORIZATION_REQUIRED")
        result = AuthorizationResult(OAuthIdentity(**a["authorization"]["identity"]),
                                     a["authorization"]["credentials"])
        instance = admin_url(self.cloud()["sub2api_url"])
        binding = a.get("binding")
        if binding:
            if binding["instance"] != instance:
                raise VaultError("CLOUD_INSTANCE_CHANGED")
            cloud_id = binding["cloud_id"]
            cloud = self.cloud_read(f"/accounts/{cloud_id}")
            if expected_revision is not None and (cloud.get('updated_at') != expected_revision or config_fingerprint(cloud) != expected_config):
                raise VaultError('CLOUD_CHANGED_DURING_RECOVERY')
            # An explicit separate user action in sub2api is required to pause first.
            # Do not quietly override active traffic or a manual inactive status.
            plan = plan_reauthorize(cloud, result, cloud_id, OAuthIdentity(**binding["identity"]))
        else:
            if not staging_verified:
                raise VaultError("STAGING_ISOLATION_CONFIRMATION_REQUIRED")
            profile = ImportProfile.from_dict(a["profile"])
            if profile.instance_id.startswith(("http://", "https://")) and profile.instance_id != instance:
                raise VaultError("PROFILE_INSTANCE_MISMATCH")
            # Check groups exist before any create call.
            self.cloud_read(f"/groups/{profile.staging_group_id}")
            for group_id in profile.target_group_ids:
                self.cloud_read(f"/groups/{group_id}")
            plan = plan_create(result, profile, account_id)
        if before_write:before_write()
        if self.stop.is_set():raise VaultError('MONITOR_JOB_STALE')
        prior_intent=a.get('write_intent')
        intent = {"state": "pending", "key": "s2e-" + secrets.token_hex(16), "started": time.time()}
        self.vault.update_account(account_id, write_intent=intent)
        try:
            if before_write:before_write()
            if self.stop.is_set():raise VaultError('MONITOR_JOB_STALE')
        except Exception:
            self.vault.update_account(account_id,write_intent=prior_intent)
            raise
        try:
            if binding:
                self.cloud_write(f"/accounts/{cloud_id}/apply-oauth-credentials", plan.body)
            else:
                created = self.cloud_write("/accounts", plan.body, intent["key"])
                if not isinstance(created, dict) or type(created.get("id")) is not int or created["id"] <= 0:
                    raise VaultError("CLOUD_WRITE_RESULT_UNKNOWN")
                cloud_id = created["id"]
                binding = {"cloud_id": cloud_id, "instance": instance, "identity": asdict(result.identity)}
                # Save ID before the second HTTP action so a crash cannot lose the binding.
                self.vault.update_account(account_id, binding=binding)
                self.cloud_write(f"/accounts/{cloud_id}/schedulable", {"schedulable": False})
            observed = self.cloud_read(f"/accounts/{cloud_id}")
            if observed.get("schedulable") is not False:
                raise VaultError("CLOUD_NOT_PAUSED_AFTER_WRITE")
            creds = observed.get("credentials") or {}
            if (email(creds.get("email")) != result.identity.account_email
                    or creds.get("chatgpt_account_id") != result.identity.chatgpt_account_id):
                raise VaultError("CLOUD_IDENTITY_MISMATCH")
            intent["state"] = "confirmed"
            self.vault.update_account(account_id, status="cloud_paused", binding=binding, write_intent=intent,
                                      authorization=None, raw_result=None,
                                      last_applied_auth_digest=self.vault.authorization_digest(a['authorization']))
        except Exception:
            intent["state"] = "unknown"
            self.vault.update_account(account_id, status="write_unknown", write_intent=intent)
            raise VaultError("CLOUD_WRITE_RESULT_UNKNOWN") from None


async def body_object(request):
    try:
        value = await request.json()
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise VaultError("INVALID_JSON_OBJECT") from None


def create_app(directory, port=8765, token=None, connector=None, start_worker=True):
    vault = Vault(directory)
    service = DesktopService(vault, connector)
    token = token or secrets.token_urlsafe(32)
    origin = f"http://127.0.0.1:{port}"
    failed_unlocks = []
    update_cache = {}
    update_lock = threading.Lock()

    def check_update():
        if not update_lock.acquire(blocking=False):raise VaultError('UPDATE_CHECK_RUNNING')
        try:
            if update_cache and time.monotonic()-update_cache['at']<60:return update_cache['result']
            try:result=updater.check()
            except updater.UpdateError as exc:raise VaultError(str(exc)) from None
            result['update_command']=shlex.join(['uv','run','--locked','sub2easy-update','--apply','--yes',
                '--data-dir',str(Path(directory).absolute()),'--expected-commit',result['commit']])
            update_cache.update(at=time.monotonic(),result=result)
            return result
        finally:update_lock.release()

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            service.start()
        try:
            yield
        finally:
            service.stop.set()
            if service.thread:
                # Never close the vault while an in-flight recovery still owns it.
                await asyncio.to_thread(service.thread.join)
            if service.monitor_thread:
                await asyncio.to_thread(service.monitor_thread.join)
            await asyncio.to_thread(service.tasks.close)
            await asyncio.to_thread(service.usage.close)
            vault.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.vault, app.state.service, app.state.token = vault, service, token

    @app.middleware("http")
    async def local_boundary(request, call_next):
        if request.headers.get("host") != f"127.0.0.1:{port}":
            return JSONResponse({"code": "LOCAL_HOST_REQUIRED"}, 403)
        if request.headers.get("origin") not in {None, origin}:
            return JSONResponse({"code": "ORIGIN_REJECTED"}, 403)
        if request.url.path.startswith("/api/"):
            if not secrets.compare_digest(request.headers.get("x-local-token", ""), token):
                return JSONResponse({"code": "LOCAL_SESSION_REQUIRED"}, 401)
            if request.method == "POST":
                if request.headers.get("content-type", "").split(";")[0] != "application/json":
                    return JSONResponse({"code": "JSON_REQUIRED"}, 415)
                # Bound even chunked bodies (no trusting Content-Length alone).
                raw = bytearray()
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > 3 * 1024 * 1024:
                        return JSONResponse({"code": "BODY_TOO_LARGE"}, 413)
                request._body = bytes(raw)
        try:
            response = await call_next(request)
        except Exception:
            # Do not let ASGI default tracebacks serialize validation objects or secret payloads.
            response = JSONResponse({"code": "INTERNAL_OPERATION_ERROR"}, 500)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    for exc_type in (VaultError, IntakeError, ContractError, ConnectorError, PreflightError):
        async def handled(request, exc):
            # Preflight exceptions are fixed text; others are fixed enum codes.
            code = str(exc) if isinstance(exc, UsageError) else "SUB2API_CONNECTION_FAILED" if isinstance(exc, PreflightError) else str(exc)
            return JSONResponse({"code": code}, status_code=400)
        app.add_exception_handler(exc_type, handled)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/app.js")
    async def js():
        return FileResponse(STATIC / "app.js", media_type="text/javascript")

    @app.get("/style.css")
    async def css():
        return FileResponse(STATIC / "style.css", media_type="text/css")

    @app.get('/binding-ui.js')
    async def binding_js():
        return FileResponse(STATIC / 'binding-ui.js',media_type='text/javascript')

    @app.get("/api/status")
    async def status():
        return {"initialized": vault.initialized, "unlocked": vault.key is not None, "version": __version__,
                "features": ["server_deploy", "checkpoint_recovery", "probe_401_retry", "sub2_json_import", "account_usage", "job_cancellation", "inline_import_deploy", "parallel_tasks", "team_lost_retirement", "update_check"]}

    @app.post('/api/updates/check')
    async def updates_check():
        # Read-only public GitHub metadata; no installation, vault material or NVT requests.
        return await asyncio.to_thread(check_update)

    @app.post("/api/unlock")
    async def unlock(request: Request):
        data = await body_object(request)
        if vault.key is not None:
            raise VaultError("VAULT_ALREADY_UNLOCKED")
        now = time.monotonic()
        failed_unlocks[:] = [v for v in failed_unlocks if now - v < 60]
        if len(failed_unlocks) >= 5:
            raise VaultError("UNLOCK_RATE_LIMITED")
        try:
            await asyncio.to_thread(vault.unlock, data.get("password"), data.get("setup") is True)
        except VaultError:
            failed_unlocks.append(now)
            raise
        failed_unlocks.clear()
        return {"ok": True}

    @app.post("/api/lock")
    async def lock():
        if not service.operation.acquire(blocking=False):
            raise VaultError("OPERATION_RUNNING")
        try:
            service.require_idle()
            vault.lock()
            service.usage.invalidate()
        finally:
            service.operation.release()
        return {"ok": True}

    @app.get("/api/state")
    async def state():
        settings = vault.get_setting("connection", {})
        accounts = vault.accounts()
        catalog=vault.get_setting('cloud_catalog',None)
        index={}
        if catalog and settings.get('sub2api_url') and (catalog['instance'],catalog['connection_revision'])==(
                admin_url(settings['sub2api_url']),settings.get('cloud_revision','legacy')):
            index={a['id']:a for a in catalog['accounts']}
        usage_index={}
        instance=admin_url(settings['sub2api_url']) if settings.get('sub2api_url') else None
        if settings.get('sub2api_url') and settings.get('admin_key'):
            bound=[a['binding']['cloud_id'] for a in accounts if a.get('binding') and
                   a['binding']['instance']==admin_url(settings['sub2api_url'])]
            for offset in range(0,len(bound),50):
                usage_index.update({r['account_id']:r for r in service.usage_result(bound[offset:offset+50])['accounts']})
        for account in accounts:
            if account["binding"]:
                account['cloud_metadata']=index.get(account['binding']['cloud_id']) if catalog and account['binding']['instance']==catalog.get('instance') else None
                account["binding"] = {k: v for k, v in account["binding"].items() if k != "identity"}
                account['usage']=usage_index.get(account['binding']['cloud_id']) if account['binding']['instance']==instance else None
        report=service.bindings.report() if settings.get('sub2api_url') and settings.get('admin_key') else None
        return {
            "accounts": accounts, "jobs": vault.jobs(),
            "profile": vault.get_setting("profile", DEFAULT_PROFILE),
            "settings": {"sub2api_url": settings.get("sub2api_url", ""),
                         "instance": admin_url(settings['sub2api_url']) if settings.get('sub2api_url') else None,
                         "oauth_client_id": settings.get("oauth_client_id", ""),
                         "has_cookie": bool(settings.get("nvt_cookie")), "has_admin_key": bool(settings.get("admin_key")),
                         "cloud_revision": settings.get("cloud_revision", "legacy"),
                         "connector_paused": settings.get("connector_paused", False)},
            "cloud": vault.get_setting("cloud_snapshot", None),
            "monitor": service.monitor.view(),
            "task_pool": service.tasks.view(),
            "retirement": service.retirement.view(),
            'binding_summary':{'counts':report['counts'],'updated_at':report['updated_at']} if report else None,
            'deployment_report':vault.get_setting('deployment_report',None),
        }

    @app.post("/api/settings")
    async def settings(request: Request):
        data = await body_object(request)
        if not service.operation.acquire(blocking=False):
            raise VaultError("OPERATION_RUNNING")
        try:
            service.require_idle()
            old = vault.get_setting("connection", {})
            old_cloud = (old.get("sub2api_url", ""), old.get("admin_key", ""))
            if "sub2api_url" in data:
                url = data["sub2api_url"]
                if not isinstance(url, str):
                    raise VaultError("INVALID_SUB2API_URL")
                if url:
                    admin_url(url)
                if old.get("sub2api_url") != url:
                    vault.set_setting("cloud_snapshot", None)
                old["sub2api_url"] = url
            for key in ("admin_key", "oauth_client_id"):
                if key not in data:
                    continue
                value = data.get(key, "")
                if not isinstance(value, str) or len(value) > 8192 or any(c.isspace() for c in value):
                    raise VaultError("INVALID_CONNECTION_FIELD")
                if value or key == "oauth_client_id":
                    old[key] = value
            if data.get("nvt_cookie"):
                old["nvt_cookie"] = session_value(data["nvt_cookie"])
                old["connector_paused"] = False
            if data.get("clear_cookie") is True:
                old.pop("nvt_cookie", None)
            if data.get("clear_admin_key") is True:
                old.pop("admin_key", None)
            if old_cloud != (old.get("sub2api_url", ""), old.get("admin_key", "")):
                old["cloud_revision"] = secrets.token_hex(12)
                vault.set_setting("cloud_snapshot", None)
                vault.set_setting('cloud_catalog',None)
                vault.set_setting('binding_report',None)
                service.usage.invalidate()
            vault.set_setting("connection", old)
        finally:
            service.operation.release()
        return {"ok": True}

    @app.post("/api/profile")
    async def profile(request: Request):
        data = await body_object(request)
        saved = await exclusive(service.save_profile, data)
        return {"ok": True, "profile": saved}

    @app.post("/api/import/preview")
    async def preview(request: Request):
        vault.require_key()
        data = await body_object(request)
        if data.get('format') == 'sub2':
            client_id = vault.get_setting('connection',{}).get('oauth_client_id','')
            return parse_sub2(data.get('text'),client_id).preview()
        if data.get('format', 'login') != 'login':raise IntakeError('UNSUPPORTED_IMPORT_FORMAT')
        return parse_batch(data.get("text")).preview()

    @app.post("/api/import")
    async def import_accounts(request: Request):
        vault.require_key()
        data = await body_object(request)
        # Local intake does not require a live server connection. The real
        # template and isolation checks run before server deployment.
        return service.import_workflow.save_local(data,vault.get_setting('profile',DEFAULT_PROFILE))

    @app.post("/api/jobs")
    async def queue(request: Request):
        data = await body_object(request)
        settings = vault.get_setting("connection", {})
        if not settings.get("nvt_cookie") or settings.get("connector_paused"):
            raise VaultError("CONFIGURE_OR_RENEW_COOKIE")
        ids = data.get("account_ids")
        if not isinstance(ids, list) or not ids or len(ids) > 100 or any(not isinstance(i, str) for i in ids):
            raise VaultError("SELECT_1_TO_100_ACCOUNTS")
        if data.get("confirm_send_to_nvtokens") is not True:
            raise VaultError("CONFIRM_REAUTH_TRANSMISSION")
        results=[];queued=[]
        for aid in dict.fromkeys(ids):
            try:
                job_ids=vault.queue([aid]);queued.extend(job_ids)
                results.append({'account_id':aid,'state':'queued' if job_ids else 'already_pending','job_ids':job_ids})
            except VaultError as exc:
                results.append({'account_id':aid,'state':'failed','code':str(exc)})
        return {"queued":queued,'results':results}

    @app.post('/api/import/deploy')
    async def import_and_deploy(request: Request):
        data=await body_object(request)
        return await exclusive(service.import_workflow.submit,data)

    @app.get('/api/import/batch')
    async def import_batch(request_id: str|None=None):
        return service.import_workflow.get(request_id)

    @app.get('/api/import/history')
    async def import_history():
        return service.import_workflow.history()

    @app.post('/api/import/batch/{request_id}/deploy')
    async def deploy_import_batch(request_id: str, request: Request):
        return await exclusive(service.import_workflow.deploy_saved,request_id,await body_object(request))

    @app.post('/api/import/batch/{request_id}/retry')
    async def retry_import_batch(request_id: str, request: Request):
        data=await body_object(request)
        return await exclusive(service.import_workflow.retry,request_id,data.get('indices'))

    @app.post("/api/jobs/cancel")
    async def cancel():
        vault.cancel_queued()
        return {"ok": True}

    @app.post('/api/jobs/{job_id}/cancel')
    async def cancel_job(job_id: str):
        return vault.cancel_jobs([job_id])

    @app.post('/api/deployments')
    async def deployments(request: Request):
        data=await body_object(request)
        if data.get('confirm_deploy') is not True:raise VaultError('CONFIRM_SERVER_DEPLOYMENT')
        if data.get('connection_revision') is not None and data['connection_revision']!=service.cloud().get('cloud_revision','legacy'):
            raise VaultError('DEPLOY_CONNECTION_CHANGED')
        return await exclusive(service.deployments.queue,data.get('account_ids'),data.get('model_id'),
                               data.get('profile'),data.get('staging_verified') is True,data.get('auto_monitor',True))

    async def exclusive(fn, *args):
        def run():
            if not service.operation.acquire(blocking=False):
                raise VaultError("OPERATION_RUNNING")
            try: return fn(*args)
            finally: service.operation.release()
        return await asyncio.to_thread(run)

    @app.post('/api/tasks/config')
    async def task_config(request: Request):
        return await exclusive(service.tasks.save, await body_object(request))

    @app.post('/api/usage')
    async def usage(request: Request):
        vault.require_key();data=await body_object(request)
        return service.usage_result(data.get('account_ids'),refresh=True)

    @app.get('/api/usage')
    async def cached_usage(ids: str=Query('',max_length=1000)):
        vault.require_key()
        try:account_ids=[int(value) for value in ids.split(',') if value]
        except ValueError:raise VaultError('USAGE_INVALID_ACCOUNT_IDS') from None
        return service.usage_result(account_ids)

    @app.post("/api/cloud/sync")
    async def sync():
        return await exclusive(service.sync)

    @app.post("/api/cloud/options")
    async def options():
        return await exclusive(service.options)

    @app.post('/api/cloud/catalog')
    async def cloud_catalog():
        _,catalog=await exclusive(service.bindings.catalog)
        return {'count':len(catalog['accounts']),'fetched_at':catalog['fetched_at']}

    @app.get('/api/cloud/accounts')
    async def cloud_accounts(q: str=Query('',max_length=200),platform: str='',status: str='',group: str='',
                             proxy: str='',binding: str='',page: int=Query(1,ge=1),page_size: int=Query(25,ge=1,le=100),
                             sort: str='created_desc',since: float|None=None,until: float|None=None):
        result=service.bindings.browse(q,platform,status,group,proxy,binding,page,page_size,sort,since,until)
        rows=result['items'];usage_index={}
        for offset in range(0,len(rows),50):
            usage_index.update({r['account_id']:r for r in service.usage_result([a['id'] for a in rows[offset:offset+50]])['accounts']})
        for row in rows:row['usage']=usage_index.get(row['id'])
        return result

    @app.get('/api/bindings/report')
    async def binding_report():
        vault.require_key()
        return service.bindings.report()

    @app.post('/api/bindings/scan')
    async def binding_scan(request: Request):
        data=await body_object(request)
        auto=data.get('auto_bind') is True
        return await exclusive(service.bindings.scan,data.get('account_ids'),auto)

    @app.post('/api/bindings/resolve')
    async def binding_resolve(request: Request):
        data=await body_object(request)
        if data.get('confirm_selection') is not True:raise VaultError('CONFIRM_BIND_SELECTION')
        return await exclusive(service.bindings.resolve,data.get('account_id'),data.get('cloud_id'),
                               data.get('fingerprint'),data.get('connection_revision'))

    @app.post('/api/monitor/config')
    async def monitor_config(request: Request):
        data=await body_object(request)
        if data.get('enabled') is False:
            return service.monitor.save_config(data)
        return await exclusive(service.monitor.save_config,data)

    @app.post('/api/monitor/accounts')
    async def monitor_accounts(request: Request):
        data=await body_object(request)
        if data.get('enabled') is True and data.get('confirm_auto_reauth') is not True:
            raise VaultError('CONFIRM_AUTO_REAUTH')
        if data.get('enabled') is False:
            service.monitor.set_accounts(data.get('account_ids'),False)
            return {'ok':True}
        await exclusive(service.monitor.set_accounts,data.get('account_ids'),data.get('enabled'))
        return {'ok':True}

    @app.post('/api/monitor/check')
    async def monitor_check():
        await asyncio.to_thread(service.monitor.poll,True)
        return service.monitor.view()

    @app.post('/api/retirement/config')
    async def retirement_config(request: Request):
        result=await exclusive(service.retirement.save_config,await body_object(request))
        return result

    @app.post('/api/retirement/check')
    async def retirement_check():
        await asyncio.to_thread(service.retirement.scan,True)
        return service.retirement.view()

    @app.post('/api/monitor/accounts/{account_id}/acknowledge')
    async def monitor_acknowledge(account_id: str, request: Request):
        data=await body_object(request)
        if data.get('confirm_manually_recovered') is not True:raise VaultError('CONFIRM_MANUAL_RECOVERY')
        await exclusive(service.monitor.acknowledge,account_id)
        return {'ok':True}

    @app.post('/api/monitor/accounts/{account_id}/continue')
    async def continue_recovery(account_id: str, request: Request):
        data=await body_object(request)
        if data.get('confirm_test_and_enable') is not True:raise VaultError('CONFIRM_TEST_AND_ENABLE')
        queued=await exclusive(service.monitor.queue_continuation,account_id,True)
        return {'queued':queued}

    @app.post("/api/accounts/{account_id}/bind")
    async def bind(account_id: str, request: Request):
        data = await body_object(request)
        await exclusive(service.bind, account_id, data.get("cloud_id"))
        return {"ok": True}

    @app.post("/api/accounts/{account_id}/write")
    async def write(account_id: str, request: Request):
        data = await body_object(request)
        if data.get("confirm_write") is not True:
            raise VaultError("CONFIRM_CLOUD_WRITE")
        def write_idle():
            service.require_account_idle(account_id)
            service.write_credentials(account_id, data.get('staging_verified') is True)
        await exclusive(write_idle)
        return {"ok": True}

    @app.get("/api/accounts/{account_id}/export")
    async def export(account_id: str):
        a = vault.account(account_id)
        if a.get("raw_result") is None:
            raise VaultError("NO_RESULT_TO_EXPORT")
        return Response(json.dumps(export_document(a["raw_result"]), ensure_ascii=False, indent=2), media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="sub2-result.json"'})

    @app.post("/api/accounts/{account_id}/revalidate")
    async def revalidate(account_id: str):
        if not service.operation.acquire(blocking=False):
            raise VaultError("OPERATION_RUNNING")
        try:
            service.require_account_idle(account_id)
            a = vault.account(account_id)
            if a.get('retirement'):raise VaultError('ACCOUNT_RETIRED')
            if a.get("raw_result") is None or a["status"] not in {"review", "authorized"}:
                raise VaultError("NO_RESULT_TO_REVALIDATE")
            binding = a.get("binding")
            identity = OAuthIdentity(**binding["identity"]) if binding else None
            result = parse_response(200, json.dumps(a["raw_result"]).encode(), a["login"]["account"], identity,
                                    vault.get_setting("connection", {}).get("oauth_client_id", ""))
            vault.update_account(account_id, status="authorized" if result.authorization else "review",
                                 authorization=result.authorization, result_code=result.review_code)
            return {"validated": result.authorization is not None, "code": result.review_code}
        finally:
            service.operation.release()

    return app


def previous_session(directory, port):
    """Opt-in local upgrade: reuse only the private launcher's matching token."""
    path = Path(directory) / 'launch-url.txt'
    if path.is_symlink() or not path.is_file():return None
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:return None
    try:
        url=urlsplit(path.read_text().strip())
        if url.scheme!='http' or url.netloc!=f'127.0.0.1:{port}' or url.path!='/':return None
        token=parse_qs(url.fragment).get('token',[''])[0]
        return token if re.fullmatch(r'[A-Za-z0-9_-]{32,128}',token) else None
    except (ValueError,OSError):return None


def main():
    parser = argparse.ArgumentParser(description="Sub2Easy 本地图形管理台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=None,
                        help='凭据库目录；默认用户数据目录，已有源码目录的 data/vault.sqlite3 保持沿用')
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument('--reuse-session', action='store_true', help='本地升级时复用当前私有启动会话，避免旧标签页失效')
    args = parser.parse_args()
    args.data_dir = args.data_dir.expanduser().absolute() if args.data_dir else default_data_dir()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be 1024–65535")
    os.umask(0o077)
    args.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        lockfile = private_lock_file(args.data_dir / '.process.lock')
    except OSError:
        parser.exit(2, '无法创建私有进程锁，请检查数据目录权限及符号链接。\n')
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        existing_token = previous_session(args.data_dir, args.port)
        if existing_token and not args.no_browser:
            webbrowser.open(f'http://127.0.0.1:{args.port}/#token={existing_token}')
            return
        parser.exit(2, "Sub2Easy 已在使用这个数据目录；请使用已有启动地址。\n")
    app = create_app(args.data_dir, args.port,
                     token=previous_session(args.data_dir,args.port) if args.reuse_session else None)
    url = f"http://127.0.0.1:{args.port}/#token={app.state.token}"
    # Private local launcher. API token is not injected into unauthenticated HTML.
    write_private_launcher(args.data_dir / 'launch-url.txt', url)
    print(f"Sub2Easy 本地界面：http://127.0.0.1:{args.port}/", flush=True)
    print(f"带本机会话的启动地址保存在：{args.data_dir.resolve() / 'launch-url.txt'}", flush=True)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
