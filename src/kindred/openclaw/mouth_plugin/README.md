# Kindred Mouth Plugin

Kindred wheel 内置的最小 OpenClaw Mouth Plugin。它只在固定
`OpenClaw 2026.6.10 (aa69b12)` 上使用 typed `before_prompt_build` hook，
并通过 `prependContext` 向一个已批准的 direct peer 注入 Mouth bundle。

本目录不是安装器。OPEN3-B 仍需负责：

- 在写配置前校验 exact OpenClaw binary/build 与 Gateway protocol；
- 从 OpenClaw 官方结构化输出核验 agent、session、peer 和 route；
- 生成私有 binding；
- 安装、inspect、doctor Plugin；
- 在启动前执行完整 composition preflight。

P0 已实测 `allowPromptInjection=false` 会让 OpenClaw 阻止
`before_prompt_build`，bundle 不会进入 Prompt。OPEN3-B 必须在固定版本、专用
agent/account、`per-channel-peer` scope 和 binding 全部核验成功后，显式启用
`allowPromptInjection=true`；本 package 不自行修改 OpenClaw 配置。

Plugin 固定读取：

```text
~/.config/kindred/openclaw-binding.json
```

binding 使用 session、workspace 和 peer target 的 SHA-256 摘要，不保存完整 session
key、workspace 路径、账号或 outbound route。`bundle_path` 与
`resident_marker_path` 是 Plugin 读取文件所需的两个私有绝对路径。

缺少或损坏 binding、marker 不匹配、bundle 不可读或 turn 不属于批准作用域时，
Plugin 返回空结果，不打断 Mouth。

运行定向测试：

```bash
node --test src/kindred/openclaw/mouth_plugin/test/binding.test.js
```
