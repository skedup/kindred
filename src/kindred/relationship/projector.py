"""Tick-local Relationship evidence projection and Host decision."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kindred.relationship.models import (
    RELATIONSHIP_MAGNITUDE_DELTAS,
    USER_SUBJECT_KEY,
    RelationshipChange,
    RelationshipFacet,
    RelationshipFacetProposal,
    RelationshipProfile,
    RelationshipRoleEvent,
)


@dataclass(frozen=True)
class RelationshipEvidenceContext:
    partner_lines: tuple[str, ...]
    previous_experience: str | None

    @property
    def available(self) -> bool:
        return bool(self.partner_lines or self.previous_experience)


def project_relationship_evidence(
    partner_lines: tuple[str, ...],
    previous_tick: dict[str, Any] | None,
) -> RelationshipEvidenceContext:
    previous_experience = _project_previous_experience(previous_tick)
    return RelationshipEvidenceContext(partner_lines, previous_experience)


def project_relationship_change(
    profile: RelationshipProfile,
    evidence: RelationshipEvidenceContext,
    proposals: list[RelationshipFacetProposal],
    role_event: RelationshipRoleEvent | None,
) -> RelationshipChange | None:
    if not evidence.available:
        return None

    target_role = None
    if (
        role_event is not None
        and role_event.target_role != profile.declared_role
        and (role_event.target_role in {"unlabeled", "hostile"} or bool(evidence.partner_lines))
    ):
        target_role = role_event.target_role
    preserve_large = target_role is not None
    deltas: list[tuple[RelationshipFacet, int]] = []
    for proposal in proposals:
        amount = RELATIONSHIP_MAGNITUDE_DELTAS[proposal.magnitude]
        if amount == 6 and not preserve_large:
            amount = 3
        signed = amount if proposal.direction == "up" else -amount
        current = getattr(profile, proposal.facet)
        projected = max(0, min(100, current + signed))
        if projected != current:
            deltas.append((proposal.facet, projected - current))

    if target_role is None and not deltas:
        return None
    return RelationshipChange(
        subject_key=USER_SUBJECT_KEY,
        target_role=target_role,
        facet_deltas=tuple(deltas),
    )


def _project_previous_experience(tick: dict[str, Any] | None) -> str | None:
    if not isinstance(tick, dict):
        return None
    activity = tick.get("activity")
    result = tick.get("act_result")
    if (
        not isinstance(activity, dict)
        or not isinstance(result, dict)
        or result.get("committed") is not True
        or "user" not in (activity.get("with_whom") or [])
        or result.get("kind") != "end_activity"
        or activity.get("step") != "settle"
    ):
        return None
    name = activity.get("name")
    desc = activity.get("desc")
    if not isinstance(name, str) or not name or not isinstance(desc, str):
        return None
    compact_desc = " ".join(desc.split())[:240].rstrip("。！？!?")
    if not compact_desc:
        return None
    return f"紧邻上一拍共同 Activity={name} 已完成；已提交经历：{compact_desc}。"
