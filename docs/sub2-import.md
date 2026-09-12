# v0.3.1 · sub2api JSON 账号导入

## 怎么使用

在“批量导入”的**导入格式**中选择 `sub2api JSON（Token 账号）`，粘贴 JSON 或选择一个/多个 `.json` 文件，点击“校验并预览”，再“加密保存到本地”。文件总计最多 2 MiB，每批最多 1000 项。

随后在号池选择这些账号，点击“导入服务器并上线”。只要 Token 仍有效，整条部署链不需要账号密码、2FA 或 NVT Cookie，也不会额外登录。服务器创建/验号/移组/启用的行为仍按 `docs/deployment.md`。

## 支持结构

- `{"type":"sub2api-data","version":1,"accounts":[...]}`。
- `sub2api-bundle` v1。
- 当前上游省略 type/version 的 `{"accounts":[...]}`。
- 单个 `platform/type/credentials` 账号对象。
- 账号对象或上述 bundle 的数组，用于一次选取多文件。
- NVT `{"filename":"…","account_json":{…},"summary":{…}}`；也支持已有适配器的明确单路径 data/sub2json/sub2api 包装。summary 仍会核对，不会拆开分配给多个账号。

**当前部署适配器支持 OpenAI OAuth**。其他平台、API Key、service_account 等类型逐项报错，不假装通用 sub2 导入已支持它们。不能把任意客户端 auth.json 视为 sub2api 格式。

必需的凭据元数据与现有授权链一致：email、chatgpt_account_id、access_token、refresh_token、client_id、expires_at。缺少 expires_at 时允许从 access token 的 JWT exp 读取；这不是验签。缺少 client_id 可以使用连接设置中明确配置的 OAuth client_id，不猜测默认值。缺少 email 时不能用文件名伪造身份。

示例文件 `examples/sub2-accounts.json` 只有虚构 Token，用于校验格式，不能用来访问上游。

## 去重、更新与错误

- 批内同邮箱且同授权结果：去重；同邮箱不同 Token/workspace：冲突项全部暂停，不静默挑一个。
- 库中同邮箱、同凭据：重复跳过。
- 库中已有三段材料、还没有 Token：JSON 附加到原本地身份，不重新建本地号。
- 库中已有同身份但不同 Token：默认拒绝覆盖；勾选“更新本地同身份的已有凭据”后才替换，材料版本递增，cloud ID 和登录材料不变。
- 同邮箱不同 workspace/user ID：即使勾选更新也不覆盖。
- 在途任务、修复待办、部署记录或未知写入状态：不直接替换 Token，避免旧任务拿到新一代凭据造成错乱。
- 有效项逐项入库，失败项在页面统一显示序号和错误码；不会打印完整输入/Token。发生部分失败时保留文本，便于修改再提交；重复成功项会被去重。

## 与 2FA 材料的关联

JSON-only 账号保存 `login.account` 身份，但不捏造 password/totp_secret。页面显示“sub2 Token · 无密码/2FA”。它可以：

- 复用有效 Token 部署、绑定、验号；
- 加入云端状态观察；
- 让 sub2api 按其原有逻辑维护 OAuth refresh token。

但 NVT 的“完整重新登录”需要三段材料。没有材料的账号出现401时显示“缺少登录材料”，不会发送缺字段的请求、也不会因键缺失报内部错误。后续用 TXT 导入同邮箱的三段材料，会补充到同一本地 ID，保留 Token 和 cloud ID；已有不同密码/种子仍报冲突。已完成部署可补充登录材料，进行中的部署/恢复不允许更换。

## 配置与存储

导入只落本地加密库，不写云端。JSON 中的分组、代理、并发、指纹等配置不自动具有覆盖权；新号部署使用当前确认模板，旧云端号保留原配置。

只保存规范化 OAuth 凭据、身份、显示名称和导出时间；丢弃 password、TOTP、Cookie、私钥残留、代理认证资料及外部路径。代理清单会显示“已忽略数量”，不会自动创建代理。与三段材料共用带密钥 HMAC 身份索引和 AEAD 存储。

## 本轮验证

覆盖单对象/多账号/多文件、无 header bundle、部分错误、批内冲突、库中去重、显式更新、workspace不符、密文存储、先TXT后JSON、先JSON后TXT、在途保护、无材料401处理。

真实本地 HTTP 模拟测试直接从 JSON 导入入口开始，在没有密码/2FA/NVT Cookie 的条件下完成创建、停调度、SSE验号、移组、再次验号、启用和最终读回。不是用手工注入的本地授权对象代替 JSON 导入测试。未拿虚构 Token 或用户未选择的真实账号尝试生产部署。
