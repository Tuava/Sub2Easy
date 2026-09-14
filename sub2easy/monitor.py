"""Persistent, opt-in 401 recovery for bound credential-owner accounts.

Independent polling, short coordination locks, and per-account worker ownership.
All remote mutations are journaled; ambiguous steps stop instead of replaying.
"""

from datetime import datetime, timezone
import hashlib
import json
import math
import re
import secrets
import time
import threading

import httpx

from sub2easy.lifecycle import OAuthIdentity, email, normalize_sub2json
from sub2easy.nvtokens import ConnectorError
from sub2easy.preflight import Client, PreflightError, admin_url, timestamp
from sub2easy.vault import VaultError
from sub2easy.intake import has_login_material


DEFAULT_MONITOR = {
    'enabled': False, 'interval_seconds': 60, 'grace_seconds': 120,
    'model_id': '', 'resume_after_success': True, 'max_per_hour': 6,
    # The requested recovery contract is: a managed account with a clear 401 may
    # be re-enabled after a successful credential update and probe. Keep this
    # opt-out rather than requiring a second hidden switch for the main flow.
    'resume_paused_401': True,
}


def require_fresh_checkpoint(checkpoint):
    created = checkpoint.get('created') if isinstance(checkpoint, dict) else None
    if (type(created) not in (int, float) or not math.isfinite(created)
            or not 0 <= time.time() - created <= 86400):
        raise VaultError('NO_SAFE_RECOVERY_CHECKPOINT')


def auth_signal(account, now=None):
    """Recognize the upstream handler's account-auth prefixes, not arbitrary '401'."""
    now = now or datetime.now(timezone.utc)
    if account.get('platform') != 'openai' or account.get('type') != 'oauth' or account.get('parent_account_id') is not None:
        return None
    if account.get('status') == 'error':
        reason = account.get('error_message')
    elif account.get('status') == 'active':
        try:
            until = timestamp(account.get('temp_unschedulable_until'))
        except (ValueError, OverflowError, OSError):
            return None
        if until is None or until <= now:
            return None
        reason = account.get('temp_unschedulable_reason')
    else:
        return None
    if not isinstance(reason, str):
        return None
    reason = reason.strip()
    prefixes = ('Authentication failed (401):', 'OAuth 401:', 'OAuth 401 (no refresh_token):',
                'Token revoked (401):', 'Unauthorized (401):')
    if not reason.startswith(prefixes):
        return None
    # Hash for deduplication; do not persist or display upstream error bodies.
    return hashlib.sha256(json.dumps([account.get('id'),reason,account.get('temp_unschedulable_until')],
                                     sort_keys=True).encode()).hexdigest()


def guard_identity(cloud, binding):
    if (not isinstance(cloud, dict) or cloud.get('id') != binding['cloud_id']
            or cloud.get('platform') != 'openai' or cloud.get('type') != 'oauth'
            or cloud.get('parent_account_id') is not None):
        raise VaultError('CLOUD_BINDING_MISMATCH')
    c = cloud.get('credentials') or {}
    identity = binding['identity']
    if (email(c.get('email')) != identity['account_email']
            or c.get('chatgpt_account_id') != identity['chatgpt_account_id']
            or (identity.get('chatgpt_user_id') and c.get('chatgpt_user_id') != identity['chatgpt_user_id'])):
        raise VaultError('CLOUD_IDENTITY_MISMATCH')


def config_fingerprint(cloud):
    # Exclude changing usage/cooldown/token metadata; only fields automation must preserve.
    fields = ('group_ids','proxy_id','priority','concurrency','rate_multiplier','load_factor',
              'expires_at','auto_pause_on_expired','name','notes')
    c = cloud.get('credentials') or {}
    extra = cloud.get('extra') or {}
    value = {key: cloud.get(key) for key in fields}
    value['credentials'] = {k:c[k] for k in ('model_mapping','base_url','custom_headers') if k in c}
    value['extra'] = {k:extra[k] for k in ('codex_fingerprint_mode','codex_fingerprint_seed','privacy_mode',
                        'base_rpm','window_cost_limit','max_sessions','quota_limit','quota_daily_limit','quota_weekly_limit') if k in extra}
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def parse_test_sse(lines):
    """Only an explicit completion is successful; 200, [DONE], EOF aren't enough."""
    done = False
    data_lines = []
    total = 0

    def event(data):
        if not data:
            return False
        try:
            obj = json.loads('\n'.join(data))
        except ValueError:
            raise VaultError('PROBE_INVALID_SSE') from None
        if not isinstance(obj, dict):
            raise VaultError('PROBE_INVALID_SSE')
        if obj.get('type') in {'error','test_error'} or obj.get('error'):
            # Exact upstream test-handler prefix, not arbitrary numbers in a body.
            message = obj.get('error')
            match = re.match(r'^API returned (\d{3}):', message) if isinstance(message,str) else None
            if match:
                code = {'401':'PROBE_AUTH_401','403':'PROBE_ACCESS_DENIED',
                        '429':'PROBE_RATE_LIMITED','502':'PROBE_UPSTREAM_UNAVAILABLE',
                        '503':'PROBE_UPSTREAM_UNAVAILABLE','504':'PROBE_UPSTREAM_UNAVAILABLE'}.get(match[1])
                if code:raise VaultError(code)
            raise VaultError('PROBE_FAILED')
        if obj.get('type') == 'test_complete':
            if obj.get('success') is not True:
                raise VaultError('PROBE_FAILED')
            return True
        return False

    for line in lines:
        total += len(line.encode('utf-8'))
        if total > 1024 * 1024:
            raise VaultError('PROBE_RESPONSE_TOO_LARGE')
        if not line:
            done = event(data_lines) or done
            data_lines = []
        elif line.startswith('data:'):
            data_lines.append(line[5:].lstrip())
    # Require terminated SSE frames: a truncated final completion is not trusted.
    if data_lines or not done:
        raise VaultError('PROBE_INCOMPLETE')
    return True


class AccountMonitor:
    def __init__(self, service):
        self.service, self.vault = service, service.vault
        self.next_poll = 0
        self.poll_lock = threading.Lock()

    def config(self):
        return {**DEFAULT_MONITOR, **self.vault.get_setting('monitor_config', {})}

    def save_config(self, data):
        with self.vault.transaction():
            cfg = self.config()
            for key in DEFAULT_MONITOR:
                if key in data: cfg[key] = data[key]
            for key, low, high in [('interval_seconds',30,3600),('grace_seconds',60,1800),('max_per_hour',1,60)]:
                if type(cfg[key]) is not int or not low <= cfg[key] <= high:
                    raise VaultError('INVALID_MONITOR_CONFIG')
            if any(type(cfg[k]) is not bool for k in ('enabled','resume_after_success','resume_paused_401')):
                raise VaultError('INVALID_MONITOR_CONFIG')
            if not isinstance(cfg['model_id'],str) or len(cfg['model_id']) > 160 or any(ord(c)<32 for c in cfg['model_id']):
                raise VaultError('INVALID_MONITOR_MODEL')
            cfg['model_id'] = cfg['model_id'].strip()
            if cfg['enabled']:
                setting = self.service.cloud()
                if not setting.get('nvt_cookie') or setting.get('connector_paused'):
                    raise VaultError('CONFIGURE_OR_RENEW_COOKIE')
                if not cfg['model_id']:
                    raise VaultError('MONITOR_MODEL_REQUIRED')
                if data.get('confirm_auto_reauth') is not True:
                    raise VaultError('CONFIRM_AUTO_REAUTH')
                cfg['instance'] = admin_url(setting['sub2api_url'])
                cfg['connection_revision'] = setting.get('cloud_revision','legacy')
            cfg['generation'] = secrets.token_hex(12)
            self.vault.set_setting('monitor_config',cfg)
            self.vault.cancel_auto()
            self.next_poll = 0
            return cfg

    def set_accounts(self, ids, enabled):
        if not isinstance(ids,list) or not ids or len(ids)>100 or any(not isinstance(i,str) for i in ids) or type(enabled) is not bool:
            raise VaultError('INVALID_MONITOR_SELECTION')
        setting = self.service.cloud() if enabled else None
        instance = admin_url(setting['sub2api_url']) if setting else None
        accounts = [self.vault.account(i) for i in set(ids)]
        if enabled:
            for a in accounts:
                if a.get('retirement'):raise VaultError('ACCOUNT_RETIRED')
                self.service.require_account_idle(a['id'])
            owners = set()
            for other in self.vault.accounts():
                if other['id'] not in ids and other['monitor'].get('enabled'):
                    b=other.get('binding')
                    if b:owners.add(b['identity']['account_email'])
            for a in accounts:
                b = a.get('binding')
                if not b or b['instance'] != instance:
                    raise VaultError('BIND_ACCOUNT_BEFORE_MONITORING')
                guard_identity(self.service.cloud_read(f"/accounts/{b['cloud_id']}"),b)
                owner=b['identity']['account_email']
                if owner in owners:
                    raise VaultError('DUPLICATE_CREDENTIAL_OWNER')
                owners.add(owner)
        for a in accounts:
            with self.vault.transaction():
                m = self.vault.account(a['id']).get('monitor',{})
                state='watching'
                if m.get('blocked') or m.get('owned_pause'):state='needs_attention'
                elif m.get('last_code')=='AUTO_REAUTH_RECOVERED_PAUSED':state='recovered_paused'
                self.record(a['id'],enabled=enabled,state=state if enabled else 'disabled')
                if not enabled:self.vault.cancel_auto(a['id'])
        self.next_poll = 0

    def prepare_deployment(self, model_id):
        """Explicit deploy-and-monitor choice starts polling, even for Token-only imports.

        Cookie is required at reauthorization time, not for observing account health.
        Preserve an already-running generation so other recovery jobs aren't cancelled.
        """
        settings = self.service.cloud()
        instance = admin_url(settings['sub2api_url'])
        revision = settings.get('cloud_revision', 'legacy')
        cfg = self.config()
        if cfg['enabled'] and (cfg.get('instance'), cfg.get('connection_revision')) != (instance, revision):
            raise VaultError('MONITOR_CONNECTION_CHANGED')
        if not cfg['enabled']:
            cfg.update(enabled=True, instance=instance, connection_revision=revision,
                       model_id=model_id, generation=secrets.token_hex(12))
            self.vault.set_setting('monitor_config', cfg)
            self.vault.set_setting('monitor_runtime', {'state':'checking'})
            self.next_poll = 0
        return cfg['generation']

    def enroll_deployed(self, aid, dep, cloud):
        """Enroll only after final cloud readback; never turn a stopped monitor back on."""
        if not dep.get('auto_monitor'):
            return
        cfg = self.config()
        if (not cfg['enabled'] or cfg.get('generation') != dep.get('monitor_generation')
                or cfg.get('instance') != dep['instance']
                or cfg.get('connection_revision') != dep['connection_revision']):
            self.record(aid, enrollment_code='MONITOR_STOPPED_DURING_DEPLOY')
            return
        a = self.vault.account(aid)
        guard_identity(cloud, a['binding'])
        owner = a['binding']['identity']['account_email']
        for other in self.vault.accounts():
            b = other.get('binding')
            if (other['id'] != aid and other['monitor'].get('enabled') and b
                    and b['instance'] == dep['instance'] and b['identity']['account_email'] == owner):
                self.record(aid, enrollment_code='DUPLICATE_CREDENTIAL_OWNER')
                return
        self.record(aid, enabled=True, state='watching', model_id=dep['model_id'],
                    enrollment_code='', last_code='', last_check=time.time(),
                    cloud_status=cloud['status'], cloud_schedulable=cloud['schedulable'], auth_401=False)
        self.next_poll = 0

    def adopt_legacy_deployments(self, cfg):
        """Repair old successful deployments missing enrollment; respect explicit opt-outs.

        Cloud reads only. Existing monitor enable/disable choices, incomplete writes,
        changed identities/configuration and manual holds are never overwritten.
        """
        if not cfg['enabled']:return
        from sub2easy.deployment import fingerprint
        for row in self.vault.accounts():
            a=self.vault.account(row['id']);dep=a.get('deployment') or {};m=a.get('monitor',{})
            if ('enabled' in m or 'auto_monitor' in dep or dep.get('state')!='complete'
                    or not dep.get('verified_at') or self.vault.pending(a['id'])
                    or m.get('blocked') or m.get('owned_pause') or a['status']!='active'
                    or (a.get('write_intent') and a['write_intent'].get('state')!='confirmed')
                    or (dep.get('instance'),dep.get('connection_revision')) !=
                       (cfg.get('instance'),cfg.get('connection_revision'))):continue
            self._configured(cfg,require_cookie=False)
            cloud=self.service.cloud_read(f"/accounts/{dep['cloud_id']}")
            if self.service.stop.is_set() or self.config().get('generation')!=cfg.get('generation'):return
            try:guard_identity(cloud,a['binding'])
            except (VaultError,ValueError):continue
            if (cloud.get('status')!='active' or cloud.get('schedulable') is not True
                    or fingerprint(cloud)!=dep.get('baseline')):continue
            with self.vault.transaction():
                dep.update(auto_monitor=True,monitor_generation=cfg['generation'])
                self.enroll_deployed(a['id'],dep,cloud)
                self.vault.update_account(a['id'],deployment=dep)

    def acknowledge(self, account_id):
        """Operator explicitly resolved a stopped recovery in sub2api; never toggles remote state."""
        self.service.require_account_idle(account_id)
        a=self.vault.account(account_id);b=a.get('binding')
        if a.get('retirement'):raise VaultError('ACCOUNT_RETIRED')
        if not b or b['instance']!=admin_url(self.service.cloud()['sub2api_url']):
            raise VaultError('CLOUD_BINDING_MISMATCH')
        if self.vault.pending(account_id):raise VaultError('OPERATION_RUNNING')
        observed=self.service.cloud_read(f"/accounts/{b['cloud_id']}");guard_identity(observed,b)
        if observed.get('status')!='active' or observed.get('schedulable') is not True or auth_signal(observed):
            raise VaultError('ACCOUNT_NOT_MANUALLY_RECOVERED')
        # Ambiguous cloud mutation requires explicit reconciliation, not acknowledgement alone.
        if a.get('write_intent') and a['write_intent'].get('state')!='confirmed':
            raise VaultError('PREVIOUS_WRITE_NEEDS_RECONCILIATION')
        self.record(account_id,blocked=False,owned_pause=None,last_code='',state='watching',first_seen=None,
                    signal=None,consumed_signal=None,automatic_reauth=None,probe_reauth_attempts=0,
                    continuation=None,next_retry=None,continuation_attempts=0,recovery_evidence=None)
        self.vault.update_account(account_id,status='active')

    def view(self):
        cfg=self.config();run=self.vault.get_setting('monitor_runtime',{})
        return {'config':{k:cfg[k] for k in DEFAULT_MONITOR}, 'runtime':run}

    def record(self, account_id, **changes):
        with self.vault.transaction():
            a=self.vault.account(account_id);m=a.get('monitor',{});m.update(changes)
            self.vault.update_account(account_id,monitor=m)
            return m

    def _configured(self, cfg, require_cookie=True):
        setting=self.service.cloud()
        if (cfg.get('instance')!=admin_url(setting['sub2api_url'])
                or cfg.get('connection_revision')!=setting.get('cloud_revision','legacy')):
            raise VaultError('MONITOR_CONNECTION_CHANGED')
        if require_cookie and (not setting.get('nvt_cookie') or setting.get('connector_paused')):
            raise VaultError('CONFIGURE_OR_RENEW_COOKIE')
        return setting

    def queue_continuation(self, account_id, enable_after_test=True, automatic=False):
        """Resume only known, unambiguous local checkpoints. Never log in again."""
        if type(enable_after_test) is not bool or type(automatic) is not bool:
            raise VaultError('INVALID_MONITOR_SELECTION')
        cfg=self.config();self._configured(cfg,require_cookie=False)
        a=self.vault.account(account_id);m=a.get('monitor',{});b=a.get('binding')
        if self.service.stop.is_set() or not cfg['enabled'] or not m.get('enabled') or not b or b['instance']!=cfg.get('instance'):
            raise VaultError('MONITOR_JOB_STALE')
        if self.vault.pending(account_id):raise VaultError('OPERATION_RUNNING')
        intent=a.get('write_intent')
        if intent and intent.get('state')!='confirmed':raise VaultError('PREVIOUS_WRITE_NEEDS_RECONCILIATION')
        if a['status'] in {'unknown','write_unknown','review'}:raise VaultError('RESULT_REVIEW_REQUIRED_BEFORE_RETRY')
        checkpoint=m.get('continuation')
        legacy_baseline=False
        owner=m.get('owned_pause') or {}
        # Backward-compatible recovery of the observed v0.2 read-before-apply failure.
        if not checkpoint and m.get('last_code')=='MONITOR_FETCH_FAILED' and a.get('authorization') and owner.get('state')=='confirmed' and not intent:
            checkpoint={'step':'apply','base':owner.get('baseline'),'revision':owner.get('revision'),
                        'resume':enable_after_test,
                        'created':self.vault.job_checkpoint_time(owner.get('job_id'), account_id)}
        if not checkpoint and m.get('last_code')=='AUTO_REAUTH_RECOVERED_PAUSED':
            # A previous marker alone is not proof of a successful credential write.
            if not intent or intent.get('state') != 'confirmed':
                raise VaultError('NO_SAFE_RECOVERY_CHECKPOINT')
            evidence = m.get('recovery_evidence') or {}
            checkpoint={'step':'verify','base':evidence.get('base'),'created':m.get('last_recovered'),
                        'resume':enable_after_test}
            for key in ('binding','account_revision'):
                if key in evidence:checkpoint[key]=evidence[key]
            # Older records with no historical configuration baseline require an
            # explicit operator action, not an automatic migration on every poll.
            if automatic and not checkpoint['base']:
                raise VaultError('RECOVERY_BASELINE_REVIEW_REQUIRED')
            legacy_baseline=not automatic and not checkpoint['base']
        require_fresh_checkpoint(checkpoint)
        if (('binding' in checkpoint and checkpoint['binding']!=b)
                or ('account_revision' in checkpoint and checkpoint['account_revision']!=a['revision'])):
            raise VaultError('CLOUD_CHANGED_DURING_RECOVERY')
        if not checkpoint.get('base') and not legacy_baseline:
            raise VaultError('NO_SAFE_RECOVERY_CHECKPOINT')
        if automatic and enable_after_test and not cfg['resume_after_success']:
            raise VaultError('AUTO_RESUME_DISABLED')
        if checkpoint.get('base'):
            # Preserve original migration time before a potentially failing GET.
            self.record(account_id,continuation=checkpoint)
        cloud=self.service.cloud_read(f"/accounts/{b['cloud_id']}");guard_identity(cloud,b)
        if cloud.get('schedulable') is not False or cloud.get('status') not in {'active','error'}:
            raise VaultError('CLOUD_ACCOUNT_ON_HOLD')
        base=config_fingerprint(cloud)
        if checkpoint.get('base') and checkpoint['base']!=base:raise VaultError('CLOUD_CHANGED_DURING_RECOVERY')
        if checkpoint.get('step')=='apply':
            if intent or not a.get('authorization'):raise VaultError('NO_SAFE_RECOVERY_CHECKPOINT')
            if not checkpoint.get('base') or not checkpoint.get('revision'):
                raise VaultError('NO_SAFE_RECOVERY_CHECKPOINT')
            if cloud.get('updated_at')!=checkpoint.get('revision'):raise VaultError('CLOUD_CHANGED_DURING_RECOVERY')
            normalize_sub2json({'platform':'openai','type':'oauth','credentials':a['authorization']['credentials']},
                              b['identity']['account_email'],OAuthIdentity(**b['identity']))
        elif checkpoint.get('step')=='verify':
            if cloud.get('status') != 'active' or not intent or intent.get('state') != 'confirmed':
                raise VaultError('NO_SAFE_RECOVERY_CHECKPOINT')
        else:raise VaultError('NO_SAFE_RECOVERY_CHECKPOINT')
        current=self.vault.account(account_id)
        if (self.service.stop.is_set() or self.config().get('generation')!=cfg.get('generation')
                or not current.get('monitor',{}).get('enabled') or current['revision']!=a['revision']
                or current.get('binding')!=b):
            raise VaultError('MONITOR_JOB_STALE')
        require_fresh_checkpoint(checkpoint)
        checkpoint={**checkpoint,'base':base,'resume':bool(enable_after_test),'automatic':automatic}
        # Keep the validated legacy evidence even if the queue is temporarily full.
        self.record(account_id,continuation=checkpoint)
        auth_digest=hashlib.sha256(json.dumps(a.get('authorization'),sort_keys=True).encode()).hexdigest()
        with self.vault.transaction():
            jobs=self.vault.queue([account_id],kind='recovery_continue',context={
                'binding':b,'generation':cfg['generation'],'checkpoint':checkpoint,'auth_digest':auth_digest,
                'model_id':m.get('model_id') or cfg['model_id'],
            })
            if jobs:self.record(account_id,state='continuation_queued',continuation=checkpoint,next_retry=None,last_code='')
        return jobs

    def _save_retry(self, account_id, exc, phase, job):
        """Only GET errors after an established checkpoint can be continued automatically."""
        a=self.vault.account(account_id);m=a.get('monitor',{});cp=m.get('continuation');intent=a.get('write_intent')
        retryable=isinstance(exc,PreflightError) and exc.retryable and exc.http_status not in {401,403}
        compatible=(cp and phase in {'applying','verifying'} and a['status'] not in {'unknown','write_unknown','review'}
                    and (not intent or intent.get('state')=='confirmed'))
        try:require_fresh_checkpoint(cp)
        except VaultError:compatible=False
        if compatible:
            compatible=bool(cp.get('base') and (
                (cp.get('step')=='apply' and not intent and a.get('authorization') and cp.get('revision'))
                or (cp.get('step')=='verify' and intent and intent.get('state')=='confirmed')))
        attempts=m.get('continuation_attempts',0)+1
        if retryable and compatible and attempts<=3:
            self.record(account_id,state='retry_wait',continuation_attempts=attempts,
                        next_retry=time.time()+min(300,30*attempts),last_http_status=exc.http_status)
            return True
        return False

    def _continuation_error(self, account_id, exc, now):
        """Keep transient queue/read failures actionable without losing the checkpoint."""
        code=('ADMIN_AUTH_FAILED' if exc.http_status in {401,403} else 'MONITOR_FETCH_FAILED') if isinstance(exc,PreflightError) else str(exc) if isinstance(exc,VaultError) else 'RECOVERY_VALIDATION_FAILED'
        self.record(account_id,state='needs_attention',blocked=True,last_code=code,next_retry=None)
        if code=='RECOVERY_CONTINUE_BUDGET':
            self.record(account_id,state='rate_budget',next_retry=now+1800)
        else:
            self._save_retry(account_id,exc,'verifying',{})

    def _schedule_probe_reauth(self, account_id, exc, phase, job):
        """Known failed probe + fresh matching 401 may start a NEW bounded repair.

        Not used for an ambiguous POST, wrong workspace, CAPTCHA, 403, or 429.
        Retains the disabled account until repair and a successful probe finish.
        """
        if str(exc) != 'PROBE_AUTH_401' or phase != 'verifying':return False
        a=self.vault.account(account_id);m=a.get('monitor',{});b=a.get('binding')
        if not has_login_material(a['login']):
            self.record(account_id,state='login_material_missing',last_code='LOGIN_MATERIAL_MISSING',automatic_reauth=None)
            return False
        intent=a.get('write_intent')
        if not b or not intent or intent.get('state')!='confirmed' or a['status'] in {'unknown','write_unknown','review'}:return False
        ctx=job.get('context',{});cp=ctx.get('checkpoint') or m.get('continuation') or {}
        try:require_fresh_checkpoint(cp)
        except VaultError:return False
        base=cp.get('base') or (m.get('owned_pause') or {}).get('baseline')
        if not base:return False
        try:
            observed=self.service.cloud_read(f"/accounts/{b['cloud_id']}");guard_identity(observed,b)
            signal=auth_signal(observed)
            if not signal or observed.get('schedulable') is not False or config_fingerprint(observed)!=base:return False
        except (VaultError,PreflightError,ValueError):return False
        if m.get('probe_reauth_attempts',0)>=1:
            self.record(account_id,last_code='REAUTH_STILL_UNAUTHORIZED',blocked=True,state='needs_attention',
                        automatic_reauth=None)
            return False
        cfg=self.config()
        try:
            self._configured(cfg,require_cookie=False)
            if (self.service.stop.is_set() or not cfg['enabled'] or not m.get('enabled')
                    or ctx.get('generation')!=cfg.get('generation')):return False
        except (VaultError,PreflightError):return False
        resume=bool(cp.get('resume',ctx.get('resume',False)))
        self.record(account_id,state='reauth_retry_wait',last_code='PROBE_AUTH_401',blocked=False,auth_401=True,
                    automatic_reauth={'base':base,'signal':signal,'binding':b,'revision':a['revision'],
                                      'generation':cfg.get('generation'),'resume':resume,
                                      'created':cp['created'],'due':time.time()+cfg['grace_seconds']},next_retry=None)
        return True

    def _schedule_connector_retry(self, account_id, exc, phase, job):
        """A definite connector rejection may wait for a renewed cookie, never an unknown login."""
        if (not isinstance(exc,ConnectorError) or exc.ambiguous or phase!='reauthorizing'
                or exc.code not in {'CONNECTOR_SESSION_EXPIRED','CONNECTOR_RATE_LIMITED'}):return False
        a=self.vault.account(account_id);m=a.get('monitor',{});owner=m.get('owned_pause') or {}
        ctx=job.get('context',{});cfg=self.config();b=a.get('binding');intent=a.get('write_intent')
        checkpoint=ctx.get('recovery_checkpoint') or owner
        try:
            require_fresh_checkpoint(checkpoint)
            self._configured(cfg,require_cookie=False)
            if (self.service.stop.is_set() or not cfg['enabled'] or not m.get('enabled')
                    or cfg.get('generation')!=ctx.get('generation') or b!=ctx.get('binding')
                    or a['revision']!=job['revision'] or owner.get('state')!='confirmed'
                    or a['status'] in {'unknown','write_unknown','review'} or a.get('authorization')
                    or (intent and intent.get('state')!='confirmed')):return False
            observed=self.service.cloud_read(f"/accounts/{b['cloud_id']}");guard_identity(observed,b)
            signal=auth_signal(observed)
            if (not signal or observed.get('schedulable') is not False
                    or observed.get('updated_at')!=owner.get('revision')
                    or config_fingerprint(observed)!=owner.get('baseline')):return False
        except (VaultError,PreflightError,ValueError):return False
        self.record(account_id,state='waiting_connector',blocked=False,last_code=exc.code,next_retry=None,
                    automatic_reauth={'base':owner['baseline'],'signal':signal,'binding':b,
                        'revision':a['revision'],'generation':cfg['generation'],'resume':ctx['resume'],
                        'created':checkpoint['created'],'due':time.time(),'connector_retry':True,
                        'cloud_revision':owner['revision']})
        return True

    def _dispatch_probe_reauth(self, a, cloud, cfg, now):
        retry=a.get('monitor',{}).get('automatic_reauth')
        if not retry:return False
        try:require_fresh_checkpoint(retry)
        except VaultError as exc:
            self.record(a['id'],automatic_reauth=None,blocked=True,state='needs_attention',last_code=str(exc))
            return True
        if now<retry['due']:
            self.record(a['id'],state='reauth_retry_wait');return True
        if (retry.get('generation')!=cfg.get('generation') or retry.get('binding')!=a.get('binding')
                or retry.get('revision')!=a['revision'] or cloud.get('schedulable') is not False
                or config_fingerprint(cloud)!=retry['base']
                or (retry.get('connector_retry') and cloud.get('updated_at')!=retry.get('cloud_revision'))):
            self.record(a['id'],automatic_reauth=None,blocked=True,state='needs_attention',
                        last_code='CLOUD_CHANGED_DURING_RECOVERY');return True
        signal=auth_signal(cloud)
        if not signal:
            # Another actor recovered it; this is not permission to turn it on.
            self.record(a['id'],automatic_reauth=None,blocked=True,state='needs_attention',
                        last_code='AUTH_SIGNAL_CLEARED');return True
        try:
            if self.service.stop.is_set():return True
            self._configured(cfg)
            if self.vault.auto_jobs_since(now-3600)>=cfg['max_per_hour']:
                self.record(a['id'],state='rate_budget');return True
            jobs=self.vault.queue([a['id']],kind='auto_reauth',context={
                'signal':signal,'generation':cfg['generation'],'binding':a['binding'],
                'model_id':a.get('monitor',{}).get('model_id') or cfg['model_id'],'initial_schedulable':False,
                'resume':retry['resume'] and cfg['resume_after_success'],
                'allow_paused_resume':True,'probe_retry':not retry.get('connector_retry',False),'recovery_checkpoint':retry,
            })
            if jobs:
                self.record(a['id'],automatic_reauth=None,owned_pause=None,continuation=None,blocked=False,
                            state='queued',last_code='',probe_reauth_attempts=a['monitor'].get('probe_reauth_attempts',0)+(not retry.get('connector_retry',False)))
        except VaultError as exc:
            if str(exc)=='REAUTH_BUDGET_2_PER_30_MIN':
                self.record(a['id'],state='rate_budget',last_code=str(exc))
            elif str(exc)=='CONFIGURE_OR_RENEW_COOKIE':
                self.record(a['id'],state='waiting_connector',last_code=str(exc))
            else:
                self.record(a['id'],state='needs_attention',blocked=True,last_code=str(exc),automatic_reauth=None)
        return True

    def run_continuation(self, job):
        aid=job['account_id'];ctx=job['context'];cp=ctx['checkpoint'];phase='precheck'
        def guard():
            require_fresh_checkpoint(cp)
            cfg=self.config();a=self.vault.account(aid)
            self._configured(cfg,require_cookie=False)
            if (self.service.stop.is_set() or self.vault.cancellation_requested(job['id']) or not cfg['enabled'] or cfg.get('generation')!=ctx['generation']
                    or not a.get('monitor',{}).get('enabled') or a['revision']!=job['revision'] or a.get('binding')!=ctx['binding']):
                raise VaultError('MONITOR_JOB_STALE')
            if a['status'] in {'unknown','write_unknown','review'}:
                raise VaultError('RESULT_REVIEW_REQUIRED_BEFORE_RETRY')
        def stage(value):
            nonlocal phase
            phase=value;self.vault.job_stage(job['id'],value);self.record(aid,state=value)
        try:
            guard();a=self.vault.account(aid)
            intent=a.get('write_intent')
            if intent and intent.get('state')!='confirmed':raise VaultError('PREVIOUS_WRITE_NEEDS_RECONCILIATION')
            if cp['step']=='apply':
                stage('applying')
                if intent or not a.get('authorization'):raise VaultError('NO_SAFE_RECOVERY_CHECKPOINT')
                digest=hashlib.sha256(json.dumps(a['authorization'],sort_keys=True).encode()).hexdigest()
                if digest!=ctx['auth_digest']:raise VaultError('CLOUD_CHANGED_DURING_RECOVERY')
                self.vault.update_account(aid,status='authorized')
                self.service.write_credentials(aid,expected_revision=cp['revision'],expected_config=cp['base'],before_write=guard)
                self.record(aid,continuation={**cp,'step':'verify'})
            elif cp['step']!='verify':raise VaultError('NO_SAFE_RECOVERY_CHECKPOINT')
            elif not intent or intent.get('state')!='confirmed':raise VaultError('NO_SAFE_RECOVERY_CHECKPOINT')
            self.verify_and_finish(job,ctx['binding'],cp['base'],ctx['model_id'],cp['resume'],guard,stage)
        except Exception as exc:
            code=('ADMIN_AUTH_FAILED' if exc.http_status in {401,403} else 'MONITOR_FETCH_FAILED') if isinstance(exc,PreflightError) else str(exc) if isinstance(exc,VaultError) else 'RECOVERY_VALIDATION_FAILED'
            unknown=code in {'CLOUD_WRITE_RESULT_UNKNOWN','RESUME_RESULT_UNKNOWN','PROBE_RESULT_UNKNOWN'} or phase=='resuming'
            self.record(aid,state='needs_attention',blocked=True,last_code=code)
            a=self.vault.account(aid)
            if a['status'] not in {'unknown','write_unknown','review'}:self.vault.update_account(aid,status='unknown' if unknown else 'failed')
            self.vault.finish(job['id'],'unknown' if unknown else 'failed',code,phase)
            self._save_retry(aid,exc,phase,job)
            self._schedule_probe_reauth(aid,exc,phase,job)

    def poll(self, force=False, now=None):
        # A concurrent manual refresh joins the current observation instead of
        # starting another cloud scan or contending with the job pool.
        if not self.poll_lock.acquire(blocking=False):return False
        try:return self._poll(force,now)
        finally:self.poll_lock.release()

    def _poll(self, force=False, now=None):
        if self.vault.key is None or self.service.stop.is_set():return False
        now=time.time() if now is None else now
        cfg=self.config()
        if not cfg['enabled'] or (not force and now<self.next_poll):return False
        self.next_poll=now+cfg['interval_seconds']
        runtime={**self.vault.get_setting('monitor_runtime',{}),'last_attempt':now,'next_check':self.next_poll,'state':'checking','last_code':''}
        try:
            setting=self._configured(cfg,require_cookie=False)
            with self.service.operation:
                self.adopt_legacy_deployments(cfg)
            targets=[self.vault.account(a['id']) for a in self.vault.accounts() if a['monitor'].get('enabled')]
            if not targets:
                runtime.update(state='no_managed_accounts',last_success=now,managed=0)
                return True
            # Fetch whole snapshot successfully before any account decisions or queue writes.
            clouds=Client(setting['sub2api_url'],setting['admin_key']).accounts(platform='openai')
            if self.service.stop.is_set() or self.config().get('generation')!=cfg.get('generation'):
                raise VaultError('MONITOR_JOB_STALE')
            self._configured(cfg,require_cookie=False)
            index={a['id']:a for a in clouds}
            queued_logins={j['account_id'] for j in self.vault.jobs() if j['kind']=='auto_reauth' and j['state']=='queued'}
            queued=0
            for observed in targets:
                with self.service.operation:
                    a=self.vault.account(observed['id'])
                    # Stale snapshots cannot overwrite freshly completed workers.
                    if (self.service.tasks.busy(a) or a['updated']!=observed['updated']
                            or not a.get('monitor',{}).get('enabled')):continue
                    if self.config().get('generation')!=cfg.get('generation'):
                        raise VaultError('MONITOR_JOB_STALE')
                    if self.service.stop.is_set():raise VaultError('MONITOR_JOB_STALE')
                    m=a.get('monitor',{});b=a.get('binding')
                    if self.vault.pending(a['id']):
                        if a['id'] in queued_logins:
                            waiting=not setting.get('nvt_cookie') or setting.get('connector_paused')
                            self.record(a['id'],state='waiting_connector' if waiting else 'queued',last_check=now,
                                        last_code='CONFIGURE_OR_RENEW_COOKIE' if waiting else '')
                            if waiting:runtime.update(state='paused',last_code='CONFIGURE_OR_RENEW_COOKIE')
                        continue
                    if a.get('deployment') and a['deployment'].get('state')!='complete':
                        self.record(a['id'],state='deployment_pending',last_check=now)
                        continue
                    if not b or b['instance']!=cfg['instance']:
                        self.record(a['id'],state='binding_conflict',last_check=now);continue
                    cloud=index.get(b['cloud_id'])
                    if cloud is None:
                        self.record(a['id'],state='remote_missing',last_check=now);continue
                    try:guard_identity(cloud,b)
                    except (VaultError,ValueError):
                        self.record(a['id'],state='binding_conflict',last_check=now);continue
                    signal=auth_signal(cloud,datetime.fromtimestamp(now,timezone.utc))
                    self.record(a['id'],last_check=now,cloud_status=cloud.get('status'),
                                cloud_schedulable=cloud.get('schedulable') if type(cloud.get('schedulable')) is bool else None,
                                auth_401=signal is not None)
                    if a.get('authorization') and a.get('result_code')=='SUB2_IMPORTED':
                        # New credentials are staged explicitly by the operator;
                        # don't throw them away by starting another automatic login.
                        self.record(a['id'],state='credentials_imported',last_code='SUB2_IMPORTED')
                        continue
                    if self._dispatch_probe_reauth(a,cloud,cfg,now):
                        if self.vault.account(a['id'])['monitor'].get('state')=='waiting_connector':
                            runtime.update(state='paused',last_code='CONFIGURE_OR_RENEW_COOKIE')
                        continue
                    if m.get('continuation') and m.get('next_retry') and now>=m['next_retry']:
                        try:
                            jobs=self.queue_continuation(a['id'],m['continuation']['resume'],automatic=True)
                            queued+=len(jobs)
                        except Exception as exc:
                            self._continuation_error(a['id'],exc,now)
                        continue
                    if m.get('continuation') and m.get('next_retry'):
                        self.record(a['id'],state='rate_budget' if m.get('last_code')=='RECOVERY_CONTINUE_BUDGET' else 'retry_wait')
                        continue
                    # Migrate the two states produced by the earlier GUI versions:
                    # (a) a 401 recovery that stopped after receiving new credentials,
                    # (b) a recovery whose pre-apply GET failed. Both have enough
                    # encrypted local evidence to continue without another NVT login.
                    legacy_resume = cfg['resume_after_success'] and cfg['resume_paused_401']
                    if (legacy_resume and not self.vault.pending(a['id']) and
                            ((m.get('state') == 'recovered_paused' and
                              m.get('last_code') == 'AUTO_REAUTH_RECOVERED_PAUSED') or
                             (not m.get('continuation') and m.get('last_code') == 'MONITOR_FETCH_FAILED' and
                              m.get('blocked') and a.get('authorization') and
                              (m.get('owned_pause') or {}).get('state') == 'confirmed'))):
                        try:
                            jobs = self.queue_continuation(a['id'], True, automatic=True)
                            if jobs:
                                self.record(a['id'], state='continuation_queued', next_retry=None)
                                queued += len(jobs)
                                continue
                        except (VaultError,PreflightError) as exc:
                            # Keep the latest 401 visible; only expose a separate
                            # attention state when the checkpoint is genuinely unsafe.
                            self._continuation_error(a['id'],exc,now)
                            continue
                    # Keep health evidence visible even when an earlier recovery is blocked.
                    if m.get('blocked') or m.get('owned_pause') or a['status'] in {'unknown','write_unknown','review'}:
                        self.record(a['id'],state='needs_attention',blocked=True,
                                    last_code=m.get('last_code') or 'MONITOR_RECOVERY_NEEDS_REVIEW');continue
                    if cloud.get('status')=='inactive':
                        self.record(a['id'],state='account_disabled',first_seen=None,signal=None);continue
                    if cloud.get('status') not in {'active','error'} or type(cloud.get('schedulable')) is not bool:
                        self.record(a['id'],state='state_unknown',first_seen=None,signal=None);continue
                    if not signal:
                        paused_state='recovered_paused' if m.get('state')=='recovered_paused' else 'paused_unknown'
                        self.record(a['id'],state=paused_state if cloud['schedulable'] is False else 'watching',
                                    first_seen=None,signal=None,consumed_signal=None);continue
                    if not has_login_material(a['login']):
                        self.record(a['id'],state='login_material_missing',last_code='LOGIN_MATERIAL_MISSING',first_seen=None)
                        continue
                    # Consecutive current 401 observations form one incident even when message IDs
                    # or cooldown timestamps change. Do not postpone reauth forever on busy accounts.
                    first=m.get('first_seen')
                    if first is None:
                        self.record(a['id'],state='refresh_grace',last_check=now,first_seen=now,signal=signal);continue
                    if now-first<cfg['grace_seconds']:
                        self.record(a['id'],state='refresh_grace',last_check=now,signal=signal);continue
                    if not setting.get('nvt_cookie') or setting.get('connector_paused'):
                        self.record(a['id'],state='waiting_connector',last_code='CONFIGURE_OR_RENEW_COOKIE')
                        runtime.update(state='paused',last_code='CONFIGURE_OR_RENEW_COOKIE')
                        continue
                    if self.vault.auto_jobs_since(now-3600)>=cfg['max_per_hour']:
                        self.record(a['id'],state='rate_budget',last_check=now);continue
                    try:
                        jobs=self.vault.queue([a['id']],kind='auto_reauth',context={
                            'signal':signal,'generation':cfg['generation'],'binding':b,
                            'model_id':m.get('model_id') or cfg['model_id'],
                            'initial_schedulable':cloud['schedulable'],
                            'resume':cfg['resume_after_success'] and (cloud['schedulable'] or cfg['resume_paused_401']),
                            'allow_paused_resume':cfg['resume_paused_401'],
                        })
                        if jobs:
                            queued+=1;self.record(a['id'],state='queued',last_check=now,consumed_signal=signal,last_code='')
                    except VaultError as exc:
                        budget=str(exc)=='REAUTH_BUDGET_2_PER_30_MIN'
                        self.record(a['id'],state='rate_budget' if budget else 'needs_attention',last_check=now,
                                    blocked=not budget,last_code=str(exc))
            runtime.update(state='paused' if runtime.get('last_code') else 'watching',
                           last_success=now,managed=len(targets),queued=queued)
        except PreflightError as exc:
            runtime.update(state='paused',last_code='ADMIN_AUTH_FAILED' if exc.http_status in {401,403} else 'MONITOR_FETCH_FAILED')
        except VaultError as exc:
            runtime.update(state='paused',last_code=str(exc))
        finally:
            with self.vault.mutex:
                if self.vault.key is not None and self.config().get('generation')==cfg.get('generation'):
                    self.vault.set_setting('monitor_runtime',runtime)
        return True

    def probe(self, cloud_id, model_id):
        setting=self.service.cloud()
        try:
            with httpx.Client(timeout=httpx.Timeout(60,connect=15),follow_redirects=False,trust_env=False) as client:
                with client.stream('POST',admin_url(setting['sub2api_url'])+f'/accounts/{cloud_id}/test',
                                   headers={'x-api-key':setting['admin_key'],'Accept':'text/event-stream'},
                                   json={'model_id':model_id,'prompt':'Reply with OK.'}) as response:
                    if response.status_code in {401,403}:raise VaultError('ADMIN_AUTH_FAILED')
                    if response.status_code!=200:raise VaultError('PROBE_HTTP_FAILED')
                    if response.headers.get('content-type','').split(';',1)[0].strip().lower()!='text/event-stream':raise VaultError('PROBE_INVALID_SSE')
                    deadline=time.monotonic()+120
                    def bounded_lines():
                        for line in response.iter_lines():
                            if time.monotonic()>deadline:raise VaultError('PROBE_RESULT_UNKNOWN')
                            yield line
                    return parse_test_sse(bounded_lines())
        except httpx.HTTPError:
            raise VaultError('PROBE_RESULT_UNKNOWN') from None

    def verify_and_finish(self, job, binding, base, model_id, resume, still_enabled, stage):
        account_id=job['account_id'];b=binding;cloud_id=b['cloud_id'];path=f'/accounts/{cloud_id}'
        stage('verifying')
        still_enabled()
        latest=self.service.cloud_read(path);guard_identity(latest,b)
        if latest.get('schedulable') is not False or latest.get('status') not in {'active','error'} or config_fingerprint(latest)!=base:
            raise VaultError('CLOUD_CHANGED_DURING_RECOVERY')
        # Persist the probe intent before its server-side test/recovery effects.
        still_enabled()
        if self.probe(cloud_id,model_id) is not True:raise VaultError('PROBE_FAILED')
        still_enabled()
        after=self.service.cloud_read(path);guard_identity(after,b)
        if after.get('status')!='active' or after.get('schedulable') is not False or config_fingerprint(after)!=base:
            raise VaultError('CLOUD_NOT_READY_AFTER_PROBE')
        if not isinstance(after.get('updated_at'),str) or not after['updated_at']:raise VaultError('CLOUD_REVISION_REQUIRED')
        expires=timestamp(after.get('expires_at'))
        if after.get('auto_pause_on_expired') and expires and expires<=datetime.now(timezone.utc):
            raise VaultError('ACCOUNT_EXPIRED')
        for field in ('rate_limit_reset_at','overload_until','temp_unschedulable_until'):
            until=timestamp(after.get(field))
            if until and until>datetime.now(timezone.utc):raise VaultError('CLOUD_NOT_READY_AFTER_PROBE')
        if resume:
            still_enabled()
            # Test has server-side recovery effects. Compare another fresh revision before enable.
            fresh=self.service.cloud_read(path);guard_identity(fresh,b)
            if fresh.get('updated_at')!=after.get('updated_at') or fresh.get('schedulable') is not False or fresh.get('status')!='active' or config_fingerprint(fresh)!=base:
                raise VaultError('CLOUD_CHANGED_DURING_RECOVERY')
            still_enabled()
            stage('resuming')
            self.service.cloud_write(path+'/schedulable',{'schedulable':True})
            live=self.service.cloud_read(path);guard_identity(live,b)
            if live.get('schedulable') is not True or live.get('status')!='active' or config_fingerprint(live)!=base:
                raise VaultError('RESUME_RESULT_UNKNOWN')
            for field in ('rate_limit_reset_at','overload_until','temp_unschedulable_until'):
                until=timestamp(live.get(field))
                if until and until>datetime.now(timezone.utc):raise VaultError('RESUME_RESULT_UNKNOWN')
        else:
            still_enabled()
        code='AUTO_REAUTH_RECOVERED' if resume else 'AUTO_REAUTH_RECOVERED_PAUSED'
        with self.vault.transaction():
            self.record(account_id,state='recovered' if resume else 'recovered_paused',blocked=False,auth_401=False,
                        cloud_status='active',cloud_schedulable=bool(resume),owned_pause=None,last_recovered=time.time(),
                        last_code=code,first_seen=None,signal=None,consumed_signal=None,continuation=None,next_retry=None,
                        continuation_attempts=0,
                        recovery_evidence={'base':base,'verified_at':time.time(),'model_id':model_id,
                                           'job_id':job['id'],'cloud_id':cloud_id,'sse_success':True,
                                           'binding':b,'account_revision':job['revision']})
            self.record(account_id,automatic_reauth=None,probe_reauth_attempts=0)
            self.vault.update_account(account_id,status='active' if resume else 'cloud_paused')
            self.vault.finish(job['id'],'succeeded',code,'complete')

    def run(self, job):
        account_id=job['account_id'];ctx=job['context'];phase='precheck';needs_cookie=True
        def still_enabled():
            current=self.config();a=self.vault.account(account_id)
            if (self.service.stop.is_set() or self.vault.cancellation_requested(job['id']) or not current['enabled'] or current.get('generation')!=ctx.get('generation')
                    or not a.get('monitor',{}).get('enabled') or a['revision']!=job['revision']
                    or a.get('binding')!=ctx.get('binding')):
                raise VaultError('MONITOR_JOB_STALE')
            self._configured(current,require_cookie=needs_cookie)
            if a['status'] in {'unknown','write_unknown','review'}:
                raise VaultError('RESULT_REVIEW_REQUIRED_BEFORE_RETRY')
            if not needs_cookie:require_fresh_checkpoint(a.get('monitor',{}).get('continuation'))
        def stage(value):
            nonlocal phase
            phase=value;self.vault.job_stage(job['id'],value);self.record(account_id,state=value)
        try:
            still_enabled()
            cfg=self.config();setting=self._configured(cfg);a=self.vault.account(account_id)
            if (not cfg['enabled'] or not a.get('monitor',{}).get('enabled')
                    or ctx.get('generation')!=cfg.get('generation') or job['revision']!=a['revision']):
                raise VaultError('MONITOR_JOB_STALE')
            if a.get('monitor',{}).get('blocked') or a.get('monitor',{}).get('owned_pause'):
                raise VaultError('MONITOR_RECOVERY_NEEDS_REVIEW')
            if a.get('write_intent') and a['write_intent'].get('state')!='confirmed':
                raise VaultError('PREVIOUS_WRITE_NEEDS_RECONCILIATION')
            if ctx.get('probe_retry') or ctx.get('recovery_checkpoint'):
                require_fresh_checkpoint(ctx.get('recovery_checkpoint'))
            b=a['binding']
            if b!=ctx.get('binding'):raise VaultError('CLOUD_BINDING_MISMATCH')
            cloud_id=b['cloud_id'];path=f'/accounts/{cloud_id}'
            before=self.service.cloud_read(path);guard_identity(before,b)
            initial=ctx.get('initial_schedulable',True)  # jobs created before this migration were active-only
            if before.get('status')=='inactive':raise VaultError('CLOUD_ACCOUNT_ON_HOLD')
            if before.get('status') not in {'active','error'} or type(before.get('schedulable')) is not bool or type(initial) is not bool:
                raise VaultError('CLOUD_SCHEDULING_STATE_UNKNOWN')
            if before['schedulable'] is not initial:raise VaultError('CLOUD_CHANGED_DURING_RECOVERY')
            resume=ctx['resume'] and (initial or ctx.get('allow_paused_resume',False))
            if not auth_signal(before):
                self.vault.finish(job['id'],'cancelled','AUTH_SIGNAL_CLEARED');self.record(account_id,state='watching');return
            if not isinstance(before.get('updated_at'),str) or not before['updated_at']:
                raise VaultError('CLOUD_REVISION_REQUIRED')
            base=config_fingerprint(before)
            retry=ctx.get('recovery_checkpoint') or {}
            if retry and (base!=retry.get('base') or
                          (retry.get('connector_retry') and before.get('updated_at')!=retry.get('cloud_revision'))):
                raise VaultError('CLOUD_CHANGED_DURING_RECOVERY')
            still_enabled()
            stage('pausing')
            # Repair a previously paused 401 account, but do not claim permission to enable it.
            self.record(account_id,owned_pause={'job_id':job['id'],'state':'pending' if initial else 'preexisting',
                                               'baseline':base,'initial_schedulable':initial})
            if initial:self.service.cloud_write(path+'/schedulable',{'schedulable':False})
            held=self.service.cloud_read(path);guard_identity(held,b)
            if held.get('schedulable') is not False or held.get('status') not in {'active','error'} or config_fingerprint(held)!=base:
                raise VaultError('CLOUD_CHANGED_DURING_RECOVERY')
            if not isinstance(held.get('updated_at'),str) or not held['updated_at']:raise VaultError('CLOUD_REVISION_REQUIRED')
            if not auth_signal(held):raise VaultError('AUTH_SIGNAL_CHANGED_AFTER_PAUSE')
            pause_revision=held['updated_at']
            self.record(account_id,owned_pause={'job_id':job['id'],'state':'confirmed','baseline':base,
                                               'revision':pause_revision,'initial_schedulable':initial,'created':time.time()})
            still_enabled()
            stage('reauthorizing')
            self.vault.update_account(account_id,status='authorizing',authorization=None,raw_result=None)
            result=self.service.authorize(setting, a['login'], OAuthIdentity(**b['identity']),
                                          job['id'], guard=still_enabled)
            self.vault.update_account(account_id,status='authorized' if result.authorization else 'review',
                                      authorization=result.authorization,raw_result=result.raw,result_code=result.review_code)
            if result.authorization is None:raise VaultError(result.review_code or 'INVALID_SUB2JSON')
            # A previously confirmed write belongs to older credentials, not this
            # new apply checkpoint. Never clear an unresolved write intent.
            intent=self.vault.account(account_id).get('write_intent')
            if intent and intent.get('state')!='confirmed':raise VaultError('PREVIOUS_WRITE_NEEDS_RECONCILIATION')
            self.vault.update_account(account_id,write_intent=None)
            self.record(account_id,continuation={'step':'apply','base':base,'revision':pause_revision,
                                                'resume':resume,'created':time.time(),'binding':b,
                                                'account_revision':job['revision']},continuation_attempts=0)
            needs_cookie=False
            still_enabled()
            stage('applying')
            latest=self.service.cloud_read(path);guard_identity(latest,b)
            if (latest.get('schedulable') is not False or latest.get('status') not in {'active','error'}
                    or config_fingerprint(latest)!=base or latest.get('updated_at')!=pause_revision):
                raise VaultError('CLOUD_CHANGED_DURING_RECOVERY')
            # Reuse credential merger + persistent remote-write intent. Never recreate accounts.
            still_enabled()
            self.service.write_credentials(account_id, expected_revision=pause_revision, expected_config=base,before_write=still_enabled)
            cp=self.vault.account(account_id)['monitor']['continuation']
            self.record(account_id,continuation={**cp,'step':'verify'})
            self.verify_and_finish(job,b,base,ctx['model_id'],resume,still_enabled,stage)
        except Exception as exc:
            if isinstance(exc,ConnectorError):
                code=exc.code
                if code in {'CONNECTOR_SESSION_EXPIRED','CONNECTOR_RATE_LIMITED'}:
                    self.service.pause_connector(setting)
            elif isinstance(exc,PreflightError):code='ADMIN_AUTH_FAILED' if exc.http_status in {401,403} else 'MONITOR_FETCH_FAILED'
            elif isinstance(exc,ValueError):code=str(exc) if isinstance(exc,VaultError) else 'RECOVERY_VALIDATION_FAILED'
            else:code='AUTO_RECOVERY_INTERNAL_ERROR'
            ambiguous=(isinstance(exc,ConnectorError) and exc.ambiguous) or code in {'CLOUD_WRITE_RESULT_UNKNOWN','PROBE_RESULT_UNKNOWN','RESUME_RESULT_UNKNOWN'} or phase in {'pausing','resuming'}
            self.record(account_id,state='needs_attention',blocked=True,last_code=code)
            a=self.vault.account(account_id)
            if a['status'] not in {'review','unknown','write_unknown'}:
                self.vault.update_account(account_id,status='unknown' if ambiguous else 'failed')
            self.vault.finish(job['id'],'unknown' if ambiguous else 'failed',code,phase)
            self._save_retry(account_id,exc,phase,job)
            self._schedule_probe_reauth(account_id,exc,phase,job)
            self._schedule_connector_retry(account_id,exc,phase,job)
            # Never enable blindly in a finally block; a pause may still belong to an operator.
