# 贡献指南

支持 macOS/Linux、Python 3.11+。请先阅读 README 与 SECURITY.md。

```bash
uv sync --locked
npm ci --ignore-scripts
uv run --locked python -m unittest discover -s tests -v
npm run test:ui
uv run --locked python scripts/release_check.py
```

## 约定

- 不引入真实账号、数据库、Token、Cookie、主密码、原始响应或账号截图。测试数据只能使用保留示例域名和明显的虚构凭据。
- 每个测试使用临时目录；不读取开发者的 `data/` 或浏览器状态，不发真实授权/部署请求。
- 保留同身份互斥、取消检查、发送前写入意图和读回确认。网络调用不要放在 SQLite 事务中。
- 不把 HTTP 200、登录成功或任务入队当作上线成功；SSE 完整成功与最终状态均需验证。
- 任何默认自动写云端行为、依赖变更、凭据格式变更和数据库迁移必须在 PR 中说明并测试。
- 只记录固定错误码，不把上游任意响应/异常原文写入日志或任务列表。
- 带源码的第三方贡献必须注明来源与许可证，不能仅因研究了某 API 就复制其实现。
- 界面保持无构建运行方式，避免不必要的依赖或大规模无关重构。

## 提交前

运行 `uv build`，检查 wheel/sdist，按发布文档生成干净源码包。以 `git diff --cached` 复核要公开的内容；`.gitignore` 不能保护已经被跟踪的文件。

提交贡献表示你有权提供这些内容，并同意以项目 MIT 许可证分发自己的贡献。不要提交你无权重新许可的代码。
