# Kindred Mouth Plugin

Kindred wheel 内置的最小 OpenClaw Mouth Plugin。它只支持固定
`OpenClaw 2026.6.10 (aa69b12)`，通过 typed `before_prompt_build` hook 向一个已批准的
direct peer 注入 Mouth bundle，并提供 operator-only
`kindred.mouth.commitOutbound` transcript RPC。

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

private binding v3 保存稳定的 `session_key`，以及 resident、workspace 和 peer 摘要；不保存会随
`/new`、`/reset` 轮换的 `session_id`，也不保存账号或 outbound route。`bundle_path` 与
`resident_marker_path` 是 Plugin 读取文件所需的两个私有绝对路径。binding 由 installer 原子写为
`0600`，旧 schema 不兼容，由 installer 受控升级。

缺少或损坏 binding、marker 不匹配、bundle 不可读或 turn 不属于批准作用域时，
Plugin 返回空结果，不打断 Mouth。

`commitOutbound` 只接受 64 位小写十六进制 operation id 和非空 committed text。它从批准
`session_key` 的 canonical entry 解析当前 `session_id`，在官方 transcript write lock 中再次核对，
从该 transcript 的最近普通 assistant 取得可信模型 metadata，并追加 `display=false` 的幂等
assistant row；落锁前恰逢 rollover 时只重解析一次。它不扫描其他 session、不发送 channel，也不调用
LLM。

运行定向测试：

```bash
node --test src/kindred/openclaw/mouth_plugin/test/*.test.js
```
