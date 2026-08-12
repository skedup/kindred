"""Host-owned Action lock and same-tick Outcome session."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kindred.activity import ActivitySkillError, load_activity_skill
from kindred.activity.action import is_valid_action_name, load_atomic_action
from kindred.activity.skill import ActivitySkill
from kindred.graph.tick._act_contract import ActLlmContractError
from kindred_capability_sdk import ToolCall, ToolDef, ToolResult

LOCK_ACTION = "lock_action"
RESOLVE_OUTCOME = "resolve_action_outcome"
ACTION_OUTCOME_TOOL_DEFS = (
    ToolDef(
        LOCK_ACTION,
        "Lock this tick's Action before its effects; allowed preparation may happen first.",
        {
            "type": "OBJECT",
            "properties": {"action_step": {"type": "STRING"}},
            "required": ["action_step"],
        },
        "read_only",
    ),
    ToolDef(
        RESOLVE_OUTCOME,
        "After Action tools, resolve its A-E experience grade immediately before final JSON.",
        {"type": "OBJECT", "properties": {}, "required": []},
        "read_only",
    ),
)


@dataclass
class ActionOutcomeKernelSession:
    kind: str
    activity_name: str
    triggered_at: str | None
    current_activity: dict[str, Any]
    activities_dir: Path
    actions_dir: Path
    run_started_at: str | None = field(default=None, init=False)
    prior_step: str | None = field(default=None, init=False)
    skill: ActivitySkill | None = field(default=None, init=False)
    locked_action: str | None = field(default=None, init=False)
    locked_capabilities: frozenset[str] = field(default_factory=frozenset, init=False)
    locked_bindings: frozenset[str] = field(default_factory=frozenset, init=False)
    is_entry: bool = field(default=False, init=False)
    lock_attempts: int = field(default=0, init=False)
    outcome_attempts: int = field(default=0, init=False)
    resolved_count: int = field(default=0, init=False)
    post_outcome_attempts: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        current = self.current_activity
        if current.get("name") != self.activity_name:
            current = {}
        self.run_started_at = (
            self.triggered_at if self.kind == "start_activity" else _text(current.get("started_at"))
        )
        self.prior_step = None if self.kind == "start_activity" else _text(current.get("step"))
        try:
            self.skill = load_activity_skill(
                self.activity_name, activities_dir=self.activities_dir, actions_dir=self.actions_dir
            )
        except ActivitySkillError:
            pass

    @property
    def tool_defs(self) -> tuple[ToolDef, ...]:
        return ACTION_OUTCOME_TOOL_DEFS

    def handles(self, name: str) -> bool:
        return name in {LOCK_ACTION, RESOLVE_OUTCOME}

    def handle(self, call: ToolCall) -> ToolResult:
        return self._lock(call) if call.name == LOCK_ACTION else self._resolve(call)

    def guard_tool(
        self,
        call: ToolCall,
        *,
        owner: str,
        effect: str = "",
        binding_id: str | None = None,
    ) -> ToolResult | None:
        if rejected := self._after_outcome(call):
            return rejected
        if self.locked_action is None:
            allowed = call.name == "choose_destination" or (
                effect == "read_only" and call.name in {"find_places", "list_inventory"}
            )
            return None if allowed else _error(call, "ActionLockRequired")
        if owner in {"location", "location_kernel"}:
            binding_id = binding_id or _text(call.args.get("binding_id"))
            if binding_id is None:
                return None
            return (
                None
                if binding_id in self.locked_bindings
                else _error(call, "ActionBindingMismatch")
            )
        return (
            None if owner in self.locked_capabilities else _error(call, "ActionCapabilityMismatch")
        )

    def validate_committed(self, diff: dict[str, Any]) -> None:
        if self.locked_action is None or self.lock_attempts != 1:
            raise ActLlmContractError("T2.act.llm: committed Action requires exactly one lock")
        if diff.get("current_state") != self.locked_action:
            raise ActLlmContractError("T2.act.llm: final Action differs from locked Action")
        if self.is_entry:
            if self.resolved_count != 1 or self.outcome_attempts != 1:
                raise ActLlmContractError("T2.act.llm: Action entry requires exactly one Outcome")
        elif self.resolved_count:
            raise ActLlmContractError("T2.act.llm: same-step Action cannot resolve an Outcome")
        if self.post_outcome_attempts:
            raise ActLlmContractError("T2.act.llm: tool call attempted after Outcome")

    def debug_shape(self) -> dict[str, Any]:
        return {
            "action_lock_status": "locked" if self.locked_action else "missing",
            "outcome_status": "resolved" if self.resolved_count else "missing",
            "entry": self.is_entry,
            "lock_attempts": self.lock_attempts,
            "outcome_attempts": self.outcome_attempts,
            "resolved_count": self.resolved_count,
        }

    def _after_outcome(self, call: ToolCall) -> ToolResult | None:
        if not self.resolved_count:
            return None
        self.post_outcome_attempts += 1
        return _error(call, "OutcomeAlreadyResolved")

    def _lock(self, call: ToolCall) -> ToolResult:
        self.lock_attempts += 1
        if rejected := self._after_outcome(call):
            return rejected
        step = call.args.get("action_step")
        if (
            set(call.args) != {"action_step"}
            or not isinstance(step, str)
            or not is_valid_action_name(step)
        ):
            return _error(call, "InvalidActionStep")
        if self.locked_action is not None:
            error = (
                "ActionAlreadyLocked" if step == self.locked_action else "ActionIdentityConflict"
            )
            return _error(call, error)
        if self.skill is None or self.run_started_at is None or self.triggered_at is None:
            return _error(call, "ActionPackageInvalid")
        uses = [use for use in self.skill.uses if use.action == step]
        if len(uses) != 1:
            return _error(call, "InvalidActionStep")
        try:
            action = load_atomic_action(step, actions_dir=self.actions_dir)
            if self.kind == "advance_activity":
                prior = [use for use in self.skill.uses if use.action == self.prior_step]
                if len(prior) != 1 or self.prior_step is None:
                    return _error(call, "ActionPackageInvalid")
                load_atomic_action(self.prior_step, actions_dir=self.actions_dir)
        except ActivitySkillError:
            return _error(call, "ActionPackageInvalid")
        self.locked_action = step
        self.locked_capabilities = frozenset(action.capabilities)
        self.locked_bindings = frozenset(uses[0].bind_places.values())
        self.is_entry = self.kind == "start_activity" or step != self.prior_step
        return _ok(call, {"status": "locked", "action_step": step, "entry": self.is_entry})

    def _resolve(self, call: ToolCall) -> ToolResult:
        self.outcome_attempts += 1
        if call.args:
            return _error(call, "InvalidOutcomeArgs")
        if self.locked_action is None:
            return _error(call, "ActionNotLocked")
        if rejected := self._after_outcome(call):
            return rejected
        if not self.is_entry:
            return _error(call, "NotActionEntry", status="not_entry")
        grade = _grade_for_identity(
            [
                self.triggered_at,
                self.kind,
                self.activity_name,
                self.run_started_at,
                self.prior_step,
                self.locked_action,
            ]
        )
        self.resolved_count = 1
        return _ok(call, {"status": "resolved", "action_step": self.locked_action, "grade": grade})


def _grade_for_identity(identity: list[str | None]) -> str:
    raw = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
    bucket = int.from_bytes(hashlib.sha256(raw).digest(), "big") % 100
    return _grade_for_bucket(bucket)


def _grade_for_bucket(bucket: int) -> str:
    return next(grade for upper, grade in _GRADE_BOUNDS if bucket < upper)


_GRADE_BOUNDS = ((5, "A"), (25, "B"), (75, "C"), (95, "D"), (100, "E"))


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _ok(call: ToolCall, response: dict[str, Any]) -> ToolResult:
    return ToolResult.ok(call, response).with_trace(
        args={"arg_keys": sorted(call.args)}, response={"status": "attempted"}
    )


def _error(call: ToolCall, error_type: str, *, status: str = "error") -> ToolResult:
    result = ToolResult.error(
        call, error_type=error_type, message="Action outcome request rejected"
    )
    return result.with_trace(
        args={"arg_keys": sorted(call.args)},
        response={"status": status, "error_type": error_type},
    )
