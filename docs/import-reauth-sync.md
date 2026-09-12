> 历史设计提案：含尚未实现的架构/策略设想，不代表当前功能或默认设置。当前行为以根目录 README 和对应实现文档为准。

# 导入、自动重授权与云端对账：统一生命周期

## 1. 决策：本地控制账本 + 云端运行事实，不做双向完整复制

这里的「本地」指 Sub2Easy 部署主机上的持久化数据库，不要求运行在站长电脑上。需要 24 小时工作时，应部署在常在线服务器。

| 数据 | 权威来源 | 规则 |
| --- | --- | --- |
| 登录账号、密码、TOTP 种子、材料版本 | Sub2Easy 加密凭据库 | 不写入 sub2api 的 notes/extra，不进入操作日志或通知 |
| local_identity_id、账号绑定、托管归属、重授权任务 | Sub2Easy 账本 | 不以云端显示名称、批次行号或 access token 作主键 |
| 新号导入模板、模板版本、自动维护/通知策略 | Sub2Easy | 每批次冻结配置快照；改模板不自动改存量账号 |
| 云端账号 ID、实际分组、代理、指纹、并发、倍率 | sub2api | 导入完成后默认以云端实际值为准；模板仅是创建时默认值 |
| 用量、额度、冷却、调度状态、运行错误、模型调用结果 | sub2api | 本地保存带采集时间的观测，不把旧快照写回 |
| access/refresh token 的当前有效版本 | sub2api | 日常轮换由内置服务负责；本地只暂存重授权未提交/未确认版本，不能定时“恢复”旧 token |
| 待部署账号与尚未完成的授权材料 | Sub2Easy | 本地备用库存可有、云端可尚未创建；与生产可用量分开统计 |
| 人工停用/人工删除/外部修改 | 人工意图优先 | 停止自动恢复或报冲突；云端删除不能触发本地自动复活 |

**默认配置同步策略是 cloud-managed，而不是 periodic-overwrite。** 可以逐项启用 template-managed；只对显式选中的字段做三方对比：上次应用值 / 当前云端值 / 新模板值，不能整个 extra 覆盖。

为什么不完全以云端为准？云端不应该保存邮箱密码和 TOTP 材料，脱敏管理 API 也不会返回这些材料，所以无法独立完成你提供的重授权流程。

为什么不复制一份完整号池再双向同步？云端会自行刷新 Token、变更限流、接收人工编辑；双向复制会造成旧 Token 回写、人工停用被覆盖、账号重复创建以及指纹变化。

## 2. 账号关系：材料、身份、云端实例分开

```text
login_identity（稳定 local_identity_id）
  ├─ login_material_versions（密码/TOTP 密文，版本 1、2…）
  ├─ authorizations（初次/重授权任务，generation 1、2…）
  └─ deployments（部署绑定）
       ├─ instance_id + sub2api_account_id
       ├─ platform + issuer + provider_user_id + workspace/account_id
       ├─ profile_id + frozen_profile_revision
       └─ credential_owner_id / managed / manual_hold / last_observed_at
```

必要约束：

- 初次按 `platform + 规范化登录账号` 去重，创建随机 local_identity_id；取得授权后补齐稳定平台身份和 workspace 标识，不改本地主键。
- 同一邮箱可能有多个 workspace、组织或账号上下文。首次材料尚未选择 workspace 时，进入待确认或依据显式配置选择；不能仅凭邮箱覆盖旧云端账号。
- `(instance_id, sub2api_account_id)` 唯一绑定；同一云端账号不能同时被两份材料自动控制。
- 默认同一凭据所有者在同一实例只允许一个主部署；影子账号关联到母账号，不独立保存登录材料、也不独立重授权。
- 重授权互斥范围是 **credential_owner / login identity**，不是单个 cloud ID。多实例共享凭据时仍需串行，避免多个 refresh token 链互相覆盖。第一期建议不同时把同一登录会话部署到多个实例。
- 每个部署有 generation，重授权 job 固定 login_material_revision + generation + deployment_id；回调只能应用到对应任务的那一代。
- 重复回调复用已有结果，旧任务迟到不能覆盖新凭据。平台身份不同不能通过“按照返回数组顺序配对”解决。

### 已有云端号怎么接管

先完整读取云端账号，然后按稳定平台身份匹配。导入三段材料后，若只有邮箱匹配且恰好一个候选，显示拟绑定关系供确认，确认后再启用自动重授权；零候选可走新号导入，多候选进入冲突列表。

只有云端账号、没有登录材料：保留观察、内置刷新、故障通知能力，状态标为 `LOGIN_MATERIAL_MISSING`，不假装可自动重新登录。

## 3. 一行一个：账号----密码----2FA种子

示意格式（仅虚构材料）：

```text
demo-one@example.invalid----DEMO-password-one----BASE32_TOTP_SECRET
demo-two@example.invalid----DEMO-password-two----BASE32_TOTP_SECRET
```

解析规则已实现：

1. UTF-8，支持 BOM、LF/CRLF、空行。一行一份材料。
2. 第一个 `----` 前为账号，最后一个 `----` 后为 TOTP 种子，中间全部为密码；因此密码中可包含 `----`。
3. 密码原样保留，不 strip、不自动反转义。账号字段接受 Markdown 的 `\@`；只对账号与种子边缘空格做清理。
4. 账号按 OAuth 登录口径大小写归一化，不擅自去掉点号或 `+tag`，不跨邮件域合并别名。
5. 第三段是 Base32 TOTP 共享种子，**不是当前 6 位验证码**。规范化大小写、校验 Base32 编码与 padding。
6. 批内相同账号且材料相同：去重。相同账号但密码/种子不同：所有冲突行暂停，不静默“最后一行覆盖”。
7. 库中已存在但材料有变更：未来写入流程需显式选择“更新登录材料”，新增版本而非新建云端账号；解析器本身不做数据库写入。
8. 预览只返回行号、数量、错误码，不返回输入行、密码或种子。后续 Web 表格可以按 local_id 显示脱敏账号，但不把整份材料 JSON 序列化到页面。

本次 `sub2easy.intake` 只解析，不保存、不联网、不产生动态 TOTP。实际接入重授权接口时，根据接口合同明确它需要 `totp_secret` 还是当下 OTP；不能擅自把一种当另一种。若需要 OTP，由执行 Worker 在请求前即时生成并检查时间同步，不把验证码提前排队保存。

登录材料确需长期保留用于后续 401 修复：部署时用成熟 AEAD（例如 AES-GCM）加密，AAD 绑定 local_id/材料版本/用途；索引用独立密钥的 HMAC，不存密码或 TOTP 的裸哈希；密钥使用 secret 挂载，与数据库备份分离。目前凭据库尚未实现，所以解析工具明确不落盘。

## 4. 批量导入模板

每次导入先选实例和版本化模板，再粘贴材料或上传文件。导入预览显示本批统一配置，允许在提交前针对单行显式覆盖；冻结后任务重试继续使用原快照。

已经定义的 OpenAI OAuth 配置字段：

| 模板字段 | 写入 sub2api | 备注 |
| --- | --- | --- |
| staging_group_id | 创建时 group_ids | 显式隔离组，不能等于生产组，路由隔离需验证 |
| target_group_ids | 验号通过后更新 group_ids | 不使用授权返回 JSON 中的分组 |
| proxy_id | proxy_id | null 表示新建时直连；要在表单明确显示，不默默改变已有号代理 |
| concurrency | concurrency | 创建时默认，重授权不覆盖 |
| priority | priority | 不使用供应接口返回的默认值 |
| rate_multiplier | rate_multiplier | 不属于凭据内容 |
| fingerprint_mode | extra.codex_fingerprint_mode | off / device / session / full |
| account_expires_at | 顶层 expires_at | 账号运营到期时间，不是 Token 过期时间 |
| auto_pause_on_expired | auto_pause_on_expired | 与账号运营到期配套 |

后续表单补齐：验证模型、模型映射、备用/直接上线模式、首次授权接口配置、账号命名模板、隐私选项、窗口限制、重授权预算及通知策略。不同平台显示不同选项，不能把 OpenAI 指纹字段无差别加到其他账号类型。

### 指纹收敛的持久关系

源码已核实：

- `off`：关闭；当前上游默认。
- `device`：设备级收敛。
- `session`：设备与会话收敛，线程按客户端会话派生。
- `full`：设备、会话、线程全部收敛。

模板是**选择模式**，不是“全池共用一份指纹种子”。上游 `codex_fingerprint_seed` 按账号生成并维护；不要导入外部 seed，不自行重置，不给所有账号写同一个 seed。

**重授权不重建 cloud ID、不改 extra，因此不主动改变现有指纹模式和种子。** 不能因为供应接口返回的新 sub2json 默认 `full`，就把原账号 `device` 改掉。默认 off，开哪档由模板显式选，不能声称越强越稳定。

与 TLS 指纹功能是两套配置，表单不能合并成含义不清的“启用指纹”开关。

源码证据：`backend/internal/service/openai_codex_fingerprint.go`、`frontend/src/i18n/locales/zh/admin/accounts.ts`、`frontend/src/components/account/CreateAccountModal.vue`，均基于研究提交 `98d86915becae9fe9491a91ffc6defd5235c8d2b`。

## 5. 初次授权和重授权，共用接口但走不同落库路径

```text
三段材料 → 本地身份/材料版本 → 授权任务 → 重授权服务 → sub2json
                                                    │
                              解析 + 身份/任务/代次匹配 + 凭据校验
                                      │                      │
                               没有云端绑定               已有云端绑定
                                      │                      │
                              创建到 staging           更新原账号凭据
                                      │                      │
                              记录返回 cloud ID         保留原 cloud ID/配置
                                      └──────── 验证 ────────┘
                                                   │
                                      上线或按原托管意图恢复
```

首次能否调用你已有的“重授权接口”取决于其合同：若同样支持仅传登录材料完成授权，就共用；如果要求旧 session/job ID，则需初次授权端点，不能凭接口名字假定。

### sub2json 不是“直接全部导入”的指令

- 当前合约解析器支持单个 `platform/type/credentials` 对象，或 `type=sub2api-data/sub2api-bundle, version=1, accounts=[一个账号]`。
- 外层 `data/sub2json/result` 包装、JSON 字符串、异步 task_id 等待实际接口脱敏样例后做显式适配，不层层猜测字段。
- 新授权结果只允许 Token 与身份元数据进入授权层；外部 JSON 的账号 ID、名字、分组、代理、倍率、extra、指纹 seed、base_url、model_mapping 均不具有配置权威。
- 首期要求 access_token、refresh_token、credentials.expires_at、email、chatgpt_account_id、client_id。client_id 与 refresh token 配对，不能随意保留旧 OAuth 客户端 ID；若接口不返回它，必须由明确的连接器配置补齐，不能猜。
- 若已经绑定 chatgpt_user_id，也要求本次返回并一致。workspace 变更进入身份冲突流程，而不是覆盖旧账号。
- Token 到期与顶层账号到期分别处理；明显过期/临界到期、空值、脱敏占位值都不能写入。
- 解析器仅做结构和身份字段比对，不是 JWT 签名校验。真正 Worker 还需要将可信 HTTPS 响应关联到 job，并通过 provider 身份验证/经验证的令牌声明确认身份；只解码未验签 JWT 不能宣称身份已认证。

## 6. 401 的自动修复流程

先区分三个 401：

1. **业务上游账号 401**：才可能触发对应登录身份重授权。
2. **sub2api 管理接口 401**：管理员 Key 失效，暂停实例写入、报接入故障，绝不全池重登。
3. **重授权接口自身 401**：连接器密钥失效，暂停该连接器。若接口用 HTTP 401 同时表达登录材料错误，需按其结构化错误码进一步区分，不靠状态码猜。

账号重授权流水线：

```text
AUTH_SUSPECT
  → 去重事件、排除旧观测、读回当前状态
  → 等待 sub2api 内置 refresh 的实际结果（不是轮询一次就假定失败）
  → refresh 仍失败 / 明确撤销：QUIESCING
  → REAUTH_QUEUED → REAUTH_RUNNING → RESULT_VALIDATED
  → APPLYING_TO_ORIGINAL_ID → VERIFYING → RECOVERED
```

具体约束：

- 托管号才可自动处理，人工停用/冻结不能被越过；未托管号只通知。
- 同一凭据所有者同一时间一个任务；每份事件 evidence_id 只消费一次。同一日志反复被拉取不能重复入队。
- 暂停原账号调度并记录自动化持有的隔离意图。已有在途请求和后台 refresh 仍可能进行，暂停调度不等于停止所有凭据写入；用上游可用的锁/条件写入协调，无法保证时在应用前后读回、观察并保留冲突待办。
- 从**指定材料版本**解密密码/TOTP，调用配置的重授权服务。材料更新后，旧 generation 的结果不再应用。
- 初始预算提案：同一身份 30 分钟最多 2 次完整重授权。超时先查任务结果，不盲目重复登录；错误密码、TOTP 拒绝、账号停用、明确交互要求等分类停止/待办。
- 默认支持异步：提交获得 provider_task_id 后轮询，或者接签名回调。若接口是同步响应则走同步适配器，二者都保留本地 job_id。
- 回调需要 HMAC/签名、时间窗口、重放保护，按 job_id/provider_task_id/generation 匹配，不能相信仅传来的 email 或 cloud ID。
- 成功返回 sub2json 后，严格验证对应身份再更新原账号；**不走 `/accounts/data` 导入，更不删除旧号再创建**。
- 应用成功不代表业务已恢复；读回和目标模型测试通过才恢复自动化持有的调度开关。恢复期间人工再次暂停，则放弃自动启用。

### 原账号凭据更新：保住模型映射和指纹

使用：`POST /api/v1/admin/accounts/{original_id}/apply-oauth-credentials`。

本次源码进一步确认，`MergePreservingSensitiveCreds` 的实际行为是：

- 未提交的**敏感**字段从旧值保留。
- **非敏感** credentials 字段则由 incoming 完全决定。

因此请求构建器先读最新云端 credentials 元数据作为底，再合并新授权白名单字段，保住 `model_mapping`、已有 `base_url` 等；不从本地旧 Token 快照复制敏感值，也不接受授权接口修改这些业务字段。

请求仅带 `type + credentials`，不携带顶层分组/代理/并发/倍率/有效期，也不携带 extra；现有指纹模式、配额配置因此不被主动覆盖。

另一个修正：尽管上游注释提到“原子落库”，当前 handler 的凭据更新、extra merge、ClearError、缓存失效是分步骤执行，部分失败仅记录日志。**不能把它当成跨步骤 all-or-nothing 事务**，更不能把 HTTP 200 等同于清错、缓存失效、可调度全部成功。读回与业务验证必不可少。

当前无完整 CAS/generation 检查的远端 API，所以本地租约不能从数学上消除与上游后台 refresh 的竞态。生产执行器需要核对实际版本的共享锁能力，必要时补一个“指定账号代次条件应用”的上游接口；未实现前不宣传强一致/完全无人值守。

## 7. 对账监听具体做什么

初始每 60 秒抖动采集，成功收齐完整快照才做差异；观测标注采集时间和证据有效期。

| 差异 | 默认动作 |
| --- | --- |
| 本地新库存，云端无账号 | 只由明确的导入/补位任务创建，不因一次对账自动全量上传 |
| 已绑定账号短暂不在列表 | 核对筛选范围，GET 对应 ID 确认；网络失败不是删除 |
| 已绑定账号明确 404 | REMOTE_MISSING / tombstone；暂停重授权，不自动补建同一账号 |
| 云端多出账号 | DISCOVERED，只观察；没有登录材料不自动接管 |
| 云端账号人工 inactive/停调度 | MANUAL_HOLD 或冲突，不能自动纠正 |
| 云端分组/代理/指纹/并发改变 | cloud-managed 接受为现状；template-managed 产生变更冲突，不循环覆盖 |
| 云端 Token 轮换 | 正常现象，本地不做 Token 相等性对账、不回写旧版本 |
| 同一平台身份出现多个云端主账号 | DUPLICATE_BINDING，暂停自动改凭据并核对 |
| 旧任务迟到、旧材料修订回调 | STALE_RESULT，记审计但不应用 |
| 同一批账号同时 401 | 先排除接入故障、代理/平台公共故障，限制平台级重登并发 |

字段差异仅用于发现漂移，自动写操作由有审计、租约、版本条件的任务执行，不能在采集循环里直接调用修改 API。

## 8. 批量导入页面应怎样呈现

1. **选择目标与配置**：实例、平台、模板版本、生产组/隔离组、代理、并发、倍率、指纹模式。
2. **输入材料**：一行一个三段格式；预览新增/已存在/材料更新/重复/错误/绑定冲突数量。
3. **提交批次**：明确选择新建部署、仅补充登录材料、更新已有材料；不能把这三个动作混成“覆盖导入”。
4. **逐号进度**：待授权 → 授权中 → 身份校验 → 云端写入 → 验号 → 上架；失败显示阶段和脱敏错误码。
5. **长期维护**：同一行对应的 local_id 可以追溯到 cloud ID、材料修订、模板版本、最近重授权；不用翻原始文本找密码。

## 9. 本次实现与仍待接入项

已实现并可离线验证：

- 三段文本解析、Base32 校验、批内去重/冲突、脱敏预览 CLI。
- 版本化导入模板模型及校验，OpenAI OAuth 指纹四档映射。
- sub2json 结构/账号身份校验、授权字段白名单。
- 首次创建与原 ID 重授权的不同请求构建路径；保留云端非敏感 credentials 配置，不带 extra。
- 三种 401 来源分流、托管/人工冻结/任务进行中/预算门控的纯策略函数。
- 显式配置字段的三方差异判断。

尚未实现：加密数据库、持久化身份绑定和互斥租约、真正重授权接口 HTTP 调用/回调、远端写入执行器、定时监听与 Web 页面。当前请求构建器返回计划及前置条件，不执行网络操作。

下一项必要输入：你的重授权接口**脱敏请求和成功/失败响应样例**，说明同步返回还是 task_id/回调，以及 2FA 字段接收种子还是 OTP。真实密码、Token、种子不放在样例里。
