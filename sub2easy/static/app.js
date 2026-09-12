'use strict';
const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.hash.slice(1));
let token = params.get('token') || sessionStorage.getItem('s2e-local-token') || '';
if (params.has('token')) {
  sessionStorage.setItem('s2e-local-token', token);
  history.replaceState(null, '', '/');
}
let state = null, initialized = false, unlocked = false, page = 'pool', timer = null;
let choices = null, choicesRequest = 0, choicesLoading = false;
let refreshPending=null,sessionEpoch=0,deploymentDraft=null,foregroundActions=0;
const busyButtons=new WeakSet();
const usageById=new Map();
let usageRequest=null,usageRevision=null;
let accountRenderKey='',jobRenderKey='';
let usageLoadTimer=null;
const usageAttempts=new Map();
const usageLoadingIds=new Set();
let importBatch=null,importBatchRequest=0,inlineSubmit=null,importSubmitBusy=false;
const selected = new Set();
const names = {pool:'号池总览',bindings:'绑定处理中心',cloud:'全站账号',import:'批量导入',jobs:'任务中心',monitor:'账号监控',settings:'连接与模板'};
let deploymentSelection=[];
const deployStages={precheck:'检查目标与去重',authorize:'获取或复用授权',create:'创建云端账号',pause_new:'新号停止调度',pause:'暂停原账号',pause_confirm:'确认暂停生效',apply:'更新原账号凭据',verify:'模型验号',promote:'移入生产分组',enable:'开启调度',confirm:'最终状态确认',complete:'导入上线完成'};
let localPage=1;
const localPageSize=25;
const statuses = {
  local:['仅在本地',''],authorizing:['授权中','progress'],authorized:['凭据就绪 · 未必上线','success'],
  review:['结果待核对','warning'],failed:['操作失败','error'],unknown:['结果未知','warning'],
  cloud_paused:['服务器已写入 · 暂停','warning'],write_unknown:['写入待核对','warning'],
  queued:['排队中',''],running:['执行中','progress'],succeeded:['任务成功','success'],cancelled:['已取消',''],
  active:['已上线','success'],retired:['已移测试组 · 不再修复',''],
};
const messages = {
  CONFIRM_RETIREMENT_TRANSFER:'请确认将符合条件的账号停调度并移入所选测试组。',
  TEAM_LOST_RETIRED:'团队授权未恢复，已移入测试组、停止调度并退出监控。不再尝试重登。',
  ACCOUNT_RETIRED:'该账号已进入测试组清理流程，不再授权、部署或开启自动监控。',
  RETIREMENT_GROUP_REQUIRED:'没有找到唯一的“测试组”。请从下拉框明确选择目标分组。',
  RETIREMENT_GROUP_UNAVAILABLE:'所选测试组已不可用，请刷新分组选项。',
  RETIREMENT_GROUP_READ_FAILED:'读取测试组失败，未执行移组；下轮会重新读取。',
  RETIREMENT_NOT_ELIGIBLE:'账号状态或原身份已变化，不符合本次清理条件。',
  RETIREMENT_SITE_CHANGED:'站点或清理目标已变化，已停止旧清理任务。',
  RETIREMENT_CLOUD_CHANGED:'移组前发现云端被修改，已停止覆盖。',
  RETIREMENT_FINAL_STATE_INVALID:'尚未确认移组且停调度成功，需要核对云端状态；不会盲目重发。',
  RETIREMENT_READ_FAILED:'测试组清理读取失败，尚未确认完成；保留任务供核对。',
  RETIREMENT_STOPPED:'测试组清理已停止，未继续后续步骤。',
  RETIREMENT_WRITE_UNKNOWN:'存在未确认写入，请先核对云端；不重复发送。',
  RETIREMENT_DISABLED:'测试组自动清理已关闭。',
  INVALID_RETIREMENT_CONFIG:'清理配置无效，请重新选择测试组。',
  INVALID_TASK_CONCURRENCY:'任务并发和授权并发须为 1–8 的整数。',
  TASKS_RUNNING_CONFIG_LOCKED:'仍有任务执行中。可先在任务中心暂停新任务，等执行中的任务结束后再修改连接或锁库。',
  TASK_WORKER_FAILED:'任务工作线程异常退出，结果保留为未知；其他账号继续执行。',
  TASK_DISPATCH_FAILED:'任务分发失败，请检查本地服务；没有自动重发未知请求。',
  TASK_CANCELLED_BEFORE_AUTH:'已停止，等待授权名额时尚未发送登录请求。',
  TASK_CONNECTION_CHANGED:'等待授权期间连接配置发生变化，未使用旧配置发请求。',
  USAGE_INVALID_ACCOUNT_IDS:'一次最多刷新 50 个当前站点账号的用量。',
  USAGE_COLLECTION_FAILED:'用量采集失败，保留未知状态；没有按零计算。',
  LOCAL_SESSION_REQUIRED:'本机会话失效。请使用启动器生成的地址重新打开页面。',
  VAULT_LOCKED:'凭据库已锁定，请重新解锁。',VAULT_DECRYPT_FAILED:'主密码不正确，或凭据库已损坏。',
  MASTER_PASSWORD_MIN_10:'主密码至少需要 10 个字符。',UNLOCK_RATE_LIMITED:'解锁尝试过多，请等待一分钟。',
  CONFIGURE_OR_RENEW_COOKIE:'请先在连接设置中保存有效 Cookie。连接器暂停时，更新 Cookie 后恢复。',
  INVALID_SESSION_COOKIE:'请输入 scm_session 的完整值，不要粘贴整段 curl 或其他 Cookie。',
  SUB2API_NOT_CONFIGURED:'请先配置 sub2api 地址和管理员 API Key。',
  SUB2API_CONNECTION_FAILED:'无法读取 sub2api 管理接口，请检查地址、管理员 Key 和版本。',
  SELECTED_GROUP_UNAVAILABLE:'任务实际使用的分组不在服务器当前可用 OpenAI 列表中；请核对下方分组 ID 与执行模板。',
  SELECTED_PROXY_UNAVAILABLE:'所选代理已删除、停用或到期，请刷新选项重新选择。',
  PROFILE_INSTANCE_MISMATCH:'模板属于其他站点，请加载当前站点选项并重新保存模板。',
  CLOUD_CHOICES_STALE:'站点配置已变化，请刷新分组和代理选项后再保存。',
  INCORRECT_CODE:'验证码错误或已失效。检查 TOTP 种子与授权服务时间；不会自动重试。',
  NETWORK_RESULT_UNKNOWN:'网络超时或断开，远端结果未知。请先核对，勿直接重复登录。',
  NON_JSON_RESPONSE:'接口未返回有效 JSON，可能是登录页或网关响应。',
  CONNECTOR_SESSION_EXPIRED:'NVT Cookie 失效，连接器已暂停；请更新 Cookie。',
  CONNECTOR_RATE_LIMITED:'授权接口限流，连接器已暂停。请稍后更新连接配置再继续。',
  INCOMPLETE_AUTH_CREDENTIALS:'导出字段不完整，已加密保留原结果；可导出核对，未写云端。',
  UNSUPPORTED_AUTH_ACCOUNT:'返回格式或平台尚未匹配，已保留原结果待核对。',
  AUTH_EMAIL_MISMATCH:'返回账号与当前材料不一致，已阻止云端写入。',
  AUTH_SUBJECT_OR_WORKSPACE_MISMATCH:'原账号授权未恢复：返回的用户身份或workspace与原绑定不一致。可能选到了个人空间，不能当作原团队账号上线。',
  INVALID_SUB2JSON:'成功响应结构尚未识别，已保留原结果待核对。',
  ACCESS_TOKEN_EXPIRY_UNAVAILABLE:'缺少有效的 access token 到期信息，已保留结果待核对。',
  PROVIDER_IDENTITY_NOT_VERIFIED:'授权服务未确认身份，不能应用本次凭据。',
  PROVIDER_SUMMARY_IDENTITY_MISMATCH:'响应摘要与账号邮箱不一致，已阻止写入。',
  PROVIDER_SUMMARY_WORKSPACE_MISMATCH:'响应摘要与实际 workspace 不一致，已阻止写入。',
  PROVIDER_WORKSPACE_ALIAS_MISMATCH:'返回的 workspace 标识相互冲突，已保留待核对。',
  AUTHORIZATION_READY:'授权结构及身份字段校验通过，待手动写入或导出。',
  INTERRUPTED_RESULT_UNKNOWN:'程序在请求进行中退出，远端结果未知；不会自动重发。',
  EXPECTED_ACCOUNT_PASSWORD_TOTP:'需要 账号----密码----2FA种子 三段格式。',
  INVALID_TOTP_SECRET:'第三段不是有效的 Base32 TOTP 种子。',INVALID_PASSWORD:'密码为空或包含非法控制字符。',
  INVALID_ACCOUNT:'账号格式无效。',CONFLICTING_LOGIN_MATERIAL:'相同账号出现不同登录材料，请先消除冲突。',
  FIX_IMPORT_ERRORS_FIRST:'请先修正所有错误行再保存。',INVALID_PROFILE_GROUPS:'隔离组与生产组须是不同的正整数 ID。',
  REAUTH_BUDGET_2_PER_30_MIN:'触发预算：每个账号 30 分钟内最多提交 2 次。',
  OPERATION_RUNNING:'另一个操作正在执行，请稍后重试。',JOB_RUNNING_CANNOT_LOCK:'授权请求正在执行，结束后才能锁定。',
  ACCOUNT_MUST_BE_QUIESCED:'请先在 sub2api 将原账号设为停止调度，保留 active/error 状态，再执行更新。',
  CLOUD_ACCOUNT_ON_HOLD:'账号处于 inactive 等人工停用状态，不能自动清错更新。',
  CLOUD_WRITE_RESULT_UNKNOWN:'云端写入结果需核对。已保留操作记录，禁止自动重建或重复提交。',
  PREVIOUS_WRITE_NEEDS_RECONCILIATION:'上一次写入结果尚未确认，请先在 sub2api 核对。',
  RESULT_REVIEW_REQUIRED_BEFORE_RETRY:'已有待核对或结果未知的授权。请先核对结果，不能直接重复登录。',
  CLOUD_IDENTITY_MISMATCH:'云端账号身份不匹配，不能建立绑定。',
  CLOUD_ALREADY_BOUND:'这个云端账号已有本地绑定。',CLOUD_INSTANCE_CHANGED:'当前云端地址与账号原绑定实例不同。',
  BINDING_ALREADY_EXISTS_OR_WRITE_PENDING:'此账号已有绑定或未确认写入，不能重新绑定。',
  VALIDATED_AUTHORIZATION_REQUIRED:'需要一份新鲜、校验通过的授权结果。',
  BIND_ACCOUNT_BEFORE_MONITORING:'请先绑定当前站点的原 sub2api 账号 ID，再开启账号监控。',
  MONITOR_MODEL_REQUIRED:'请填写用于恢复后验号的可用模型 ID。',
  INVALID_MONITOR_CONFIG:'监控参数无效，请检查间隔、等待窗口和预算。',
  MONITOR_CONNECTION_CHANGED:'站点连接已经变化，请重新确认并保存监控设置。',
  MONITOR_JOB_STALE:'监控已停止或任务配置已经变化，未继续自动修复。',
  MONITOR_FETCH_FAILED:'监控无法完整读取账号状态，本轮未触发新的重授权。',
  ADMIN_AUTH_FAILED:'sub2api 管理员认证失效，请更新管理员 Key；不是账号池全部 401。',
  CLOUD_CHANGED_DURING_RECOVERY:'账号配置或凭据版本在修复期间发生变化，已停止覆盖并保留待办。',
  CLOUD_REVISION_REQUIRED:'云端缺少更新时间字段，无法校验并发修改，已停止自动修复。',
  MONITOR_RECOVERY_NEEDS_REVIEW:'该账号有未完成的自动恢复/隔离记录，请先核对云端状态。',
  DUPLICATE_CREDENTIAL_OWNER:'同一登录身份已在其他托管账号中使用，不能并发自动轮换凭据。',
  AUTO_REAUTH_RECOVERED:'自动重授权、原账号更新与模型验证已完成。',
  AUTO_REAUTH_RECOVERED_PAUSED:'授权已修复且测试通过；账号原本停止调度，保持暂停，不自动上线。',
  NO_SAFE_RECOVERY_CHECKPOINT:'没有可验证的续跑记录，或记录已经过期，不能盲目补写/启用。',
  RECOVERY_BASELINE_REVIEW_REQUIRED:'旧恢复记录缺少历史配置基线。请核对当前账号配置后，手动确认“续跑验号并启用”；不会自动迁移上线。',
  AUTO_RESUME_DISABLED:'“验证成功后自动恢复调度”已关闭，不会由旧任务迁移逻辑绕过。',
  RECOVERY_CONTINUE_BUDGET:'旧版的导入次数限制；新版已取消，待当前批次自动重新排队。',
  CLOUD_SCHEDULING_STATE_UNKNOWN:'调度状态缺失或无效，不能按人工暂停或正常账号处理。',
  PROBE_FAILED:'新凭据的模型测试失败，保持停止调度，等待处理。',
  PROBE_AUTH_401:'验号明确返回上游401；重新读回确认后自动安排一次受限重授权，不需点击续跑。',
  REAUTH_STILL_UNAUTHORIZED:'已再次重授权但验号仍是401，停止重登，保持暂停；请检查原workspace资格。',
  PROBE_ACCESS_DENIED:'验号上游返回403（模型权限或访问限制），不会把它当401反复登录。',
  PROBE_RATE_LIMITED:'验号上游返回429（限流或配额），不会通过反复登录清除限制。',
  PROBE_UPSTREAM_UNAVAILABLE:'验号上游暂时不可用，账号保持暂停。',
  EXPECTED_WORKSPACE_NOT_RETURNED:'掉号 · 团队授权未恢复：NVT只返回同一用户的个人免费workspace，没有恢复原绑定的团队workspace。停止自动重登，不覆盖原凭据、不自动上线；这不代表整个登录账号被封。',
  REAUTH_WORKSPACE_CHANGED:'授权结果属于同一用户的另一个workspace，不能冒充原账号恢复。',
  PROBE_INCOMPLETE:'测试没有收到完整的成功结束事件，未恢复调度。',
  PROBE_RESULT_UNKNOWN:'测试连接异常，保持停止调度，等待处理。',
  CLOUD_NOT_READY_AFTER_PROBE:'模型测试后账号状态仍不可恢复，保持停止调度。',
  ACCOUNT_NOT_MANUALLY_RECOVERED:'请先在 sub2api 核对账号已恢复 active 且开启调度，再解除待办。',
  CLOUD_CANDIDATE_CHANGED:'候选账号信息在选择后发生变化，请重新扫描核对。',
  BINDING_FETCH_FAILED:'云端读取失败，未尝试绑定；请检查连接后重试。',
  MULTIPLE_CLOUD_MATCHES:'有多个身份相符的候选，需要你选择。',
  NO_CLOUD_MATCH:'没有找到候选，请检查名称、邮箱和时间，或刷新全站账号。',
  CLOUD_MATCH_CONFLICT:'候选有身份缺失、身份冲突或已被占用，不能自动绑定。',
  CLOUD_IDENTITY_METADATA_MISSING:'云端缺少可校验的邮箱或 workspace，无法安全建立绑定。',
  BINDING_WRITE_NEEDS_RECONCILIATION:'存在未处理的云端写入记录，请先对账，不能另外绑定。',
  UNIQUE_IDENTITY_MATCH:'发现唯一且可校验的账号身份。',AUTO_BOUND:'已自动绑定唯一身份匹配。',
  MANUALLY_BOUND:'已按你的选择绑定，并重新核对云端身份。',
  BINDING_ALREADY_EXISTS:'已存在绑定，本次跳过。',BINDING_REPORT_STALE:'绑定列表或站点配置已变化，请重新扫描。',
  DEPLOY_MODEL_REQUIRED:'请填写可用的验号模型 ID。',
  DEPLOY_BIND_EXISTING_FIRST:'发现同邮箱/同创建标识的云端账号，请先用批量绑定核对，不能再建一个号。',
  DEPLOY_RESULT_NEEDS_REVIEW:'上次创建、授权或写入结果不明，已保留云端 ID 和阶段，不会重复建号。',
  DEPLOY_CONFIG_CHANGED:'导入期间云端配置或调度状态发生变化，已停止后续写入。',
  DEPLOY_CONNECTION_CHANGED:'任务的目标站点或管理员 Key 已变化，不能把任务发到新站点。',
  DEPLOY_VERIFICATION_STALE:'验号已过期或云端状态变更，请重新发起导入以重新验号，不会重复创建。',
  DEPLOYMENT_INCOMPLETE:'该账号有未完成的服务器导入，请在“导入服务器并上线”中续跑，勿另发授权任务。',
  DEPLOY_AUTH_ALREADY_ATTEMPTED:'本次导入已尝试过授权，未自动重复登录，请核对上一失败结果。',
  DEPLOY_READ_FAILED:'读取服务器失败，可用相同入口重试已确认阶段，不会重复创建。',
  DEPLOY_FINAL_STATE_INVALID:'服务器最终状态不符合上线要求，不能标记成功。',
  DEPLOY_NOT_READY:'验号后的账号状态仍未就绪，未上线。',
  DEPLOY_RUNTIME_UNKNOWN:'服务器未返回完整运行态，无法确认上线条件。',
  DEPLOY_COOLDOWN_ACTIVE:'账号仍在冷却或限流中，未启用。',
  DEPLOYED_AND_ENABLED:'服务器已写入，模型测试成功，生产分组及启用状态已读回确认。',
  DEPLOY_TEMPLATE_NOT_APPLIED:'服务器保存的代理、并发、倍率或指纹未匹配提交模板，账号不会进入生产组。',
  DEPLOY_QUEUED:'已排队导入服务器。',
  LOGIN_MATERIAL_MISSING:'只有sub2 Token，没有密码和2FA种子。可用Token可直接部署；完整重授权需补入三段材料。',
  SUB2_INVALID_JSON:'JSON语法无效，请粘贴完整JSON内容。',SUB2_UNSUPPORTED_FORMAT:'不是支持的sub2账号对象、账号数组或v1 bundle。',
  SUB2_UNSUPPORTED_ACCOUNT_TYPE:'目前JSON导入支持OpenAI OAuth；此项平台/类型尚未接入。',
  SUB2_EMAIL_REQUIRED:'credentials.email缺失或无效，无法建立稳定身份映射。',
  SUB2_CONFLICTING_ACCOUNT:'同邮箱有不同workspace或不同凭据，未擅自选择，请拆分核对。',
  SUB2_IDENTITY_CONFLICT:'本地已有同邮箱的不同workspace或用户，未覆盖。',
  SUB2_ACCOUNT_BUSY:'账号存在进行中任务、恢复待办或部署记录，不能直接替换凭据。',
  SUB2_UPDATE_CONFIRM_REQUIRED:'本地已有不同Token；核对同一身份后勾选“更新本地已有凭据”再保存。',
  SUB2_INPUT_TOO_LARGE:'JSON输入不得超过2 MiB。',SUB2_TOO_MANY_ACCOUNTS:'每批最多1000个账号。',
  SUB2_NO_ACCOUNTS:'文件没有账号。',SUB2_IMPORTED:'sub2凭据已加密导入。',
  SUB2_AMBIGUOUS_SUMMARY:'单身份summary不能对应多个账号，请核对包装结构。',
  IMPORT_NO_ACCOUNTS:'请先粘贴账号材料或选择文件。',
  IMPORT_REQUEST_ID_REQUIRED:'本次提交标识无效，请重新打开导入页。',
  IMPORT_REQUEST_CONFLICT:'同一提交标识对应了不同材料；请重新发起本批次。',
  IMPORT_BATCH_UNAVAILABLE:'本批次不属于当前站点，或批次已不可用。',
  MONITOR_STOPPED_DURING_DEPLOY:'上线期间监控被停止或设置已更改，未擅自重启。',
  ALREADY_IMPORTED:'本地已有相同材料，已复用。',DUPLICATE_IN_FILE:'本文件中重复，已跳过。',
};
const monitorNames={watching:'监控中',disabled:'已关闭',refresh_grace:'401 掉授权 · 等待刷新',queued:'401 修复已排队',continuation_queued:'续跑已排队（不重新登录）',retry_wait:'临时读取失败 · 等待续跑',reauth_retry_wait:'验号401 · 自动重授权等待中',waiting_connector:'等待连接器恢复 · 自动保留任务',pausing:'正在确认停调度',reauthorizing:'正在重授权',applying:'正在更新原账号',verifying:'正在验号',resuming:'正在恢复调度',recovered:'已恢复',recovered_paused:'授权已修复 · 仍停调度',needs_attention:'需要处理',manual_hold:'旧版暂停判断 · 待重新检查',account_disabled:'云端已停用 · 不自动处理',paused_unknown:'停止调度 · 原因待确认',state_unknown:'账号/调度状态未知',remote_missing:'云端账号未找到',binding_conflict:'绑定不匹配',rate_budget:'等待小时预算',waiting_new_evidence:'等待新证据',paused:'监控暂停',checking:'检查中',no_managed_accounts:'未选择托管账号'};
const errorText = code => messages[code] || `操作未完成：${code || 'UNKNOWN_ERROR'}`;
Object.assign(monitorNames,{retirement_waiting:'已停止重登 · 等待测试组',retirement_queued:'测试组清理排队中',retirement_failed:'清理未完成',retired:'已移测试组',retirement_pause:'停止调度',retirement_move:'移入测试组',retirement_confirm:'确认清理结果'});
monitorNames.login_material_missing='401 · 缺少登录材料，无法完整重授权';
const text = (tag, content, className) => { const n=document.createElement(tag);n.textContent=content;if(className)n.className=className;return n; };
function toast(message) { $('toast').textContent=message;$('toast').hidden=false;clearTimeout(timer);timer=setTimeout(()=>{$('toast').hidden=true;},6500); }
async function api(path, data, raw=false) {
  let response;
  try{response = await fetch('/api/'+path,{method:data===undefined?'GET':'POST',headers:{'x-local-token':token,...(data===undefined?{}:{'Content-Type':'application/json'})},...(data===undefined?{}:{body:JSON.stringify(data)})});}
  catch{throw new Error(data===undefined?'本地服务连接失败，页面可能是旧快照。请确认启动器仍在运行。':'本地请求连接中断，操作结果可能未知。先刷新任务结果，不要连续重复提交。');}
  if(!response.ok){let info={};try{info=await response.json();}catch{}if(info.code==='VAULT_LOCKED'||info.code==='LOCAL_SESSION_REQUIRED'){clearLocalSession();$('unlock-error').textContent=errorText(info.code);}const error=new Error(errorText(info.code||`HTTP_${response.status}`));error.code=info.code;throw error;}
  return raw?response:response.json();
}
async function action(button, fn) { if(button&&busyButtons.has(button))return;if(button){busyButtons.add(button);button.disabled=true;button.setAttribute('aria-busy','true');}foregroundActions++;try{await fn();}catch(e){toast(e.message);}finally{foregroundActions--;if(button){busyButtons.delete(button);button.disabled=false;button.removeAttribute('aria-busy');}if(unlocked)updateSelection();} }
function clearLocalSession(){
  sessionEpoch++;unlocked=false;state=null;selected.clear();usageById.clear();usageRevision=null;deploymentDraft=null;
  usageAttempts.clear();usageLoadingIds.clear();clearTimeout(usageLoadTimer);
  accountRenderKey='';jobRenderKey='';
  importBatch=null;inlineSubmit=null;importBatchRequest++;
  $('import-batch-rows').replaceChildren();$('import-batch-panel').hidden=true;
  $('import-history').replaceChildren(new Option('暂无导入记录',''));
  delete $('import-history').dataset.key;
  $('import-deploy-confirm').checked=false;$('import-deploy-isolation').checked=false;
  $('materials').value='';$('nvt-cookie').value='';$('admin-key').value='';$('master-password').value='';$('confirm-password').value='';
  for(const id of ['account-rows','job-rows','monitor-rows','candidate-list','cloud-list-rows','import-profile-summary'])$(id)?.replaceChildren();
  for(const dialog of document.querySelectorAll('dialog[open]'))dialog.close();
  if(typeof clearBindingUI==='function')clearBindingUI();clearChoices('请先解锁凭据库。');
  for(const id of ['stat-total','stat-ready','stat-attention','stat-cloud'])$(id).textContent='—';
  $('runtime-version').textContent=$('runtime-version').textContent.replace(/ · .*$/,' · 已锁定');
  $('lock-screen').hidden=false;
}
function goto(next) {
  page=next;for(const n of Object.keys(names)){$('page-'+n).hidden=n!==next;document.querySelector(`[data-page="${n}"]`).classList.toggle('active',n===next);}
  $('breadcrumb-current').textContent=names[next];window.scrollTo({top:0,behavior:'instant'});
  try{sessionStorage.setItem('s2e-page',next);}catch{}
  if(next==='bindings'&&typeof loadBindingReport==='function')loadBindingReport().catch(e=>toast(e.message));
  if(next==='cloud'&&typeof loadCloudAccounts==='function')loadCloudAccounts().catch(e=>toast(e.message));
  if(next==='pool'&&unlocked)scheduleUsage();
  const slot=next==='import'?$('import-template-slot'):$('settings-template-slot');slot.append($('profile-form'));
  if(next==='import'&&unlocked)loadImportBatch().catch(e=>{$('import-result').hidden=false;$('import-result').textContent=e.message;});
}
function date(ts){return ts?new Date(ts*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';}
function ageLabel(value){const ts=typeof value==='string'?Date.parse(value):value*1000;if(!Number.isFinite(ts)||!ts)return '时间未知';const age=Math.max(0,(Date.now()-ts)/1000);return age<60?'刚刚':age<3600?`${Math.floor(age/60)} 分钟前`:`${Math.floor(age/3600)} 小时前`;}
function usageValue(value,unit=''){return typeof value==='number'&&Number.isFinite(value)?value.toLocaleString('zh-CN',{maximumFractionDigits:unit==='$'?4:1})+(unit==='$'?' USD':unit):'未知';}
function usageCell(accountId,embedded=null,reason='未绑定云端'){
  const cell=document.createElement('td');cell.className='usage-cell';
  if(!accountId){cell.append(text('span',reason,'usage-unavailable'));return cell;}
  const usage=usageById.get(accountId)||embedded;
  if(!usage){cell.append(text('span',usageLoadingIds.has(accountId)?'正在读取…':usageAttempts.has(accountId)?'用量未知 · 可刷新重试':'未采集',usageLoadingIds.has(accountId)?'usage-loading':'usage-unavailable'));return cell;}
  const windows=usage.windows||[];
  if(!windows.length)cell.append(text('span','额度窗口：未知 / 平台未支持','usage-unavailable'));
  for(const win of windows){const key={five_hour:'5 小时',seven_day:'7 天'}[win.key]||win.key;
    const expired=win.freshness==='expired'||(win.reset_at&&Date.parse(win.reset_at)<=Date.now());
    const used=expired?null:win.used_percent;
    const line=document.createElement('div');line.className='usage-window';
    line.append(text('span',`${key} 已用 ${usageValue(used,'%')}`));
    if(typeof used==='number'&&Number.isFinite(used)){const meter=document.createElement('meter');meter.min=0;meter.max=Math.max(100,used);meter.value=used;meter.className='usage-meter';meter.setAttribute('aria-label',`${key} 已用 ${used}%`);line.append(meter);}
    cell.append(line);
    const reset=win.reset_at?new Date(win.reset_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'未知';
    cell.append(text('small',`重置 ${reset} · ${expired?'旧窗口已结束，等新快照':win.freshness==='stale'?'历史快照':win.freshness==='unknown'?'新鲜度未知':ageLabel(win.updated_at)}`,expired||win.freshness!=='recent'?'usage-detail usage-stale':'usage-detail'));
  }
  const today=usage.today||{};const details=document.createElement('details');details.className='usage-today';
  details.append(text('summary',`今日 ${usageValue(today.requests)} 次 · ${usageValue(today.tokens)} Token · ${usageValue(today.cost,'$')}`));
  details.append(text('div',`账号口径费用 ${usageValue(today.cost,'$')}；标准费用 ${usageValue(today.standard_cost,'$')}；用户口径费用 ${usageValue(today.user_cost,'$')}`,'usage-detail'));
  details.append(text('small','按 sub2api 服务端“今日”统计，仅限经过本站的请求；不是订阅余额。','usage-detail'));
  cell.append(details,text('small','采集 '+ageLabel(usage.collected_at)+' · 已保存快照 / 本站日志','usage-detail'));
  const errors=usage.errors||[];
  if(errors.some(e=>['USAGE_READ_FAILED','USAGE_INVALID_RESPONSE','USAGE_ACCOUNT_ID_MISMATCH'].includes(e.code)))cell.append(text('small','部分读取失败，缺项按未知显示','usage-stale'));
  cell.title='用量来源：sub2api 已保存额度快照 + 本站使用日志；不主动刷新上游、不请求模型。';
  return cell;
}
function visibleUsageIds(){
  if(page==='cloud'&&typeof cloudResult!=='undefined')return (cloudResult?.items||[]).map(a=>a.id);
  return visibleAccounts().filter(a=>a.binding?.instance===state.settings.instance).map(a=>a.binding.cloud_id);
}
function scheduleUsage(){
  clearTimeout(usageLoadTimer);if(!unlocked||!['pool','cloud'].includes(page))return;
  usageLoadTimer=setTimeout(()=>refreshUsage(false).catch(()=>{}),250);
}
async function refreshUsage(force=false,idsOverride=null){
  if(!unlocked||!state?.settings.has_admin_key)return;
  if(usageRequest){if(force)toast('用量正在后台采集，请稍候。');return usageRequest;}
  let ids=idsOverride||visibleUsageIds();ids=[...new Set(ids)].filter(Number.isSafeInteger).slice(0,50);
  if(!ids.length){if(force)toast('当前页没有已绑定的本机站点账号。');return;}
  const epoch=sessionEpoch,revision=state.settings.cloud_revision;
  ids=ids.filter(id=>force||!usageAttempts.has(id)||Date.now()-usageAttempts.get(id)>60000);
  if(!ids.length)return;
  ids.forEach(id=>{usageAttempts.set(id,Date.now());usageLoadingIds.add(id);});
  for(const id of ['refresh-usage','cloud-refresh-usage'])if($(id)){$(id).disabled=true;$(id).setAttribute('aria-busy','true');}
  const work=(async()=>{try{
    let report=await api('usage',{account_ids:ids}),resubmitted=false;
    for(let n=0;n<120;n++){
      if(!unlocked||sessionEpoch!==epoch||state.settings.cloud_revision!==revision)return;
      for(const row of report.accounts||[])usageById.set(row.account_id,row);
      usageRevision=revision;
      if(page==='cloud'&&typeof renderCloud==='function')renderCloud();else if(!$('account-rows').contains(document.activeElement))renderAccounts();
      if(!report.pending){
        const missing=ids.filter(id=>!(report.accounts||[]).some(row=>row.account_id===id));
        if(missing.length&&!resubmitted&&!report.error){resubmitted=true;report=await api('usage',{account_ids:missing});continue;}
        break;
      }
      await new Promise(resolve=>setTimeout(resolve,1000));
      report=await api('usage?ids='+ids.join(','));
    }
    if(force)toast(report.pending?'用量仍在后台采集，稍后刷新查看。':report.error?errorText(report.error):'用量已更新；未知项不是零，旧快照已标明。');
  }catch(e){if(force)toast(e.message);}finally{
    ids.forEach(id=>usageLoadingIds.delete(id));
    for(const id of ['refresh-usage','cloud-refresh-usage'])if($(id)){$(id).disabled=false;$(id).removeAttribute('aria-busy');}
    if(unlocked&&sessionEpoch===epoch){accountRenderKey='';if(page==='cloud'&&typeof renderCloud==='function')renderCloud();else if(!$('account-rows').contains(document.activeElement))renderAccounts();}
  }})();usageRequest=work;try{await work;}finally{if(usageRequest===work)usageRequest=null;}
}
function badge(status){const v=statuses[status]||[status,''];return text('span',v[0],'status-tag '+v[1]);}
function button(label, callback){const b=text('button',label,'text-button');b.type='button';b.addEventListener('click',()=>action(b,callback));return b;}
function filteredAccounts(){
  const q=$('search').value.toLowerCase(),filter=$('status-filter').value,b=$('local-binding').value,m=$('local-monitor').value,g=$('local-group').value,p=$('local-proxy').value,t=$('local-profile').value;
  const rows=(state?.accounts||[]).filter(a=>{const c=a.cloud_metadata;const current=a.binding?.instance===state.settings.instance;
    if(filter==='team_lost'?!isTeamLost(a):filter&&a.status!==filter)return false;
    if(q&&![a.label,a.id,c?.name,c?.email,a.binding?.cloud_id].filter(Boolean).join(' ').toLowerCase().includes(q))return false;
    if(b==='unbound'&&a.binding||b==='bound'&&(!a.binding||!current)||b==='other'&&(!a.binding||current))return false;
    if(m==='enabled'&&!a.monitor?.enabled||m==='disabled'&&a.monitor?.enabled||m==='attention'&&!(a.monitor?.auth_401||a.monitor?.blocked||['failed','review','unknown','write_unknown'].includes(a.status)))return false;
    if(g&&!(c?.group_ids||[]).map(String).includes(g))return false;
    if(p&&!(p==='direct'?c&&c.proxy_id===null:String(c?.proxy_id)===p))return false;
    if(t&&a.profile.profile_id!==t)return false;return true;
  });
  const sort=$('local-sort').value;rows.sort((a,b)=>(sort==='name'?a.label.localeCompare(b.label):sort==='updated_desc'?b.updated-a.updated:(b.imported_at||0)-(a.imported_at||0))||a.id.localeCompare(b.id));return rows;
}
function isTeamLost(a){
  if(!a||a.status==='retired')return false;
  const m=a.monitor||{};
  // Keep an old diagnostic from reclassifying an explicitly recovered account.
  if(!m.blocked&&['active','authorized','local'].includes(a.status))return false;
  return m.last_code==='EXPECTED_WORKSPACE_NOT_RETURNED'||a.result_code==='EXPECTED_WORKSPACE_NOT_RETURNED';
}
const teamLostLabel='掉号 · 团队授权未恢复';
function monitorLabel(a){
  if(a.status==='retired')return `已移测试组 #${a.retirement?.group_id||'—'} · 停调度 · 已退出监控`;
  if(a.monitor?.state==='retirement_failed')return '测试组清理未完成 · '+errorText(a.retirement?.code||a.monitor.last_code);
  if(a.monitor?.state==='retirement_waiting')return '已停止重登 · 等待确认测试组';
  if(a.monitor?.state==='retirement_queued')return '已停止重登 · 测试组清理已排队';
  if(isTeamLost(a))return teamLostLabel+' · 自动重登已停止';
  if(!a.monitor?.enabled)return a.monitor?.enrollment_code?errorText(a.monitor.enrollment_code):'未加入监控';
  if(!state.monitor.config.enabled)return '已托管 · 全局监控已停止';
  if(state.monitor.runtime?.last_code)return '已托管 · '+errorText(state.monitor.runtime.last_code);
  return (monitorNames[a.monitor.state]||'等待检查')+(a.has_login_material?'':' · 仅Token，缺少重登材料');
}
function visibleAccounts(){const rows=filteredAccounts();localPage=Math.max(1,Math.min(localPage,Math.ceil(rows.length/localPageSize)||1));return rows.slice((localPage-1)*localPageSize,localPage*localPageSize);}
function updateSelection(){
  const rows=visibleAccounts(),visible=rows.filter(a=>selected.has(a.id)).length;
  $('selected-count').textContent=`已选 ${selected.size} 个${selected.size>visible?`（其他页/筛选外 ${selected.size-visible} 个）`:''}`;
  for(const id of ['deploy-selected','authorize-selected','monitor-selected','batch-bind-selected'])$(id).disabled=!selected.size||busyButtons.has($(id));
  $('select-all').checked=rows.length>0&&visible===rows.length;$('select-all').indeterminate=visible>0&&visible<rows.length;
}
function renderAccounts(){
  const body=$('account-rows');const rows=visibleAccounts();
  const key=JSON.stringify([rows,[...selected],localPage,usageRevision,[...usageById],Math.floor(Date.now()/60000)]);
  if(key===accountRenderKey)return;accountRenderKey=key;body.replaceChildren();
  $('empty-pool').hidden=state.accounts.length>0;$('no-matches').hidden=rows.length>0||!state.accounts.length;
  for(const a of rows){const tr=document.createElement('tr');const checkCell=document.createElement('td');const check=document.createElement('input');check.type='checkbox';check.checked=selected.has(a.id);check.setAttribute('aria-label','选择 '+a.label);check.onchange=()=>{check.checked?selected.add(a.id):selected.delete(a.id);updateSelection();};checkCell.append(check);tr.append(checkCell);
    const accountCell=document.createElement('td');accountCell.className='account-identity';accountCell.append(text('span',a.label),text('small','导入 '+date(a.imported_at)+' · '+a.id.slice(0,8)),text('small',a.has_login_material?'有密码/2FA材料':'sub2 Token · 无密码/2FA'));tr.append(accountCell);
    const s=document.createElement('td');if(isTeamLost(a))s.append(text('span',teamLostLabel,'status-tag error'));else if(a.monitor?.auth_401)s.append(text('span','401 掉授权','status-tag error'));else s.append(badge(a.status));if(a.deployment?.step&&!isTeamLost(a)&&a.status!=='retired')s.append(text('small','导入：'+(deployStages[a.deployment.step]||a.deployment.step)));if(a.binding)s.append(text('small','◉ '+monitorLabel(a)));
    const latestReview=a.status==='review'?state.jobs.find(j=>j.account_id===a.id&&j.state==='review'):null;
    if(latestReview){const cause=text('small',errorText(a.result_code||latestReview.code),'error-text');cause.style.whiteSpace='normal';s.append(cause);}tr.append(s);
    const binding=document.createElement('td');binding.append(text('span',a.binding?'#'+a.binding.cloud_id:'未绑定'));if(!a.binding)binding.append(text('small','本地库存'));else if(a.cloud_metadata)binding.append(text('small',a.cloud_metadata.name));tr.append(binding);
    tr.append(usageCell(a.binding?.instance===state.settings.instance?a.binding.cloud_id:null,a.usage,a.binding?'其他站点 · 不串用量':'未绑定云端'));
    const profile=document.createElement('td');profile.append(text('span',a.profile.profile_id),text('small',a.profile.fingerprint_mode+' · rev '+a.profile.revision));tr.append(profile);
    const controls=document.createElement('td');const row=document.createElement('div');row.className='row-actions';
    const pending=state.jobs.some(j=>j.account_id===a.id&&['queued','running'].includes(j.state));
    const blocked=!!a.retirement?.state||a.deployment?.state==='unknown'||a.deployment?.state==='review'||a.status==='unknown'||a.status==='write_unknown'||a.status==='review';
    const deploy=button(a.status==='retired'?'已移测试组':a.deployment?.state==='complete'?'已完成上线':a.deployment?.state&&a.deployment.state!=='complete'?'继续导入':'导入服务器并上线',()=>openDeployment([a.id]));deploy.disabled=pending||blocked||a.deployment?.state==='complete';
    deploy.title=pending?'已有任务执行中':blocked?'先核对失败原因，未知写入不能重复提交':'授权、服务器写入、验号、移组和启用会自动完成';row.append(deploy);
    const more=document.createElement('details');more.className='row-more';const summary=text('summary','更多');summary.setAttribute('aria-label',a.label+'的更多操作');more.append(summary);
    const menu=document.createElement('div');menu.className='row-more-menu';more.append(menu);
    const auth=button('仅获取授权，不写服务器',()=>authorize([a.id]));auth.disabled=pending||!a.has_login_material||blocked;menu.append(auth);
    if(!a.binding)menu.append(button('匹配已有服务器账号',()=>bind(a)));
    if(a.binding&&!a.retirement?.state)menu.append(button(a.monitor?.enabled?'关闭该号监控':'开启该号监控',()=>setMonitored([a.id],!a.monitor?.enabled)));
    if(!isTeamLost(a)&&a.binding&&a.monitor?.enabled&&(a.monitor?.blocked||a.monitor?.state==='recovered_paused'))menu.append(button('续跑验号并启用',async()=>{const yes=await confirmDialog('续跑恢复并启用',`为原账号 #${a.binding.cloud_id} 继续未完成的恢复。优先使用已有新凭据，不再调用 NVT 登录。\n再次验号成功、身份和配置检查通过后，开启此账号调度。测试可能产生用量；写入结果未知的任务不会被盲目重放。`);if(!yes)return;await api(`monitor/accounts/${a.id}/continue`,{confirm_test_and_enable:true});await refresh();toast('已加入续跑任务，请查看任务中心的验号与启用结果。');}));
    if(a.monitor?.blocked&&!a.retirement?.state)menu.append(button('我已人工修好，解除待办',async()=>{const yes=await confirmDialog('确认账号已人工恢复','此操作只解除本地修复待办，不修改云端。请先在 sub2api 核对凭据、验号并恢复调度；云端写入结果仍未知的账号不会被直接解除。');if(!yes)return;await api(`monitor/accounts/${a.id}/acknowledge`,{confirm_manually_recovered:true});await refresh();toast('已核对云端状态并解除本地待办。');}));
    if(a.validated&&a.status==='authorized')menu.append(button(a.binding?'高级：仅更新凭据，不上线':'高级：仅写隔离组，不上线',()=>writeCloud(a)));
    if(a.has_result)menu.append(button('导出凭据 JSON',()=>downloadResult(a)));
    if(a.has_result&&a.status==='review'&&!a.retirement?.state)menu.append(button('重新校验已有结果',async()=>{const r=await api(`accounts/${a.id}/revalidate`,{});toast(r.validated?'已有结果重新校验通过，没有重发登录请求。':errorText(r.code));await refresh();}));
    row.append(more);
    controls.append(row);tr.append(controls);body.append(tr);
  }const total=filteredAccounts().length;$('local-page-info').textContent=`筛选 ${total} / 总计 ${state.accounts.length} 个 · 第 ${localPage} / ${Math.max(1,Math.ceil(total/localPageSize))} 页`;$('local-prev').disabled=localPage<=1;$('local-next').disabled=localPage*localPageSize>=total;updateSelection();scheduleUsage();
}
function renderJobs(){
  renderTaskPool();
  const key=JSON.stringify([state.jobs,state.deployment_report]);if(key===jobRenderKey)return;jobRenderKey=key;
  $('job-rows').replaceChildren();$('empty-jobs').hidden=state.jobs.length>0;
  for(const j of state.jobs){const tr=document.createElement('tr');const a=state.accounts.find(a=>a.id===j.account_id);const one=document.createElement('td');one.append(text('span',a?.label||j.account_id.slice(0,8)),text('small',j.id.slice(0,8)));tr.append(one);
    const two=document.createElement('td');two.append(j.kind!=='manual'&&j.state==='succeeded'?text('span',j.kind==='retire'?'已移测试组':j.kind==='server_deploy'?'已导入并上线':'修复完成','status-tag success'):badge(j.state));two.append(text('small',j.kind==='retire'?'团队失效清理':j.kind==='server_deploy'?'服务器导入':j.kind==='recovery_continue'?'续跑恢复（未重登）':j.kind==='auto_reauth'?'自动 401 修复':'手动授权'));tr.append(two);tr.append(text('td',(j.kind==='server_deploy'?deployStages[j.stage]:monitorNames[j.stage])||j.stage||'—'));
    const result=text('td',j.code||'等待 Worker');result.title=messages[j.code]||j.code;result.append(text('small',messages[j.code]||''));
    if(['queued','running'].includes(j.state)){const cancel=button(j.cancel_requested?'停止已请求':'停止任务',async()=>{await api(`jobs/${j.id}/cancel`,{});await refresh();toast(j.state==='running'?'已请求停止后续步骤；已发出的请求仍需读回确认结果。':'已取消排队任务。');});cancel.disabled=!!j.cancel_requested;result.append(cancel);}
    tr.append(result,text('td',date(j.created)));$('job-rows').append(tr);
  }
  const report=state.deployment_report;$('deploy-report').hidden=!report;
  if(report)$('deploy-report').textContent=report.items.map(r=>{const job=state.jobs.find(j=>j.id===r.job_id);const result=job?(job.state==='succeeded'?'已完成并确认上线':job.state==='running'?'执行中：'+(deployStages[job.stage]||job.stage):job.state==='queued'?'排队中':errorText(job.code)):r.state==='queued'?'已提交（查看对应任务）':r.state==='already_complete'?'已上线，无需重复导入':errorText(r.code);return `${state.accounts.find(a=>a.id===r.account_id)?.label||r.account_id.slice(0,8)}：${result}`;}).join('\n');
}
function renderTaskPool(fill=false){
  const p=state?.task_pool;if(!p){$('task-pool-tag').textContent='运行版本不支持并行';return;}
  const c=p.config,r=p.runtime;
  $('task-pool-tag').textContent=c.paused?'暂停领取新任务':`并行 × ${c.max_workers}`;
  $('task-pool-runtime').textContent=`正在处理 ${r.active} / ${c.max_workers} 个账号 · 正在授权 ${r.authorizing} / ${c.max_authorizations} · 排队 ${r.queued} 个。${r.last_error?errorText(r.last_error):'监控独立运行；降低上限不会中断已发出的请求。'}`;
  if(fill){$('task-max-workers').value=String(c.max_workers);$('task-max-auth').value=String(c.max_authorizations);$('task-pool-paused').checked=c.paused;}
}
$('task-pool-save').onclick=()=>action($('task-pool-save'),async()=>{
  state.task_pool=await api('tasks/config',{max_workers:Number($('task-max-workers').value),max_authorizations:Number($('task-max-auth').value),paused:$('task-pool-paused').checked});
  renderTaskPool(true);toast('并发设置已保存，下次领取任务立即生效；已有请求不会被强行中断。');
});

function openDeployment(ids){
  if(!state?.settings.sub2api_url||!state.settings.has_admin_key){toast('先保存站点地址和管理员 Key，再导入服务器。');goto('settings');return;}
  if(connectionDirty()){toast('连接信息尚未保存，请先保存，避免发往错误站点。');goto('settings');return;}
  if(!Array.isArray(ids)||!ids.length)return;
  deploymentDraft={profile:structuredClone(state.profile),revision:state.settings.cloud_revision,instance:state.settings.instance};
  deploymentSelection=[...ids];const newCount=ids.filter(id=>!state.accounts.find(a=>a.id===id)?.binding).length;
  const p=deploymentDraft.profile;const groupLabel=id=>{const g=choices?.instance_id===p.instance_id?choices.groups.find(g=>g.id===id):null;return g?`${g.name} (#${id})`:`#${id}`;};
  $('deployment-summary').textContent=`所选 ${ids.length} 个账号（新号 ${newCount} 个）。目标：${state.settings.sub2api_url}。本次固定模板 ${p.profile_id} v${p.revision}：${groupLabel(p.staging_group_id)} → ${p.target_group_ids.map(groupLabel).join('、')}。旧号保留原分组；续跑使用任务原快照。失败不会中断其他账号。`;
  $('deployment-model').value=state.monitor.config.model_id||'';$('deployment-confirm').checked=false;$('deployment-isolation').checked=false;
  $('deployment-isolation-wrap').hidden=!newCount;$('deployment-isolation').required=!!newCount;$('deployment-error').textContent='';$('deployment-dialog').showModal();
}
function profileSummary(){
  const p=state.profile;$('import-profile-summary').replaceChildren();
  const catalog=choices?.instance_id===p.instance_id?choices:null;
  const boundProfile=/^https?:\/\//.test(p.instance_id);
  const labelFor=(rows,id)=>{const row=rows?.find(r=>r.id===id);return row?`${row.name} · #${id}`:`#${id}`;};
  for(const [label,value] of [['模板',`${p.profile_id} · v${p.revision}`],['平台','OpenAI OAuth'],['隔离组',boundProfile?labelFor(catalog?.groups,p.staging_group_id):'请先从站点选择并保存'],['生产目标组',boundProfile?p.target_group_ids.map(id=>labelFor(catalog?.groups,id)).join(', '):'请先从站点选择并保存'],['代理',p.proxy_id?labelFor(catalog?.proxies,p.proxy_id):'直连'],['并发 / 优先级',`${p.concurrency} / ${p.priority}`],['计费倍率',p.rate_multiplier],['指纹收敛',p.fingerprint_mode]]){const line=document.createElement('div');line.className='profile-line';line.append(text('span',label),text('span',String(value)));$('import-profile-summary').append(line);}
  $('import-destination').textContent=state.settings.sub2api_url?`服务器：${state.settings.sub2api_url} · 本次使用上面已保存的模板。`:'尚未设置站点地址和管理员 Key；可先保存到本地。';
  $('import-monitor-help').textContent=!state.settings.has_cookie?'未保存NVT Cookie：可上线和监听，但401完整重登需要补齐Cookie及该号的密码/2FA。':state.settings.connector_paused?'NVT连接器已暂停：仍能监听；自动重登需先恢复连接器。':'监控在本地程序中运行，不依赖网页保持打开；程序需运行且凭据库保持解锁。';
}
function targetIds(){return [...$('target-group-options').querySelectorAll('input:checked')].map(n=>Number(n.value));}
function connectionDirty(){return !state||$('sub2-url').value.trim()!==state.settings.sub2api_url||!!$('admin-key').value.trim()||$('clear-admin').checked;}
function updateChoiceSelection(){
  const staging=Number($('staging-group').value), targets=targetIds();
  for(const box of $('target-group-options').querySelectorAll('input'))box.disabled=(box.dataset.unavailable==='true'&&!box.checked)||Number(box.value)===staging;
  const labels=targets.map(id=>{const row=choices?.groups.find(g=>g.id===id);return row?`${row.name} · #${id}`:`不可用 · #${id}`;});
  $('target-groups-summary').textContent=labels.length?labels.join('、'):choices?'选择生产分组':'请先配置站点并加载选项';
  const groupIds=new Set(choices?.groups.map(g=>g.id)||[]),proxy=$('proxy-id').value;
  const missing=targets.some(id=>!groupIds.has(id))||(staging&&!groupIds.has(staging))||(proxy&&!choices?.proxies.some(p=>p.id===Number(proxy)));
  const overlap=targets.includes(staging);
  $('selection-warning').textContent=overlap?'隔离组与生产组不能相同，请取消重复的生产组选择。':missing?'原选择已不在可用列表中，请重新选择；不会自动切换为直连。':'';
  $('save-profile').disabled=choicesLoading||!choices||connectionDirty()||!groupIds.has(staging)||!targets.length||missing||overlap;
}
function clearChoices(message){
  choicesRequest++;choices=null;$('retirement-group').replaceChildren(new Option('先加载站点分组选项',''));delete $('retirement-group').dataset.catalog;$('staging-group').replaceChildren(new Option('请选择站点中的隔离组',''));$('staging-group').disabled=true;
  $('proxy-id').replaceChildren(new Option('直连 · 不指定代理',''));$('proxy-id').disabled=true;
  $('target-group-options').replaceChildren();$('target-groups').open=false;$('target-groups-summary').setAttribute('aria-disabled','true');
  $('choices-status').textContent=message;updateChoiceSelection();
}
function populateChoices(catalog, previous){
  choices=catalog;renderRetirementChoices();const p=previous||((state.profile.instance_id===catalog.instance_id)?state.profile:{staging_group_id:null,target_group_ids:[],proxy_id:null});
  const staging=$('staging-group');staging.replaceChildren(new Option('选择隔离分组',''));
  for(const g of catalog.groups)staging.add(new Option(`${g.name} · #${g.id}`,String(g.id)));
  if(p.staging_group_id&&!catalog.groups.some(g=>g.id===p.staging_group_id)){const o=new Option(`不可用 · #${p.staging_group_id}`,String(p.staging_group_id));o.disabled=true;staging.add(o);}
  staging.value=p.staging_group_id?String(p.staging_group_id):'';staging.disabled=false;
  const proxy=$('proxy-id');proxy.replaceChildren(new Option('直连 · 不指定代理',''));
  for(const row of catalog.proxies)proxy.add(new Option(`${row.name} · #${row.id}`,String(row.id)));
  if(p.proxy_id&&!catalog.proxies.some(r=>r.id===p.proxy_id)){const o=new Option(`不可用 · #${p.proxy_id}`,String(p.proxy_id));o.disabled=true;proxy.add(o);}
  proxy.value=p.proxy_id?String(p.proxy_id):'';proxy.disabled=false;
  const targets=new Set(p.target_group_ids), container=$('target-group-options');container.replaceChildren();
  const rows=[...catalog.groups,...[...targets].filter(id=>!catalog.groups.some(g=>g.id===id)).map(id=>({id,name:'不可用',unavailable:true}))];
  for(const row of rows){const label=document.createElement('label');const input=document.createElement('input');input.type='checkbox';input.value=String(row.id);input.checked=targets.has(row.id);input.dataset.unavailable=String(!!row.unavailable);input.onchange=updateChoiceSelection;label.append(input,text('span',`${row.name} · #${row.id}`));container.append(label);}
  if(!rows.length)container.append(text('p','没有可用 OpenAI 分组，请先在 sub2api 创建分组。','field-help'));
  $('target-groups-summary').setAttribute('aria-disabled','false');
  $('choices-status').textContent=`已读取 ${catalog.groups.length} 个 OpenAI 分组、${catalog.proxies.length} 个可用代理 · ${date(catalog.fetched_at)}。选择隔离组不等于已验证路由隔离。`;
  updateChoiceSelection();profileSummary();
}
async function loadChoices(){
  if(!unlocked||!state?.settings.sub2api_url||!state?.settings.has_admin_key){clearChoices('先保存站点地址和管理员 API Key，再加载分组与代理。');return;}
  if(connectionDirty()){clearChoices('站点配置尚未保存，请先保存左侧连接设置。');return;}
  const previous=choices?{staging_group_id:Number($('staging-group').value)||null,target_group_ids:targetIds(),proxy_id:Number($('proxy-id').value)||null}:null;
  const revision=state.settings.cloud_revision,request=++choicesRequest;choicesLoading=true;
  $('choices-status').textContent='正在读取已保存站点的分组与代理…';$('save-profile').disabled=true;$('reload-choices').disabled=true;
  try{const catalog=await api('cloud/options',{});if(request!==choicesRequest||!unlocked)return;
    if(catalog.connection_revision!==revision||state.settings.cloud_revision!==revision){clearChoices('站点配置已变化，请重新加载选项。');return;}
    populateChoices(catalog,previous);
  }catch(e){if(request===choicesRequest)clearChoices(`读取失败：${e.message} 不会沿用旧站点选项。`);}
  finally{choicesLoading=false;$('reload-choices').disabled=false;updateChoiceSelection();}
}
function fillForms(scope='all'){
  renderTaskPool(true);renderRetirement(true);
  const s=state.settings,p=state.profile;$('sub2-url').value=s.sub2api_url;$('oauth-client-id').value=s.oauth_client_id;
  $('cookie-indicator').textContent=s.has_cookie?'已加密保存'+(s.connector_paused?' · 已暂停':''):'未配置';$('admin-indicator').textContent=s.has_admin_key?'已加密保存':'未配置';
  if(scope==='connection')return;
  $('profile-name').value=p.profile_id;$('concurrency').value=p.concurrency;$('priority').value=p.priority;$('rate').value=p.rate_multiplier;$('fingerprint').value=p.fingerprint_mode;$('auto-pause').checked=p.auto_pause_on_expired;
  $('account-expiry').value=p.account_expires_at?new Date(p.account_expires_at*1000-new Date().getTimezoneOffset()*60000).toISOString().slice(0,16):'';
  const m=state.monitor.config;$('monitor-interval').value=String(m.interval_seconds);$('monitor-grace').value=String(m.grace_seconds);$('monitor-model').value=m.model_id;$('monitor-budget').value=m.max_per_hour;$('monitor-resume').checked=m.resume_after_success;
  $('monitor-resume-paused').checked=m.resume_paused_401===true;
  if(!$('import-deploy-model').value)$('import-deploy-model').value=m.model_id||'';
}
function renderMonitor(){
  renderRetirement();
  const {config:cfg,runtime:r}=state.monitor;const managed=state.accounts.filter(a=>a.monitor?.enabled);
  $('pool-mode-tag').textContent=cfg.enabled?'AUTO 401':'MANUAL';
  $('pool-mode-description').textContent=cfg.enabled?'已开启账号 401 监控；仅对单独托管的绑定账号执行重授权和原 ID 修复。':'监控尚未开启，手动授权不会自动写入云端。';
  $('monitor-footer').textContent=cfg.enabled?'只监听 127.0.0.1 · 401 监控已开启（需解锁）':'只监听 127.0.0.1 · 监控未开启';
  $('monitor-state').textContent=cfg.enabled?(monitorNames[r.state]||'等待首次检查'):'监控已关闭';
  $('monitor-runtime').textContent=cfg.enabled?`最近检查：${date(r.last_attempt)}。${r.last_code?errorText(r.last_code):'后台定时运行，不依赖网页打开。'}`:'开启后只处理已绑定且单独启用监控的账号。';
  $('monitor-scope-count').textContent=`${managed.length} ACCOUNTS`;$('monitor-check').disabled=!cfg.enabled;
  $('monitor-stop').disabled=!cfg.enabled;
  $('monitor-rows').replaceChildren();$('monitor-empty').hidden=managed.length>0;
  for(const a of managed){const row=document.createElement('tr'),m=a.monitor;const id=document.createElement('td');id.append(text('span',a.label),text('small',a.binding?'云端 #'+a.binding.cloud_id:'未绑定'));const health=document.createElement('td');if(m.auth_401)health.append(text('span','401 掉授权','status-tag error'));health.append(text('div',!cfg.enabled?'全局已停止 · '+(monitorNames[m.state]||'等待检查'):monitorNames[m.state]||'等待检查'));if(m.last_check)health.append(text('small',`${m.cloud_status||'未知'} / ${m.cloud_schedulable===true?'可调度':m.cloud_schedulable===false?'停止调度':'调度未知'}`));row.append(id,health,text('td',date(m.last_check)),text('td',m.last_code?errorText(m.last_code):'—'));$('monitor-rows').append(row);}
}
async function refresh(fill=false){
  if(!unlocked)return;
  if(refreshPending){await refreshPending;if(!fill)return;}
  const epoch=sessionEpoch;
  const work=(async()=>{
  const next=await api('state');if(!unlocked||epoch!==sessionEpoch)return;
  const oldRevision=state?.settings.cloud_revision;state=next;if(oldRevision&&oldRevision!==state.settings.cloud_revision){if(typeof clearBindingUI==='function')clearBindingUI();usageById.clear();usageAttempts.clear();usageLoadingIds.clear();usageRevision=null;importBatch=null;inlineSubmit=null;importBatchRequest++;$('import-batch-panel').hidden=true;$('import-batch-rows').replaceChildren();}
  const ids=new Set(state.accounts.map(a=>a.id));for(const id of selected)if(!ids.has(id))selected.delete(id);
  if(choices&&choices.connection_revision!==state.settings.cloud_revision)clearChoices('站点配置已变化，请刷新选项并重新选择。');
  $('stat-total').textContent=state.accounts.length;$('pool-count').textContent=state.accounts.length;$('nav-count').textContent=state.accounts.length;
  $('stat-ready').textContent=state.accounts.filter(a=>a.status==='authorized'&&!a.monitor?.auth_401).length;$('stat-attention').textContent=state.accounts.filter(a=>a.monitor?.auth_401||a.monitor?.blocked||['failed','review','unknown','write_unknown'].includes(a.status)).length;
  $('stat-cloud').textContent=state.cloud?(state.cloud.counts.candidate||0):'—';$('cloud-time').textContent=state.cloud?'快照 '+date(state.cloud.synced_at)+' · 非验号结果':'尚未采集，不等于不可用';
  $('job-count').textContent=state.jobs.filter(j=>['queued','running'].includes(j.state)).length;$('connection-pill').textContent=state.settings.has_admin_key?'云端配置已保存':'尚未连接云端';
  $('refresh-time').textContent='本地进度更新于 '+new Date().toLocaleTimeString('zh-CN');
  if(typeof localFilterOptions==='function')localFilterOptions();
  const listInUse=$('account-rows').contains(document.activeElement)||$('account-rows').querySelector('details[open]');
  if(!listInUse)renderAccounts();if(!$('job-rows').contains(document.activeElement))renderJobs();renderMonitor();profileSummary();
  if(fill){fillForms(fill===true?'all':fill);await loadChoices();}
  if(page==='import'&&!importSubmitBusy&&!foregroundActions)await loadImportBatch();
  })();refreshPending=work;
  try{await work;}finally{if(refreshPending===work)refreshPending=null;}
}
function confirmDialog(title,message,{input=false,checkbox=''}={}){
  return new Promise(resolve=>{const d=$('action-dialog');$('dialog-title').textContent=title;$('dialog-message').textContent=message;$('dialog-input-wrap').hidden=!input;$('dialog-input').value='';$('dialog-check-wrap').hidden=!checkbox;$('dialog-check').checked=false;$('dialog-check-label').textContent=checkbox;$('dialog-confirm').disabled=!!checkbox;$('dialog-check').onchange=()=>{$('dialog-confirm').disabled=!$('dialog-check').checked;};d.returnValue='cancel';d.onclose=()=>resolve(d.returnValue==='confirm'?{cloud_id:Number($('dialog-input').value),checked:$('dialog-check').checked}:null);d.showModal();});
}
async function authorize(ids){
  if(ids.some(id=>!state.accounts.find(a=>a.id===id)?.has_login_material))throw new Error(errorText('LOGIN_MATERIAL_MISSING'));
  const yes=await confirmDialog('向 NVT 发起授权',`将把所选 ${ids.length} 个账号的账号、密码及 TOTP 种子发送到 nvtokens.com 的 account-reauthorize 接口。\n\n按任务中心配置并行执行，同一账号互斥。成功结果暂存本机，不自动写入 sub2api。`,{checkbox:'我确认发送这些账号的登录材料。'});
  if(!yes)return;const result=await api('jobs',{account_ids:ids,confirm_send_to_nvtokens:true});const failed=(result.results||[]).filter(r=>r.state==='failed');toast(`已加入 ${result.queued.length} 个授权任务。${failed.length?'失败 '+failed.length+' 项：'+failed.map(r=>errorText(r.code)).join('；'):''}`);goto('jobs');await refresh();
}
async function bind(a){await scanBindings([a.id],true);}
async function setMonitored(ids,enabled){
  if(enabled){const yes=await confirmDialog('开启所选账号的自动修复',`为 ${ids.length} 个已绑定账号启用托管。全局监控开启后，持续 401 会自动把对应账号、密码和 2FA 种子发送给 nvtokens.com，更新原 sub2api 账号并发起模型测试；按全局设置恢复调度。\n不会重建账号。测试可能产生用量。`,{checkbox:'我确认这些账号可由系统自动重授权和维护。'});if(!yes)return;}
  await api('monitor/accounts',{account_ids:ids,enabled,confirm_auto_reauth:enabled});await refresh();toast(enabled?'账号已加入监控范围；请确认全局监控已开启。':'已关闭所选账号监控，未自动改变其云端调度状态。');
}
async function writeCloud(a){
  const message=a.binding?`将把新凭据应用到原账号 #${a.binding.cloud_id}。\n请先在 sub2api 停止该账号调度（不设为 inactive）。保留原分组、代理、指纹等配置，写入后不自动上线。\n\n解析器已按成功响应样本适配；云端写入后仍需验号。`:`将在当前 sub2api 创建一个新账号，绑定隔离组 #${a.profile.staging_group_id}，随后停止调度。\n隔离组必须没有生产路由、用户 Key 或 fallback 访问；否则不能保证创建瞬间不接流量。\n写入后请在 sub2api 验号，再手动移入生产组并上线。`;
  const yes=await confirmDialog(a.binding?'更新原账号凭据':'创建到隔离组',message,{checkbox:a.binding?'我已核对授权结果与原账号，确认更新。':'我已验证隔离组不会被生产流量选中，确认创建。'});if(!yes)return;
  await api(`accounts/${a.id}/write`,{confirm_write:true,staging_verified:!a.binding&&yes.checked});toast('云端写入完成，账号保持停止调度。请在 sub2api 验号后上线。');await refresh();
}
async function downloadResult(a){const yes=await confirmDialog('导出授权结果','导出的 JSON 包含 OAuth 凭据，下载后为明文文件。它只下载到本机，不会上传其他服务。');if(!yes)return;const response=await api(`accounts/${a.id}/export`,undefined,true);const blob=await response.blob();const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=`sub2-${a.id.slice(0,8)}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);}
async function boot(){
  try{const status=await api('status');$('unlock-submit').disabled=false;$('runtime-version').textContent=`v${status.version} · 并行任务池`;$('deploy-selected').title=status.features?.includes('server_deploy')?'服务器导入已加载':'后台版本尚未加载服务器导入';initialized=status.initialized;unlocked=status.unlocked;$('unlock-title').textContent=initialized?'解锁工作空间':'建立你的本地凭据库';$('unlock-description').textContent=initialized?'输入主密码，解锁本机加密凭据库。':'设置主密码，加密账号材料、Cookie 和授权结果。';$('unlock-submit').textContent=initialized?'解锁 →':'创建凭据库 →';$('confirm-label').hidden=initialized;$('confirm-password').hidden=initialized;$('confirm-password').required=!initialized;$('lock-screen').hidden=unlocked;if(unlocked){await refresh(true);const saved=sessionStorage.getItem('s2e-page');if(names[saved])goto(saved);}}catch(e){$('unlock-error').textContent=e.message;$('unlock-submit').disabled=true;}
}
document.querySelectorAll('[data-page]').forEach(n=>n.onclick=()=>goto(n.dataset.page));document.querySelectorAll('[data-go]').forEach(n=>n.onclick=()=>goto(n.dataset.go));
$('unlock-form').onsubmit=event=>{event.preventDefault();action($('unlock-submit'),async()=>{const password=$('master-password').value;if(!initialized&&password!==$('confirm-password').value){$('unlock-error').textContent='两次主密码不一致。';return;}try{await api('unlock',{password,setup:!initialized});initialized=true;unlocked=true;$('master-password').value='';$('confirm-password').value='';$('unlock-error').textContent='';$('lock-screen').hidden=true;await refresh(true);}catch(e){$('unlock-error').textContent=e.message;}});};
$('lock-btn').onclick=()=>action($('lock-btn'),async()=>{await api('lock',{});clearLocalSession();await boot();});
$('compact-lock').onclick=()=>$('lock-btn').click();
$('search').oninput=()=>{localPage=1;selected.clear();renderAccounts();};$('status-filter').onchange=()=>{localPage=1;selected.clear();renderAccounts();};$('select-all').onchange=()=>{for(const a of visibleAccounts())$('select-all').checked?selected.add(a.id):selected.delete(a.id);renderAccounts();};
$('authorize-selected').onclick=()=>action($('authorize-selected'),()=>authorize([...selected]));
$('deploy-selected').onclick=()=>openDeployment([...selected]);
$('refresh-usage').onclick=()=>{const ids=selected.size?state.accounts.filter(a=>selected.has(a.id)&&a.binding?.instance===state.settings.instance).map(a=>a.binding.cloud_id):null;refreshUsage(true,ids).catch(e=>toast(e.message));};
$('cloud-refresh-usage').onclick=()=>refreshUsage(true,(cloudResult?.items||[]).map(a=>a.id)).catch(e=>toast(e.message));
$('deployment-close').onclick=()=>$('deployment-dialog').close();
$('deployment-form').onsubmit=event=>{event.preventDefault();action($('deployment-submit'),async()=>{try{
  if(!deploymentDraft||deploymentDraft.revision!==state.settings.cloud_revision)throw new Error('确认期间站点已变化，请重新打开导入窗口。');
  const report=await api('deployments',{account_ids:deploymentSelection,model_id:$('deployment-model').value.trim(),profile:deploymentDraft.profile,connection_revision:deploymentDraft.revision,confirm_deploy:$('deployment-confirm').checked,staging_verified:$('deployment-isolation').checked,auto_monitor:$('deployment-auto-monitor').checked});
  const failed=report.items.filter(r=>r.state==='failed'),queued=report.items.filter(r=>r.state==='queued');
  if(!queued.length&&failed.length){$('deployment-error').textContent=failed.map(r=>`${state.accounts.find(a=>a.id===r.account_id)?.label||r.account_id.slice(0,8)}：${errorText(r.code)}`).join('\n');await refresh();return;}
  $('deployment-dialog').close();await refresh();goto('jobs');toast(`已排队 ${queued.length} 个服务器导入任务${failed.length?'，失败 '+failed.length+' 项见任务中心':''}。`);
 }catch(e){$('deployment-error').textContent=e.message;}});};
$('monitor-selected').onclick=()=>action($('monitor-selected'),()=>setMonitored([...selected],true));
$('monitor-form').onsubmit=event=>{event.preventDefault();action(event.submitter,async()=>{const yes=await confirmDialog('开启后台 401 自动修复','持续 401 的托管账号将自动调用 NVT（发送对应密码和 2FA 种子），更新原账号凭据并进行模型验证。程序和凭据库需保持运行、解锁。'+($('monitor-resume-paused').checked?'\n本次允许明确401的托管暂停号在修复并验号通过后自动启用。':''),{checkbox:'确认自动发送托管账号材料、写入原账号并按设置恢复调度。'});if(!yes)return;await api('monitor/config',{enabled:true,interval_seconds:Number($('monitor-interval').value),grace_seconds:Number($('monitor-grace').value),model_id:$('monitor-model').value.trim(),max_per_hour:Number($('monitor-budget').value),resume_after_success:$('monitor-resume').checked,resume_paused_401:$('monitor-resume-paused').checked,confirm_auto_reauth:true});await refresh();toast('后台账号监控已开启。');});};
$('monitor-stop').onclick=()=>action($('monitor-stop'),async()=>{await api('monitor/config',{enabled:false});await refresh();toast('已停止自动监控并取消排队中的自动任务；已发出的请求不能撤回，后续步骤将停止。');});
$('monitor-check').onclick=()=>action($('monitor-check'),async()=>{await api('monitor/check',{});await refresh();toast('本轮检查完成，请查看监控状态与授权任务。');});
$('sync-btn').onclick=()=>action($('sync-btn'),async()=>{await api('cloud/sync',{});await refresh();toast('云端快照已更新，可在全站账号中查看和筛选。');});
function importMode(){return $('import-format').value;}
function currentImportForm(){
  return {text:$('materials').value,format:importMode(),profile:structuredClone(state.profile),
    connection_revision:state.settings.cloud_revision,model_id:$('import-deploy-model').value.trim(),
    update_credentials:$('sub2-update').checked,confirm_deploy:$('import-deploy-confirm').checked,
    auto_monitor:$('import-auto-monitor').checked,
    staging_verified:$('import-deploy-isolation').checked};
}
async function loadImportBatch(){
  if(!unlocked)return;
  const request=++importBatchRequest,epoch=sessionEpoch;
  const chosen=$('import-history').value;
  const [response,history]=await Promise.all([api('import/batch'+(chosen?'?request_id='+encodeURIComponent(chosen):'')),api('import/history')]);
  if(request!==importBatchRequest||epoch!==sessionEpoch||!unlocked)return;
  const options=(Array.isArray(history)?history:[]).map(b=>[b.id,`${date(b.created_at)} · ${Object.values(b.counts).reduce((a,b)=>a+b,0)} 项 · ${b.instance?'已提交服务器':'本地保存'}`]);
  if(response&&!options.some(([id])=>id===response.id))options.unshift([response.id,'本次导入']);
  const key=JSON.stringify(options);
  if($('import-history').dataset.key!==key){$('import-history').replaceChildren(...(options.length?options.map(([id,label])=>new Option(label,id)):[new Option('暂无导入记录','')]));$('import-history').dataset.key=key;}
  $('import-history').value=response?.id||'';
  importBatch=response;renderImportBatch();
}
function renderImportBatch(){
  const panel=$('import-batch-panel');panel.hidden=!importBatch;if(!importBatch)return;
  const rows=importBatch.items;$('import-batch-rows').replaceChildren();
  const done=rows.filter(r=>r.state==='succeeded'||r.state==='already_complete').length;
  const running=rows.filter(r=>r.state==='running'||r.state==='queued').length;
  const failed=rows.filter(r=>['failed','unknown','review'].includes(r.state)).length;
  const local=rows.filter(r=>r.state==='local_saved').length;
  $('import-batch-deploy').hidden=!local;
  $('import-batch-summary').textContent=`共 ${rows.length} 项 · 本地待上传 ${local} · 已完成 ${done} · 排队/执行中 ${running} · 失败/待核对 ${failed} · ${importBatch.instance?'目标 '+importBatch.instance:'尚未上传服务器，请确认上方模板后点右侧上传按钮'} · 模板 ${importBatch.profile.profile_id} v${importBatch.profile.revision}`;
  for(const r of rows){const tr=document.createElement('tr');tr.append(text('td',`第 ${r.index} 项 · ${r.label||'输入有误/重复项'}`),text('td',r.cloud_id?'#'+r.cloud_id:'尚未创建'),text('td',deployStages[r.step]||r.step||'材料校验'));
    const result=document.createElement('td');result.className='inline-result';
    const label=r.state==='local_saved'?'已保存本地 · 尚未上传':r.state==='succeeded'?'已在服务器上线':r.state==='already_complete'?'原任务已完成':r.state==='queued'?'已保存本地，等待上传':r.state==='running'?'正在导入服务器':r.state==='skipped'?'重复项，已跳过':errorText(r.code);
    result.append(text('span',label));tr.append(result);
    if(r.execution_profile){const p=r.execution_profile;result.append(text('small',`执行模板 ${p.profile_id} v${p.revision} · 隔离 #${p.staging_group_id} → 生产 ${(p.target_group_ids||[]).map(id=>'#'+id).join('、')} · 代理 ${p.proxy_id?'#'+p.proxy_id:'直连'}`));}
    const e=r.precheck_error;if(e){const missing=[e.missing_staging_group_id?'隔离组 #'+e.missing_staging_group_id:'',...(e.missing_target_group_ids||[]).map(id=>'生产组 #'+id),e.missing_proxy_id?'代理 #'+e.missing_proxy_id:''].filter(Boolean);if(missing.length)result.append(text('small','当前不可用：'+missing.join('、'),'error-text'));}
    if(['succeeded','already_complete'].includes(r.state))result.append(text('small',monitorLabel({monitor:{enabled:r.monitor_enabled,state:'watching',enrollment_code:r.monitor_code},has_login_material:r.has_login_material})));
    const ops=document.createElement('td');
    if(r.job_id&&['queued','running'].includes(r.state)){const b=button(r.cancel_requested?'停止已请求':'停止本项',async()=>{await api(`jobs/${r.job_id}/cancel`,{});await loadImportBatch();});b.disabled=!!r.cancel_requested;ops.append(b);}
    if(r.account_id&&['failed','cancelled'].includes(r.state)&&!['failed','conflict'].includes(r.intake_state))ops.append(button('重试本项',async()=>{importBatch=await api(`import/batch/${importBatch.id}/retry`,{indices:[r.index]});renderImportBatch();}));
    tr.append(ops);$('import-batch-rows').append(tr);
  }
}
$('import-deploy-btn').onclick=()=>action($('import-deploy-btn'),async()=>{
  if(importSubmitBusy)return;
  if(!state.settings.sub2api_url||!state.settings.has_admin_key)throw new Error('请先配置服务器地址与管理员 Key；本页仍可仅保存到本地。');
  if(connectionDirty())throw new Error('连接设置尚未保存，请先保存当前站点信息。');
  if(state.profile.instance_id!==state.settings.instance)throw new Error('请先在本页“修改本次模板配置”中选择分组并保存。');
  const data=currentImportForm();
  if(!data.text.trim())throw new Error('先填入账号材料或上传TXT/JSON文件。');
  if(!data.model_id)throw new Error('请填写本页右侧的验号模型 ID。');
  if(!data.confirm_deploy)throw new Error('请勾选本页右侧的服务器写入及上线确认。');
  const signature=JSON.stringify(data);
  if(!inlineSubmit||inlineSubmit.signature!==signature)inlineSubmit={signature,request_id:crypto.randomUUID()};
  importSubmitBusy=true;importBatchRequest++;
  const original=$('import-deploy-btn').textContent;$('import-deploy-btn').textContent='正在保存并提交服务器任务…';
  try{
    importBatch=await api('import/deploy',{...data,request_id:inlineSubmit.request_id});renderImportBatch();
    $('import-history').replaceChildren(new Option('本次导入',importBatch.id));delete $('import-history').dataset.key;
    const failed=importBatch.items.filter(r=>['failed','unknown','review'].includes(r.state));
    if(!failed.length&&$('materials').value===data.text){$('materials').value='';inlineSubmit=null;}
    $('import-result').hidden=false;$('import-result').textContent=failed.length?'部分条目未能导入；材料已保留，原因见下方本批进度。':'本批已处理，服务器任务会在后台自动执行；请看下方进度，无需切页勾选。';
    await refresh();
  }catch(e){$('import-result').hidden=false;$('import-result').textContent=e.message+'\n再次提交相同内容会核对原提交，不重复建立任务。';}
  finally{importSubmitBusy=false;$('import-deploy-btn').textContent=original;}
});
$('import-batch-refresh').onclick=()=>action($('import-batch-refresh'),loadImportBatch);
$('import-history').onchange=()=>loadImportBatch().catch(e=>toast(e.message));
$('import-batch-deploy').onclick=()=>action($('import-batch-deploy'),async()=>{
  if(!importBatch)return;
  if(connectionDirty())throw new Error('站点设置尚未保存，请先保存。');
  const {text:unused,...data}=currentImportForm();
  if(!data.model_id)throw new Error('请填写上方验号模型 ID。');
  if(!data.confirm_deploy)throw new Error('请勾选上方服务器写入及上线确认。');
  importBatchRequest++;
  importBatch=await api(`import/batch/${importBatch.id}/deploy`,data);renderImportBatch();
  await refresh();
});
function resetImportFormat(){
  const isJSON=importMode()==='sub2';$('import-result').hidden=true;$('sub2-update').checked=false;
  $('sub2-update-wrap').hidden=!isJSON;$('material-file').accept=isJSON?'.json,application/json':'.txt,text/plain';
  $('material-file-label').textContent=isJSON?'导入 JSON':'导入 TXT';
  $('materials-label').textContent=isJSON?'粘贴 sub2api JSON':'账号----密码----2FA种子';
  $('materials').placeholder=isJSON?'支持 sub2api-data / sub2api-bundle、单账号对象和 NVT account_json 包装':'account@example.com----password----BASE32_TOTP_SECRET';
  $('import-format-help').textContent=isJSON?'Token直接导入，不需要密码和2FA。目前支持OpenAI OAuth；其他类型逐项报错。':'三段材料可用于后续 NVT 自动重新授权。';
  $('material-help').textContent=isJSON?'单文件可含多个账号，可选择多个JSON；合计不超过2 MiB。':'第三段为种子，不是六位动态验证码。密码保持原样。';
}
$('import-format').onchange=resetImportFormat;
$('sample-btn').onclick=()=>{
 if(importMode()==='sub2'){$('materials').value=JSON.stringify({type:'sub2api-data',version:1,proxies:[],accounts:[{name:'虚构示例',platform:'openai',type:'oauth',credentials:{email:'json-demo@example.invalid',chatgpt_account_id:'synthetic-workspace',chatgpt_user_id:'synthetic-user',access_token:'SYNTHETIC_ACCESS',refresh_token:'SYNTHETIC_REFRESH',client_id:'synthetic-client',expires_at:'2099-01-01T00:00:00Z'}}]},null,2);}
 else{$('materials').value='demo-one@example.invalid----DEMO-password-one----JBSWY3DPEHPK3PXP\ndemo-two@example.invalid----DEMO-password-two----JBSWY3DPEHPK3PXP';}
 $('import-result').hidden=true;
};
$('material-file').onchange=()=>action(null,async()=>{
 const files=[...$('material-file').files];if(!files.length)return;
 if(files.reduce((sum,f)=>sum+f.size,0)>2*1024*1024)throw new Error('所选文件合计不得超过 2 MiB。');
 const texts=[];for(const file of files)texts.push((await file.text()).replace(/^\uFEFF/,''));
 if(importMode()==='sub2'){
  try{const docs=texts.map(t=>JSON.parse(t));$('materials').value=JSON.stringify(docs.length===1?docs[0]:docs);}
  catch{throw new Error('所选文件中有无效JSON，未加载；请检查格式。');}
 }else{$('materials').value=texts.join('\n');}
 $('material-file').value='';$('import-result').hidden=true;
});
$('preview-btn').onclick=()=>action($('preview-btn'),async()=>{
 const r=await api('import/preview',{text:$('materials').value,format:importMode()});
 const dup=r.duplicate_indices||r.duplicate_lines||[];
 $('import-result').textContent=`可保存 ${r.accepted} 项 · 重复 ${dup.length} 项 · 错误 ${r.errors.length} 项`+
 (r.errors.length?'\n'+r.errors.map(e=>`第 ${e.index||e.line} 项：${errorText(e.code)}`).join('\n'):'\n校验通过。尚未保存或发送任何材料。')+
 (r.ignored_proxies?`\n文件内 ${r.ignored_proxies} 个代理仅作来源资料，不自动导入；部署使用已配置模板。`:'');
 $('import-result').hidden=false;
});
$('import-btn').onclick=()=>action($('import-btn'),async()=>{
 const original=$('materials').value;importBatchRequest++;
 const r=await api('import',{text:original,profile:state.profile,format:importMode(),update_credentials:$('sub2-update').checked});
 if(r.batch){importBatch=r.batch;$('import-history').replaceChildren(new Option('本次导入',r.batch.id));delete $('import-history').dataset.key;renderImportBatch();}
 if(r.format==='sub2'){
  $('import-result').textContent=`新增 ${r.added} · 更新凭据 ${r.updated} · 重复 ${r.duplicates} · 失败/冲突 ${r.failed}`+
   (r.failed?'\n'+r.results.filter(i=>['failed','conflict'].includes(i.state)).map(i=>`第 ${i.index} 项：${errorText(i.code)}`).join('\n'):'\n已保存有效Token，可直接选择“导入服务器并上线”。');
  if(!r.failed&&$('materials').value===original)$('materials').value='';
 }else{
  if(!r.conflict_lines.length&&$('materials').value===original)$('materials').value='';$('import-result').textContent=`已加密保存 ${r.added} 个账号；补充登录材料 ${(r.supplemented_lines||[]).length} 个。\n已存在 ${r.duplicate_lines.length} 行；材料冲突 ${r.conflict_lines.length} 行（未覆盖）。`+(r.conflict_lines.length?'\n冲突行号：'+r.conflict_lines.join(', '):'');
 }
 $('import-result').textContent+='\n账号明细就在下方。本批可直接上传，不必去号池重新找号。';
 $('import-result').hidden=false;await refresh();toast('本地导入结果已保留在下方；点击“将本批本地账号导入服务器”继续。');
});
$('connection-form').onsubmit=event=>{event.preventDefault();action(event.submitter,async()=>{await api('settings',{sub2api_url:$('sub2-url').value.trim(),admin_key:$('admin-key').value.trim(),nvt_cookie:$('nvt-cookie').value.trim(),oauth_client_id:$('oauth-client-id').value.trim(),clear_cookie:$('clear-cookie').checked,clear_admin_key:$('clear-admin').checked});$('admin-key').value='';$('nvt-cookie').value='';$('clear-cookie').checked=false;$('clear-admin').checked=false;await refresh('connection');toast('连接配置已加密保存；右侧未保存的模板表单不会被清空。');});};
$('profile-form').onsubmit=event=>{event.preventDefault();action(event.submitter,async()=>{if(!choices||connectionDirty())throw new Error('请先保存站点并加载下拉选项。');const p={...state.profile,instance_id:choices.instance_id,connection_revision:choices.connection_revision,profile_id:$('profile-name').value.trim(),revision:state.profile.revision+1,staging_group_id:Number($('staging-group').value),target_group_ids:targetIds(),proxy_id:$('proxy-id').value?Number($('proxy-id').value):null,concurrency:Number($('concurrency').value),priority:Number($('priority').value),rate_multiplier:Number($('rate').value),fingerprint_mode:$('fingerprint').value,account_expires_at:$('account-expiry').value?Math.floor(new Date($('account-expiry').value).getTime()/1000):null,auto_pause_on_expired:$('auto-pause').checked};await api('profile',p);await refresh();toast('新模板版本已保存，仅影响之后导入的账号。');}).finally(updateChoiceSelection);};
$('reload-choices').onclick=loadChoices;
$('staging-group').onchange=()=>{const id=$('staging-group').value;for(const box of $('target-group-options').querySelectorAll('input'))if(box.value===id)box.checked=false;updateChoiceSelection();};
$('proxy-id').onchange=updateChoiceSelection;
for(const id of ['sub2-url','admin-key','clear-admin'])$(id).addEventListener('input',()=>{if(connectionDirty())clearChoices('连接信息已修改，保存后将读取新站点选项。');});
$('target-groups-summary').onclick=e=>{if(!choices)e.preventDefault();};
document.addEventListener('click',e=>{if(!$('target-groups').contains(e.target))$('target-groups').open=false;});
document.addEventListener('click',e=>{for(const d of document.querySelectorAll('.row-more[open],.bulk-more[open]'))if(!d.contains(e.target))d.open=false;});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){for(const d of document.querySelectorAll('.row-more[open],.bulk-more[open]'))d.open=false;}});
$('cancel-queued').onclick=()=>action($('cancel-queued'),async()=>{await api('jobs/cancel',{});await refresh();toast('排队中的任务已取消；进行中的请求不会中断。');});
setInterval(()=>{if(unlocked&&document.visibilityState==='visible'&&!foregroundActions&&!refreshPending&&!document.querySelector('dialog[open]'))refresh().catch(e=>{$('refresh-time').textContent='刷新失败：'+e.message;});},4000);
boot();

function renderRetirementChoices(){
  const select=$('retirement-group'),cfg=state?.retirement?.config||{};
  const catalog=choices?.instance_id===state?.settings?.instance?choices:null;
  if(!catalog)return;
  const key=JSON.stringify([catalog.instance_id,catalog.groups]);if(select.dataset.catalog===key)return;
  const chosen=select.value||String(cfg.group_id||'');
  select.replaceChildren(new Option('自动匹配唯一名称“测试组”',''),...catalog.groups.map(g=>new Option(`${g.name} · #${g.id}`,String(g.id))));
  if(chosen&&!catalog.groups.some(g=>String(g.id)===chosen)){const missing=new Option('不可用 · #'+chosen,chosen);missing.disabled=true;select.add(missing);}
  select.value=chosen;select.dataset.catalog=key;
}
function renderRetirement(fill=false){
  const value=state?.retirement;if(!value)return;
  renderRetirementChoices();
  if(fill){$('retirement-enabled').checked=value.config.enabled;$('retirement-group').value=String(value.config.group_id||'');}
  const r=value.runtime||{};
  const retired=state.accounts.filter(a=>a.status==='retired').length;
  $('retirement-runtime').textContent=`已清理 ${retired} 个 · 清理失败/待核对 ${(r.counts?.failed||0)+(r.counts?.unknown||0)} 个。`+(r.code?errorText(r.code):value.config.enabled?'检测到这类终止修复账号后自动移入测试组，不再登录。':'自动清理已关闭。');
}
$('retirement-save').onclick=()=>action($('retirement-save'),async()=>{
  if(connectionDirty())throw new Error('请先保存站点连接。');
  const enabled=$('retirement-enabled').checked,revision=state.settings.cloud_revision;
  const group_id=$('retirement-group').value?Number($('retirement-group').value):null;
  if(enabled){const yes=await confirmDialog('启用测试组清理','符合团队workspace回退错误的账号（含已有失败记录）将停止调度、移出原生产组并退出监控。目标：'+($('retirement-group').selectedOptions[0]?.textContent||'自动匹配唯一测试组'),{checkbox:'确认执行以上分组与调度变更。'});if(!yes)return;}
  if(revision!==state.settings.cloud_revision)throw new Error('站点已变化，请重新确认。');
  state.retirement=await api('retirement/config',{enabled,group_id,confirm_transfer:enabled,connection_revision:revision});
  renderRetirement(true);toast('团队失效账号清理规则已保存。');
});
$('retirement-check').onclick=()=>action($('retirement-check'),async()=>{state.retirement=await api('retirement/check',{});await refresh();renderRetirement();});
