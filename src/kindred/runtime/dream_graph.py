"""共享的 dream graph 装配工厂（D6.7）。

与 ``tick_graph.py`` 对称：CLI（``kindred dream run``）和 daemon（清晨 04:00 触发）
都要用「给定 client + db + config 装配完整 dream graph」这同一组合点。这个组合点
会随 5 个 node deps（client / db / 文件路径）一起演进——复制在两处迟早漂移。抽到
runtime 核心层，CLI 和 daemon 都依赖它，但它不 import 任何一方（核心不依赖 UI）。

dream graph 拓扑（docs/14 §2.4 / docs/11 §3）：
``START → Step1.summarize → Step2.reset_stack → Step3.reflect → Step4.gate
──pass/warn──→ Step5.land → END / ──block──→ END``
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

    from kindred.config import KindredConfig
    from kindred.db import KindredDB
    from kindred.llm.client import LlmClient
    from kindred.state.dream import DreamState


def build_client_dream_graph(
    client: LlmClient,
    db: KindredDB,
    *,
    config: KindredConfig,
) -> CompiledStateGraph[DreamState, Any, Any, Any]:
    """用给定 client + db 装配完整 dream graph（mock / 真 LLM 共用）。

    5 个节点的依赖注入：

    - ``summarize`` / ``reset_stack``：拿 db + session_key（读/重置 messages stack）
    - ``reflect`` / ``gate``：拿 client（LLM 反思 + 闸门）+ 灵魂文件路径
    - ``land``：拿 config.paths（SoulLayout + snapshot 根 + index）

    任意满足 ``LlmClient`` Protocol 的实现都可注入，节点零改动（与 tick 同模式）。
    """
    from kindred.graph.dream.build import build_dream_graph
    from kindred.graph.dream.excerpt import make_excerpt_compiler
    from kindred.graph.dream.gate import make_gate_node
    from kindred.graph.dream.land import make_land_node
    from kindred.graph.dream.reflect import make_reflect_node
    from kindred.graph.dream.reset_stack import make_reset_stack_node
    from kindred.graph.dream.summarize import make_summarize_node

    paths = config.paths
    session_key = config.daemon.session_key
    return build_dream_graph(
        summarize_node=make_summarize_node(client, db, session_key=session_key),
        reset_stack_node=make_reset_stack_node(db, session_key=session_key),
        reflect_node=make_reflect_node(
            client,
            soul_full_path=paths.soul_full,
            identity_path=paths.identity,
            user_path=paths.user,
        ),
        gate_node=make_gate_node(client),
        land_node=make_land_node(paths, excerpt_compiler=make_excerpt_compiler(client)),
    )
