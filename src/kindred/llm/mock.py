"""Mock LLM 客户端 —— deterministic fixture，集成测试用。

参考文档：docs/14-heart-graph.md §3.3 (sense.llm) / §3.4 (act.llm)

为什么独立 fixture
==================

集成测试需要"调 LLM 出 ``act_decision`` / ``act_result``"——但真接 LLM 既慢
又有外部依赖。用 mock 客户端按预定义场景返回 dict，让 graph 跑完整路径而
保持 deterministic。

mock LLM 提供 4 个确定场景：

- ``act_false`` —— sense.llm 决策不 act，T2 跳过，最常见的"心跳没事"
- ``start_activity`` —— sense.llm 决策启动新 activity（如 dine_out）
- ``advance_activity`` —— sense.llm 决策推进当前 activity 的下一 step
- ``failure`` —— LLM 返回坏 schema（让 ``validate_act_decision`` raise），
  覆盖反向测试

用法::

    client = MockLlmClient(scenario="start_activity")
    result = client.complete(prompt, role="sense.llm")
    # result 是 dict，下游 ``validate_act_decision(result["act_decision"])``

为什么不约束 act_result schema
==============================

14 §1.2 act_result 故意 free-form dict（"暂留 Optional[dict]，等 Phase β
手写 SKILL 时再细化字段约定"）。本 mock 按场景返回 ``committed: True/False``
+ ``diff: dict`` + ``failure_reason: str | None``——这只是 mock 内部约定，不进
state schema。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from kindred.llm.client import Role, ToolLoopError  # SSOT 在 client.py；本模块 re-export 保向后兼容
from kindred.llm.tools import (
    ToolEvent,
    ToolHandler,
    ToolLoopResult,
    call_tool_handler,
    effect_for_tool,
    tool_map,
)
from kindred_capability_sdk import ToolCall, ToolDef, ToolResult

# Mock 场景标识 — 4 个 deterministic 输出
Scenario = Literal[
    "act_false",  # sense.llm: 不 act，最常见心跳
    "start_activity",  # sense.llm: 启动新 activity
    "advance_activity",  # sense.llm: 推进当前 activity
    "end_activity",  # sense.llm: 结束当前 activity → settle 谢幕
    "failure",  # sense.llm: 返回坏 schema（反向测试）
]

# 节点角色 Role 真正定义在 kindred.llm.client.Role；这里仅 re-export。
# 优先 `from kindred.llm.client import Role`（与 LlmClient Protocol 同位置）。


@dataclass(frozen=True)
class MockToolRound:
    """Mock 工具环的一轮：模型发起哪些 tool call，以及期待 handler 返回什么。"""

    calls: tuple[ToolCall, ...]
    expected_results: tuple[ToolResult, ...] = ()


@dataclass(frozen=True)
class MockToolScript:
    """MockLlmClient 的工具环剧本。

    ``rounds`` 模拟模型在每轮发起的 tool calls；``final`` 是最后一轮模型给出的
    final dict。``final=None`` 表示模型耗尽轮数仍无 final，用于 ToolLoopError 反向测试。
    """

    rounds: tuple[MockToolRound, ...]
    final: dict[str, Any] | None


class MockLlmClient:
    """Deterministic LLM 客户端，按场景返回固定 dict。

    用法（集成测试）::

        client = MockLlmClient(scenario="start_activity")
        sense_out = client.complete("...prompt...", role="sense.llm")
        # sense_out = {
        #   "note": "...",
        #   "significance": 6,
        #   "act_decision": {"act": True, "kind": "start_activity", ...},
        #   "thought_diff": {...},
        # }

        act_out = client.complete_with_tools("...prompt...", role="act.llm", ...)
        # act_out.final = {"final_state_diff": {...}, "committed": True, ...}

    场景与 role 的组合
    ==================

    - sense.llm + act_false        → act=False
    - sense.llm + start_activity   → act=True, kind=start_activity
    - sense.llm + advance_activity → act=True, kind=advance_activity
    - sense.llm + end_activity     → act=True, kind=end_activity
    - sense.llm + failure          → 缺 reason 字段（让 validate raise）

    - act.llm + act_false          → 不应被调用（T2 跳过）；调了 raise
    - act.llm + start_activity     → committed=True, final_state_diff 含 activity 启动
    - act.llm + advance_activity   → committed=True, final_state_diff 含 step 推进
    - act.llm + end_activity       → committed=True, step 强制 settle（§0.4 谢幕）
    - act.llm + failure            → committed=False, failure_reason="mock failure"

    单 client 实例固定单一场景；切换需重新 ``MockLlmClient(scenario=...)``。
    """

    def __init__(
        self,
        scenario: Scenario = "act_false",
        *,
        tool_script: MockToolScript | None = None,
    ) -> None:
        self.scenario: Scenario = scenario
        self.tool_script = tool_script
        self.call_count: int = 0  # 测试用：统计 complete 被调几次
        self.last_prompt: str | None = None
        self.last_role: Role | None = None

    def complete(self, prompt: str, *, role: Role) -> dict[str, Any]:
        """Mock LLM 调用，根据 (scenario, role) 返回固定 dict。

        Parameters
        ----------
        prompt
            渲染好的 prompt 字符串（mock 不解析，只 ``last_prompt`` 记录）。
        role
            "sense.llm" / "act.llm"——决定输出 schema。

        Returns
        -------
        dict
            按 (scenario, role) 组合返回 deterministic dict。

        Raises
        ------
        ValueError
            ``role="act.llm"`` + ``scenario="act_false"``——T2 不应在 act=False
            时被调；调了说明 graph 路由错。这是路由防御的反向 testbed。
        """
        self.call_count += 1
        self.last_prompt = prompt
        self.last_role = role

        if role == "sense.llm":
            return self._sense_response()
        if role == "act.llm":
            return self._act_response()
        if role == "dream.summarize":
            return self._summary_response()
        if role == "dream.reflect":
            return self._reflect_response()
        if role == "dream.gate":
            return self._gate_response()
        if role == "dream.excerpt":
            return {"soul_excerpt": "重视真诚，也愿意在生活里保持好奇与温柔。"}
        # 不可达 —— Literal 已穷举
        raise AssertionError(f"unexpected role: {role!r}")

    def complete_with_tools(
        self,
        prompt: str,
        *,
        role: Role,
        tools: Sequence[ToolDef],
        handler: ToolHandler,
        max_rounds: int,
    ) -> ToolLoopResult:
        """按预设剧本运行工具环，不调用真实 LLM。

        剧本支持：
        - happy path：若干轮工具调用后返回 final；
        - handler 返回 error ToolResult：expected_results 可钉住错误结果；
        - handler 抛错：按真实 client 契约包成 error ToolResult；
        - 轮数耗尽：raise ToolLoopError，携带已发生 tool_events。
        """

        self.call_count += 1
        self.last_prompt = prompt
        self.last_role = role

        if max_rounds < 1:
            raise ValueError("max_rounds must be >= 1")
        if self.tool_script is None:
            final = self._act_response()
            known_tools = tool_map(tuple(tools))
            default_events: list[ToolEvent] = []
            diff = final.get("final_state_diff")
            action_step = diff.get("current_state") if isinstance(diff, dict) else None
            if (
                final.get("committed") is True
                and isinstance(action_step, str)
                and "lock_action" in known_tools
            ):
                lock_call = ToolCall("lock_action", {"action_step": action_step}, "mock-lock")
                lock_result = call_tool_handler(handler, lock_call)
                default_events.append(
                    ToolEvent(
                        1, lock_call, lock_result, effect_for_tool(known_tools, lock_call.name)
                    )
                )
                if (
                    not lock_result.is_error
                    and lock_result.response.get("entry") is True
                    and "resolve_action_outcome" in known_tools
                ):
                    outcome_call = ToolCall("resolve_action_outcome", {}, "mock-outcome")
                    default_events.append(
                        ToolEvent(
                            2,
                            outcome_call,
                            call_tool_handler(handler, outcome_call),
                            effect_for_tool(known_tools, outcome_call.name),
                        )
                    )
            return ToolLoopResult(
                final=final,
                tool_events=tuple(default_events),
                rounds=len(default_events) + 1,
            )

        known_tools = tool_map(tuple(tools))
        events: list[ToolEvent] = []
        rounds_done = 0
        for round_index, scripted_round in enumerate(self.tool_script.rounds, start=1):
            if round_index > max_rounds:
                break
            rounds_done = round_index
            results: list[ToolResult] = []
            for call in scripted_round.calls:
                result = self._mock_tool_result(call, handler, known_tools)
                results.append(result)
                events.append(
                    ToolEvent(
                        round_index=round_index,
                        call=call,
                        result=result,
                        effect=effect_for_tool(known_tools, call.name),
                    )
                )
            if scripted_round.expected_results:
                self._assert_expected_results(scripted_round.expected_results, tuple(results))

        if rounds_done < len(self.tool_script.rounds):
            raise ToolLoopError(
                "MockLlmClient: tool loop exhausted without final",
                tool_events=events,
                rounds=max_rounds,
            )
        if self.tool_script.final is None:
            raise ToolLoopError(
                "MockLlmClient: tool loop exhausted without final",
                tool_events=events,
                rounds=max_rounds,
            )
        if rounds_done >= max_rounds:
            raise ToolLoopError(
                "MockLlmClient: tool loop exhausted before final",
                tool_events=events,
                rounds=max_rounds,
            )
        return ToolLoopResult(
            final=self.tool_script.final,
            tool_events=tuple(events),
            rounds=rounds_done + 1,
        )

    @staticmethod
    def _mock_tool_result(
        call: ToolCall,
        handler: ToolHandler,
        known_tools: dict[str, ToolDef],
    ) -> ToolResult:
        if call.name not in known_tools:
            return ToolResult.error(
                call,
                error_type="UnknownTool",
                message="tool is not registered",
            )
        return call_tool_handler(handler, call)

    @staticmethod
    def _assert_expected_results(
        expected: tuple[ToolResult, ...],
        actual: tuple[ToolResult, ...],
    ) -> None:
        if actual != expected:
            raise AssertionError(
                f"mock tool results mismatch: expected={expected!r}, actual={actual!r}"
            )

    # ─── dream.summarize 输出 ────────────────────────────

    def _summary_response(self) -> dict[str, Any]:
        """dream Step 1.summarize 的 deterministic 摘要（不分 scenario）。

        scenario 是 sense/act 专用维度，dream 总结不区分场景，固定返一段
        合 ``SummaryResponse`` schema 的 markdown 摘要。
        """
        return {
            "summary": (
                "# 昨日摘要\n\n"
                "- 关键事件：（mock）一天平淡地过去了。\n"
                "- 情绪轨迹：mood 平稳。\n"
                "- 做了什么：读书、发呆。\n"
                "- 自反馈：暂无值得改灵魂的线索。"
            ),
        }

    # ─── dream.reflect 输出 ──────────────────

    def _reflect_response(self) -> dict[str, Any]:
        """dream Step 3.reflect 的 deterministic diff 提议（不分 scenario）。

        固定返 ``skip_today``——最安全的默认（不提议任何灵魂改写，与 D6.1 桩
        语义一致）。真实改写提议的多场景由 real client / 专项测覆盖。
        """
        return {
            "decision": "skip_today",
            "reasoning": "（mock）昨夜平淡，没有值得改灵魂的线索。",
            "changes": [],
        }

    # ─── dream.gate 输出 ──────────────────

    def _gate_response(self) -> dict[str, Any]:
        """dream Step 4.gate 的 deterministic 闸门裁决（不分 scenario）。

        固定返 ``pass``——与 D6.1 桩语义一致（桩 reflect 恒 skip_today/空 changes，
        无可拦截）。真实 block/warn 场景由 real client / 专项测覆盖。
        """
        return {
            "verdict": "pass",
            "blocked": [],
            "warnings": [],
            "reasoning": "（mock）changes 未触任何规则，全部放行。",
        }

    # ─── sense.llm 输出 ────────────────────────────────────────

    def _sense_response(self) -> dict[str, Any]:
        if self.scenario == "act_false":
            return {
                "observed_user_present": None,
                "affect_event_response": {},
                "note": "杯子还有半口，没什么特别想动的。",
                "significance": 3,
                "act_decision": {
                    "act": False,
                    "kind": None,
                    "target_activity": None,
                    "reason": "心跳：底色还在，没必要起新动作。",
                },
                "thought_diff": {
                    "add": [],
                    "remove": [],
                },
                "ambience": "杯沿还温着，屋里安静得像一口慢呼吸。",
            }

        if self.scenario == "start_activity":
            return {
                "observed_user_present": None,
                "affect_event_response": {},
                "note": "肚子有点饿，想去探一家新店。",
                "significance": 5,
                "act_decision": {
                    "act": True,
                    "kind": "start_activity",
                    "target_activity": "dine_out",
                    "reason": "hunger 涨到顶了，该去吃了。",
                },
                "thought_diff": {
                    "add": [
                        {
                            "description": "肚子饿了",
                            "mood_w": -3,
                            "expire_at": None,
                            "tag": "body",
                        }
                    ],
                    "remove": [],
                },
                "ambience": "胃里空了一点，外面的街灯像在招手。",
            }

        if self.scenario == "advance_activity":
            return {
                "observed_user_present": None,
                "affect_event_response": {},
                "note": "继续手上的事，节奏顺。",
                "significance": 4,
                "act_decision": {
                    "act": True,
                    "kind": "advance_activity",
                    "target_activity": "explore_food",
                    "reason": "上一步收尾了，推进下一步。",
                },
                "thought_diff": {
                    "add": [],
                    "remove": [],
                },
                "ambience": "手头的事有了顺滑的惯性，空气里带着一点专注。",
            }

        if self.scenario == "end_activity":
            return {
                "observed_user_present": None,
                "affect_event_response": {},
                "note": "吃满足了，hunger 也降下来了，这顿下馆子可以收尾了。",
                "significance": 6,
                "act_decision": {
                    "act": True,
                    "kind": "end_activity",
                    "target_activity": "dine_out",
                    "reason": "终止条件满足：吃满足了。",
                },
                "thought_diff": {"add": [], "remove": []},
                "ambience": "吃饱后的余温还在，周围慢慢安静下来。",
            }

        if self.scenario == "failure":
            # 缺 act_decision.reason —— validate_act_decision 应 raise
            return {
                "observed_user_present": None,
                "affect_event_response": {},
                "note": "...",
                "significance": 5,
                "act_decision": {
                    "act": False,
                    "kind": None,
                    "target_activity": None,
                    # 缺 "reason" 字段
                },
                "thought_diff": {"add": [], "remove": []},
                "ambience": "杯沿还温着，屋里安静得像一口慢呼吸。",
            }

        # 不可达
        raise AssertionError(f"unexpected scenario: {self.scenario!r}")

    # ─── act.llm 输出 ──────────────────────────────────────────

    def _act_response(self) -> dict[str, Any]:
        if self.scenario == "act_false":
            # T2 不应被调到 —— 路由错的反向 testbed
            raise ValueError(
                "act.llm called with scenario='act_false'; T2 should be "
                "skipped when act_decision.act is False (graph routing bug)."
            )

        if self.scenario == "start_activity":
            # 14 §3.4：LLM 最终 JSON 返 final_state_diff（state 变更）+ committed /
            # failure_reason。tool_trace / 地点事件由真实工具调用生成，act_result
            # 由节点构造，不在这。step =
            # 原子状态（walk/eat），与 current_state 合一——节点从 current_state 落进
            # activity.step（单一真相源），所以 activity dict 不给 step。start 时心
            # 只给血肉 desc/engagement/for_what；name/started_at 是骨架，由节点强制
            # （name=target、started_at=系统时钟），心不给（防幻觉）。desc 同时体现
            # activity（探店）+ step（在路上）。
            return {
                "final_state_diff": {
                    "activity": {
                        "desc": "去探一家附近的新餐厅，在去的路上",
                        "engagement": 0.6,
                        "with_whom": [],
                        "for_what": "hunger 涨到顶了",
                    },
                    # start 时在 walk 态——energy↓med / fatigue↑med 由 Host 确定性应用。
                    "current_state": "walk",
                },
                "committed": True,
                "failure_reason": None,
            }

        if self.scenario == "advance_activity":
            # “advance” 语义：step 从 walk 推进到 eat（同一 activity 内原子状态推进）。
            # step 由 current_state 落。desc 体现 activity + step 两层。advance 只更
            # 血肉（desc/engagement）；name/started_at 是骨架，节点保留现有 activity
            # 的（心给了也被忽略），所以这里不给。
            return {
                "final_state_diff": {
                    "activity": {
                        "desc": "去探一家附近的新餐厅，坐下吃上了",
                        "engagement": 0.7,
                    },
                    "current_state": "eat",
                },
                "committed": True,
                "failure_reason": None,
            }

        if self.scenario == "end_activity":
            # §0.4：end_activity → settle 谢幕。心给回望血肉（desc 更新）+ 小档
            # Needs/Affect 回味。**不给 current_state**——settle 由节点强制落
            # （谢幕一拍由框架约定，不是心选的原子动作）。骨架 name/started_at 保留。
            return {
                "final_state_diff": {
                    "activity": {
                        "desc": "刚吃完，坐着回味这一程，心里暖暖的",
                        "engagement": 0.3,
                    },
                    # end 没有 Action effect；回味只提交一项 small signed delta。
                    "affect": {"clarity": 4},
                },
                "committed": True,
                "failure_reason": None,
            }

        if self.scenario == "failure":
            # committed=False：想做没做成，无 final_state_diff。
            return {
                "committed": False,
                "failure_reason": "mock failure: tool unavailable",
            }

        # 不可达
        raise AssertionError(f"unexpected scenario: {self.scenario!r}")


__all__ = ["MockLlmClient", "MockToolRound", "MockToolScript", "Role", "Scenario"]
