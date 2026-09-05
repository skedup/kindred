# 21 - Kindred Memory MCP 与 Mouth Host 接入

Kindred Memory 是 Mouth 的可选只读记忆源；OpenClaw、Hermes 等宿主的原生 Memory 仍是主记忆。
该接入只暴露 `kindred_memory_search`，不会提供 sync/store/forget，也不会自动把历史写入
`context-bundle.md`。

## 1. v0.4.1 Bundle 基线

`v0.4.1` Complete Bundle 已离线安装 MCP transport，但没有 vector 模型。公开 Bundle 必须显式使用
`lexical`；安装、doctor、start 和 Heart 都不会自动建索引或修改 Mouth Host 配置。

先确定安装后的 Python 和 Kindred 配置绝对路径：

```sh
RUNTIME_PY="$HOME/.local/share/kindred/runtime/0.4.1/venv/bin/python"
KINDRED_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/kindred/config.yaml"
test -x "$RUNTIME_PY" && test -f "$KINDRED_CONFIG"
```

如果自定义了 `XDG_DATA_HOME`，相应调整 `RUNTIME_PY`，不要指向源码 checkout 或系统 Python。

## 2. 显式构建索引

索引写入不是 MCP 工具的一部分。先从已提交 canonical Episode 构建本地派生索引，再用同一 channel
做一次查询：

```sh
"$RUNTIME_PY" -I -m kindred.cli memory sync \
  --config "$KINDRED_CONFIG" --channel lexical
"$RUNTIME_PY" -I -m kindred.cli memory search '还记得那次海边画画吗' \
  --config "$KINDRED_CONFIG" --channel lexical
```

默认索引位于 `life_root/data/memory-index.db`。canonical DB 只读；派生索引可删除后用 `--rebuild`
重建。后续有新 Episode 时由 operator 再次执行 sync，查询不会偷偷补索引。

## 3. 注册到 OpenClaw

以下命令会修改当前用户的 OpenClaw MCP 配置，只在 operator 确认后执行：

```sh
openclaw mcp add kindred-memory \
  --command "$RUNTIME_PY" \
  --arg=-I \
  --arg=-m \
  --arg=kindred.cli \
  --arg=memory \
  --arg=serve-mcp \
  --arg=--config \
  --arg="$KINDRED_CONFIG" \
  --arg=--channel \
  --arg=lexical \
  --include=kindred_memory_search
```

带 `--command` 的 server 由 OpenClaw 作为 stdio 子进程启动，不要额外填写 HTTP transport。注册后验证：

```sh
openclaw mcp status --verbose
openclaw mcp probe kindred-memory --json
openclaw mcp reload
```

`probe` 应只看到 `kindred_memory_search`。随后可在非关键会话中提出一个明确依赖 resident 过往经历的
问题，确认 agent 会按需调用；普通知识、当前状态、用户偏好和待办不应依赖这个工具。

要撤销接入，执行：

```sh
openclaw mcp unset kindred-memory
openclaw mcp reload
```

撤销注册不会删除 canonical Episode 或本地派生索引。

## 4. Vector / hybrid 与 Hermes

Vector 和 hybrid 不属于 `v0.4.1` Bundle 闭包。需要语义检索的 operator 可在自行管理的源码环境中安装：

```sh
uv sync --no-dev --extra memory-mcp --extra vector
uv run --no-sync kindred memory sync --channel hybrid --rebuild
uv run --no-sync kindred memory serve-mcp --channel hybrid
```

首次使用会从 Hugging Face 下载固定 revision 的模型文件；Episode 内容在本机计算 embedding，不发送给
Hugging Face。Hermes 当前的 MCP 参数合同不能直接表达 Bundle 所需的 `--channel lexical`，因此
`v0.4.1` 不声明 Hermes Bundle 的 Memory MCP 接线已经完成，也不会提供隐式 wrapper。

## 5. 数据边界与失败语义

派生索引保存在本机。只有 operator 显式注册 MCP 且 Mouth Host 实际调用搜索后，命中的 Episode 正文和
identifier、时间、Activity、Location、mood、significance 等字段才会发送给该 Host，并可能进入它所配置
的 LLM 上下文。未注册或未调用时不会发生这条数据流。

- `INDEX_NOT_READY`：先用相同 channel 执行 `kindred memory sync`；
- `EMBEDDING_UNAVAILABLE`：只影响显式 vector/hybrid 路径；Bundle 基线应使用 lexical；
- `SEARCH_UNAVAILABLE`：检查 index/source/version 合同，必要时显式 rebuild；
- `INVALID_QUERY` / `INVALID_FILTER`：调用参数不满足稳定工具合同；
- 空 `results`：成功的拒召回，不应改写成猜测出来的记忆。

Kindred 不读取、改写或迁移宿主原生 Memory，也不会因安装 Memory MCP 自动启动同步任务。
