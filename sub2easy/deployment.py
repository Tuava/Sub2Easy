"""Persisted local-to-sub2api deployment pipeline, separate from 401 recovery.

No mutation is replayed after an ambiguous response. Confirmed steps and local
credentials survive retries; every enable follows a fresh successful SSE probe.
"""

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import secrets
import time

from sub2easy.binding import candidates
from sub2easy.lifecycle import ContractError, ImportProfile, OAuthIdentity, normalize_sub2json, plan_create, plan_reauthorize
from sub2easy.monitor import config_fingerprint, guard_identity
from sub2easy.nvtokens import ConnectorError
from sub2easy.preflight import Client, PreflightError, admin_url, timestamp
from sub2easy.vault import VaultError
from sub2easy.intake import has_login_material


def fingerprint(account):
    # Group order is not a routing/configuration change.
    value = deepcopy(account)
    if isinstance(value.get('group_ids'), list):
        value['group_ids'] = sorted(value['group_ids'])
    return config_fingerprint(value)


def check_ready(account):
    if account.get('status') != 'active' or account.get('schedulable') is not False:
        raise VaultError('DEPLOY_NOT_READY')
    now = datetime.now(timezone.utc)
    for key in ('rate_limit_reset_at', 'overload_until', 'temp_unschedulable_until'):
        if key not in account:
            raise VaultError('DEPLOY_RUNTIME_UNKNOWN')
        until = timestamp(account[key])
        if until and until > now:
            raise VaultError('DEPLOY_COOLDOWN_ACTIVE')
    expires = timestamp(account.get('expires_at'))
    if expires and expires <= now and account.get('auto_pause_on_expired'):
        raise VaultError('ACCOUNT_EXPIRED')


def replaceable_precheck(account):
    """Only an untouched new-account precheck may adopt a newly submitted template."""
    dep=account.get('deployment') or {}
    return (dep.get('new_account') is True and dep.get('step')=='precheck'
            and dep.get('state')=='failed'
            and dep.get('code') in {'SELECTED_GROUP_UNAVAILABLE','SELECTED_PROXY_UNAVAILABLE'}
            and not account.get('binding') and not dep.get('binding') and not dep.get('cloud_id')
            and not dep.get('mutation') and not account.get('write_intent')
            and account.get('status') not in {'unknown','write_unknown','review'}
            and dep.get('auth_attempts',0)==0
            and all(h.get('stage')=='precheck' for h in dep.get('history',[])))


def profile_summary(profile):
    return {k:profile.get(k) for k in ('profile_id','revision','staging_group_id','target_group_ids','proxy_id')}


class DeploymentService:
    def __init__(self, service):
        self.service, self.vault = service, service.vault

    def queue(self, ids, model_id, profile_data, staging_verified=False, auto_monitor=True):
        if (not isinstance(ids, list) or not ids or len(ids) > 100
                or any(not isinstance(i, str) for i in ids)):
            raise VaultError('SELECT_1_TO_100_ACCOUNTS')
        if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 160 or any(ord(c)<32 for c in model_id):
            raise VaultError('DEPLOY_MODEL_REQUIRED')
        settings = self.service.cloud()
        instance = admin_url(settings['sub2api_url'])
        profile = ImportProfile.from_dict(profile_data)
        if profile.instance_id != instance:
            raise VaultError('PROFILE_INSTANCE_MISMATCH')
        if type(auto_monitor) is not bool:
            raise VaultError('INVALID_MONITOR_CONFIG')
        # Results are per-account; one conflict does not roll back other jobs.
        results = []
        for aid in dict.fromkeys(ids):
            try:
                a = self.vault.account(aid)
                if a.get('retirement'):raise VaultError('ACCOUNT_RETIRED')
                if self.vault.pending(aid) or self.service.tasks.busy(a):
                    raise VaultError('OPERATION_RUNNING')
                if a.get('monitor', {}).get('owned_pause') or a.get('monitor', {}).get('blocked'):
                    raise VaultError('MONITOR_RECOVERY_NEEDS_REVIEW')
                if a.get('write_intent') and a['write_intent'].get('state') != 'confirmed':
                    raise VaultError('PREVIOUS_WRITE_NEEDS_RECONCILIATION')
                if a.get('binding') and a['binding']['instance'] != instance:
                    raise VaultError('CLOUD_INSTANCE_CHANGED')
                previous = a.get('deployment')
                if previous and previous['state'] == 'complete':
                    results.append({'account_id':aid,'state':'already_complete','cloud_id':previous.get('cloud_id')})
                    continue
                if previous:
                    if previous.get('mutation') or previous['state'] in {'unknown','review'}:
                        raise VaultError('DEPLOY_RESULT_NEEDS_REVIEW')
                    if (previous['instance'] != instance or previous['connection_revision'] != settings.get('cloud_revision','legacy')):
                        raise VaultError('DEPLOY_CONNECTION_CHANGED')
                    dep = deepcopy(previous)
                    if replaceable_precheck(a) and dep['profile']!=asdict(profile):
                        if not staging_verified:
                            raise VaultError('STAGING_ISOLATION_CONFIRMATION_REQUIRED')
                        dep['previous_profile']=profile_summary(dep['profile'])
                        dep['profile']=asdict(profile)
                        dep['staging_verified']=True
                        dep['precheck_error']=None
                    if dep.get('step') in {'promote','enable'}:
                        dep['step'] = 'verify'  # expired probe evidence must be renewed
                    if dep.get('step') in {'authorize','create','apply'}:
                        # Explicit retries may renew expired credentials or retry a
                        # definitive connector failure, but never an unknown login.
                        dep['step'] = 'authorize'
                        dep['auth_attempts'] = 0
                    dep['model_id'] = model_id.strip()
                else:
                    if a['status'] in {'unknown','write_unknown','review'}:
                        raise VaultError('RESULT_REVIEW_REQUIRED_BEFORE_RETRY')
                    if not a.get('binding') and not staging_verified:
                        raise VaultError('STAGING_ISOLATION_CONFIRMATION_REQUIRED')
                    dep = {'id':secrets.token_hex(16),'step':'precheck','state':'queued','created':time.time(),
                           'instance':instance,'connection_revision':settings.get('cloud_revision','legacy'),
                           'profile':asdict(profile),'model_id':model_id.strip(),'material_revision':a['revision'],
                           'new_account':not bool(a.get('binding')),'binding':deepcopy(a.get('binding')),
                           'cloud_id':(a.get('binding') or {}).get('cloud_id'),'mutation':None,
                           'staging_verified':staging_verified,'auth_attempts':0,'history':[]}
                with self.vault.transaction():
                    dep['auto_monitor'] = auto_monitor
                    if auto_monitor:
                        dep['monitor_generation'] = self.service.monitor.prepare_deployment(model_id.strip())
                    jobs = self.vault.queue([aid],kind='server_deploy',context={'deployment_id':dep['id']})
                    if not jobs:raise VaultError('OPERATION_RUNNING')
                    dep.update(state='queued',code='DEPLOY_QUEUED',job_id=jobs[0])
                    self.vault.update_account(aid,deployment=dep)
                results.append({'account_id':aid,'state':'queued','job_id':jobs[0],'step':dep['step'],
                                'execution_profile':profile_summary(dep['profile'])})
            except (VaultError, ContractError) as exc:
                results.append({'account_id':aid,'state':'failed','code':str(exc)})
        report = {'updated':time.time(),'items':results}
        self.vault.set_setting('deployment_report',report)
        return report

    def _save(self, aid, dep, **updates):
        dep.update(updates,updated=time.time())
        self.vault.update_account(aid,deployment=dep)

    def _guard(self, aid, dep):
        a = self.vault.account(aid)
        s = self.service.cloud()
        if self.service.stop.is_set() or self.vault.cancellation_requested(dep.get('job_id')):raise VaultError('DEPLOY_STOPPED')
        if (admin_url(s['sub2api_url']) != dep['instance']
                or s.get('cloud_revision','legacy') != dep['connection_revision']):
            raise VaultError('DEPLOY_CONNECTION_CHANGED')
        if a['revision'] != dep['material_revision'] or a.get('binding') != dep['binding']:
            raise VaultError('DEPLOY_BINDING_CHANGED')
        if a.get('monitor',{}).get('owned_pause') or a.get('monitor',{}).get('blocked'):
            raise VaultError('MONITOR_RECOVERY_NEEDS_REVIEW')
        return a

    def _read(self, aid, dep):
        self._guard(aid,dep)
        cloud = self.service.cloud_read(f"/accounts/{dep['cloud_id']}")
        self._guard(aid,dep)
        guard_identity(cloud,dep['binding'])
        if cloud.get('status') not in {'active','error'} or type(cloud.get('schedulable')) is not bool:
            raise VaultError('CLOUD_ACCOUNT_ON_HOLD')
        if fingerprint(cloud) != dep['baseline']:
            raise VaultError('DEPLOY_CONFIG_CHANGED')
        if not isinstance(cloud.get('updated_at'),str) or not cloud['updated_at']:
            raise VaultError('CLOUD_REVISION_REQUIRED')
        return cloud

    def _mutate(self, aid, dep, stage, path, body, method='POST'):
        self._guard(aid,dep)
        # Journal first, including random request ID but not plaintext payload.
        self._save(aid,dep,mutation={'stage':stage,'key':f"s2e-{dep['id']}-{stage}",'started':time.time()})
        self._guard_unsent(aid,dep)
        if method == 'PUT':
            result = self.service.cloud_put(path,body)
        else:
            result = self.service.cloud_write(path,body,dep['mutation']['key'])
        return result

    def _guard_unsent(self, aid, dep):
        # No request has left yet, so a stop/change here is not an unknown write.
        try:
            self._guard(aid,dep)
        except Exception:
            self._save(aid,dep,mutation=None)
            raise

    def _paused(self, aid, dep):
        # A successful pause POST can precede read-side convergence. Poll only
        # reads, bounded and interruptible; retries resume here without a POST.
        for delay in (0,0.25,0.5):
            if delay:self.service.stop.wait(delay)
            cloud=self._read(aid,dep)
            if cloud['schedulable'] is False:return cloud
        raise VaultError('DEPLOY_NOT_READY')

    def _authorization(self, a, dep):
        saved = a.get('authorization')
        if not saved:return None
        expected = OAuthIdentity(**dep['binding']['identity']) if dep['binding'] else None
        try:
            return normalize_sub2json({'platform':'openai','type':'oauth','credentials':saved['credentials']},
                                     a['login']['account'],expected)
        except ContractError as exc:
            if str(exc) == 'TOKEN_EXPIRED_OR_TOO_CLOSE':return None
            raise

    def run(self, job):
        aid = job['account_id'];dep = self.vault.account(aid).get('deployment')
        if not dep or dep['id'] != job['context'].get('deployment_id'):
            self.vault.finish(job['id'],'failed','DEPLOY_BINDING_CHANGED','precheck');return
        try:
            if dep.get('mutation'):raise VaultError('DEPLOY_RESULT_NEEDS_REVIEW')
            self._save(aid,dep,state='running')
            while dep['step'] != 'complete':
                a=self._guard(aid,dep);step=dep['step']
                self.vault.job_stage(job['id'],step)
                if step == 'precheck':
                    if dep['new_account']:
                        if not dep['staging_verified']:raise VaultError('STAGING_ISOLATION_CONFIRMATION_REQUIRED')
                        choices=self.service.options()
                        profile=ImportProfile.from_dict(dep['profile'])
                        groups={g['id'] for g in choices['groups']}
                        if profile.staging_group_id not in groups or not set(profile.target_group_ids)<=groups:
                            self._save(aid,dep,precheck_error={
                                'missing_staging_group_id':profile.staging_group_id if profile.staging_group_id not in groups else None,
                                'missing_target_group_ids':sorted(set(profile.target_group_ids)-groups),
                                'missing_proxy_id':None})
                            raise VaultError('SELECTED_GROUP_UNAVAILABLE')
                        if profile.proxy_id is not None and profile.proxy_id not in {p['id'] for p in choices['proxies']}:
                            self._save(aid,dep,precheck_error={'missing_staging_group_id':None,
                                'missing_target_group_ids':[],'missing_proxy_id':profile.proxy_id})
                            raise VaultError('SELECTED_PROXY_UNAVAILABLE')
                        self._save(aid,dep,precheck_error=None)
                        self._check_duplicates(a,dep)
                    else:
                        c=self.service.cloud_read(f"/accounts/{dep['cloud_id']}");guard_identity(c,dep['binding'])
                        self._guard(aid,dep)
                        if c.get('status') not in {'active','error'} or type(c.get('schedulable')) is not bool:
                            raise VaultError('CLOUD_ACCOUNT_ON_HOLD')
                        if not isinstance(c.get('updated_at'),str) or not c['updated_at']:
                            raise VaultError('CLOUD_REVISION_REQUIRED')
                        self._save(aid,dep,baseline=fingerprint(c),initial_schedulable=c['schedulable'])
                    self._save(aid,dep,step='authorize' if dep['new_account'] else 'pause')
                elif step == 'authorize':
                    if not dep['new_account']:
                        # A delayed explicit retry must not log in after someone
                        # resumed or edited the old account in the meantime.
                        c=self._read(aid,dep)
                        if c['schedulable'] or c['updated_at']!=dep.get('pause_revision'):
                            raise VaultError('DEPLOY_CONFIG_CHANGED')
                    auth=self._authorization(a,dep)
                    if auth is None:
                        if not has_login_material(a['login']):raise VaultError('LOGIN_MATERIAL_MISSING')
                        if dep['auth_attempts'] >= 1:raise VaultError('DEPLOY_AUTH_ALREADY_ATTEMPTED')
                        settings=self.service.cloud()
                        if not settings.get('nvt_cookie') or settings.get('connector_paused'):
                            raise VaultError('CONFIGURE_OR_RENEW_COOKIE')
                        def authorize_intent():
                            self._guard(aid,dep)
                            self._save(aid,dep,mutation={'stage':'authorize','started':time.time()},auth_attempts=1)
                            self._guard_unsent(aid,dep)
                        try:
                            result=self.service.authorize(settings, a['login'],
                                OAuthIdentity(**dep['binding']['identity']) if dep['binding'] else None,
                                dep.get('job_id'), guard=lambda:self._guard(aid,dep), before_send=authorize_intent)
                        except ConnectorError as exc:
                            if not exc.ambiguous:self._save(aid,dep,mutation=None)
                            if exc.code in {'CONNECTOR_SESSION_EXPIRED','CONNECTOR_RATE_LIMITED'}:
                                self.service.pause_connector(settings)
                            raise
                        self.vault.update_account(aid,authorization=result.authorization,raw_result=result.raw,
                                                  status='authorized' if result.authorization else 'review')
                        self._save(aid,dep,mutation=None)
                        if not result.authorization:raise ContractError(result.review_code or 'INVALID_SUB2JSON')
                        auth=self._authorization(self.vault.account(aid),dep)
                        if auth is None:raise VaultError('TOKEN_EXPIRED_OR_TOO_CLOSE')
                    self._save(aid,dep,step='create' if dep['new_account'] else 'apply')
                elif step == 'create':
                    auth=self._authorization(a,dep)
                    if auth is None:raise VaultError('TOKEN_EXPIRED_OR_TOO_CLOSE')
                    self._check_duplicates(a,dep)  # authorization may have changed available identity evidence
                    plan=plan_create(auth,ImportProfile.from_dict(dep['profile']),aid)
                    created=self._mutate(aid,dep,'create','/accounts',plan.body)
                    if not isinstance(created,dict) or type(created.get('id')) is not int or created['id']<=0:
                        raise VaultError('CLOUD_WRITE_RESULT_UNKNOWN')
                    binding={'cloud_id':created['id'],'instance':dep['instance'],'identity':asdict(auth.identity)}
                    confirmed={**dep,'binding':binding,'cloud_id':created['id'],'step':'pause_new',
                               'mutation':None,'updated':time.time()}
                    # One local transaction: never persist a new binding with an
                    # old create checkpoint that cannot pass the retry guard.
                    self.vault.update_account(aid,binding=binding,deployment=confirmed)
                    dep.update(confirmed)
                elif step == 'pause_new':
                    c=self.service.cloud_read(f"/accounts/{dep['cloud_id']}");guard_identity(c,dep['binding'])
                    self._guard(aid,dep)
                    if set(c.get('group_ids',[])) != {dep['profile']['staging_group_id']}:
                        raise VaultError('DEPLOY_CONFIG_CHANGED')
                    if c.get('status') not in {'active','error'} or type(c.get('schedulable')) is not bool:
                        raise VaultError('CLOUD_ACCOUNT_ON_HOLD')
                    if not isinstance(c.get('updated_at'),str) or not c['updated_at']:
                        raise VaultError('CLOUD_REVISION_REQUIRED')
                    for key in ('proxy_id','concurrency','priority','rate_multiplier','auto_pause_on_expired'):
                        if c.get(key)!=dep['profile'][key]:
                            raise VaultError('DEPLOY_TEMPLATE_NOT_APPLIED')
                    if timestamp(c.get('expires_at'))!=timestamp(dep['profile']['account_expires_at']):
                        raise VaultError('DEPLOY_TEMPLATE_NOT_APPLIED')
                    if (c.get('extra') or {}).get('codex_fingerprint_mode','off')!=dep['profile']['fingerprint_mode']:
                        raise VaultError('DEPLOY_TEMPLATE_NOT_APPLIED')
                    if dep.get('baseline') and fingerprint(c)!=dep['baseline']:
                        raise VaultError('DEPLOY_CONFIG_CHANGED')
                    self._save(aid,dep,baseline=fingerprint(c))
                    if c.get('schedulable') is not False:
                        self._mutate(aid,dep,'pause',f"/accounts/{dep['cloud_id']}/schedulable",{'schedulable':False})
                    self._save(aid,dep,mutation=None,step='pause_confirm')
                elif step == 'pause':
                    c=self._read(aid,dep)
                    if c['schedulable'] is not dep['initial_schedulable']:raise VaultError('DEPLOY_CONFIG_CHANGED')
                    if c['schedulable']:
                        self._mutate(aid,dep,'pause',f"/accounts/{dep['cloud_id']}/schedulable",{'schedulable':False})
                    self._save(aid,dep,mutation=None,step='pause_confirm')
                elif step == 'pause_confirm':
                    held=self._paused(aid,dep)
                    self._save(aid,dep,pause_revision=held['updated_at'],
                               step='verify' if dep['new_account'] else 'authorize')
                elif step == 'apply':
                    auth=self._authorization(a,dep)
                    if auth is None:raise VaultError('TOKEN_EXPIRED_OR_TOO_CLOSE')
                    c=self._read(aid,dep)
                    if c['updated_at']!=dep.get('pause_revision'):raise VaultError('DEPLOY_CONFIG_CHANGED')
                    plan=plan_reauthorize(c,auth,dep['cloud_id'],OAuthIdentity(**dep['binding']['identity']))
                    self._mutate(aid,dep,'apply',f"/accounts/{dep['cloud_id']}/apply-oauth-credentials",plan.body)
                    self._save(aid,dep,mutation=None,step='verify')
                elif step == 'verify':
                    c=self._read(aid,dep)
                    if c['schedulable']:raise VaultError('DEPLOY_CONFIG_CHANGED')
                    self._guard(aid,dep)
                    self.service.monitor.probe(dep['cloud_id'],dep['model_id'])
                    c=self._read(aid,dep);check_ready(c)
                    self._save(aid,dep,verified_at=time.time(),verified_revision=c['updated_at'],
                               step='promote' if dep['new_account'] and not dep.get('promoted') else 'enable')
                elif step == 'promote':
                    c=self._before_enable(aid,dep)
                    targets=dep['profile']['target_group_ids']
                    choices=self.service.options()
                    if not set(targets)<={g['id'] for g in choices['groups']}:raise VaultError('SELECTED_GROUP_UNAVAILABLE')
                    c=self._before_enable(aid,dep)
                    self._mutate(aid,dep,'promote',f"/accounts/{dep['cloud_id']}",{'group_ids':targets},method='PUT')
                    expected=deepcopy(c);expected['group_ids']=targets
                    self._save(aid,dep,baseline=fingerprint(expected),promoted=True,mutation=None,step='verify')
                elif step == 'enable':
                    self._before_enable(aid,dep)
                    self._mutate(aid,dep,'enable',f"/accounts/{dep['cloud_id']}/schedulable",{'schedulable':True})
                    self._save(aid,dep,mutation=None,step='confirm')
                elif step == 'confirm':
                    c=self._read(aid,dep)
                    if c['schedulable'] is not True or c['status']!='active':raise VaultError('DEPLOY_FINAL_STATE_INVALID')
                    check_ready({**c,'schedulable':False})
                    if dep['new_account'] and set(c.get('group_ids',[]))!=set(dep['profile']['target_group_ids']):
                        raise VaultError('DEPLOY_FINAL_STATE_INVALID')
                    with self.vault.transaction():
                        self.service.monitor.enroll_deployed(aid,dep,c)
                        dep['history'].append({'stage':'complete','at':time.time()})
                        self._save(aid,dep,step='complete',state='complete',code='DEPLOYED_AND_ENABLED')
                        self.vault.update_account(aid,status='active',authorization=None,raw_result=None)
                else:raise VaultError('DEPLOY_INVALID_STEP')
                self._save(aid,dep,history=(dep['history']+[{'stage':step,'at':time.time()}])[-60:])
            self.vault.finish(job['id'],'succeeded','DEPLOYED_AND_ENABLED','complete')
        except Exception as exc:
            code = str(exc) if isinstance(exc,(VaultError,ContractError,ConnectorError)) else 'DEPLOY_READ_FAILED' if isinstance(exc,PreflightError) else 'DEPLOY_INTERNAL_ERROR'
            unknown=bool(dep.get('mutation'))
            state='unknown' if unknown else 'review' if isinstance(exc,ContractError) else 'failed'
            self._save(aid,dep,state=state,code=code)
            self.vault.update_account(aid,status='unknown' if unknown else 'review' if state=='review' else 'failed')
            self.vault.finish(job['id'],state,code,dep['step'])

    def _check_duplicates(self,a,dep):
        self._guard(a['id'],dep)
        s=self.service.cloud()
        clouds=Client(s['sub2api_url'],s['admin_key']).accounts(platform='openai')
        self._guard(a['id'],dep)
        if any('email' in c['reasons'] or 'name' in c['reasons']
               for c in candidates(a,clouds,{})):
            raise VaultError('DEPLOY_BIND_EXISTING_FIRST')

    def _before_enable(self,aid,dep):
        c=self._read(aid,dep);check_ready(c)
        if time.time()-dep.get('verified_at',0)>120 or c['updated_at']!=dep.get('verified_revision'):
            raise VaultError('DEPLOY_VERIFICATION_STALE')
        return c
