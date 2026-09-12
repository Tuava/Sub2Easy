'use strict';
let bindingReport=null,bindingRow=null,bindingBusy=false,cloudResult=null,cloudPage=1,cloudRequest=0,reportRequest=0;
const resultNames={bound:'绑定成功',already_bound:'已有绑定',ready:'唯一匹配',ambiguous:'多个候选',not_found:'未找到',conflict:'有冲突',failed:'失败'};
const reasonNames={workspace:'workspace 一致',email:'邮箱一致',name:'名称一致',near_time:'创建时间接近'};
function detailedDate(ts){return ts?new Date(ts*1000).toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}):'未知';}
function optionSet(id,items,prefix){const s=$(id),old=s.value;s.replaceChildren(...prefix.map(([value,label])=>new Option(label,value)));for(const [value,label] of items)s.add(new Option(label,value));if([...s.options].some(o=>o.value===old))s.value=old;}
function localFilterOptions(){
  const groups=new Map(),proxies=new Map();for(const a of state.accounts){const c=a.cloud_metadata;if(!c)continue;for(const id of c.group_ids)groups.set(String(id),c.groups.find(g=>g.id===id)?.name||`#${id}`);if(c.proxy_id)proxies.set(String(c.proxy_id),c.proxy_name||`#${c.proxy_id}`);}
  optionSet('local-group',[...groups],[['','全部分组']]);optionSet('local-proxy',[...proxies],[['','全部代理'],['direct','直连']]);optionSet('local-profile',[...new Set(state.accounts.map(a=>a.profile.profile_id))].map(v=>[v,v]),[['','全部模板']]);
}
function clearBindingUI(){bindingReport=null;bindingRow=null;cloudResult=null;cloudRequest++;reportRequest++;$('binding-dialog').close();$('binding-report-rows').replaceChildren();$('candidate-list').replaceChildren();$('cloud-list-rows').replaceChildren();$('binding-report-summary').textContent='站点已切换或凭据库已锁定，请重新匹配。';$('cloud-list-status').textContent='请刷新当前站点完整列表。';}
function pendingRows(){return (bindingReport?.items||[]).filter(r=>!['bound','already_bound'].includes(r.status));}
function renderBindingReport(){
  const rows=bindingReport?.items||[],filter=$('binding-result-filter').value;
  $('binding-report-rows').replaceChildren();$('binding-empty').hidden=false;
  if(bindingReport)$('binding-report-summary').textContent=`累计 ${rows.length} 项 · 成功 ${rows.filter(r=>r.status==='bound').length} · 待处理 ${pendingRows().length} · ${date(bindingReport.updated_at)}`;
  for(const r of rows){if(filter==='pending'&&['bound','already_bound'].includes(r.status)||!['all','pending'].includes(filter)&&r.status!==filter)continue;
    $('binding-empty').hidden=true;const tr=document.createElement('tr'),first=document.createElement('td');first.append(text('span',r.local?.label||state?.accounts.find(a=>a.id===r.account_id)?.label||r.account_id),text('small',`本地 ${r.account_id.slice(0,8)} · 导入 ${date(r.local?.imported_at)}`));tr.append(first);
    const status=document.createElement('td');status.append(text('span',resultNames[r.status]||r.status,'status-tag '+(['bound','already_bound'].includes(r.status)?'success':'warning')));tr.append(status,text('td',String(r.candidates.length)),text('td',errorText(r.code)));
    const controls=document.createElement('td');if(!['bound','already_bound'].includes(r.status)){controls.append(button('查看候选',()=>openCandidates(r.account_id)),button('重新匹配',()=>scanBindings([r.account_id],false)));}else controls.append(text('span','#'+r.cloud_id));tr.append(controls);$('binding-report-rows').append(tr);
  }
  $('bind-retry').disabled=bindingBusy||!pendingRows().length;$('bind-all').disabled=bindingBusy;
}
async function loadBindingReport(){if(!unlocked)return;const request=++reportRequest;const report=await api('bindings/report');if(request!==reportRequest||!unlocked)return;bindingReport=report;renderBindingReport();}
async function scanBindings(ids,openSingle=false){
  if(bindingBusy)throw new Error('已有批量匹配正在执行。');if(!ids.length){toast('没有需要绑定的账号。');return;}
  const yes=await confirmDialog('自动匹配并绑定',`将完整读取当前站点账号，为所选 ${ids.length} 个本地账号匹配候选。\n唯一且身份一致的账号自动建立本地绑定；重名/多个workspace/占用冲突统一保留待处理。\n不会向云端写凭据，不自动开启监控。`);if(!yes)return;
  bindingBusy=true;$('binding-result-filter').value='all';goto('bindings');
  try{
    // Each chunk receives a complete snapshot; the backend retains other pending report rows.
    for(let offset=0;offset<ids.length;offset+=100){reportRequest++;bindingReport=await api('bindings/scan',{account_ids:ids.slice(offset,offset+100),auto_bind:true});renderBindingReport();}
    await refresh();renderBindingReport();
    if(openSingle&&ids.length===1){const row=bindingReport.items.find(r=>r.account_id===ids[0]);if(row&&!['bound','already_bound'].includes(row.status))openCandidates(ids[0]);}
    toast('批量匹配完成。成功与待处理项已统一列出。');
  }finally{bindingBusy=false;renderBindingReport();}
}
function openCandidates(id){bindingRow=bindingReport?.items.find(r=>r.account_id===id);if(!bindingRow)return;
  $('binding-dialog-title').textContent='选择云端候选';$('binding-dialog-description').textContent=`本地：${bindingRow.local?.label||id} · 导入：${detailedDate(bindingRow.local?.imported_at)} · 授权/导入时间参考：${detailedDate(bindingRow.local?.reference_at)}。${errorText(bindingRow.code)}`;
  $('candidate-query').value='';$('candidate-eligible').checked=false;$('candidate-sort').value='match';renderCandidates();$('binding-dialog').showModal();
}
function renderCandidates(){
  const query=$('candidate-query').value.toLowerCase();let rows=[...(bindingRow?.candidates||[])].filter(r=>(!$('candidate-eligible').checked||r.eligible)&&[r.id,r.name,r.email,r.workspace_id].join(' ').toLowerCase().includes(query));
  const sort=$('candidate-sort').value;if(sort==='time')rows.sort((a,b)=>(a.time_delta_seconds??Infinity)-(b.time_delta_seconds??Infinity));if(sort==='newest')rows.sort((a,b)=>(b.created_at||0)-(a.created_at||0));
  $('candidate-list').replaceChildren();if(!rows.length)$('candidate-list').append(text('p','没有符合条件的候选。请检查邮箱/名称或刷新全站账号；时间资料缺失时不自动推断。','field-help'));
  for(const c of rows){const card=document.createElement('article');card.className='candidate-card';const heading=document.createElement('div');heading.className='candidate-heading';heading.append(text('strong',`#${c.id} · ${c.name||'未命名'}`),text('span',c.eligible?'身份可校验':'不可绑定','status-tag '+(c.eligible?'success':'warning')));card.append(heading);
    const detail=document.createElement('dl');detail.className='candidate-details';for(const [label,value] of [['邮箱',c.email||'缺失'],['Workspace',c.workspace_id||'缺失'],['平台 / 类型',`${c.platform} / ${c.type}`],['状态 / 调度',`${c.status} / ${c.schedulable===true?'可调度':c.schedulable===false?'已暂停':'未知'}`],['分组',c.group_ids.map(id=>`${c.groups.find(g=>g.id===id)?.name||'分组'} #${id}`).join('、')||'无分组'],['代理',c.proxy_id?`${c.proxy_name||'代理'} #${c.proxy_id}`:'直连'],['并发 / 优先级',`${c.concurrency??'—'} / ${c.priority??'—'}`],['指纹模式',c.fingerprint_mode||'off / 未设置'],['创建时间',date(c.created_at)],['更新时间',date(c.updated_at)],['最近使用',date(c.last_used_at)],['参考时间差',c.time_delta_seconds==null?'未知':`${Math.round(c.time_delta_seconds/60)} 分钟`]]){detail.append(text('dt',label),text('dd',String(value)));}card.append(detail);
    card.append(text('p','匹配依据：'+c.reasons.map(k=>reasonNames[k]||k).join('、'),'field-help'));if(c.conflicts.length)card.append(text('p',c.conflicts.map(errorText).join('；'),'error-text'));
    const b=text('button','选择并绑定这个账号','button '+(c.eligible?'primary':'secondary'));b.disabled=!c.eligible;b.onclick=()=>action(b,async()=>{const yes=await confirmDialog('确认绑定关系',`本地 ${bindingRow.local?.label||bindingRow.account_id}\n→ 云端 #${c.id} ${c.name}\nWorkspace：${c.workspace_id}\n\n保存前会再次读取云端核对，不会修改云端账号。`);if(!yes)return;bindingReport=await api('bindings/resolve',{account_id:bindingRow.account_id,cloud_id:c.id,fingerprint:c.fingerprint,connection_revision:bindingReport.connection_revision,confirm_selection:true});$('binding-dialog').close();await refresh();renderBindingReport();});card.append(b);$('candidate-list').append(card);
  }
}
function cloudParams(){const p=new URLSearchParams({q:$('cloud-query').value,platform:$('cloud-platform').value,status:$('cloud-status').value,group:$('cloud-group').value,proxy:$('cloud-proxy').value,binding:$('cloud-binding').value,sort:$('cloud-sort').value,page:cloudPage,page_size:25});
  if($('cloud-since').value)p.set('since',new Date($('cloud-since').value+'T00:00:00').getTime()/1000);
  if($('cloud-until').value){const end=new Date($('cloud-until').value+'T00:00:00');end.setDate(end.getDate()+1);p.set('until',end.getTime()/1000);}return p;
}
async function loadCloudAccounts(){if(!unlocked)return;const request=++cloudRequest;const result=await api('cloud/accounts?'+cloudParams());if(request!==cloudRequest||!unlocked)return;cloudResult=result;cloudPage=result.page;renderCloud();scheduleUsage();}
function renderCloud(){const r=cloudResult;if(!r)return;$('cloud-list-rows').replaceChildren();$('cloud-empty').hidden=r.items.length>0;
  optionSet('cloud-platform',(r.facets.platforms||[]).map(s=>[s,s]),[['','全部平台']]);optionSet('cloud-group',Object.entries(r.facets.groups||{}),[['','全部分组'],['ungrouped','未分组']]);optionSet('cloud-proxy',Object.entries(r.facets.proxies||{}),[['','全部代理'],['direct','直连']]);
  $('cloud-list-status').textContent=r.needs_refresh?'还没有当前站点的完整快照，请点“刷新全站账号”。':`匹配 ${r.total} 个账号 · 快照 ${date(r.fetched_at)} · 筛选只读，不触发授权或改号`;
  for(const a of r.items){const row=document.createElement('tr');const first=document.createElement('td');first.append(text('span',`#${a.id} · ${a.name||'未命名'}`));const second=document.createElement('td');second.append(text('span',a.email||'无邮箱'),text('small',a.workspace_id||'无 workspace'));const status=document.createElement('td');status.append(text('span',`${a.platform} / ${a.type}`),text('small',a.auth_401?'账号 401':`${a.status} · ${a.schedulable===true?'可调度':a.schedulable===false?'停调度':'调度未知'}`));const group=document.createElement('td');group.append(text('span',a.group_ids.map(id=>`${a.groups.find(g=>g.id===id)?.name||'分组'} #${id}`).join('、')||'无分组'),text('small',a.proxy_id?`${a.proxy_name||'代理'} #${a.proxy_id}`:'直连'));const dates=document.createElement('td');dates.append(text('span',date(a.created_at)),text('small','使用 '+date(a.last_used_at)));const binding=document.createElement('td');binding.append(text('span',a.local_id?(a.monitored?'已绑定 · 监控中':'已绑定'):'未绑定本地'));if(a.local_id)binding.append(text('small',a.local_id.slice(0,8)));row.append(first,second,status,group,usageCell(a.id,a.usage),dates,binding);$('cloud-list-rows').append(row);}
  $('cloud-page-info').textContent=`第 ${r.page} / ${r.pages} 页 · ${r.total} 条`;$('cloud-prev').disabled=r.page<=1;$('cloud-next').disabled=r.page>=r.pages;
}
$('batch-bind-selected').onclick=()=>action($('batch-bind-selected'),()=>scanBindings([...selected]));
$('bind-all').onclick=()=>action($('bind-all'),()=>scanBindings(state.accounts.filter(a=>!a.binding).map(a=>a.id)));
$('bind-retry').onclick=()=>action($('bind-retry'),()=>scanBindings(pendingRows().map(r=>r.account_id)));
$('binding-result-filter').onchange=renderBindingReport;
$('binding-close').onclick=()=>$('binding-dialog').close();
for(const id of ['candidate-query','candidate-eligible','candidate-sort'])$(id).addEventListener('input',renderCandidates);
for(const id of ['local-binding','local-monitor','local-group','local-proxy','local-profile','local-sort'])$(id).onchange=()=>{localPage=1;selected.clear();renderAccounts();};
$('local-reset').onclick=()=>{for(const id of ['local-binding','local-monitor','local-group','local-proxy','local-profile','status-filter','search'])$(id).value='';$('local-sort').value='import_desc';localPage=1;selected.clear();renderAccounts();};
$('local-prev').onclick=()=>{localPage--;renderAccounts();};$('local-next').onclick=()=>{localPage++;renderAccounts();};
$('cloud-reload').onclick=()=>action($('cloud-reload'),async()=>{await api('cloud/catalog',{});cloudPage=1;await loadCloudAccounts();await refresh();});
$('cloud-apply').onclick=()=>action($('cloud-apply'),async()=>{cloudPage=1;await loadCloudAccounts();});
$('cloud-query').onkeydown=e=>{if(e.key==='Enter')$('cloud-apply').click();};
for(const id of ['cloud-platform','cloud-status','cloud-group','cloud-proxy','cloud-binding','cloud-sort','cloud-since','cloud-until'])$(id).onchange=()=>action(null,async()=>{cloudPage=1;await loadCloudAccounts();});
$('cloud-reset').onclick=()=>action($('cloud-reset'),async()=>{for(const id of ['cloud-query','cloud-platform','cloud-status','cloud-group','cloud-proxy','cloud-binding','cloud-since','cloud-until'])$(id).value='';$('cloud-sort').value='created_desc';cloudPage=1;await loadCloudAccounts();});
$('cloud-prev').onclick=()=>action($('cloud-prev'),async()=>{cloudPage--;await loadCloudAccounts();});$('cloud-next').onclick=()=>action($('cloud-next'),async()=>{cloudPage++;await loadCloudAccounts();});

$('local-unbound').onclick=()=>{$('local-reset').click();$('local-binding').value='unbound';renderAccounts();};
$('local-last-import').onclick=()=>goto('import');
