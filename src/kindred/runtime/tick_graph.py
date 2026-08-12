"""共享的 tick graph 装配工厂（earlier milestone N-2）。

CLI（``cli._build_client_graph``）和 daemon（``HeartDaemon``）都要用「给定
client + db + config 装配完整 tick graph」这同一组合点。这个组合点会随
node deps / 路径注入 / prompt_dumper / client protocol 一起演进——复制在
两处迟早漂移。抽到非 UI 模块（runtime 层），CLI 和 daemon 都依赖它。

放在 runtime（核心层）而非 cli（UI 层），保证「核心层不依赖 UI 层」：
daemon 可以 import 它，cli 也可以 import 它，但它不 import 任何一方。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from contextlib import ExitStack

    from langgraph.graph.state import CompiledStateGraph

    from kindred.config import KindredConfig
    from kindred.db import KindredDB
    from kindred.llm.client import ToolCapableLlmClient
    from kindred.observability import PromptDumper
    from kindred.runtime.io_bridge import IOBridge
    from kindred.state.tick import TickState


def build_client_tick_graph(
    client: ToolCapableLlmClient,
    db: KindredDB,
    *,
    config: KindredConfig,
    prompt_dumper: PromptDumper,
    io_bridge: IOBridge | None = None,
    resource_stack: ExitStack | None = None,
) -> CompiledStateGraph[TickState, Any, Any, Any]:
    """用给定 client + db 装配完整 tick graph（mock / 真 LLM 共用）。

    sense / act 节点共用同一个 ``client`` 实例；persist 节点拿 db + bundle 路径。
    act 节点要求 ``ToolCapableLlmClient``；sense/dream 仍只使用基础 ``complete``。

    ``io_bridge``（earlier milestone / codex N-1）：心主动 push 的出向桥，透给 act 节点。
    这是**生产路径的依赖注入点**：daemon run() 在 db + gateway 就绪后构造
    真 IOBridge 传进来，否则 act 节点 ``io_bridge=None`` 走降级（不 push）。
    CLI 单跑 graph 不 push（保持 None）。
    """
    from kindred.capability_host import (
        ArtifactStore,
        HostUserMessenger,
        InternalBinding,
        build_host_runtime,
    )
    from kindred.capability_host.facts import (
        ACTIVITY_CURRENT_FACT,
        ARTIFACT_EXPLICIT_REFS_FACT,
        build_current_activity,
        build_explicit_artifact_refs,
    )
    from kindred.character_card import load_home_profile, load_interior_trait_profile
    from kindred.graph import build_tick_graph
    from kindred.graph.tick import (
        make_act_llm_node,
        make_persist_nodes,
        make_sense_derive_node,
        make_sense_io_node,
        make_sense_llm_node,
    )
    from kindred.inventory.facts import (
        INVENTORY_CHOICE_CONTEXT_FACT,
        build_inventory_choice_context,
    )
    from kindred.life_assets import ACTIONS_DIR, ACTIVITIES_DIR
    from kindred.llm.client import ToolCapableLlmClient
    from kindred.location.capability import (
        LOCATION_PROVIDER_DEPENDENCY,
        LocationCapability,
    )
    from kindred.providers import (
        RecentContactProvider,
        build_environment_provider,
        build_location_provider,
    )

    if not isinstance(client, ToolCapableLlmClient):
        raise TypeError(
            "build_client_tick_graph: act graph requires ToolCapableLlmClient; "
            "configure a tool-capable llm.provider such as google/deepseek/openai."
        )

    paths = config.paths
    home = load_home_profile(paths.character_card)
    interior_traits = load_interior_trait_profile(paths.character_card)
    location_provider = build_location_provider(config)
    location = LocationCapability()
    artifact_store = ArtifactStore(paths.doc_dir / "artifacts")
    host_runtime = build_host_runtime(
        config=config,
        internal_bindings=(InternalBinding(location.name, location.tool_defs(), location.handle),),
        provider_handles={
            LOCATION_PROVIDER_DEPENDENCY: location_provider,
        },
        fact_view_builders={
            ACTIVITY_CURRENT_FACT: build_current_activity,
            ARTIFACT_EXPLICIT_REFS_FACT: build_explicit_artifact_refs,
            INVENTORY_CHOICE_CONTEXT_FACT: build_inventory_choice_context,
        },
        host_services={
            "artifact_writer": artifact_store,
            "artifact_reader": artifact_store,
            "user_messenger": HostUserMessenger(io_bridge),
        },
    )
    if resource_stack is not None:
        resource_stack.callback(host_runtime.close)
    return build_tick_graph(
        sense_io_node=make_sense_io_node(
            db,
            session_key=config.daemon.session_key,
            environment_provider=build_environment_provider(config),
            recent_contact_provider=RecentContactProvider(
                db,
                config.daemon.session_key,
            ),
            weather_ttl_minutes=config.world.weather_ttl_minutes,
            weather_location=config.world.weather_location,
        ),
        sense_derive_node=make_sense_derive_node(
            arousal_baseline=interior_traits.arousal_baseline,
            weather_ttl_minutes=config.world.weather_ttl_minutes,
            weather_location=config.world.weather_location,
        ),
        sense_llm_node=make_sense_llm_node(
            client,
            db,
            activities_dir=ACTIVITIES_DIR,
            soul_excerpt_path=paths.soul_excerpt,
            soul_full_path=paths.soul_full,
            identity_path=paths.identity,
            user_path=paths.user,
            highlights_path=paths.bundle_highlights,
            prompt_dumper=prompt_dumper,
            relationship_reader=db,
        ),
        act_llm_node=make_act_llm_node(
            client,
            activities_dir=ACTIVITIES_DIR,
            actions_dir=ACTIONS_DIR,
            host_runtime=host_runtime,
            prompt_dumper=prompt_dumper,
            home=home,
            soul_excerpt_path=paths.soul_excerpt,
            # L3 PlaceStore：pre-act 本地经验（place_visits exact lookup，只读）。
            db=db,
            relationship_reader=db,
        ),
        persist_nodes=make_persist_nodes(db, paths.context_bundle, relationship_reader=db),
    )
