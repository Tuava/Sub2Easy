const {test}=require('node:test');
const assert=require('node:assert/strict');
const {JSDOM}=require('jsdom');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.resolve(__dirname,'../..');
const read=name=>fs.readFileSync(path.join(root,'sub2easy/static',name),'utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));

function sample(){return {
  accounts:[{id:'local-1',label:'demo@example.invalid',status:'authorized',has_login_material:true,
    updated:1,imported_at:1,revision:1,monitor:{},validated:true,has_result:true,
    binding:{instance:'https://example.invalid/api/v1/admin',cloud_id:42},
    profile:{profile_id:'default',revision:1,fingerprint_mode:'off'}}],jobs:[],cloud:null,monitor:{config:{enabled:false,
    interval_seconds:60,grace_seconds:120,model_id:'test-model',max_per_hour:6,resume_after_success:true},runtime:{}},
  profile:{profile_id:'default',revision:1,instance_id:'https://example.invalid/api/v1/admin',staging_group_id:1,
    target_group_ids:[2],concurrency:1,priority:50,rate_multiplier:1,fingerprint_mode:'off',auto_pause_on_expired:true},
  settings:{sub2api_url:'https://example.invalid',instance:'https://example.invalid/api/v1/admin',
    cloud_revision:'revision-1',has_admin_key:true,has_cookie:true,oauth_client_id:''},
};}
async function setup(){
  const dom=new JSDOM(read('index.html'),{url:'http://127.0.0.1:8765/#token=TEST',runScripts:'outside-only'});
  const w=dom.window,calls=[];let payload=sample();
  let batch=null;
  w.eval=code=>vm.runInContext(code,dom.getInternalVMContext());
  w.structuredClone=structuredClone;w.scrollTo=()=>{};w.setInterval=()=>0;w.setTimeout=()=>0;w.clearTimeout=()=>{};
  w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};
  w.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');this.dispatchEvent(new w.Event('close'));};
  w.fetch=async(url,opts={})=>{
    calls.push({url,opts});let data;
    if(url==='/api/status')data={initialized:true,unlocked:false,version:'0.4.0',features:['server_deploy']};
    else if(url==='/api/state')data=payload;
    else if(url==='/api/usage')data={accounts:[],pending:false};
    else if(url==='/api/cloud/options')data={groups:[{id:1,name:'隔离'},{id:2,name:'生产'}],proxies:[],instance_id:payload.settings.instance,connection_revision:payload.settings.cloud_revision,fetched_at:1};
    else if(url==='/api/deployments')data={items:[{account_id:'local-1',state:'queued',job_id:'job-1'}]};
    else if(url==='/api/import')data={added:1,supplemented_lines:[],duplicate_lines:[],conflict_lines:[]};
    else if(url==='/api/import/deploy'){
      const body=JSON.parse(opts.body);batch={id:body.request_id,instance:payload.settings.instance,
        connection_revision:payload.settings.cloud_revision,profile:body.profile,model_id:body.model_id,
        items:[{index:1,intake_state:'added',account_id:'local-1',label:'demo@example.invalid',
                state:'queued',step:'precheck',job_id:'job-1'}],pending:true};data=batch;
    }
    else if(url.startsWith('/api/import/batch'))data=batch;
    else data={ok:true};
    return {ok:true,status:200,json:async()=>structuredClone(data)};
  };
  w.eval(read('app.js'));w.eval(read('binding-ui.js'));await tick();
  w.eval(`unlocked=true;state=${JSON.stringify(payload)};fillForms();$('lock-screen').hidden=true;renderAccounts();`);
  return {dom,w,calls,setPayload:p=>payload=p,close:()=>dom.window.close()};
}
function cell(w){return w.document.querySelector('#account-rows tr').children[4];}

test('both lists render 7 columns and usage inline, not a separate page',async()=>{
  const t=await setup();try{
    assert.equal(t.w.document.querySelector('#account-rows tr').children.length,7);
    assert.match(cell(t.w).textContent,/未采集/);
    t.w.eval(`cloudResult={items:[{id:42,name:'x',platform:'openai',type:'oauth',status:'active',schedulable:true,group_ids:[],groups:[],proxy_id:null}],facets:{},page:1,pages:1,total:1};renderCloud();`);
    assert.equal(t.w.document.querySelector('#cloud-list-rows tr').children.length,7);
  }finally{t.close();}
});
test('unknown usage is not zero; explicit zero and today costs are displayed',async()=>{
  const t=await setup();try{
    const row={account_id:42,collected_at:new Date().toISOString(),windows:[{key:'five_hour',used_percent:0,
      updated_at:new Date().toISOString(),reset_at:new Date(Date.now()+3600000).toISOString(),freshness:'recent'},
      {key:'seven_day',used_percent:null,freshness:'unknown'}],today:{requests:0,tokens:1000,cost:1.25},errors:[]};
    t.w.eval(`usageById.set(42,${JSON.stringify(row)});renderAccounts();`);
    const txt=cell(t.w).textContent;assert.match(txt,/5 小时 已用 0%/);assert.match(txt,/7 天 已用 未知/);
    assert.match(txt,/今日 0 次/);assert.match(txt,/1,000 Token/);assert.match(txt,/1.25 USD/);
    assert.match(txt,/标准费用 未知/);assert.match(txt,/采集/);
  }finally{t.close();}
});
test('expired windows show unknown rather than stale percentage as live',async()=>{
  const t=await setup();try{
    t.w.eval(`usageById.set(42,{account_id:42,windows:[{key:'five_hour',used_percent:90,reset_at:'2020-01-01T00:00:00Z',freshness:'recent'}],today:{},errors:[]});renderAccounts();`);
    assert.match(cell(t.w).textContent,/已用 未知/);assert.match(cell(t.w).textContent,/旧窗口已结束/);
    assert.equal(cell(t.w).querySelectorAll('meter').length,0);
  }finally{t.close();}
});
test('unbound account does not invent usage and other-site bindings do not reuse ID cache',async()=>{
  const t=await setup();try{
    t.w.eval(`usageById.set(42,{windows:[],today:{requests:98765}});state.accounts[0].binding.instance='https://other.invalid';renderAccounts();`);
    assert.match(cell(t.w).textContent,/其他站点/);assert.doesNotMatch(cell(t.w).textContent,/98765/);
    t.w.eval(`state.accounts[0].binding=null;renderAccounts();`);assert.match(cell(t.w).textContent,/未绑定/);
  }finally{t.close();}
});
test('opening deployment fixes the confirmed template; later refresh cannot change payload',async()=>{
  const t=await setup();try{
    t.w.eval(`openDeployment(['local-1']);state.profile.target_group_ids=[999];$('deployment-confirm').checked=true;`);
    t.w.document.querySelector('#deployment-form').dispatchEvent(new t.w.Event('submit',{cancelable:true}));await tick();await tick();
    const req=t.calls.find(c=>c.url==='/api/deployments');assert.ok(req);
    assert.deepEqual(JSON.parse(req.opts.body).profile.target_group_ids,[2]);
  }finally{t.close();}
});
test('all rejected deployments keep errors visible and do not claim queued success',async()=>{
  const t=await setup();try{
    const fetch=t.w.fetch;t.w.fetch=async(url,opts)=>url==='/api/deployments'?{ok:true,json:async()=>({items:[{account_id:'local-1',state:'failed',code:'DEPLOY_CONFIG_CHANGED'}]})}:fetch(url,opts);
    t.w.eval(`openDeployment(['local-1']);$('deployment-confirm').checked=true;`);
    t.w.document.querySelector('#deployment-form').dispatchEvent(new t.w.Event('submit',{cancelable:true}));await tick();await tick();
    assert.ok(t.w.document.querySelector('#deployment-dialog').hasAttribute('open'));
    assert.match(t.w.document.querySelector('#deployment-error').textContent,/配置.*变化/);
  }finally{t.close();}
});
test('filter change clears hidden selections; row focus is preserved by background refresh',async()=>{
  const t=await setup();try{
    const checkbox=t.w.document.querySelector('#account-rows input[type=checkbox]');checkbox.focus();checkbox.checked=true;
    checkbox.dispatchEvent(new t.w.Event('change'));
    const p=sample();p.accounts[0].updated=2;t.setPayload(p);await t.w.eval('refresh()');
    assert.equal(t.w.document.activeElement,checkbox);
    t.w.document.querySelector('#search').value='missing';t.w.document.querySelector('#search').dispatchEvent(new t.w.Event('input'));
    assert.match(t.w.document.querySelector('#selected-count').textContent,/已选 0/);
  }finally{t.close();}
});
test('switching import format keeps typed material, and local intake needs no cloud options',async()=>{
  const t=await setup();try{
    t.w.eval(`$('materials').value='saved-input';$('import-format').value='sub2';resetImportFormat();`);
    assert.equal(t.w.document.querySelector('#materials').value,'saved-input');
    t.w.eval(`$('import-format').value='login';state.settings.sub2api_url='';state.settings.has_admin_key=false;choices=null;`);
    t.w.document.querySelector('#import-btn').click();await tick();await tick();
    assert.ok(t.calls.some(c=>c.url==='/api/import'));
  }finally{t.close();}
});
test('lock clears usage, rows and secrets; pending stale state cannot resurrect them',async()=>{
  const t=await setup();try{
    let resolve;const fetch=t.w.fetch;t.w.fetch=(url,opts)=>url==='/api/state'?new Promise(r=>resolve=r):fetch(url,opts);
    const pending=t.w.eval('refresh()');t.w.eval(`usageById.set(42,{});$('materials').value='SECRET';clearLocalSession();`);
    resolve({ok:true,json:async()=>sample()});await pending;
    assert.equal(t.w.document.querySelector('#account-rows').children.length,0);
    assert.equal(t.w.document.querySelector('#materials').value,'');assert.equal(t.w.eval('usageById.size'),0);
    assert.equal(t.w.document.querySelector('#lock-screen').hidden,false);
  }finally{t.close();}
});
test('live task report reflects failure instead of permanently saying queued',async()=>{
  const t=await setup();try{
    t.w.eval(`state.jobs=[{id:'j',account_id:'local-1',state:'failed',kind:'server_deploy',stage:'verify',code:'PROBE_FAILED'}];state.deployment_report={items:[{account_id:'local-1',state:'queued',job_id:'j'}]};renderJobs();`);
    assert.match(t.w.document.querySelector('#deploy-report').textContent,/模型测试失败/);
    assert.doesNotMatch(t.w.document.querySelector('#deploy-report').textContent,/已加入/);
  }finally{t.close();}
});

test('direct import saves and deploys on same page without selecting account rows',async()=>{
  const t=await setup();try{
    t.w.eval(`goto('import');$('materials').value='demo@example.invalid----PASSWORD----JBSWY3DPEHPK3PXP';$('import-deploy-model').value='test-model';$('import-deploy-confirm').checked=true;$('import-deploy-isolation').checked=true;`);
    await tick();t.w.document.querySelector('#import-deploy-btn').click();await tick();await tick();
    const requests=t.calls.filter(r=>r.url==='/api/import/deploy');assert.equal(requests.length,1);
    assert.deepEqual(JSON.parse(requests[0].opts.body).profile.target_group_ids,[2]);
    assert.equal(t.w.eval('page'),'import');assert.equal(t.w.eval('selected.size'),0);
    assert.equal(t.w.document.querySelector('#import-batch-panel').hidden,false);
    assert.match(t.w.document.querySelector('#import-batch-rows').textContent,/等待上传/);
    assert.equal(t.calls.filter(r=>r.url==='/api/import'||r.url==='/api/deployments').length,0);
  }finally{t.close();}
});
test('same-page template editor moves the existing form rather than duplicating IDs',async()=>{
  const t=await setup();try{
    t.w.eval(`goto('import')`);await tick();
    assert.equal(t.w.document.querySelector('#profile-form').parentElement.id,'import-template-slot');
    assert.equal(t.w.document.querySelectorAll('#profile-form').length,1);
    t.w.eval(`goto('settings')`);assert.equal(t.w.document.querySelector('#profile-form').parentElement.id,'settings-template-slot');
  }finally{t.close();}
});
test('failed direct import keeps input and progress in place',async()=>{
  const t=await setup();try{
    const original=t.w.fetch;t.w.fetch=async(url,opts)=>url==='/api/import/deploy'?{ok:true,json:async()=>({id:'test-batch',instance:'https://example.invalid',profile:sample().profile,items:[{index:1,state:'failed',intake_state:'conflict',code:'CONFLICTING_LOGIN_MATERIAL'}]})}:original(url,opts);
    t.w.eval(`goto('import');$('materials').value='retained-conflicting-material';$('import-deploy-model').value='test-model';$('import-deploy-confirm').checked=true;`);await tick();
    t.w.document.querySelector('#import-deploy-btn').click();await tick();await tick();
    assert.equal(t.w.document.querySelector('#materials').value,'retained-conflicting-material');
    assert.equal(t.w.eval('page'),'import');assert.match(t.w.document.querySelector('#import-batch-rows').textContent,/冲突/);
  }finally{t.close();}
});
test('direct import identical retry reuses request ID after network response loss',async()=>{
  const t=await setup();try{
    const original=t.w.fetch;const submissions=[];
    t.w.fetch=async(url,opts)=>{if(url==='/api/import/deploy'){submissions.push(JSON.parse(opts.body));throw new Error('lost response');}return original(url,opts);};
    t.w.eval(`goto('import');$('materials').value='material';$('import-deploy-model').value='test-model';$('import-deploy-confirm').checked=true;`);await tick();
    t.w.document.querySelector('#import-deploy-btn').click();await tick();await tick();
    t.w.document.querySelector('#import-deploy-btn').click();await tick();await tick();
    assert.equal(submissions.length,2);assert.equal(submissions[0].request_id,submissions[1].request_id);
    assert.equal(t.w.document.querySelector('#materials').value,'material');
  }finally{t.close();}
});

test('local intake keeps full-email result and uploads persisted batch without hunting accounts',async()=>{
 const t=await setup();try{
  const original=t.w.fetch;let batch={id:'local-batch-123456',instance:null,created_at:1,profile:sample().profile,
   counts:{local_saved:1},items:[{index:1,label:'found@example.invalid',account_id:'saved-id',state:'local_saved',intake_state:'added'}]};
  t.w.fetch=async(url,opts)=>{
   if(url==='/api/import')return {ok:true,json:async()=>({added:1,duplicate_lines:[],conflict_lines:[],batch:structuredClone(batch)})};
   if(url==='/api/import/history')return {ok:true,json:async()=>[structuredClone(batch)]};
   if(url.startsWith('/api/import/batch')){
    if(url.endsWith('/deploy')){t.calls.push({url,opts});batch.items[0].state='queued';batch.items[0].job_id='job-saved';}
    return {ok:true,json:async()=>structuredClone(batch)};
   }
   return original(url,opts);
  };
  t.w.eval(`goto('import');$('materials').value='input-material';`);await tick();
  t.w.document.querySelector('#import-btn').click();await tick();await tick();
  assert.equal(t.w.eval('page'),'import');assert.equal(t.w.document.querySelector('#materials').value,'');
  assert.match(t.w.document.querySelector('#import-batch-rows').textContent,/found@example.invalid/);
  assert.equal(t.w.document.querySelector('#import-batch-deploy').hidden,false);
  t.w.eval(`$('import-deploy-model').value='model';$('import-deploy-confirm').checked=true;$('import-deploy-isolation').checked=true;`);
  t.w.document.querySelector('#import-batch-deploy').click();await tick();await tick();
  const call=t.calls.find(c=>c.url==='/api/import/batch/local-batch-123456/deploy');assert.ok(call);
  const payload=JSON.parse(call.opts.body);assert.equal(payload.text,undefined);assert.equal(payload.account_ids,undefined);
  assert.equal(payload.auto_monitor,true);assert.equal(t.w.eval('page'),'import');
  assert.equal(t.w.eval('selected.size'),0);
 }finally{t.close();}
});
test('monitor updates never reorder default account list and full email is searchable',async()=>{
 const t=await setup();try{
  t.w.eval(`state.accounts=[{...state.accounts[0],id:'older',label:'older-person@example.invalid',imported_at:1,updated:999}, {...state.accounts[0],id:'newer',label:'newer-person@example.invalid',imported_at:2,updated:1}];renderAccounts();`);
  const labels=()=>[...t.w.document.querySelectorAll('#account-rows .account-identity>span')].map(n=>n.textContent);
  assert.deepEqual(labels(),['newer-person@example.invalid','older-person@example.invalid']);
  t.w.eval(`state.accounts[0].updated=10000;state.accounts.reverse();renderAccounts();`);
  assert.deepEqual(labels(),['newer-person@example.invalid','older-person@example.invalid']);
  t.w.document.querySelector('#search').value='older-person@example.invalid';
  t.w.document.querySelector('#search').dispatchEvent(new t.w.Event('input'));
  assert.deepEqual(labels(),['older-person@example.invalid']);
 }finally{t.close();}
});
test('completed rows show global monitor stop rather than pretending to watch',async()=>{
 const t=await setup();try{
  t.w.eval(`state.accounts[0].monitor={enabled:true,state:'watching'};state.monitor.config.enabled=false;renderAccounts();`);
  assert.match(t.w.document.querySelector('#account-rows').textContent,/全局监控已停止/);
  t.w.eval(`state.monitor.config.enabled=true;state.accounts[0].has_login_material=false;renderAccounts();`);
  assert.match(t.w.document.querySelector('#account-rows').textContent,/缺少重登材料/);
 }finally{t.close();}
});
test('historical batch selection remains pinned during polling',async()=>{
 const t=await setup();try{
  const original=t.w.fetch;
  const make=id=>({id,created_at:1,instance:null,profile:sample().profile,counts:{local_saved:1},items:[{index:1,state:'local_saved',label:id+'@example.invalid'}]});
  t.w.fetch=async(url,opts)=>{
   if(url==='/api/import/history')return {ok:true,json:async()=>[make('latest-batch'),make('older-batch')]};
   if(url.startsWith('/api/import/batch'))return {ok:true,json:async()=>make(url.includes('older-batch')?'older-batch':'latest-batch')};
   return original(url,opts);
  };
  t.w.eval(`goto('import')`);await tick();
  t.w.document.querySelector('#import-history').value='older-batch';
  t.w.document.querySelector('#import-history').dispatchEvent(new t.w.Event('change'));await tick();
  await t.w.eval('loadImportBatch()');
  assert.match(t.w.document.querySelector('#import-batch-rows').textContent,/older-batch@example.invalid/);
  assert.equal(t.w.document.querySelector('#import-history').value,'older-batch');
 }finally{t.close();}
});
test('only-unuploaded shortcut clears old filters and does not select hidden accounts',async()=>{
 const t=await setup();try{
  t.w.eval(`state.accounts.push({...state.accounts[0],id:'local-only',label:'local@example.invalid',binding:null});$('search').value='missing';selected.add('local-1');`);
  t.w.document.querySelector('#local-unbound').click();
  assert.equal(t.w.eval('selected.size'),0);
  assert.match(t.w.document.querySelector('#account-rows').textContent,/local@example.invalid/);
  assert.doesNotMatch(t.w.document.querySelector('#account-rows').textContent,/demo@example.invalid/);
 }finally{t.close();}
});

test('task pool renders live concurrency without overwriting unsaved selection',async()=>{
 const t=await setup();try{
  t.w.eval(`state.task_pool={config:{max_workers:3,max_authorizations:2,paused:false},runtime:{active:2,authorizing:1,queued:7,last_error:''}};renderTaskPool(true);`);
  assert.match(t.w.document.querySelector('#task-pool-runtime').textContent,/正在处理 2 \/ 3/);
  assert.match(t.w.document.querySelector('#task-pool-tag').textContent,/并行 × 3/);
  t.w.document.querySelector('#task-max-workers').value='5';
  t.w.eval(`state.task_pool.runtime.active=3;renderJobs();`);
  assert.equal(t.w.document.querySelector('#task-max-workers').value,'5');
  assert.match(t.w.document.querySelector('#task-pool-runtime').textContent,/正在处理 3 \/ 3/);
 }finally{t.close();}
});
test('saving task pool limits submits both caps and pause state without a restart',async()=>{
 const t=await setup();try{
  const original=t.w.fetch;let sent;
  t.w.fetch=async(url,opts)=>{
   if(url==='/api/tasks/config'){sent=JSON.parse(opts.body);return {ok:true,json:async()=>({config:sent,runtime:{active:1,authorizing:1,queued:5,last_error:''}})};}
   return original(url,opts);
  };
  t.w.eval(`$('task-max-workers').value='4';$('task-max-auth').value='1';$('task-pool-paused').checked=true;`);
  t.w.document.querySelector('#task-pool-save').click();await tick();
  assert.deepEqual(sent,{max_workers:4,max_authorizations:1,paused:true});
  assert.match(t.w.document.querySelector('#task-pool-tag').textContent,/暂停/);
 }finally{t.close();}
});

test('retired account leaves repair counts and has no redeploy or monitoring action',async()=>{
 const t=await setup();try{
  t.w.eval(`state.retirement={config:{enabled:true,group_id:90},runtime:{counts:{complete:1}}};state.accounts[0].status='retired';state.accounts[0].result_code='EXPECTED_WORKSPACE_NOT_RETURNED';state.accounts[0].retirement={state:'complete',group_id:90};state.accounts[0].monitor={enabled:false,blocked:false,auth_401:false,state:'retired',last_code:'TEAM_LOST_RETIRED'};renderAccounts();renderMonitor();`);
  const row=t.w.document.querySelector('#account-rows');
  assert.match(row.textContent,/已移测试组/);assert.match(row.textContent,/#90/);
  assert.doesNotMatch(row.textContent,/开启该号监控|团队授权未恢复|续跑验号并启用/);
  assert.equal([...row.querySelectorAll('button')].find(b=>b.textContent==='已移测试组').disabled,true);
  assert.equal(t.w.document.querySelector('#monitor-rows').children.length,0);
  assert.match(t.w.document.querySelector('#retirement-runtime').textContent,/已清理 1 个/);
 }finally{t.close();}
});
test('retirement job success is labeled cleanup not recovered or live',async()=>{
 const t=await setup();try{
  t.w.eval(`state.jobs=[{id:'retire-job',account_id:'local-1',kind:'retire',state:'succeeded',stage:'complete',code:'TEAM_LOST_RETIRED'}];renderJobs();`);
  const row=t.w.document.querySelector('#job-rows');
  assert.match(row.textContent,/已移测试组/);assert.match(row.textContent,/团队失效清理/);
  assert.doesNotMatch(row.textContent,/修复完成|已导入并上线/);
 }finally{t.close();}
});
test('test-group dropdown preserves unavailable selection rather than choosing another group',async()=>{
 const t=await setup();try{
  t.w.eval(`state.retirement={config:{enabled:true,group_id:99},runtime:{}};choices={instance_id:state.settings.instance,groups:[{id:90,name:'测试组'}]};renderRetirement(true);`);
  const group=t.w.document.querySelector('#retirement-group');
  assert.equal(group.value,'99');assert.match(group.selectedOptions[0].textContent,/不可用/);
  assert.equal(group.selectedOptions[0].disabled,true);
 }finally{t.close();}
});

test('new installation keeps retirement disabled and requires transfer confirmation',async()=>{
 const t=await setup();try{
  assert.equal(t.w.document.querySelector('#retirement-enabled').checked,false);
  t.w.eval(`state.retirement={config:{enabled:false,group_id:null,instance:state.settings.instance},runtime:{}};choices={instance_id:state.settings.instance,groups:[{id:90,name:'测试组'}]};renderRetirement(true);$('retirement-enabled').checked=true;$('retirement-group').value='90';`);
  let sent;const original=t.w.fetch;
  t.w.fetch=async(url,opts)=>{if(url==='/api/retirement/config'){sent=JSON.parse(opts.body);return {ok:true,json:async()=>({config:sent,runtime:{}})};}return original(url,opts);};
  t.w.document.querySelector('#retirement-save').click();await tick();
  assert.equal(sent,undefined);assert.equal(t.w.document.querySelector('#action-dialog').open,true);
  t.w.eval(`$('dialog-check').checked=true;$('dialog-check').dispatchEvent(new Event('change'));$('action-dialog').returnValue='confirm';$('action-dialog').close();`);
  await tick();assert.equal(sent.confirm_transfer,true);assert.equal(sent.connection_revision,'revision-1');
  assert.equal(sent.group_id,90);
 }finally{t.close();}
});
