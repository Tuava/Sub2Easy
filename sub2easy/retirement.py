"""Terminal workspace-loss routing. No login, credential apply, probe or enable."""

from copy import deepcopy
import time
import uuid

from sub2easy.monitor import config_fingerprint, guard_identity
from sub2easy.preflight import PreflightError, admin_url
from sub2easy.vault import VaultError


TEAM_LOST = 'EXPECTED_WORKSPACE_NOT_RETURNED'


def team_lost(account):
    return (account.get('status') in {'review', 'failed'} and
            TEAM_LOST in {account.get('result_code'), (account.get('monitor') or {}).get('last_code')})


class RetirementService:
    def __init__(self, service):
        self.service, self.vault = service, service.vault
        self.next_poll = 0

    def config(self):
        return {'enabled': False, 'group_id': None, 'instance': None,
                **self.vault.get_setting('retirement_config', {})}

    def view(self):
        from collections import Counter
        setting=self.vault.get_setting('connection',{})
        instance=admin_url(setting['sub2api_url']) if setting.get('sub2api_url') else None
        counts=Counter()
        for a in self.vault.accounts():
            if (a.get('binding') or {}).get('instance')!=instance:continue
            plan=a.get('retirement') or {}
            if plan.get('state'):counts[plan['state']]+=1
            elif a['monitor'].get('state')=='retirement_waiting':counts['waiting']+=1
        runtime={**self.vault.get_setting('retirement_runtime',{}),'counts':dict(counts)}
        return {'config': self.config(), 'runtime': runtime}

    def save_config(self, data):
        if type(data.get('enabled')) is not bool:raise VaultError('INVALID_RETIREMENT_CONFIG')
        if data['enabled'] and data.get('confirm_transfer') is not True:
            raise VaultError('CONFIRM_RETIREMENT_TRANSFER')
        setting = self.service.cloud(); instance = admin_url(setting['sub2api_url'])
        if data.get('connection_revision') != setting.get('cloud_revision','legacy'):
            raise VaultError('RETIREMENT_SITE_CHANGED')
        group = data.get('group_id')
        if group is not None and (type(group) is not int or group <= 0):
            raise VaultError('INVALID_RETIREMENT_CONFIG')
        if data['enabled'] and group is not None:
            if group not in {g['id'] for g in self.service.options()['groups']}:
                raise VaultError('RETIREMENT_GROUP_UNAVAILABLE')
        self.vault.set_setting('retirement_config', {'enabled':data['enabled'],'group_id':group,'instance':instance})
        self.next_poll = 0
        return self.view()

    def target(self, setting):
        cfg = self.config(); instance = admin_url(setting['sub2api_url'])
        if not cfg['enabled']:raise VaultError('RETIREMENT_DISABLED')
        if cfg.get('instance') != instance:raise VaultError('RETIREMENT_SITE_CHANGED')
        groups = self.service.options()['groups']
        if cfg['group_id'] is not None:
            match = [g for g in groups if g['id']==cfg['group_id']]
        else:
            # No fuzzy match; staging group and production groups are not substitutes.
            match = [g for g in groups if g['name'].strip()=='测试组']
        if len(match)!=1:raise VaultError('RETIREMENT_GROUP_REQUIRED')
        return match[0]['id']

    def scan(self, force=False):
        if self.vault.key is None or self.service.stop.is_set():return
        if not force and time.time()<self.next_poll:return
        self.next_poll=time.time()+60
        if not self.service.operation.acquire(blocking=False):return
        try:
            if not self.config()['enabled']:return
            candidates=[]
            for row in self.vault.accounts():
                a=self.vault.account(row['id'])
                if (team_lost(a) and not a.get('retirement') and not self.vault.pending(a['id'])
                        and not self.service.tasks.busy(a)):
                    candidates.append(a)
            if not candidates:return
            setting=self.service.cloud();instance=admin_url(setting['sub2api_url'])
            candidates=[a for a in candidates if (a.get('binding') or {}).get('instance')==instance]
            if not candidates:return
            # Exit auto-login scope immediately, even if the target group needs configuration.
            for a in candidates:
                self.service.monitor.record(a['id'],enabled=False,state='retirement_waiting',
                                            blocked=True,automatic_reauth=None,next_retry=None)
                self.vault.cancel_auto(a['id'])
            try:group=self.target(setting)
            except (VaultError,PreflightError) as exc:
                code=str(exc) if isinstance(exc,VaultError) else 'RETIREMENT_GROUP_READ_FAILED'
                self.vault.set_setting('retirement_runtime',{'state':'needs_attention','code':code,'updated':time.time()})
                return
            items=[]
            for a in candidates:
                try:items.append(self.queue(a['id'],group,setting))
                except VaultError as exc:items.append({'account_id':a['id'],'state':'failed','code':str(exc)})
            self.vault.set_setting('retirement_runtime',{'state':'queued','items':items,'updated':time.time(),'group_id':group})
        finally:self.service.operation.release()

    def queue(self, aid, group, setting):
        a=self.vault.account(aid)
        if not team_lost(a) or not a.get('binding'):raise VaultError('RETIREMENT_NOT_ELIGIBLE')
        if a.get('retirement'):raise VaultError('RETIREMENT_ALREADY_HANDLED')
        plan={'id':str(uuid.uuid4()),'state':'queued','step':'precheck','group_id':group,
              'instance':admin_url(setting['sub2api_url']),
              'connection_revision':setting.get('cloud_revision','legacy'),
              'binding':deepcopy(a['binding']),'revision':a['revision'],
              'mutation':None,'created':time.time(),'history':[]}
        with self.vault.transaction():
            jobs=self.vault.queue([aid],kind='retire',context={'retirement_id':plan['id']})
            if not jobs:raise VaultError('OPERATION_RUNNING')
            plan['job_id']=jobs[0]
            self.vault.update_account(aid,retirement=plan)
            self.service.monitor.record(aid,enabled=False,state='retirement_queued',blocked=True)
        return {'account_id':aid,'job_id':jobs[0],'state':'queued'}

    def save(self, aid, plan, **changes):
        plan.update(changes,updated=time.time())
        self.vault.update_account(aid,retirement=plan)

    def guard(self, aid, plan):
        a=self.vault.account(aid);setting=self.service.cloud();cfg=self.config()
        if (self.service.stop.is_set() or self.vault.cancellation_requested(plan['job_id'])
                or not cfg['enabled']):raise VaultError('RETIREMENT_STOPPED')
        if (admin_url(setting['sub2api_url'])!=plan['instance']
                or setting.get('cloud_revision','legacy')!=plan['connection_revision']
                or (cfg.get('instance') and cfg['instance']!=plan['instance'])
                or (cfg.get('group_id') is not None and cfg['group_id']!=plan['group_id'])):
            raise VaultError('RETIREMENT_SITE_CHANGED')
        if (a['binding']!=plan['binding'] or a['revision']!=plan['revision'] or not team_lost(a)):
            raise VaultError('RETIREMENT_NOT_ELIGIBLE')
        return a

    def read(self, aid, plan):
        self.guard(aid,plan)
        cloud=self.service.cloud_read(f"/accounts/{plan['binding']['cloud_id']}")
        self.guard(aid,plan);guard_identity(cloud,plan['binding'])
        if type(cloud.get('schedulable')) is not bool or not cloud.get('updated_at'):
            raise VaultError('RETIREMENT_CLOUD_STATE_UNKNOWN')
        return cloud

    def mutate(self, aid, plan, step, body, *, put=False):
        self.guard(aid,plan)
        self.save(aid,plan,mutation={'stage':step,'at':time.time()})
        try:self.guard(aid,plan)
        except Exception:
            self.save(aid,plan,mutation=None)
            raise
        path=f"/accounts/{plan['binding']['cloud_id']}"
        if put:self.service.cloud_put(path,body)
        else:self.service.cloud_write(path+'/schedulable',body)

    def run(self, job):
        aid=job['account_id'];plan=self.vault.account(aid).get('retirement') or {}
        if plan.get('id')!=job['context'].get('retirement_id'):
            self.vault.finish(job['id'],'failed','RETIREMENT_NOT_ELIGIBLE');return
        try:
            if plan.get('mutation'):raise VaultError('RETIREMENT_WRITE_UNKNOWN')
            self.guard(aid,plan)
            if self.target(self.service.cloud())!=plan['group_id']:raise VaultError('RETIREMENT_GROUP_UNAVAILABLE')
            self.save(aid,plan,state='running',step='precheck')
            cloud=self.read(aid,plan);baseline=config_fingerprint(cloud)
            self.save(aid,plan,original_groups=cloud.get('group_ids'),baseline=baseline,
                      cloud_revision=cloud['updated_at'],step='pause')
            self.vault.job_stage(job['id'],'retirement_pause')
            fresh=self.read(aid,plan)
            if fresh['updated_at']!=cloud['updated_at'] or config_fingerprint(fresh)!=baseline:
                raise VaultError('RETIREMENT_CLOUD_CHANGED')
            if fresh['schedulable']:
                self.mutate(aid,plan,'pause',{'schedulable':False})
                fresh=self.read(aid,plan)
                if fresh['schedulable'] or config_fingerprint(fresh)!=baseline:
                    raise VaultError('RETIREMENT_FINAL_STATE_INVALID')
                self.save(aid,plan,mutation=None)
            self.save(aid,plan,step='move',cloud_revision=fresh['updated_at'])
            self.vault.job_stage(job['id'],'retirement_move')
            before=self.read(aid,plan)
            if (before['schedulable'] or before['updated_at']!=fresh['updated_at']
                    or config_fingerprint(before)!=baseline):raise VaultError('RETIREMENT_CLOUD_CHANGED')
            expected=deepcopy(before);expected['group_ids']=[plan['group_id']]
            if set(before.get('group_ids',[]))!={plan['group_id']}:
                self.mutate(aid,plan,'move',{'group_ids':[plan['group_id']]},put=True)
            self.vault.job_stage(job['id'],'retirement_confirm')
            after=self.read(aid,plan)
            if (after['schedulable'] or set(after.get('group_ids',[]))!={plan['group_id']}
                    or config_fingerprint(after)!=config_fingerprint(expected)
                    or after.get('credentials')!=before.get('credentials')):
                raise VaultError('RETIREMENT_FINAL_STATE_INVALID')
            with self.vault.transaction():
                self.guard(aid,plan)
                self.save(aid,plan,state='complete',step='complete',mutation=None,code='TEAM_LOST_RETIRED',
                          completed_at=time.time())
                self.service.monitor.record(aid,enabled=False,state='retired',blocked=False,auth_401=False,
                    last_code='TEAM_LOST_RETIRED',continuation=None,automatic_reauth=None,next_retry=None,
                    owned_pause=None,cloud_status=after.get('status'),cloud_schedulable=False)
                self.vault.update_account(aid,status='retired')
                catalog=self.vault.get_setting('cloud_catalog',None)
                if catalog and (catalog.get('instance'),catalog.get('connection_revision'))==(
                        plan['instance'],plan['connection_revision']):
                    from sub2easy.binding import metadata
                    catalog['accounts']=[metadata(after) if r['id']==after['id'] else r for r in catalog['accounts']]
                    self.vault.set_setting('cloud_catalog',catalog)
                self.vault.finish(job['id'],'succeeded','TEAM_LOST_RETIRED','complete')
        except Exception as exc:
            code=str(exc) if isinstance(exc,VaultError) else 'RETIREMENT_READ_FAILED'
            unknown=bool(plan.get('mutation'))
            self.save(aid,plan,state='unknown' if unknown else 'failed',code=code)
            self.service.monitor.record(aid,enabled=False,state='retirement_failed',blocked=True,last_code=code)
            self.vault.finish(job['id'],'unknown' if unknown else 'failed',code,plan.get('step','precheck'))
