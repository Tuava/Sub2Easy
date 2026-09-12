# 第三方组件与接口

## 项目关系与来源

Sub2Easy 是独立 Python 客户端，通过 HTTP 调用 sub2api 管理 API；发布包不包含 sub2api 服务端。接口/字段研究参考了 [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api) 的 commit `98d86915becae9fe9491a91ffc6defd5235c8d2b`。该基线仓库 LICENSE 是 **LGPL-3.0**，并不因此自动授予对其他版本、商标或服务的权利。

此引用用于说明兼容性研究基线，不是对整个项目不存在任何第三方衍生内容的法律保证。贡献者必须标明复制/修改的代码来源；如引入上游实现，应重新评估许可证并保留相应声明。项目发布前由所有者确认 MIT 选择。

NVT (`nvtokens.com`) 是可选的第三方授权服务，固定端点适配不表示合作、认证或可用性保证。OpenAI、Codex、sub2api 及服务名称归各自权利人所有。

## 主要依赖

以下是主要依赖的上游许可标识概览，以安装版本自身 LICENSE / 元数据为准。它们没有被重新许可成 MIT；wheel 声明依赖而不打包依赖代码。

| 组件 | 用途 | 上游许可概览 |
|---|---|---|
| FastAPI / Starlette | 本地 HTTP API | MIT / BSD-3-Clause |
| Uvicorn | ASGI 服务 | BSD-3-Clause |
| HTTPX | HTTP 客户端 | BSD-3-Clause |
| cryptography | scrypt / AES-GCM | Apache-2.0 或 BSD-3-Clause |
| Hatchling | Python 构建 | MIT |
| jsdom | 仅开发/测试 DOM | MIT |

完整 Python/npm 依赖版本分别在 `uv.lock` 和 `package-lock.json`；运行依赖审计不能替代许可证审查。CI 中使用的 GitHub Actions 同样遵循各自仓库许可证。
