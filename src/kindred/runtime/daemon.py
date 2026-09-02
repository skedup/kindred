"""常驻心跳 daemon（earlier milestone：纯 in-process 自循环）。

docs/13-heart-daemon.md 描述的是**完整** daemon（6 职责 + ws IOBridge +
MessageWatcher + 清晨做梦调度）。本文件随 D 系列逐刀长出，当前边界：

已实现：
- ``HeartbeatScheduler``：间隔由 state 决定（醒 5min / 睡 1h）（earlier milestone）
- ``Debouncer`` + 主循环：首 tick 发 cold_start → 按 scheduler 节奏 invoke
  tick_graph；SIGTERM/SIGINT graceful shutdown（earlier milestone）
- ``MessageWatcher``：你说话→心感知（表新 partner 行 fire watcher tick，earlier milestone）
- ``HistorySync``：每轮 watcher poll 前拉 chat.history 进表
  （token 未配降级 None）
- ``IOBridge`` / 心主动 push：act 工具环调用 send_to_user → core direct send 原文投递，
  再 best-effort 提交 Mouth hidden context（token 未配降级 None）
- 清晨做梦接入（D6.8b）：主循环 while 顶调 ``should_dream(wall_now, index_path)``
  （幂等补偿，``kindred.graph.dream._schedule``），命中则 invoke dream_graph
  （装配入口 ``runtime.dream_graph.build_client_dream_graph``，D6.7）后 continue
  重评节奏（那轮 tick 不走）；单次做梦失败不杀 daemon（MEMORY R3）

至此 D 系列六块拼图齐：心能稳定呼吸、听得见你（watcher）、读得了历史（sync）、
不忘事（游标持久）、主动找你（push）、也会做梦（清晨调度）。
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Mapping
from contextlib import ExitStack
from datetime import date, datetime
from types import FrameType
from typing import TYPE_CHECKING, Any, Literal, cast

from kindred.config import DEFAULT_WORLD_TIMEZONE, SoulHistoryLayout
from kindred.db import KindredDB
from kindred.inventory.catalog import InventoryPreflightError, validate_inventory_state_keys
from kindred.runtime.clock import life_now, life_now_iso
from kindred.runtime.pidfile import AlreadyRunningError
from kindred.runtime.pidfile import acquire as acquire_pidfile
from kindred.runtime.scheduler import Debouncer, HeartbeatScheduler, is_sleeping
from kindred.runtime.tick_graph import build_client_tick_graph
from kindred.runtime.watcher import (
    DbPollMessageSource,
    MessageWatcher,
    TriggerEvent,
)

if TYPE_CHECKING:
    from pathlib import Path

    from langgraph.graph.state import CompiledStateGraph

    from kindred.config import KindredConfig
    from kindred.llm.client import ManagedLlmClient
    from kindred.mouth_host.composition import HostPreparation
    from kindred.observability import PromptDumper
    from kindred.runtime.history_sync import HistorySync
    from kindred.runtime.io_bridge import IOBridge
    from kindred.runtime.telemetry_thresholds import TelemetryThresholdMonitor
    from kindred.state.dream import DreamState
    from kindred.state.tick import TickState
    from kindred.telemetry import TelemetryFacade
    from kindred.telemetry.contracts import TriggerSource

logger = logging.getLogger(__name__)

# 主循环醒来检查 stop_event 的粒度（秒）。心跳间隔是 5min/1h，但循环用小步
# sleep 轮询 stop_event，保证 SIGTERM 能在 ~1s 内被响应而不必等满一个间隔。
_WAKE_GRANULARITY_SECONDS = 1.0


def _wall_now(timezone_name: str = DEFAULT_WORLD_TIMEZONE) -> datetime:
    """当前墙钟（aware，本地时区）——should_dream 需日期语义。

    与 self._clock（monotonic，无日期，只用于间隔/去抖）分开：monotonic 不能
    推导「现在是哪天 04:00」，做梦调度必须走墙钟。与 ``_now_iso`` 同源时区。
    """
    return life_now(timezone_name)


class HeartDaemon:
    """心跳 daemon 主体。

    生命周期：``run()`` 阻塞直到收到 SIGTERM/SIGINT 或 ``stop()`` 被调用。
    每次 tick：read 最新 state → 算下一次间隔 → 到点 invoke tick_graph。
    首 tick 发 ``cold_start`` source（docs/13:437 / docs/14:100：daemon 进程刚启动
    的「重新苏醒」语义，让 T1.sense.llm 多读文件快速对齐）。这与 ``ColdStartError``
    （库为空异常，归 run() 在 invoke 前 preflight fail-fast）是两个独立语义。
    """

    def __init__(
        self,
        config: KindredConfig,
        prompt_dumper: PromptDumper,
        *,
        scheduler: HeartbeatScheduler | None = None,
        debouncer: Debouncer | None = None,
        watcher: MessageWatcher | None = None,
        history_sync: HistorySync | None = None,
        io_bridge: IOBridge | None = None,
        dream_graph: CompiledStateGraph[DreamState, Any, Any, Any] | None = None,
        index_path: Path | None = None,
        max_ticks: int | None = None,
        clock: Any = time.monotonic,
        sleep: Any = time.sleep,
        wall_clock: Any | None = None,
        telemetry: TelemetryFacade | None = None,
        threshold_monitor: TelemetryThresholdMonitor | None = None,
    ) -> None:
        self._config = config
        self._prompt_dumper = prompt_dumper
        world = getattr(config, "world", None)
        self._timezone = getattr(world, "timezone", DEFAULT_WORLD_TIMEZONE)
        self._scheduler = scheduler or HeartbeatScheduler()
        self._debouncer = debouncer or Debouncer()
        # history_sync 可注入（测试）；run() 路径下若 gateway.token 已配则构造
        # 真实 HistorySync，否则保持 None（无真源，仅 heartbeat+watcher，向后兼容）。
        self._history_sync = history_sync
        # io_bridge 可注入（测试）；run() 路径下若 gateway.token 已配则构造真
        # IOBridge（心主动 push 出向桥，codex N-1），否则 None（不 push，降级）。
        self._io_bridge = io_bridge
        self._host_preparation: HostPreparation | None = None
        # watcher 可注入（测试）；run() 路径下若为 None 会用真 db 构造。
        # _loop 路径下为 None 表示不启用 watcher（只跑 heartbeat，向后兼容）。
        self._watcher = watcher
        # dream_graph 可注入（测试）；run() 路径下用真 client+db 构造（D6.8b）。
        # 为 None 表示不启用清晨做梦（只跑 tick/watcher，向后兼容）。index_path =
        # soul-history/index.json，should_dream 的幂等真相源。两者要么都给要么都 None。
        self._dream_graph = dream_graph
        self._index_path = index_path
        # 墙钟（aware datetime）——should_dream 需日期语义，而 self._clock 是
        # monotonic（无日期）。可注入测试用。
        self._wall_clock = wall_clock or (lambda: _wall_now(self._timezone))
        self._stop_event = threading.Event()
        self._max_ticks = max_ticks  # 测试/有限运行用；None=无限
        self._clock = clock
        self._sleep = sleep
        self._tick_count = 0
        self._telemetry = telemetry
        self._threshold_monitor = threshold_monitor

    # ── 生命周期 ──────────────────────────────────────────────

    def request_stop(self, *_: object) -> None:
        """请求 graceful 停止（signal handler / 外部调用均可）。"""
        logger.info("daemon stop requested")
        self._stop_event.set()

    def _install_signal_handlers(self) -> None:
        def _handler(signum: int, _frame: FrameType | None) -> None:
            logger.info("signal %s received, graceful shutdown", signum)
            self.request_stop()

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

    def run(self, *, install_signals: bool = True) -> int:
        """启动主循环，阻塞至停止。返回完成的 tick 数。

        install_signals=False 供测试用（pytest 主线程外无法装信号）。
        """
        if install_signals:
            self._install_signal_handlers()

        db_path = self._config.paths.db
        if not db_path.exists():
            logger.error("db not found: %s — run `kindred db migrate` first", db_path)
            return 0

        # N-1: 空库（migrate 了但没 bootstrap）→ fail-fast，不进循环。
        # 契约见 sense_io.py ColdStartError：daemon 应在 invoke 前检测，不吞异常空转。
        with KindredDB.open(db_path) as db:
            latest_state = db.get_state_latest()
            if latest_state is None:
                logger.error(
                    "db has no state_latest (empty): %s — run `kindred db bootstrap` first",
                    db_path,
                )
                return 0
            try:
                validate_inventory_state_keys(latest_state, db)
            except InventoryPreflightError as exc:
                logger.error("heart startup blocked: %s", exc)
                return 0

        # pid 文件：防重复启动（已有存活进程则保守拒绝，不强杀）。在确认 db 就绪、
        # 即将真启动时获取；退出时清理（docs/16 §2.3）。
        pid_file = self._config.daemon.pid_file
        try:
            lease = acquire_pidfile(pid_file)
        except AlreadyRunningError as exc:
            logger.error("%s", exc)
            return 0

        client = self._open_client()
        if client is None:
            lease.release()
            return 0
        from kindred.llm.client import ToolCapableLlmClient

        if not isinstance(client, ToolCapableLlmClient):
            logger.error(
                "failed to init tick graph: act requires ToolCapableLlmClient; "
                "configure a tool-capable llm.provider such as google/deepseek/openai/xai"
            )
            client.close()
            lease.release()
            return 0

        try:
            with ExitStack() as resources:
                db = resources.enter_context(KindredDB.open(db_path))
                if self._telemetry is None:
                    from kindred.runtime.observed_invoke import create_runtime_telemetry

                    self._telemetry = create_runtime_telemetry(self._config)
                    owned_telemetry = self._telemetry

                    def close_owned_telemetry() -> None:
                        owned_telemetry.close()
                        if self._telemetry is owned_telemetry:
                            self._telemetry = None

                    resources.callback(close_owned_telemetry)
                if self._threshold_monitor is None:
                    from kindred.runtime.telemetry_thresholds import (
                        create_runtime_threshold_monitor,
                    )

                    self._threshold_monitor = create_runtime_threshold_monitor(
                        self._config,
                        telemetry=self._telemetry,
                    )
                if self._io_bridge is None or self._history_sync is None:
                    self._host_preparation = self._prepare_mouth_host()
                # 心经 Gateway 出向（io_bridge）+ 拉 chat.history（history_sync）。
                # io_bridge 先于 graph 构造，才能注入进去（codex N-1：生产路径依赖注入）；
                # 经 Gateway ws 出向（token 缺失降级 None）。
                if self._io_bridge is None:
                    self._io_bridge = self._build_io_bridge()
                graph = build_client_tick_graph(
                    client,
                    db,
                    config=self._config,
                    prompt_dumper=self._prompt_dumper,
                    io_bridge=self._io_bridge,
                    resource_stack=resources,
                )
                # watcher 依赖 db 实例，故在 db open 后构造（除非已注入，测试用）。
                if self._watcher is None:
                    self._watcher = MessageWatcher(
                        DbPollMessageSource(db),
                        self._debouncer,
                        session_key=self._config.daemon.session_key,
                    )
                # history_sync 同样在 db open 后构造；token 缺失则优雅降级为 None。
                if self._history_sync is None:
                    self._history_sync = self._build_history_sync(db)
                # dream graph + index_path（D6.8b）：生产路径同 client+db 装配（D6.7
                # 装配入口），index_path = soul-history/index.json。两者同生同灭（供
                # _fire_dream_if_due 的 assert）。未注入才构（测试可注入 / 留 None）。
                if self._dream_graph is None:
                    from kindred.runtime.dream_graph import build_client_dream_graph

                    self._dream_graph = build_client_dream_graph(client, db, config=self._config)
                    self._index_path = SoulHistoryLayout.from_dir(
                        self._config.paths.soul_history_dir
                    ).index_path
                self._loop(db, graph)
        finally:
            client.close()
            lease.release()
        logger.info("daemon stopped after %d tick(s)", self._tick_count)
        return self._tick_count

    def _prepare_mouth_host(self) -> HostPreparation | None:
        from kindred.adapters.openclaw.gateway import GatewayClient, GatewayError
        from kindred.mouth_host.composition import prepare_host
        from kindred.mouth_host.model import OpenClawRuntimeModel

        model = self._config.mouth_host
        if model is None:
            logger.warning("Mouth host disabled: verified binding is absent")
            return None
        try:
            if isinstance(model, OpenClawRuntimeModel):
                gateway = GatewayClient.from_config(
                    self._config.gateway,
                    identity_path=self._config.paths.life_root / ".device-identity.json",
                )
                return prepare_host(model, openclaw_gateway=gateway)
            return prepare_host(model)
        except GatewayError as exc:
            logger.warning("Mouth host unavailable error_type=%s", type(exc).__name__)
            return None

    def _build_history_sync(self, db: KindredDB) -> HistorySync | None:
        host = self._host_preparation or self._prepare_mouth_host()
        if host is None:
            return None
        from kindred.runtime.history_sync import HistorySync

        logger.info("history sync enabled for verified direct peer")
        return HistorySync(host.transcript, db)

    def _build_io_bridge(self) -> IOBridge | None:
        host = self._host_preparation or self._prepare_mouth_host()
        if host is None:
            return None
        from kindred.runtime.io_bridge import IOBridge

        logger.info("heart push enabled for verified direct peer")
        return IOBridge(host.outbound)

    def _open_client(self) -> ManagedLlmClient | None:
        # 按 Kindred config 的 provider 选择实现；Provider credential 不从 OpenClaw 读取。
        from kindred.config import KindredConfigError
        from kindred.llm.client import LlmClientError
        from kindred.llm.factory import build_llm_client

        try:
            return build_llm_client(self._config)
        except (LlmClientError, KindredConfigError) as exc:
            logger.error("failed to init LLM client: %s", exc)
            return None

    # ── 主循环 ────────────────────────────────────────────────

    def _loop(self, db: KindredDB, graph: CompiledStateGraph[TickState, Any, Any, Any]) -> None:
        # N-3: 进入即检查 stop——若 SIGTERM 在 signal 安装后、loop 开始前到达，
        # 不启动任何新 invoke（graceful：停止接收新 trigger）。
        if self._stop_event.is_set():
            return
        # baseline sync：prime 前先拉一次 chat.history 填表建立基线（earlier milestone N-1）。
        # 否则若本地表空而远端 session 已有历史，prime 看到空表→游标设到空，
        # 随后首次 poll 前的 sync_once 写入的旧 history 会被当「启动后新消息」
        # 触发 watcher tick，破坏 D2.5「启动不重放历史」契约。先 sync 后 prime，
        # 让 prime 把游标推到含历史的表尾。sync_once 永不抛（R3）。
        if self._history_sync is not None:
            self._history_sync.sync_once()
        # prime watcher：把游标设到表当前尾部，不重放历史。
        # 需在首 tick 前——否则启动时表里历史 partner 消息会被当新消息重放。
        if self._watcher is not None:
            self._watcher.prime()
        # 启动首 tick：发 source="cold_start"（docs/13:437 / docs/14:100）——
        # daemon 进程刚启动「重新苏醒」语义，让 T1.sense.llm 多读几个文件
        # （recent_ticks N=20 + 完整 SOUL）快速对齐。不被冷却按住。
        # 注：这与「库为空」无关——空库已由 run() preflight fail-fast（N-1）；
        # 已 bootstrap 的库不会触发 sense_io 空库异常。
        self._fire_event(db, graph, TriggerEvent("cold_start", self._now_iso()))
        if self._reached_limit():
            return

        while not self._stop_event.is_set():
            # 清晨做梦（D6.8b）：while 顶先问 should_dream（幂等补偿，非窗口命中，
            # docs/14 §2.4.3）。命中则走一次 dream graph 后 continue 重评节奏——
            # 那一轮 tick 不走。dream_graph 未启用（None）则跳过，向后兼容。
            # 失败**不** continue：落到下方正常等待，按 scheduler 间隔后重试
            # （docs/11 §8.1「第二天清晨重试」；避免持续失败时 tight-loop 烧 LLM）。
            if self._dream_graph is not None and self._fire_dream_if_due(db):
                if self._reached_limit():
                    break
                continue
            state = db.get_state_latest()
            interval = self._scheduler.next_interval(state)
            logger.debug("next heartbeat in %ds (sleeping=%s)", interval, is_sleeping(state))
            # 两个触发源顺序调度（earlier milestone）：watcher 高频实时 / heartbeat 低频定时。
            # 在等下一次心跳期间，每个 wake granularity（~1s）poll 一次 watcher，
            # 发现 partner 说话就立即 fire watcher tick（不等心跳）。
            outcome = self._wait_with_watcher(db, graph, interval)
            # watcher 可能在等待期间已跑满 max_ticks（有限运行）——先查再继续。
            if self._reached_limit():
                break
            if outcome == "stopped":
                break  # stop 请求打断等待
            if outcome == "fired":
                # N-2：watcher tick 已 fire（可能改了 state）→ 回 while 顶重读 state /
                # next_interval()，**不**紧接着跨过心跳 debounce 多跑一个 heartbeat。
                continue
            # outcome == "timeout"：真等到了心跳 deadline → 正常 fire heartbeat。
            now = self._clock()
            if self._debouncer.should_skip("heartbeat", now):
                logger.debug("heartbeat debounced, skip")
                continue
            self._debouncer.record("heartbeat", now)
            self._fire_event(db, graph, TriggerEvent("heartbeat", self._now_iso()))
            if self._reached_limit():
                break

    def _wait_with_watcher(
        self,
        db: KindredDB,
        graph: CompiledStateGraph[TickState, Any, Any, Any],
        seconds: float,
    ) -> Literal["stopped", "fired", "timeout"]:
        """分小步 sleep 等下一次心跳，期间每步 poll watcher。**三态返回**（N-2）：

        - ``"stopped"``：被 stop_event 打断（外层 break）。
        - ``"fired"``：watcher tick 实际 fire（外层 continue 回 while 顶重读 state，
          **不**紧接着 fire heartbeat——避免 watcher 后多跑一个心跳）。
        - ``"timeout"``：真等到心跳 deadline（外层正常 fire heartbeat）。

        watcher poll 搭 ``_WAKE_GRANULARITY_SECONDS``（~1s）的便车（earlier milestone 单线程
        顺序调度）：发现 partner 新消息立即 fire watcher tick，不等心跳。
        """
        deadline = self._clock() + seconds
        while self._clock() < deadline:
            if self._stop_event.is_set():
                return "stopped"
            fired = self._poll_watcher_once(db, graph)
            if self._reached_limit():
                # 达上限：如果刚 fire 则报 fired（外层会先查 limit break），否则 timeout。
                return "fired" if fired else "timeout"
            if fired:
                return "fired"
            remaining = deadline - self._clock()
            self._sleep(min(_WAKE_GRANULARITY_SECONDS, max(0.0, remaining)))
        return "stopped" if self._stop_event.is_set() else "timeout"

    def _poll_watcher_once(
        self,
        db: KindredDB,
        graph: CompiledStateGraph[TickState, Any, Any, Any],
    ) -> bool:
        """poll watcher 一次；未读消息含 partner（冷却到期）则 fire watcher tick。

        返回是否**实际 fire 且成功**（供 N-2 跳出 wait 重评节奏）。

        路 X / earlier milestone：watcher 无状态唤醒器，**不再调 ack**。cursor 推进由 tick 内部
        sense_io 读完即推（写 watcher_cursor 表）。读取/推进前失败时，下轮 poll 仍可能
        读到同批；推进后的下游失败不回放，符合 best-effort 感知语义。
        """
        if self._watcher is None:
            return False
        # poll 前先拉真源进表：fetch chat.history → build → upsert（幂等）。
        # sync_once 永不抛（R3：拉取失败绝不崩心跳），失败时表里仍有上轮数据，
        # watcher 照常 poll。无 history_sync（token 未配）则跳过，纯靠注入/外部写表。
        if self._history_sync is not None:
            self._history_sync.sync_once()
        now = self._clock()
        evt = self._watcher.poll_once(now=now, triggered_at=self._now_iso())
        if evt is None:
            return False
        return self._fire_event(db, graph, evt)

    def _fire_event(
        self,
        db: KindredDB,
        graph: CompiledStateGraph[TickState, Any, Any, Any],
        event: TriggerEvent,
    ) -> bool:
        """统一 graph.invoke 入口（earlier milestone）：cold_start / heartbeat / watcher 三源同走。

        用 :meth:`TriggerEvent.to_invoke_input` 展开为 invoke 入参——消除裸 dict
        与 watcher 事件“两套入口漂移”隐患。**路 X / earlier milestone**：watcher 事件 payload
        为空 ``{}``，不再带 messages list；graph tick 经 ``sense_io`` 读 ``watcher_cursor``
        后消息自己填 chat_window。

        返回 invoke 是否成功。**路 X / earlier milestone**：watcher 无 ack，返值不再驱动 cursor
        推进（cursor 写权在 sense_io）；仅用于日志 / 成败计数。单 tick 失败
        （NodeContractError / LlmClientError）不杀 daemon，返 False。
        """
        from kindred.graph.errors import NodeContractError
        from kindred.graph.tick.sense_io import ColdStartError
        from kindred.llm.client import LlmClientError

        source = cast("TriggerSource", event.source)
        # ColdStartError 不 catch：run() 已在 invoke 前挡空库；若运行中 db 被清空
        # 而 raise，属致命异常态，让它冒泡到 run() 终止 daemon，而非静默空转。
        try:
            from kindred.runtime.observed_invoke import observed_invoke

            final = observed_invoke(
                lambda: graph.invoke(event.to_invoke_input()),
                telemetry=self._telemetry,
                run_kind="tick",
                execution_mode="real",
                trigger_source=source,
            )
        except (NodeContractError, LlmClientError) as exc:
            # 单 tick 失败不杀 daemon：记录后继续呼吸。路 X / earlier milestone：watcher 无 ack；
            # sense_io 若已推进 cursor，本批消息不会因下游失败回放。只有读取/推进前
            # 失败时，后续 tick 才可能重读，属于明确接受的 best-effort 感知。
            # 注：ColdStartError 是 NodeContractError 子类，但 run() 已挡空库，
            # 正常路径不会到这；真撞上（运行中清库）应冒泡终止，故下方显式排除。
            if isinstance(exc, ColdStartError):
                raise
            logger.error("tick failed (source=%s): %s: %s", source, type(exc).__name__, exc)
            return False
        finally:
            self._check_telemetry_thresholds()
        self._tick_count += 1
        logger.info(
            "tick #%d done (source=%s, act=%s, significance=%s)",
            self._tick_count,
            source,
            _act_flag(final),
            final.get("significance"),
        )
        return True

    def _fire_dream_if_due(self, db: KindredDB) -> bool:
        """清晨做梦调度（D6.8b）：should_dream 命中则 invoke dream graph。

        返回 **True 仅当做梦成功**（调用方据此 continue 跳过本轮 tick）；
        不该做梦（not due）**或做梦失败** → False（落到下方正常等待+tick）。

        - ``should_dream(wall_now, index_path)`` 读 index 最近 morning_dream 判该补做的
          dream_date（幂等补偿）；None → 不做梦 → False。
        - 命中 → 造 DreamState（triggered_at / dream_date / prev_state）走完整 5 步。
          成功 → land 写 index（标记 due 已做），返 True → 调用方 continue，下轮
          should_dream 不再 due → 走正常 tick。
        - **失败路径为何返 False（🔴 避 tight-loop）**：单次做梦失败
          （NodeContractError / LlmClientError）不杀 daemon（MEMORY R3），且未写
          index → 下轮仍 due。若失败也返 True 让调用方 continue → 立即重进本方法
          → 又失败 → 无间隔热循环烧 LLM。故失败返 False，落到正常等待路径
          （先 wait 一个 scheduler 间隔再跑 tick），下轮 while 顶再 due 时重试——
          重试被间隔限速（对齐 docs/11 §8.1「第二天清晨重试」）。
        """
        from kindred.graph.dream._schedule import should_dream
        from kindred.graph.errors import NodeContractError
        from kindred.llm.client import LlmClientError

        assert self._index_path is not None  # dream_graph 与 index_path 同生同灭
        # 🔴 N-1：只取一次墙钟。should_dream 判 dream_date 与 dream graph 的 triggered_at
        # 必须是**同一次 life 时钟上下文**——dream window（Step 1/2）用 triggered_at 的
        # tzinfo 算 [dream_date 00:00, +1 00:00)（_window.py / docs/11:166）；若 triggered_at
        # 另取 _now_iso() 两源分裂（特别是注入测试墙钟时）。
        now = self._wall_clock()
        due = should_dream(now, self._index_path)
        if due is None:
            return False
        prev_state = db.get_state_latest() or {}
        try:
            from kindred.runtime.observed_invoke import observed_invoke

            observed_invoke(
                lambda: self._dream_graph.invoke(  # type: ignore[union-attr]
                    {
                        "triggered_at": now.isoformat(timespec="seconds"),
                        "dream_date": due,
                        "prev_state": prev_state,
                    }
                ),
                telemetry=self._telemetry,
                run_kind="dream",
                execution_mode="real",
                dream_date=date.fromisoformat(due),
            )
        except (NodeContractError, LlmClientError) as exc:
            # 单次做梦失败不杀 daemon（软心降级 MEMORY R3）；返 False 落正常等待
            # 路径，下轮按间隔重试（避 tight-loop，见 docstring）。
            logger.error(
                "dream failed (dream_date=%s): %s: %s",
                due,
                type(exc).__name__,
                exc,
            )
            return False
        finally:
            self._check_telemetry_thresholds()
        self._tick_count += 1
        logger.info("dream #%d done (dream_date=%s)", self._tick_count, due)
        return True

    def _check_telemetry_thresholds(self) -> None:
        monitor = self._threshold_monitor
        if monitor is None:
            return
        try:
            monitor.check()
        except Exception as exc:  # noqa: BLE001 - injected monitors remain fail-open
            logger.warning(
                "telemetry degraded operation=threshold_monitor error_type=%s",
                type(exc).__name__,
            )

    def _reached_limit(self) -> bool:
        return self._max_ticks is not None and self._tick_count >= self._max_ticks

    def _now_iso(self) -> str:
        return _now_iso(self._timezone)


def _act_flag(final: Mapping[str, Any]) -> object:
    decision = final.get("act_decision")
    if isinstance(decision, Mapping):
        return decision.get("act")
    return None


def _now_iso(timezone_name: str = DEFAULT_WORLD_TIMEZONE) -> str:
    return life_now_iso(timezone_name, timespec="seconds")


def run_daemon(
    config: KindredConfig,
    prompt_dumper: PromptDumper,
    *,
    max_ticks: int | None = None,
) -> int:
    """便捷入口：构造并运行 HeartDaemon。cli.run 调这个。"""
    daemon = HeartDaemon(config, prompt_dumper, max_ticks=max_ticks)
    return daemon.run()
