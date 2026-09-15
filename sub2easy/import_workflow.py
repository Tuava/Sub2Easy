"""Single-page intake -> persisted deployment batch. No network in transactions."""

from collections import Counter
from dataclasses import asdict, replace
import hashlib
import hmac
import json
import re
import time
import uuid

from sub2easy.intake import parse_batch, IntakeError
from sub2easy.lifecycle import ImportProfile
from sub2easy.preflight import admin_url
from sub2easy.sub2_import import parse_sub2
from sub2easy.vault import VaultError
from sub2easy.deployment import replaceable_precheck, profile_summary
from sub2easy.reconciliation import unknown_create


class ImportWorkflow:
    def __init__(self, service):
        self.service, self.vault = service, service.vault

    @staticmethod
    def _request_id(value):
        if not isinstance(value,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}',value):
            raise VaultError('IMPORT_REQUEST_ID_REQUIRED')
        return value

    def submit(self, data):
        self.vault.require_key()
        request_id=self._request_id(data.get('request_id'))
        if data.get('confirm_deploy') is not True:raise VaultError('CONFIRM_SERVER_DEPLOYMENT')
        settings=self.service.cloud()
        scope=(admin_url(settings['sub2api_url']),settings.get('cloud_revision','legacy'))
        profile=ImportProfile.from_dict(data.get('profile'))
        if profile.instance_id!=scope[0] or data.get('connection_revision')!=scope[1]:
            raise VaultError('DEPLOY_CONNECTION_CHANGED')
        model=data.get('model_id')
        if not isinstance(model,str) or not model.strip() or len(model)>160 or any(ord(c)<32 for c in model):
            raise VaultError('DEPLOY_MODEL_REQUIRED')
        mode=data.get('format','login')
        if mode not in {'login','sub2'}:raise IntakeError('UNSUPPORTED_IMPORT_FORMAT')
        identity={'instance':scope[0],'connection_revision':scope[1],'format':mode,'text':data.get('text'),
                  'model_id':model.strip(),'profile':asdict(profile),
                  'update_credentials':data.get('update_credentials') is True,
                  'auto_monitor':data.get('auto_monitor',True) is True,
                  'staging_verified':data.get('staging_verified') is True}
        # Store a keyed digest, never an unkeyed hash or plaintext copy of input credentials.
        digest=hmac.new(self.vault.require_key(),b'import-deploy\0'+json.dumps(identity,sort_keys=True).encode(),hashlib.sha256).hexdigest()
        key='import_batch:'+request_id
        with self.vault.transaction():
            previous=self.vault.get_setting(key,None)
            if previous:
                if not hmac.compare_digest(previous['digest'],digest):raise VaultError('IMPORT_REQUEST_CONFLICT')
                return self._project(previous)
            if mode=='sub2':
                parsed=parse_sub2(data.get('text'),settings.get('oauth_client_id',''))
                results=self.vault.import_sub2(parsed,asdict(profile),data.get('update_credentials') is True)['results']
            else:
                parsed=parse_batch(data.get('text'))
                if len(parsed.materials)+len(parsed.issues)+len(parsed.duplicates)>1000:
                    raise IntakeError('SUB2_TOO_MANY_ACCOUNTS')
                if not parsed.materials and not parsed.issues:raise IntakeError('IMPORT_NO_ACCOUNTS')
                valid=replace(parsed,issues=())
                results=self.vault.import_materials(valid,asdict(profile),include_results=True)['results']
                results += [{'index':e.line,'state':'failed','code':e.code} for e in parsed.issues]
            # Only this input's successful rows are candidates. Never diff the entire
            # account list, which could capture imports from another tab.
            ids=list(dict.fromkeys(r['account_id'] for r in results if r['state'] in
                     {'added','updated','supplemented','duplicate'} and r.get('account_id')))
            deployments={}
            for start in range(0,len(ids),100):
                queued=self.service.deployments.queue(ids[start:start+100],model.strip(),asdict(profile),
                                                      data.get('staging_verified') is True,data.get('auto_monitor',True))
                deployments.update({r['account_id']:r for r in queued['items']})
            items=[]
            for item in sorted(results,key=lambda r:r['index']):
                row={'index':item['index'],'intake_state':item['state'],'account_id':item.get('account_id'),
                     'state':'failed' if item['state'] in {'failed','conflict'} else 'skipped',
                     'code':item.get('code','')}
                dep=deployments.get(item.get('account_id'))
                if dep and item['state'] not in {'failed','conflict'}:row.update(dep)
                items.append(row)
            batch={'id':request_id,'digest':digest,'instance':scope[0],'connection_revision':scope[1],
                   'created_at':time.time(),'profile':asdict(profile),'model_id':model.strip(),'items':items,
                   'staging_verified':data.get('staging_verified') is True}
            batch['auto_monitor']=data.get('auto_monitor',True)
            self.vault.set_setting(key,batch)
            self._remember(request_id)
            return self._project(batch)

    def _remember(self, request_id):
        history=self.vault.get_setting('import_batch_history',[])
        self.vault.set_setting('import_batch_history',([request_id]+[i for i in history if i!=request_id])[:100])
        self.vault.set_setting('latest_import_batch',request_id)

    def save_local(self, data, default_profile):
        """Persist a result batch and exact IDs even with no configured server."""
        self.vault.require_key()
        profile=ImportProfile.from_dict(data.get('profile') or default_profile)
        settings=self.vault.get_setting('connection',{})
        mode=data.get('format','login')
        with self.vault.transaction():
            if mode=='sub2':
                report=self.vault.import_sub2(parse_sub2(data.get('text'),settings.get('oauth_client_id','')),
                                             asdict(profile),data.get('update_credentials') is True)
            elif mode=='login':
                parsed=parse_batch(data.get('text'))
                if len(parsed.materials)+len(parsed.issues)+len(parsed.duplicates)>1000:
                    raise IntakeError('SUB2_TOO_MANY_ACCOUNTS')
                if not parsed.materials and not parsed.issues:raise IntakeError('IMPORT_NO_ACCOUNTS')
                # Keep valid rows and report malformed rows together; local intake
                # must not force the operator to lose a large pasted batch.
                report=self.vault.import_materials(replace(parsed,issues=()),asdict(profile),include_results=True)
                report['results'] += [{'index':e.line,'state':'failed','code':e.code} for e in parsed.issues]
                report['results'].sort(key=lambda r:r['index'])
            else:raise IntakeError('UNSUPPORTED_IMPORT_FORMAT')
            items=[]
            for row in report['results']:
                accepted=row['state'] not in {'failed','conflict'} and bool(row.get('account_id'))
                items.append({**row,'intake_state':row['state'],
                              'state':'local_saved' if accepted else 'failed' if row['state'] in {'failed','conflict'} else 'skipped'})
            request_id=str(uuid.uuid4())
            batch={'id':request_id,'instance':None,'connection_revision':None,'created_at':time.time(),
                   'profile':asdict(profile),'model_id':'','items':items,'kind':'local'}
            self.vault.set_setting('import_batch:'+request_id,batch)
            self._remember(request_id)
            return {**report,'batch':self._project(batch)}

    def history(self):
        self.vault.require_key()
        ids=self.vault.get_setting('import_batch_history',[])
        latest=self.vault.get_setting('latest_import_batch',None)
        if latest and latest not in ids:ids=[latest]+ids
        result=[]
        for request_id in ids:
            batch=self.get(request_id)
            if batch:
                result.append({k:batch[k] for k in ('id','created_at','instance','counts','pending')})
        return result

    def deploy_saved(self, request_id, data):
        if self.get(request_id) is None:raise VaultError('IMPORT_BATCH_UNAVAILABLE')
        if data.get('confirm_deploy') is not True:raise VaultError('CONFIRM_SERVER_DEPLOYMENT')
        settings=self.service.cloud()
        profile=ImportProfile.from_dict(data.get('profile'))
        instance=admin_url(settings['sub2api_url'])
        if (profile.instance_id!=instance or data.get('connection_revision')!=settings.get('cloud_revision','legacy')):
            raise VaultError('DEPLOY_CONNECTION_CHANGED')
        with self.vault.transaction():
            key='import_batch:'+request_id;batch=self.vault.get_setting(key)
            rows=[r for r in batch['items'] if r['state']=='local_saved']
            if not rows:return self._project(batch)  # lost-response retries never enqueue twice
            ids=list(dict.fromkeys(r['account_id'] for r in rows))
            results={}
            for start in range(0,len(ids),100):
                result=self.service.deployments.queue(ids[start:start+100],data.get('model_id'),asdict(profile),
                                                      data.get('staging_verified') is True,data.get('auto_monitor',True))
                results.update({r['account_id']:r for r in result['items']})
            for row in rows:row.update(results[row['account_id']])
            batch.update(instance=instance,connection_revision=settings.get('cloud_revision','legacy'),
                         profile=asdict(profile),model_id=data['model_id'].strip(),
                         staging_verified=data.get('staging_verified') is True,auto_monitor=data.get('auto_monitor',True))
            self.vault.set_setting(key,batch)
            return self._project(batch)

    def _project(self,batch):
        job_ids=list(dict.fromkeys(r['job_id'] for r in batch['items'] if r.get('job_id')))
        jobs={j['id']:j for j in self.vault.jobs(job_ids)}
        accounts={}
        items=[]
        for row in batch['items']:
            item=dict(row)
            job=jobs.get(row.get('job_id'))
            if job:
                item.update(state=job['state'],code=job['code'],step=job['stage'],updated_at=job['updated'],
                            cancel_requested=job['cancel_requested'])
            aid=row.get('account_id')
            if aid:
                if aid not in accounts:
                    try:accounts[aid]=self.vault.account(aid)
                    except VaultError:accounts[aid]=None
                a=accounts[aid]
                if a:
                    dep=a.get('deployment') or {}
                    if row.get('intake_state') not in {'failed','conflict'}:
                        item['can_reconcile_create']=unknown_create(a)
                        if unknown_create(a):
                            # A rejected duplicate enqueue has no new job_id; expose
                            # the original creation phase instead of "材料校验".
                            item['step']='create'
                            item['cloud_creation_uncertain']=True
                            details=dep.get('failure_details') or {}
                            item['failure_details']={k:details.get(k) for k in ('http_status','cause')}
                        if dep.get('code')=='RECONCILED_READY_TO_RETRY' and item['state'] in {'failed','unknown','review'}:
                            item.update(state='failed',code='RECONCILED_READY_TO_RETRY',step='precheck')
                    if dep.get('job_id') and dep.get('job_id')==row.get('job_id'):
                        item['operation']='create' if dep.get('new_account') else 'update_existing'
                        item['matched_existing']=dep.get('matched_existing',False)
                        item['execution_profile']=profile_summary(dep['profile'])
                        error=dep.get('precheck_error') or {}
                        item['precheck_error']={k:error.get(k) for k in (
                            'missing_staging_group_id','missing_target_group_ids','missing_proxy_id')}
                    item['label']=a['login']['account']
                    item['monitor_enabled']=a.get('monitor',{}).get('enabled',False)
                    item['monitor_code']=a.get('monitor',{}).get('enrollment_code','')
                    item['has_login_material']=bool(a['login'].get('password') and a['login'].get('totp_secret'))
                    b=a.get('binding')
                    item['cloud_id']=b['cloud_id'] if b and (not batch['instance'] or b['instance']==batch['instance']) else None
            items.append(item)
        return {k:batch[k] for k in ('id','instance','connection_revision','created_at','profile','model_id')} | {
            'items':items,'counts':dict(Counter(i['state'] for i in items)),
            'pending':any(i['state'] in {'queued','running'} for i in items),
        }

    def get(self,request_id=None):
        self.vault.require_key()
        request_id=request_id or self.vault.get_setting('latest_import_batch',None)
        if not request_id:return None
        self._request_id(request_id)
        batch=self.vault.get_setting('import_batch:'+request_id,None)
        if batch is None:return None
        if batch['instance']:
            settings=self.vault.get_setting('connection',{})
            if not settings.get('sub2api_url'):return None
            if (batch['instance'],batch['connection_revision'])!=(admin_url(settings['sub2api_url']),settings.get('cloud_revision','legacy')):
                return None
        return self._project(batch)

    def retry(self, request_id, indices):
        self._request_id(request_id)
        if not isinstance(indices,list) or not indices or len(indices)>100 or any(type(i) is not int for i in indices):
            raise VaultError('INVALID_IMPORT_RETRY_SELECTION')
        if self.get(request_id) is None:raise VaultError('IMPORT_BATCH_UNAVAILABLE')
        with self.vault.transaction():
            key='import_batch:'+request_id
            batch=self.vault.get_setting(key)
            rows=[r for r in batch['items'] if r['index'] in indices]
            live={r['index']:r for r in self._project(batch)['items']}
            for row in rows:
                # An invalid material row must not deploy a pre-existing local
                # identity merely because its conflict result included an ID.
                if (row['intake_state'] in {'failed','conflict'} or not row.get('account_id')
                        or live[row['index']]['state'] not in {'failed','cancelled'}):
                    continue
                result=self.service.deployments.queue([row['account_id']],batch['model_id'],batch['profile'],
                                                      batch.get('staging_verified',False),batch.get('auto_monitor',True))['items'][0]
                row.pop('job_id',None)
                row.update(result)
            self.vault.set_setting(key,batch)
            return self._project(batch)

    def resume_budget_blocked(self):
        """Once per latest batch, resume only the former local attempt-cap failure."""
        if self.vault.key is None or self.service.tasks.config()['paused']:return
        with self.vault.transaction():
            batch=self.get()
            if not batch:return
            key='budget_unblocked:'+batch['id']
            if self.vault.get_setting(key,False):return
            indices=[r['index'] for r in batch['items'] if r['state']=='failed'
                     and r.get('code')=='RECOVERY_CONTINUE_BUDGET'
                     and r.get('account_id') and r.get('intake_state') not in {'failed','conflict'}]
            if not indices:return
            for start in range(0,len(indices),100):self.retry(batch['id'],indices[start:start+100])
            self.vault.set_setting(key,True)

    def resume_stale_precheck(self):
        """Finish the latest explicit submission that mistakenly retained an older template.

        Never apply global setting edits to an in-flight or already-created account.
        Once replaced, profile equality makes this migration a no-op (no retry loop).
        """
        if self.vault.key is None or self.service.tasks.config()['paused']:return
        with self.vault.transaction():
            batch=self.get()
            if not batch:return
            ids=[]
            for row in batch['items']:
                if (row['state']!='failed' or row.get('intake_state') in {'failed','conflict'}
                        or not row.get('account_id') or row.get('code') not in {
                            'SELECTED_GROUP_UNAVAILABLE','SELECTED_PROXY_UNAVAILABLE'}):continue
                a=self.vault.account(row['account_id'])
                dep=a.get('deployment') or {}
                if (replaceable_precheck(a) and dep['profile']!=batch['profile']
                        and dep.get('job_id')==row.get('job_id') and not self.vault.pending(a['id'])
                        and not self.service.tasks.busy(a)):
                    ids.append(row['index'])
            for start in range(0,len(ids),100):self.retry(batch['id'],ids[start:start+100])
