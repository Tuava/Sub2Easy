# Sub2Easy

**面向 sub2api 的本地账号运营工作台。** 在一个页面完成账号材料导入、绑定、部署、验号和进度跟踪，并提供可选的 401 恢复与团队失效账号清理。

Python + FastAPI + 原生 HTML/JS；没有前端构建步骤、云端数据库直写或外部 CDN。本项目为独立工具，不隶属于 sub2api、OpenAI 或授权服务提供商。

> **当前版本：0.7.0，开源候选版。** 支持 macOS / Linux、Python 3.11+。Windows 原生尚不支持（使用 POSIX 文件锁），请勿把它标成跨平台桌面 App。它是本机浏览器 GUI，不是 Electron 应用。

## 能做什么

- **同页导入**：TXT `账号----密码----2FA种子`，或 OpenAI OAuth sub2api JSON；完整邮箱显示、历史批次、部分失败明细、直接导入服务器。
- **模板与下拉选择**：隔离组、生产组、代理、并发、优先级、计费倍率及 Codex 指纹模式。新账号与已有绑定账号分别处理。
- **部署闭环**：检查身份 → 复用/获取授权 → 原 ID 更新或隔离创建 → SSE 模型测试 → 生产分组 → 启用 → 最终读回确认。
- **账号绑定**：读取全站账号；唯一身份匹配自动绑定，多个候选人工选择，不靠名称/时间强行覆盖身份。
- **并行任务池**：默认 3 个账号、2 个授权请求，可分别设置 1–8；同账号/同登录身份互斥，监控使用独立线程。
- **可选 401 恢复**：先等 sub2api 原生刷新，持续 401 才重授权；测试通过后按设置恢复调度。人工停用、未知写入不自动强行恢复。
- **用量在列表里**：已保存的 5h/7d 窗口与本站今日统计；缺失显示未知，不发请求伪造“只读”余额。
- **可选终止修复清理**：仅对同用户个人免费 workspace 回退错误，停调度后移入明确选择的测试组，不用个人凭据覆盖团队账号。**默认关闭。**

## 交给 AI 部署

把仓库链接交给部署 AI，并要求它先阅读根目录 **[AGENTS.md](AGENTS.md)** 和 **[AI 部署手册](docs/ai-deployment.md)**。

**需要 NVT 重授权/自动401修复时，一定要配置 NV站的 CK：`scm_session` Cookie。** 部署AI必须主动提醒你在本地“连接与模板 → NVT Cookie”填写，不要把CK发到聊天、Issue或Git。只有sub2api管理员Key不够完成重授权。界面也会持续提示缺少CK/连接器暂停；已保存不代表会话仍有效。

## 版本更新

“连接与模板 → 版本与更新”可检查 GitHub main 的新版本/提交。不会静默安装，不发送账号信息。Git安装在暂停任务、停服务后运行：

```bash
uv run --locked sub2easy-update
uv run --locked sub2easy-update --apply --yes --data-dir ./data
uv run --locked sub2easy --data-dir ./data --reuse-session
```

自动备份SQLite、只快进官方main、同步锁定依赖；拒绝脏工作区/分叉，依赖失败尝试回滚。ZIP/wheel升级与旧v0.6首次升级见 [更新文档](docs/updates.md)。更新后还要核对 NV站CK 的配置状态。

## 快速开始

### 从源码运行（推荐）

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，下载/克隆仓库后在根目录执行：

```bash
uv sync --locked
uv run --locked sub2easy --data-dir ./data
```

macOS 也可双击 `start.command`（如无执行权限，先 `chmod +x start.command`）。启动器默认使用当前源码目录下的 `data/`，不会在仓库间自动共享凭据库。

### 从已构建 wheel 安装

本项目尚未声明已发布到 PyPI；使用经过检查的本地构建产物：

```bash
uv build
python3 -m venv .venv-app
.venv-app/bin/pip install dist/sub2easy-0.7.0-py3-none-any.whl
.venv-app/bin/sub2easy
```

未显式指定 `--data-dir` 时：macOS 使用 `~/Library/Application Support/Sub2Easy`，Linux 使用 `$XDG_DATA_HOME/sub2easy` 或 `~/.local/share/sub2easy`；已有源码 checkout 的 `data/vault.sqlite3` 会继续沿用。不会自动搬迁或删除旧数据。

### 首次设置

1. 使用自动打开的浏览器页面，设置并解锁主密码。
2. 在“连接与模板”填写 **sub2api Admin API 地址和管理员 Key**，读取分组/代理并保存模板。普通用户 Key 不能代替 Admin Key。
3. 有效 sub2api Token JSON 可直接部署，不需要 NVT。需要密码/2FA 重授权时，另外配置 NVT Cookie。
4. 在“批量导入”填写材料、确认模板及验号模型，点击“导入到服务器并上线”。主按钮会写云端；“仅保存到本地”不会。
5. 在本页查看每项阶段与云端 ID。HTTP 200、NVT 成功或已入队不代表上线成功。

## 默认行为与数据边界

| 操作 | 行为 |
|---|---|
| 初次启动 | 没有预置站点、Cookie、账号；401 监控关闭，测试组清理关闭 |
| 仅保存本地 | 材料加密保存，不发送给第三方，不写云端 |
| 导入服务器并上线 | 明确确认后执行云端写入及模型测试，可能产生用量 |
| 上线后自动监控 | 导入页默认勾选，可取消；随该批服务器写入确认生效，只托管这批账号 |
| NVT 重授权 | 将对应账号的邮箱、密码和**完整 TOTP 种子**发送给 `nvtokens.com`，不是仅发送六位验证码 |
| 团队失效清理 | 需在监控页选择目标/确认并开启；会移出生产组并停止调度 |
| 退出/锁库/休眠 | 停止处理；重启需再次解锁。关闭浏览器不等于退出后端 |

SQLite 中账号材料与连接配置使用 scrypt + AES-GCM 加密；任务状态、时间、ID 等运行元数据**不是全盘加密**。解锁后的界面显示完整邮箱；显式导出会生成明文 JSON。请阅读 [安全与隐私说明](SECURITY.md)。

浏览器会把启动 URL 的 `#token=...` 存入当前标签页会话后从地址栏移除，这是正常行为。**不要分享完整启动链接。** 会话失效时重新运行启动器；升级时可加 `--reuse-session` 保留当前私有本机会话（不会免除主密码解锁）。

## 限制

- 导入/授权类型目前只支持 **OpenAI OAuth**；JSON-only 账号没有密码/2FA 时无法完整重新登录。
- NVT 是可选但目前固定的第三方连接器，没有内置 Cookie，也不能保证其接口、套餐或团队 workspace 始终有效。
- 不保证任何 sub2api fork/版本均兼容。接口研究基线和依赖归属见 [第三方说明](THIRD_PARTY_NOTICES.md)。
- 上游无条件更新 API，客户端读前/读后检查无法消除全部并发修改窗口；结果未知保留人工核对，不盲目重放。
- 备用号补位、通知渠道、OS 自启动、主密码轮换、完整备份恢复 UI 尚未实现。
- 单次手动授权、导入、检查点重试不受历史次数限制；自动重登保留频率预算和停止条件，并不是无限循环登录。
- 当前自动测试是临时数据库、模拟上游和本地 HTTP 验证，不能据此保证生产账号恢复成功。

## 开发与验证

```bash
uv sync --locked
uv run --locked python -m unittest discover -s tests -v
npm ci --ignore-scripts
npm run test:ui
uv run --locked python scripts/release_check.py
uv build
uv run python scripts/release_check.py --archive dist/sub2easy-0.7.0-py3-none-any.whl --archive dist/sub2easy-0.7.0.tar.gz
uv run python scripts/export_source.py
```

Node.js 22+ 仅用于 UI 测试；运行应用不需要 npm。GitHub CI 配置覆盖 macOS/Linux、Python 3.11/3.13、DOM 测试、依赖审计及实际打包内容检查；**本地通过不代表远端 CI 已运行**。

## 文档

- [使用与排错](docs/gui.md) · [同页导入](docs/inline-import.md) · [sub2 JSON](docs/sub2-import.md)
- [部署状态机](docs/deployment.md) · [绑定](docs/binding.md) · [401 监控](docs/monitor.md)
- [并行队列](docs/parallel-tasks.md) · [团队失效清理](docs/team-lost-retirement.md)
- [开源发布清单](docs/open-source-release.md) · [贡献指南](CONTRIBUTING.md) · [更新日志](CHANGELOG.md)

## 许可证

[MIT](LICENSE)。第三方软件、API 和商标各自受原有条款约束；本许可证不重新许可它们。发布者应确认自己有权以该许可证发布贡献的代码。
