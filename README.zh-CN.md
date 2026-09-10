# Kindred

Kindred 是一个连接既有 Mouth host 的自主 AI resident 运行时。它为 resident 提供持续
内在状态、心跳、活动、记忆，以及边界明确的外部能力。

系统以 resident 为主体：每次心跳读取当前世界与已批准的对话，在私有 working state
中更新感知，可选择执行一个活动步骤，校验完整状态后再持久化。OpenClaw 或 Hermes 提供对话界面；
Portable Capability package 可以在不修改核心 graph 的情况下增加外部工具。

> **Public Preview：**`v0.4.1` 面向一台可信主机上的单 resident，适合愿意检查本地配置并
> 体验预发布软件的 operator。当前预览只支持全新安装，请使用全新用户/HOME；installer 会拒绝
> 覆盖其他预览版本。

[English](README.md)

## 环境要求

- 一个既有 Mouth host：
  - OpenClaw `2026.6.10`（`aa69b12`）或 `2026.7.1-2`（`0790d9f`），protocol 4；
  - Hermes `v2026.8.18`、package `0.20.4`（experimental）。
- Apple Silicon 的 macOS 14+，或 x86_64 的 Ubuntu 24.04
- 已有 OpenClaw agent/workspace 或 Hermes home，以及 Persona 文件
- 一个受支持的 LLM credential，以及初始化 home 时使用的地图 provider credential

发行包自带 CPython 3.11、完整 wheelhouse、只读 Web UI 和两个 Kindred Mouth Plugin。
目标机不需要 Python、Node、pnpm、软件包 registry 或 `sudo`。Draw 会安装但默认关闭，
只有 operator 明确配置 provider 后才启用。私有平台集成不属于公开版本。

## 安装

最短路径会下载版本化 bootstrap，并进入唯一的交互式安装器：

```sh
curl -fsSL \
  https://github.com/skedup/kindred/releases/download/v0.4.1/install.sh \
  | sh
```

也可以先检查脚本：

```sh
curl -fLO https://github.com/skedup/kindred/releases/download/v0.4.1/install.sh
less install.sh
sh install.sh
```

bootstrap 会校验对应平台的离线 Bundle，再安装到当前用户的数据目录。它不会安装或探测
Mouth host、修改系统 Python、解析 Persona、收集 credential 或启动服务。有 TTY 时会继续执行：

```sh
kindred install
```

安装后常用命令：

```sh
kindred doctor
kindred doctor --online
kindred start
kindred status
kindred logs
kindred stop
```

Web UI 是可信单用户的只读观察面，默认只监听 loopback。远程暴露和访问认证不属于首个
Public Preview。

`kindred uninstall` 只移除 Kindred 自有的服务、binding 和当前 Mouth Plugin。
它会保留宿主 workspace/home、Persona、resident marker、配置、数据库、Memory、Catalog、
Artifact 和历史 tick。首版没有 purge 命令。

Hermes 支持保持 experimental：Kindred 只读消费已批准的 direct transcript，每个 approved turn
只注入一次 bundle，companion Persona 只在首次或内容变化时发布。Hermes 会把该上下文保留在自己的
`api_content` replay 中；Kindred 不读取、覆盖或同步 Hermes `MEMORY.md` / `memories/USER.md`。
`hermes send` 的不确定结果不会自动重试。

## Episode Memory

Kindred 可以从已经提交、显著度较高的 Episode 构建本地派生检索索引。canonical DB 始终是
权威来源；同步和检索索引不会修改 State、tick 或 Mouth host 自己的 Memory。

```sh
kindred memory sync
kindred memory search "那次发光的浪花" --channel lexical
```

派生索引保存在本机 `<life_root>/data/memory-index.db`，其中包含 Episode 正文，以及 identifier、时间、
Activity、location（含 city/address）、mood、significance 等 metadata 的可检索副本，另有 normalized
text 与 content hash；vector/hybrid 索引还包含在本机计算的 embedding。删除索引只会移除派生副本，
随后可用 `kindred memory sync --rebuild` 从 canonical DB 重建。

`v0.4.1` Complete Bundle 已包含唯一只读 Memory MCP 工具的离线依赖闭包。先显式构建 lexical 索引，
再用相同 channel 启动 server：

```sh
kindred memory sync --channel lexical
kindred memory serve-mcp --channel lexical
```

安装 Kindred 不会自动建索引或向 Mouth host 注册该 server。完整的显式注册、验证与回滚命令见
[OpenClaw Memory MCP 运维文档](docs/21-memory-mcp-openclaw.md)。

vector 与 hybrid 检索需要同时安装两个可选 extra，并按对应 channel 重建索引。该路径会从 Hugging
Face 下载固定 revision 的模型 artifact；Episode 内容只在本机计算 embedding，不会上传给 Hugging Face。

```sh
uv sync --no-dev --extra memory-mcp --extra vector
uv run --no-sync kindred memory sync --channel hybrid --rebuild
uv run --no-sync kindred memory serve-mcp --channel hybrid
```

## 数据边界

Kindred 不会向外部服务发送 analytics、crash report 或诊断遥测。默认会在本地
`life/data/kindred-telemetry.db` 记录不含内容的 token 数、耗时、状态，以及安全的
Provider/stage/tool 标识；不会记录 prompt、response、tool 参数/结果、target 或 credential。
可通过 `observability.enabled: false` 或 `KINDRED_OBSERVABILITY_ENABLED=false` 关闭。超过
90 天的数据只做逻辑删除；SQLite 不会自动 VACUUM，这不等同于 secure erase。卸载会保留
telemetry DB；停止 Kindred 后，operator 可手动删除这一明确文件。loopback Web server 通过只读
`/api/observability/` API 提供不含内容的汇总与 run/span 明细；查询时不会创建或迁移该数据库。功能性
provider 仍会接收
完成工作所需的数据：

- 配置的 LLM 会接收选定的 Persona 与运行上下文；
- 地图 provider 会接收用于 world resolution 的 home address；
- 选定的本机 Mouth host 会处理已批准的 transcript、Mouth context 和 outbound dispatch；
- operator 注册 Memory MCP 且 Mouth host 实际调用后，命中的 Episode 正文与 metadata 会发送给该 host，
  并可能进入它所配置的 LLM 上下文；
- operator 明确启用的 Capability 可能调用各自的 provider。

Credential 和 resident 数据不会进入源码发行物。诊断输出只报告安全形状，不应展示消息、
Persona、credential、account 或 target 原值。

## 开发

```sh
uv run pytest
uv run ruff check src tests scripts
uv run mypy
uv build
```

只读 Web UI 位于 `web/`。公共生活资产打包在 `kindred.life_assets`，外部能力使用
`kindred.capability.v1` entry point。提交 Pull Request 前请阅读
[CONTRIBUTING.md](CONTRIBUTING.md)；macOS 桌面精灵由独立仓库
[skedup/kindred-desktop](https://github.com/skedup/kindred-desktop) 开发和发布；安全问题请按
[SECURITY.md](SECURITY.md) 私下报告。

## 许可证

Apache License 2.0，详见 [LICENSE](LICENSE)。
