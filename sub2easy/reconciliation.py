"""Operator-controlled reconciliation of unknown account creation. Remote GETs only."""

from copy import deepcopy
import hashlib
import json
import secrets
import time

from sub2easy.binding import candidates, classify, metadata
from sub2easy.monitor import guard_identity
from sub2easy.preflight import Client, admin_url
from sub2easy.vault import VaultError


def unknown_create(account):
    dep=account.get('deployment') or {}
    return (dep.get('state')=='unknown' and dep.get('step')=='create'
            and (dep.get('mutation') or {}).get('stage')=='create'
            and dep.get('new_account') is True and not dep.get('cloud_id')
            and not dep.get('binding') and not account.get('binding')
            and bool(account.get('authorization')) and not account.get('retirement'))


class CreateReconciliation:
    def __init__(self, service):
        self.service,self.vault=service,service.vault

    def context(self, aid):
        self.service.require_account_idle(aid)
        if self.service.stop.is_set():raise VaultError('OPERATION_RUNNING')
        a=self.vault.account(aid);s=self.service.cloud()
        if not unknown_create(a):raise VaultError('RECONCILE_NOT_CREATE_UNKNOWN')
        if ((a.get('write_intent') or {}).get('state') not in {None,'confirmed'}
                or (a.get('schedule_intent') or {}).get('state') in {'pending','unknown'}
                or (a.get('monitor') or {}).get('owned_pause')):
            raise VaultError('PREVIOUS_WRITE_NEEDS_RECONCILIATION')
        if a['deployment']['instance']!=admin_url(s['sub2api_url']):
            raise VaultError('CLOUD_INSTANCE_CHANGED')
        stamp=hashlib.sha256(json.dumps({'revision':a['revision'],'deployment':a['deployment'],
                                        'authorization':a['authorization']},sort_keys=True).encode()).hexdigest()
        return a,s,stamp

    def lookup(self, a, setting):
        # Same-email, UID or creation marker can identify candidates. A shared
        # team workspace alone is not evidence for taking over another member.
        clouds=Client(setting['sub2api_url'],setting['admin_key']).accounts(platform='openai')
        used={r['binding']['cloud_id']:r['id'] for r in self.vault.accounts()
              if r.get('binding') and r['binding']['instance']==a['deployment']['instance']}
        rows=[r for r in candidates(a,clouds,used) if set(r['reasons']) & {'email','name','user_id'}]
        kind,chosen=classify(rows)
        return rows,kind,chosen

    def inspect(self, aid):
        # Invalidate the previous report before IO: a failed refresh cannot leave
        # an older "no match" result available for a subsequent confirmation.
        self.vault.set_setting('create_reconcile:'+aid,None)
        a,s,stamp=self.context(aid)
        rows,kind,chosen=self.lookup(a,s)
        _,current,new_stamp=self.context(aid)
        if new_stamp!=stamp or current.get('cloud_revision','legacy')!=s.get('cloud_revision','legacy'):
            raise VaultError('RECONCILE_CHANGED')
        report={'id':secrets.token_hex(16),'account_id':aid,'created_at':time.time(),
                'instance':a['deployment']['instance'],'connection_revision':s.get('cloud_revision','legacy'),
                'kind':kind,'candidates':rows,'suggested_id':chosen['id'] if chosen else None,
                'stamp':stamp}
        self.vault.set_setting('create_reconcile:'+aid,report)
        return {k:v for k,v in report.items() if k!='stamp'}

    def resolve(self, aid, data):
        if data.get('confirm_resolution') is not True:raise VaultError('CONFIRM_RECONCILE')
        action=data.get('action')
        if action not in {'bind_existing','allow_create'}:raise VaultError('INVALID_RECONCILE_ACTION')
        report=self.vault.get_setting('create_reconcile:'+aid,None)
        if (not report or data.get('report_id')!=report['id']
                or not 0<=time.time()-report['created_at']<=300):
            raise VaultError('RECONCILE_REPORT_EXPIRED')
        a,s,stamp=self.context(aid)
        if (stamp!=report['stamp'] or s.get('cloud_revision','legacy')!=report['connection_revision']):
            raise VaultError('RECONCILE_CHANGED')
        # Re-read the complete current list. Missing or incomplete pages and HTTP
        # failures never count as "not created".
        rows,kind,_=self.lookup(a,s)
        binding=None;own_created=False
        if action=='allow_create':
            if (data.get('confirm_original_request_finished') is not True
                    or data.get('accept_duplicate_risk') is not True):
                raise VaultError('CONFIRM_RECREATE_RISK')
            if report['kind']!='not_found' or kind!='not_found':raise VaultError('RECONCILE_MATCH_EXISTS')
        else:
            cid=data.get('cloud_id')
            if type(cid) is not int:raise VaultError('INVALID_CLOUD_ID')
            chosen=next((r for r in rows if r['id']==cid and r['eligible']),None)
            previous=next((r for r in report['candidates'] if r['id']==cid and r['eligible']),None)
            if not chosen or not previous:raise VaultError('CLOUD_MATCH_CONFLICT')
            if chosen['fingerprint']!=previous['fingerprint']:raise VaultError('CLOUD_CANDIDATE_CHANGED')
            binding={'instance':report['instance'],'cloud_id':cid,'identity':deepcopy(a['authorization']['identity'])}
            cloud=self.service.cloud_read(f'/accounts/{cid}');guard_identity(cloud,binding)
            if metadata(cloud)['fingerprint']!=chosen['fingerprint']:raise VaultError('CLOUD_CANDIDATE_CHANGED')
            if cloud.get('status') not in {'active','error'} or type(cloud.get('schedulable')) is not bool:
                raise VaultError('CLOUD_ACCOUNT_ON_HOLD')
            own_created=(cloud.get('name')=='s2e-'+aid
                         and all((cloud.get('credentials') or {}).get(k)==a['authorization']['credentials'].get(k)
                                 for k in ('access_token','refresh_token')))
        # No remote writes or logins. Only release a known, explicitly reconciled
        # local checkpoint; the normal retry path applies/test/enables later.
        with self.vault.transaction():
            latest,settings,current_stamp=self.context(aid)
            if current_stamp!=stamp or settings.get('cloud_revision','legacy')!=report['connection_revision']:
                raise VaultError('RECONCILE_CHANGED')
            dep=deepcopy(latest['deployment'])
            evidence={'action':action,'report_id':report['id'],'at':time.time(),'old_mutation':dep['mutation'],
                      'old_connection_revision':dep['connection_revision'],'cloud_id':(binding or {}).get('cloud_id')}
            dep.update(state='failed',code='RECONCILED_READY_TO_RETRY',step='pause_new' if own_created else 'precheck',mutation=None,
                       connection_revision=report['connection_revision'],updated=time.time(),
                       reconciliation=evidence,new_account=binding is None or own_created,binding=binding,
                       cloud_id=(binding or {}).get('cloud_id'),matched_existing=binding is not None)
            if own_created:
                dep['applied_auth_digest']=self.vault.authorization_digest(a['authorization'])
            dep['history']=(dep.get('history',[])+[{'stage':'reconciled','at':time.time()}])[-60:]
            self.vault.update_account(aid,status='authorized',binding=binding,deployment=dep)
            self.vault.set_setting('create_reconcile:'+aid,None)
        return {'state':'ready_to_retry','account_id':aid,'cloud_id':dep['cloud_id'],
                'action':action,'remote_mutations':0,'jobs_queued':0}
