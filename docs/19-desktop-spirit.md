# macOS 桌面精灵

Kindred Desktop Spirit 已迁入独立公开仓库
[skedup/kindred-desktop](https://github.com/skedup/kindred-desktop)。应用构建、安装、Local/Remote
来源配置、视觉资产和桌面问题请在该仓库维护；Kindred 主仓不再包含 Tauri、renderer 或 visual-pack。

Kindred 仍拥有桌面使用的服务端合同：

- `GET /api/visual-state` 只投影最新 committed action；
- canonical schema 和 fixtures 位于 [`contracts/visual-state/`](../contracts/visual-state/)；
- Local 默认服务地址为 `http://127.0.0.1:8787`；
- Ubuntu 作为独立服务主机时，可由 operator 显式开放 Kindred Web，再在桌面应用中选择 Remote；
- 桌面应用不读取 SQLite、不管理 Heart/Web，也不写 canonical state。

非 loopback 监听会暴露完整只读 Web/API，而不只是视觉状态。Kindred V1 不提供应用认证、TLS 或 tunnel；
只应在受信网络中直接使用，或由 operator 在外部提供 HTTPS reverse proxy。
