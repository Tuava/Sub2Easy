# 本地 GUI 使用与接口适配

## 启动

macOS 双击项目根目录的 `start.command`。或在项目根目录执行：

```bash
uv sync --locked
uv run --locked sub2easy --data-dir ./data
```

服务只绑定 `127.0.0.1:8765`，自动打开浏览器本地 GUI。不是 Electron/macOS 原生 App；账号、任务、界面后端均在本机运行，不需要额外部署前端。

无 uv 时：

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m sub2easy.gui
```

支持 `--port 8765 --data-dir /ABSOLUTE/DATA/DIRECTORY --no-browser`。数据目录有单进程文件锁，避免重复启动同一个凭据库。未指定目录的安装版使用用户数据目录，详见 README。

浏览器需使用启动器生成的完整地址，含仅存在于 URL fragment 的随机本机会话。页面打开后将 fragment 移除并把会话放在该标签页 sessionStorage。手动只打开根地址可能显示“本机会话失效”，此时使用数据目录 `launch-url.txt` 的完整地址。该文件仅保存随机本机 API 会话，不是远端 Cookie。

## 第一次使用

v0.3.1 在“批量导入”增加 **sub2api JSON** 格式切换：上传/粘贴包含OAuth Token的账号，无需密码或2FA即可部署；逐项错误和凭据更新确认见 `docs/sub2-import.md`。

v0.3.0 已有 **“导入服务器并上线”**：本地账号选中后可自动授权/复用、创建或更新、验号、移组和启用。见 `docs/deployment.md`。下文“授权 → 单号写入”是保留的高级分步模式，不是唯一导入方式。

1. 建立主密码（至少 10 字符），解锁本地库。主密码不写入配置文件。
2. **连接与模板**：粘贴 NVT `scm_session` 值；保存 sub2api 地址及管理员 Key；配置隔离组、生产组、代理、并发、倍率、指纹档位。
3. **批量导入**：粘贴/导入三段 TXT，先预览，再加密保存。每批冻结模板；保存时不联网。
4. **号池**：已有 sub2api 账号点“绑定”自动匹配，或多选后“批量自动绑定”。唯一身份匹配直接建立映射，重名/多workspace/其他冲突在“绑定处理中心”查看候选详情并选择；无需先记下原 ID。详见 `docs/binding.md`。
5. 选中账号点“授权”，确认将对应登录材料发送到 nvtokens.com，然后看任务队列。按任务中心并发配置执行，同身份互斥，手动授权无历史次数上限，自动重登保留预算。
6. 授权就绪后可导出结果，或手动确认“写入隔离组 / 更新凭据”。**写入后保持停止调度，不自动验号或上线。**

以上是手动授权流程。现在另有“账号监控”工作区：绑定原 cloud ID 后逐号启用托管，在监控页配置验证模型并开启全局开关，可自动修复持续401并验号恢复。见 `docs/monitor.md`；不会自动接管所有账号，也不会替新号自动选生产组。

### 分组与代理下拉选择

保存站点连接后，页面自动读取 `GET /api/v1/admin/groups/all?platform=openai` 和 `GET /api/v1/admin/proxies/all`，也可点“刷新选项”手动重读。解锁/重新打开页面时会重新读取，4 秒本地状态轮询不会持续请求远端列表。

- 隔离组单选，生产组勾选式多选，显示名称与 `#ID`；选择隔离组时取消同组的生产用途，避免重叠。
- 只提供 active OpenAI 分组；代理只提供 active 且未到期的记录，另有“直连 · 不指定代理”。不返回代理用户名、密码或主机地址。
- 没配置站点、加载中、加载失败时不可保存模板；旧模板中不存在的 ID 显示“不可用”，不偷偷改成别的组或直连。
- 修改站点地址、管理员 Key 或清除 Key 后立即清掉旧选项，保存配置后重新加载。新旧站点同号 ID 不会被自动当成同一个分组。
- 保存模板时后端再次读取可用列表并验证选项，模板保存绑定当前规范化站点地址；版本由后端递增。新导入账号冻结这一份模板，旧账号不自动改配置。
- GUI 不再将演示 9001/1001 当作可用默认选择；首次使用或旧版模板需要先从站点列表选择并保存模板再批量导入。

这些列表只读取元数据，不会自动创建分组、测试代理或修改云端账号。某些 sub2api simple 模式不支持这些分组接口时会提示连接读取失败，不伪造列表。

绑定账号发起重授权前，需在 sub2api 将其设为 `schedulable=false`，保留 active/error 状态。GUI 不擅自清除人工 inactive 状态。重授权仍可能影响其他共享该登录会话的部署，第一期不要多实例共用同一份会话自动轮换。

## NVT 请求合同

固定目标：`POST https://nvtokens.com/api/workspace/tools/account-reauthorize`。

```json
{
  "mailbox_credential": "ACCOUNT----PASSWORD----BASE32_TOTP_SECRET",
  "output_format": "sub2api"
}
```

只设置必要的 JSON/Accept、`Cookie: scm_session=...`、Origin、Referer 和工具 User-Agent。不会复制浏览器 sec-ch-ua 等字段。第三段完整种子交由此接口处理，不在本地生成一个 OTP 替代。Cookie 经 GUI 或本机配置 API 写入加密库；源码中没有预置真实 Cookie、邮箱、密码或种子。

HTTP 连接超时 15 秒，读超时 180 秒；默认最多同时发送2份，可在任务中心配置1–8。响应上限 4 MiB，不跟随重定向、不下载返回 URL、不解压任意压缩包、不信任附件文件名。工具不自动重试授权 POST。

已适配已知失败：

```json
{"error":"…","stage":"protocol_login","code":"INCORRECT_CODE"}
```

无论 HTTP 200 还是 4xx，只要是错误正文，就记失败，不记录原始 error 消息和 request_id。GUI 显示固定提示“验证码错误或失效”，不会据此断言一定是本地种子错误——验证码也可能是供应服务协议流程中的验证码，需要服务方诊断。

连接器 HTTP 401/403（或会话错误码）暂停队列，更新 Cookie 后可恢复；429 也暂停，防止继续逐号撞限流。网络失败属于结果未知，不直接重发。

## 成功响应：兼容范围与限制

支持：

- 正文为单账号 `platform/type/credentials` JSON。
- 正文为 `sub2api-data` / `sub2api-bundle` v1，accounts 数组恰好一个账号。
- HTTP `Content-Disposition: attachment`：按响应正文 JSON 解析，忽略文件名。
- 已按真实样本适配 `{filename, account_json, summary}`：`account_json` 为真正的 sub2api bundle，`filename` 不参与身份判断、不用作磁盘路径。
- 上述 JSON 位于显式 `account_json` / `data` / `sub2json` / `sub2api` 单一路径，或字段中是 JSON 字符串，最多展开三层；多路径同时存在则不猜测。

真实样本的 credentials 没有 expires_at。仅在该字段缺失且 access_token 为 JWT 结构时读取它的整数 exp；不使用 id_token 的 exp、不按 last_refresh 猜 TTL，也不覆盖已提供的 expires_at。读取未验签 JWT 的 exp 只是元数据解析，不是签名或身份认证。

NVT summary 若提供 identity_verified，必须为布尔 true；account_email、selected_workspace_id 须与账号凭据一致，已提供的 account_id/workspace_id/user_id 别名不能相互冲突。summary 的“验证成功”是供应服务声明，不等于本机完成独立验签。

显式导出时下载解包后的 account_json，不把外层 filename/summary 一起当作 sub2api 导入文件。原始响应仍在本地加密库保留供重检。

不支持的结构、多个账号、身份不一致、字段缺失进入 `review`。原结果 AEAD 加密保存，只允许用户手动导出或重检，不视为授权就绪、不自动写入云端。

若仅缺少 OAuth client_id，可以在设置中填**已知且匹配该授权服务的 client_id**，然后点该账号“重检”。重检已有结果不发网络请求。不要把管理员 Key 或随意猜测的 client_id 放进去。

响应格式通过虚构凭据回归测试覆盖；自动测试不向 NVT 发起真实登录。身份检查是邮箱/workspace/已有 user_id 一致性和凭据结构、有效期检查，不包含 provider JWT 验签/独立身份查询，GUI 不应被当作已验证的全自动生产授权代理。

## 云端写入

新号：确认 staging 路由隔离 → 查分组存在 → 创建时显式传隔离组 + 模板配置 + 幂等键 → 先保存返回 cloud ID → 停调度 → 读回。不会把生产组当创建组，也不会假装创建 API 支持初始 schedulable 参数。

已绑定号：读原 ID 和身份/停调度状态 → 合并最新云端非敏感 credentials 与新授权白名单字段 → `apply-oauth-credentials` → 读回仍停调度、身份一致。请求不包含 extra、分组、代理、倍率等字段，因此不主动覆盖指纹和调度配置。

云端写操作前先持久化意图，超时/异常进入 `write_unknown`，不允许再次创建或重新登录遮盖问题。必须先到 sub2api 核对；本版没有自动消除结果未知状态的对账修复按钮。创建接口内部可能部分成功，不依赖状态码臆测一定回滚。

写入成功后清理本地暂存的授权结果，当前 Token 权威仍留在 sub2api。GUI 标记“已写入·待验号”，不承诺测试、缓存失效或最终业务可用性。需在 sub2api 验号并移到目标组/恢复调度。

## 凭据保护与运维

- SQLite WAL，数据目录权限 0700、数据库 0600。
- 密码通过 scrypt 派生 AES-256-GCM 密钥；随机 nonce，AAD 绑定记录 ID/用途。
- 登录材料、账号邮箱、配置 Cookie/Admin Key、授权结果整体密文存储。去重索引使用带密钥 HMAC。
- 本地 API 要求随机会话 Header；校验 Host/Origin；无 CORS 放行；CSP 禁止外站资源和嵌入；响应 no-store。
- `/api/state` 不返回密码、TOTP、Cookie、管理员 Key 或授权 Token；导出是显式明文下载操作。
- 主密码仅在解锁时进入进程内存。锁库不承诺 Python 内存清零，防护范围不包括已控制本机用户权限的恶意程序。
- 主密码丢失无法恢复。先停止服务后备份整个数据目录；运行中备份必须使用 SQLite backup 方法，不能只复制主 DB 忽略 WAL。
- 退出时 queued 任务保留；running 任务下次启动标成 unknown，不自动重发。重新解锁后，未执行的 queued 任务在连接器配置有效时继续。
- 已知限制：尚无主密码轮换、材料版本编辑、自动补位、通知渠道与云端未知写入修复。新号可通过服务器导入任务自动上架；已有账号401监控可显式启用。

## 测试

```bash
uv run python -m unittest discover -s tests -v
node --check sub2easy/static/app.js
uv run python -m compileall -q sub2easy tests
```

所有自动测试使用虚构账号和 MockTransport；UI 验证使用独立临时凭据库，不调用真实 NVT 服务，也不动现有 sub2api 账号。
