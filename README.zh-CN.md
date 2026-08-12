# Kindred

Kindred 是一个以 OpenClaw 为必需基础设施的自主 AI resident 运行时。它为 resident
提供持续内在状态、心跳、活动、记忆，以及边界明确的外部能力。

系统以 resident 为主体：每次心跳读取当前世界与已批准的对话，在私有 working state
中更新感知，可选择执行一个活动步骤，校验完整状态后再持久化。OpenClaw 提供对话界面；
Portable Capability package 可以在不修改核心 graph 的情况下增加外部工具。

> **Public Preview：**`v0.2.0` 面向一台可信主机上的单 resident，适合愿意检查本地配置并
> 体验预发布软件的 operator。当前预览只支持全新安装，请使用全新用户/HOME；installer 会拒绝
> 覆盖其他预览版本。

[English](README.md)

## 环境要求

- OpenClaw `2026.6.10`（`aa69b12`），Gateway protocol 4
- Apple Silicon 的 macOS 14+，或 x86_64 的 Ubuntu 24.04
- 已有 OpenClaw agent、workspace 和 Persona 文件
- 一个受支持的 LLM credential，以及初始化 home 时使用的地图 provider credential

发行包自带 CPython 3.11、完整 wheelhouse、只读 Web UI 和 Kindred Mouth Plugin。
目标机不需要 Python、Node、pnpm、软件包 registry 或 `sudo`。Draw 会安装但默认关闭，
只有 operator 明确配置 provider 后才启用。私有平台集成不属于公开版本。

## 安装

最短路径会下载版本化 bootstrap，并进入唯一的交互式安装器：

```sh
curl -fsSL \
  https://github.com/skedup/kindred/releases/download/v0.2.0/install.sh \
  | sh
```

也可以先检查脚本：

```sh
curl -fLO https://github.com/skedup/kindred/releases/download/v0.2.0/install.sh
less install.sh
sh install.sh
```

bootstrap 会校验对应平台的离线 Bundle，再安装到当前用户的数据目录。它不会安装
OpenClaw、修改系统 Python、解析 Persona、收集 credential 或启动服务。有 TTY 时会继续执行：

```sh
kindred openclaw install
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

`kindred openclaw uninstall` 只移除 Kindred 自有的服务、binding 和 Mouth Plugin。
它会保留 OpenClaw workspace、Persona、resident marker、配置、数据库、Memory、Catalog、
Artifact 和历史 tick。首版没有 purge 命令。

## 数据边界

Kindred 不增加 analytics、crash report 或诊断遥测，但功能性 provider 仍会接收完成工作
所需的数据：

- 配置的 LLM 会接收选定的 Persona 与运行上下文；
- 地图 provider 会接收用于 world resolution 的 home address；
- 本机 OpenClaw Gateway 会处理已批准的 transcript、Mouth context 和 outbound dispatch；
- operator 明确启用的 Capability 可能调用各自的 provider。

Credential 和 resident 数据不会进入源码发行物。诊断输出只报告安全形状，不应展示消息、
Persona、token、account 或 target 原值。

## 开发

```sh
uv run pytest
uv run ruff check src tests scripts
uv run mypy
uv build
```

只读 Web UI 位于 `web/`。公共生活资产打包在 `kindred.life_assets`，外部能力使用
`kindred.capability.v1` entry point。提交 Pull Request 前请阅读
[CONTRIBUTING.md](CONTRIBUTING.md)；安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

## 许可证

Apache License 2.0，详见 [LICENSE](LICENSE)。
