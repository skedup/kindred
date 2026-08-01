# kindred-capability-draw

Kindred 的 Portable 图片生成能力。Draw 独立于 Heart LLM 使用 OpenAI 图片模型，生成一张
PNG 并通过 Host-owned Artifact 提交。

配置：

```yaml
capabilities:
  draw:
    enabled: true
    side_effect_activities: []
    settings:
      provider: openai
      model: gpt-image-2
      timeout_s: 120
```

API key 使用 capability namespace 注入：

```text
KINDRED_CAPABILITY_DRAW_API_KEY=<secret>
```

当前正式支持并准备用于生产灰度的组合是 `provider: openai` +
`model: gpt-image-2`。Google adapter 仍存在于 package 中，但真实探针未通过，不属于正式
支持面，也不应配置到生产。Draw 不读取 Heart 的 LLM provider/model/key，不自动重试，
也不做 Provider fallback。

`draw_image` 接受必填 `prompt` 和可选
`layout=portrait|square|landscape`。每个 tick 最多尝试一次真实生成，输出固定为：

```text
kindred.draw.image.v1/
  artifact.json  # Host 生成
  image.png
```

图片 prompt、bytes、Provider 响应和绝对路径不会进入 ToolResult 或持久化 trace。

## 验证状态

- OpenAI `gpt-image-2`：真实 `portrait` 与 `landscape` probe `2/2` 通过；
- Google `gemini-3.1-flash-image`：2026-07-29 唯一一次真实 `portrait` probe 返回
  `ProviderError`；同一 key 的模型元数据查询 HTTP 200；
- Google exact wire、同 tick 单次请求预算和 Artifact 链路已通过 fake transport/self-test，
  但真实 Google Artifact 尚未形成，因此该路径保持未验收。

DRAW1 以 OpenAI 正式范围收口为 **GO**。2026-07-29 DRAW1.5 又在生产安装与配置下完成
`portrait 1024x1536` 和 `landscape 1536x1024` 各一次真实生成，两张均一次成功并通过人工
检查；单 invocation 第二次调用均在出网前被 `GenerationBudgetExceeded` 拒绝。结合 Host
commit/discard/T3 定向测试、脱敏扫描和运行健康检查，DRAW1.5 结论为 **GO**。

Google 未阻塞 DRAW1.5，也未进入本轮生产配置。
