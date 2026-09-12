# GitHub 发布前清单

当前工作目录可以包含运行数据，**不要直接上传整个文件夹，也不要上传旧 dist 包**。源码导出使用显式允许列表；公开前仍要人工审查。

## 1. 确认范围

- 项目名：Sub2Easy。许可证候选：MIT，发布者须确认贡献的权利和第三方来源。
- 当前不绑定 GitHub 用户、仓库地址、私人邮箱、站点或真实凭据。
- 这份准备工作不代表已经创建 GitHub 仓库、推送代码或发布 PyPI 包。
- 新安装清理到测试组默认关闭。原用户显式启用的配置保存在自己的加密库，不应随源码发布。

## 2. 本地验证

```bash
uv sync --locked
uv run python -m unittest discover -s tests -v
npm ci --ignore-scripts
npm run test:ui
npm audit --audit-level=high
uv run python scripts/release_check.py
uv build
uv run python scripts/release_check.py --archive dist/sub2easy-0.7.0-py3-none-any.whl --archive dist/sub2easy-0.7.0.tar.gz
uv run python scripts/export_source.py
```

`export_source.py` 生成 `dist/sub2easy-0.7.0-github-source.zip` 及 SHA256 校验文件。仅包括源码、虚构示例、测试、文档、CI 和许可；不复制 data、数据库、Cookie、运行环境、缓存或旧构建包。同名产物已存在时会拒绝覆盖，请指定新的 `--output`。

## 3. 在干净目录发布

解压源码 ZIP 到新目录，不在正在运营的源码目录里 `git add .`。

```bash
cd Sub2Easy-0.7.0
git init -b main
git add README.md LICENSE SECURITY.md CONTRIBUTING.md CHANGELOG.md THIRD_PARTY_NOTICES.md
git add .gitignore .gitattributes .github pyproject.toml uv.lock package.json package-lock.json start.command
git add sub2easy tests examples docs scripts
git diff --cached --stat
git diff --cached
```

人工确认没有邮箱、真实响应、私有域名、令牌、截图和数据库后再 commit。GitHub 仓库名称、可见性和远端地址由发布者选择；本文不预设或自动执行 `push`。

## 4. GitHub 设置

- 开启 secret scanning / push protection（取决于仓库套餐与功能可用性）。
- 开启 private vulnerability reporting，让 SECURITY.md 的私密报告入口可用。
- 首次推送后确认 CI 在 Linux/macOS 与声明的 Python 版本运行，不能把本地验证当作远端通过。
- 对主分支开启 required checks / PR review；GitHub Actions 默认只读权限，不使用生产 secret。
- 仅发布本次通过内容检查的源码包、wheel 与 sdist；不要直接上传整个 dist 目录中的历史包。

## 已知发布限制

- 原生 Windows、OS 自启动、自动备份恢复、密码轮换、通知渠道未实现。
- 部署与清理上游缺少 CAS，仍有最终 GET/PUT 间的竞态窗口。
- pattern 扫描不保证发现所有编码/间接泄漏。CI dependency-audit 依赖外部漏洞库，联网失败必须调查，不能视为无漏洞。
- 第三方 API 兼容性和生产授权结果不能由本地模拟测试保证。
