"""Deterministic candidate ranking and locally journaled batch binding.

Names and times rank candidates; only unambiguous matching identity metadata can
auto-bind. Never infer ownership from a small time delta or an array position.
"""

from collections import Counter
import hashlib
import json
import time

from sub2easy.intake import normalize_email, IntakeError
from sub2easy.preflight import Client, PreflightError, admin_url, timestamp
from sub2easy.vault import VaultError


def as_email(value):
    try:
        return normalize_email(value)
    except IntakeError:
        return None


def epoch(value):
    try:
        parsed = timestamp(value)
        return parsed.timestamp() if parsed else None
    except (ValueError, OverflowError, OSError):
        return None


def string(value, limit=400):
    return value[:limit] if isinstance(value, str) else ''


def metadata(cloud):
    """Explicit display whitelist. Never include tokens, proxy auth or raw errors."""
    from sub2easy.monitor import auth_signal
    c = cloud.get('credentials') or {}
    if not isinstance(c, dict): c = {}
    groups = cloud.get('groups') or []
    groups = [{'id': g['id'], 'name': string(g.get('name'))} for g in groups
              if isinstance(g, dict) and type(g.get('id')) is int and g['id'] > 0] if isinstance(groups, list) else []
    group_ids = cloud.get('group_ids')
    if not isinstance(group_ids, list): group_ids = [g['id'] for g in groups]
    proxy = cloud.get('proxy') or {}
    extra = cloud.get('extra') or {}
    result = {
        'id': cloud['id'], 'name': string(cloud.get('name')),
        'email': as_email(c.get('email')), 'workspace_id': string(c.get('chatgpt_account_id')),
        'user_id': string(c.get('chatgpt_user_id')), 'platform': string(cloud.get('platform'),40),
        'type': string(cloud.get('type'),40), 'status': string(cloud.get('status'),40),
        'schedulable': cloud.get('schedulable') if type(cloud.get('schedulable')) is bool else None,
        'created_at': epoch(cloud.get('created_at')), 'updated_at': epoch(cloud.get('updated_at')),
        'last_used_at': epoch(cloud.get('last_used_at')), 'expires_at': epoch(cloud.get('expires_at')),
        'group_ids': [g for g in group_ids if type(g) is int and g > 0], 'groups': groups,
        'proxy_id': cloud.get('proxy_id') if type(cloud.get('proxy_id')) is int else None,
        'proxy_name': string(proxy.get('name')) if isinstance(proxy, dict) else '',
        'concurrency': cloud.get('concurrency') if type(cloud.get('concurrency')) is int else None,
        'priority': cloud.get('priority') if type(cloud.get('priority')) is int else None,
        'parent_account_id': cloud.get('parent_account_id') if type(cloud.get('parent_account_id')) is int else
                             None if cloud.get('parent_account_id') is None else -1,
        'auth_401': auth_signal(cloud) is not None,
        'fingerprint_mode': string(extra.get('codex_fingerprint_mode'),40) if isinstance(extra,dict) else '',
    }
    # Used as a selection precondition, NOT a token revision or proof of identity.
    comparable={k:result[k] for k in ('id','name','email','workspace_id','user_id','platform','type','created_at',
                'group_ids','proxy_id','parent_account_id')}
    # /accounts?lite=true can omit preloaded group/proxy names present in GET /accounts/:id.
    result['fingerprint'] = hashlib.sha256(json.dumps(comparable,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return result


def local_hints(account):
    names = {account['login']['account'].casefold(), ('s2e-' + account['id']).casefold()}
    reference = account.get('imported_at')
    reference_kind = 'local_import' if reference else None
    raw = account.get('raw_result')
    if isinstance(raw, dict):
        from sub2easy.nvtokens import export_document
        doc = export_document(raw)
        if isinstance(doc, dict):
            accounts = doc.get('accounts', [doc])
            if isinstance(accounts,list) and len(accounts)==1 and isinstance(accounts[0],dict):
                name = accounts[0].get('name')
                if isinstance(name,str) and name.strip(): names.add(name.strip().casefold())
            generated = epoch(doc.get('exported_at'))
            if generated: reference,reference_kind=generated,'authorization_export'
    return names,reference,reference_kind


def candidates(account, clouds, used):
    expected=account.get('authorization',{} ) or {}
    identity=expected.get('identity') or {}
    names,reference,kind=local_hints(account)
    result=[]
    for cloud in clouds:
        m=metadata(cloud);reasons=[];conflicts=[];score=0
        matches_email=m['email']==account['login']['account']
        matches_workspace=bool(identity.get('chatgpt_account_id')) and identity['chatgpt_account_id']==m['workspace_id']
        name_match=m['name'].strip().casefold() in names
        if matches_workspace: reasons.append('workspace');score+=200
        if matches_email: reasons.append('email');score+=100
        if name_match: reasons.append('name');score+=50
        delta=abs(m['created_at']-reference) if m['created_at'] is not None and reference else None
        if delta is not None and delta<=3600:reasons.append('near_time');score+=10
        if not reasons:continue
        if m['platform']!='openai' or m['type']!='oauth' or m['parent_account_id'] is not None:conflicts.append('UNSUPPORTED_CLOUD_ACCOUNT')
        if not m['email'] or not m['workspace_id']:conflicts.append('CLOUD_IDENTITY_METADATA_MISSING')
        elif not matches_email:conflicts.append('CLOUD_IDENTITY_MISMATCH')
        if identity:
            if (identity.get('account_email')!=m['email'] or identity.get('chatgpt_account_id')!=m['workspace_id']
                    or (identity.get('chatgpt_user_id') and identity['chatgpt_user_id']!=m['user_id'])):
                conflicts.append('AUTH_SUBJECT_OR_WORKSPACE_MISMATCH')
        if m['id'] in used and used[m['id']]!=account['id']:conflicts.append('CLOUD_ALREADY_BOUND')
        # A name/time-only match remains visible but cannot bypass contradictory identity.
        result.append({**m,'reasons':reasons,'conflicts':sorted(set(conflicts)),
                       'eligible':not conflicts,'score':score,'time_delta_seconds':round(delta) if delta is not None else None,
                       'time_reference':kind})
    return sorted(result,key=lambda c:(-c['score'],c['time_delta_seconds'] if c['time_delta_seconds'] is not None else float('inf'),c['id']))


def classify(cands):
    # An occupied duplicate is still a duplicate, not a reason to silently pick another.
    identity_matches=[c for c in cands if not [e for e in c['conflicts'] if e!='CLOUD_ALREADY_BOUND']]
    if len(identity_matches)>1:return 'ambiguous',None
    if len(identity_matches)==1:
        c=identity_matches[0]
        return ('ready',c) if c['eligible'] else ('conflict',None)
    return ('conflict' if cands else 'not_found'),None


class BindingCenter:
    def __init__(self, service):
        self.service,self.vault=service,service.vault

    def scope(self):
        s=self.service.cloud()
        return admin_url(s['sub2api_url']),s.get('cloud_revision','legacy')

    def catalog(self):
        instance,revision=self.scope();s=self.service.cloud()
        accounts=Client(s['sub2api_url'],s['admin_key']).accounts()
        result={'instance':instance,'connection_revision':revision,'fetched_at':time.time(),
                'accounts':[metadata(a) for a in accounts]}
        self.vault.set_setting('cloud_catalog',result)
        return accounts,result

    def report(self):
        report=self.vault.get_setting('binding_report',None)
        if not report:return None
        try:scope=self.scope()
        except VaultError:return None
        if (report['instance'],report['connection_revision'])!=scope:return None
        # Annotate resolution done outside the batch center without exposing credentials.
        return report

    def save_report(self, report):
        report['updated_at']=time.time()
        report['counts']=dict(Counter(row['status'] for row in report['items']))
        self.vault.set_setting('binding_report',report)
        return report

    def scan(self, ids, auto_bind=False):
        if not isinstance(ids,list) or not ids or len(ids)>100 or any(not isinstance(i,str) for i in ids):
            raise VaultError('SELECT_1_TO_100_ACCOUNTS')
        ids=list(dict.fromkeys(ids));instance,revision=self.scope()
        old=self.report()
        retained=[r for r in old['items'] if r['account_id'] not in ids] if old else []
        report={'instance':instance,'connection_revision':revision,'created_at':time.time(),'items':retained}
        try:clouds,catalog=self.catalog()
        except (PreflightError,VaultError):
            report['items'] += [{'account_id':i,'status':'failed','code':'BINDING_FETCH_FAILED','candidates':[]} for i in ids]
            return self.save_report(report)
        used={a['binding']['cloud_id']:a['id'] for a in self.vault.accounts() if a['binding'] and a['binding']['instance']==instance}
        for account_id in ids:
            row={'account_id':account_id,'status':'failed','code':'','candidates':[]}
            try:
                a=self.vault.account(account_id)
                row['local']={'label':a['login']['account'],'imported_at':a.get('imported_at'),'revision':a['revision'],
                              'names':sorted(local_hints(a)[0]),'reference_at':local_hints(a)[1]}
                if a.get('binding'):
                    row.update(status='already_bound',code='BINDING_ALREADY_EXISTS',cloud_id=a['binding']['cloud_id'])
                elif self.vault.pending(account_id) or a.get('monitor',{}).get('owned_pause'):
                    row.update(status='conflict',code='OPERATION_RUNNING')
                elif a.get('write_intent'):
                    row.update(status='conflict',code='BINDING_WRITE_NEEDS_RECONCILIATION')
                else:
                    cands=candidates(a,clouds,used);status,chosen=classify(cands)
                    row.update(status=status,code={'ambiguous':'MULTIPLE_CLOUD_MATCHES','not_found':'NO_CLOUD_MATCH',
                                                   'conflict':'CLOUD_MATCH_CONFLICT','ready':'UNIQUE_IDENTITY_MATCH'}[status],candidates=cands)
                    if chosen and auto_bind:
                        self.service.bind(account_id,chosen['id'],expected_fingerprint=chosen['fingerprint'])
                        used[chosen['id']]=account_id
                        row.update(status='bound',code='AUTO_BOUND',cloud_id=chosen['id'])
            except (VaultError,PreflightError) as exc:
                row.update(status='failed',code='BINDING_FETCH_FAILED' if isinstance(exc,PreflightError) else str(exc))
            report['items'].append(row)
            self.save_report(report)  # completed items survive a later error/interruption
        return self.save_report(report)

    def resolve(self, account_id, cloud_id, fingerprint, connection_revision):
        report=self.report()
        if not report or connection_revision!=report['connection_revision']:raise VaultError('BINDING_REPORT_STALE')
        row=next((r for r in report['items'] if r['account_id']==account_id),None)
        if not row:raise VaultError('BINDING_REPORT_STALE')
        candidate=next((r for r in row['candidates'] if r['id']==cloud_id and r['fingerprint']==fingerprint),None)
        if not candidate or not candidate['eligible']:raise VaultError('CLOUD_MATCH_CONFLICT')
        try:
            self.service.bind(account_id,cloud_id,expected_fingerprint=fingerprint)
            row.update(status='bound',code='MANUALLY_BOUND',cloud_id=cloud_id)
        except (VaultError,PreflightError) as exc:
            row.update(status='failed',code='BINDING_FETCH_FAILED' if isinstance(exc,PreflightError) else str(exc))
        return self.save_report(report)

    def browse(self, q='', platform='', status='', group='', proxy='', binding='', page=1, page_size=25, sort='created_desc', since=None, until=None):
        instance,revision=self.scope();catalog=self.vault.get_setting('cloud_catalog',None)
        if not catalog or (catalog['instance'],catalog['connection_revision'])!=(instance,revision):
            return {'items':[],'total':0,'pages':1,'page':1,'needs_refresh':True,'facets':{}}
        owners={a['binding']['cloud_id']:a for a in self.vault.accounts() if a['binding'] and a['binding']['instance']==instance}
        rows=[]
        for a in catalog['accounts']:
            owner=owners.get(a['id']);row={**a,'local_id':owner['id'] if owner else None,
                                         'monitored':bool(owner and owner['monitor'].get('enabled'))}
            rows.append(row)
        facets={'platforms':sorted({r['platform'] for r in rows if r['platform']}),
                'groups':{str(i):next((g['name'] for g in r['groups'] if g['id']==i),'') or f'#{i}' for r in rows for i in r['group_ids']},
                'proxies':{str(r['proxy_id']):r['proxy_name'] or f"#{r['proxy_id']}" for r in rows if r['proxy_id']}}
        def match(r):
            if q.casefold() not in ' '.join(str(r[k] or '') for k in ('id','name','email','workspace_id')).casefold():return False
            if platform and r['platform']!=platform:return False
            if status and not (r['auth_401'] if status=='401' else r['schedulable'] is False if status=='paused' else r['status']==status):return False
            if group and not (not r['group_ids'] if group=='ungrouped' else group in map(str,r['group_ids'])):return False
            if proxy and not (r['proxy_id'] is None if proxy=='direct' else str(r['proxy_id'])==proxy):return False
            if binding=='bound' and not r['local_id']:return False
            if binding=='unbound' and r['local_id']:return False
            if binding=='monitored' and not r['monitored']:return False
            if since is not None and (r['created_at'] is None or r['created_at']<since):return False
            if until is not None and (r['created_at'] is None or r['created_at']>=until):return False
            return True
        rows=[r for r in rows if match(r)]
        keys={'created_desc':lambda r:-(r['created_at'] or 0),'created_asc':lambda r:r['created_at'] or 0,
              'name':lambda r:r['name'].casefold(),'id':lambda r:r['id']}
        rows.sort(key=lambda r:(keys.get(sort,keys['created_desc'])(r),r['id']))
        pages=max(1,(len(rows)+page_size-1)//page_size);page=min(page,pages)
        return {'items':rows[(page-1)*page_size:page*page_size],'total':len(rows),'pages':pages,'page':page,
                'facets':facets,'fetched_at':catalog['fetched_at'],'needs_refresh':False,'instance':instance}
