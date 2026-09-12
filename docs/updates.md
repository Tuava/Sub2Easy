# 更新系统（v0.7.0）

## 界面检查

“连接与模板 → 版本与更新 → 检查更新”读取公开 GitHub main 提交及该提交的版本。优先访问 `api.github.com`；限流/连通失败时回退到官方 GitHub Git远端及该提交的 `raw.githubusercontent.com` 元数据，不要求Token。不会附带 NVT Cookie、sub2api Key 或账号材料。同一进程缓存60秒，避免连点触发限流。

当前没有正式Release自动升级渠道；main可能包含尚未发布的变化。界面显示当前版本、远端版本、提交链接及安装方式。**不会后台自动下载或安装**。存在新提交时，git安装展示包含本次确认SHA、数据目录的终端更新命令。

## git安装更新

```bash
uv run --locked sub2easy-update
# 在任务中心暂停领取新任务，等在途任务结束，停止后端
uv run --locked sub2easy-update --apply --yes --data-dir ./data
uv run --locked sub2easy --data-dir ./data --reuse-session
```

- 必须提供与当前服务相同的 `--data-dir`；未停止该目录服务会拒绝更新，关闭网页不算停止。
- 只接受官方 `Tuava/Sub2Easy` origin、main分支、干净工作区。不覆盖本地修改/未跟踪文件、不自动stash、拒绝分叉及版本倒退。
- 数据库使用SQLite backup API备份到 `data-dir/backups/before-update-.../vault.sqlite3`，包含WAL已提交内容；不需要主密码，不解密。
- 固定检查的提交，只允许 `git merge --ff-only`，再 `uv sync --locked`。网络错误不会清空数据。
- 依赖同步失败时尝试回滚源码提交并恢复旧依赖；回滚失败显示 `UPDATE_ROLLBACK_NEEDS_REVIEW`，应人工检查，禁止继续强制覆盖。
- 不自动恢复数据库、不执行降级迁移。备份包含敏感密文及运行元数据，不能上传公开仓库。
- 更新命令不会替你重启后台或自动解锁；重启后以 `/api/status` 的版本为准。保留 CK 配置与账号库，不等于 CK 永远有效。

`--expected-commit SHA` 可锁定本次界面确认的提交；期间main变化时停止，要求重新检查。

## ZIP / wheel / fork

- ZIP/wheel安装可检查更新，但没有自动Git替换；备份后安装新wheel或另建git clone，继续指定原数据目录。
- fork、自定义分支或有本地修改的工作区请自行合并；不要更改origin来欺骗检查。
- 本版不提供远端执行更新命令的HTTP API，也不会从更新说明中提取脚本执行。

## 首次从 v0.6 升级

旧版尚无 `sub2easy-update` 命令。停服务并备份data后，在干净的官方main checkout执行一次：

```bash
git pull --ff-only
uv sync --locked
uv run --locked sub2easy --data-dir ./data --reuse-session
```

之后即可使用新版本内置更新命令。部署AI还须按 `AGENTS.md` 主动提醒并核对 NV站 `scm_session` CK。
