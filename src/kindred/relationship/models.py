"""Strict current Relationship profile and one-time bootstrap contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kindred.state._types import SAFE_NAME_PATTERN

RelationshipRole = Literal["unlabeled", "friend", "lover", "hostile"]
RelationshipLevel = Literal["none", "low", "medium", "high"]
RelationshipFacet = Literal["trust", "attachment", "attraction", "friction"]
_RELATIONSHIP_FACETS = frozenset({"trust", "attachment", "attraction", "friction"})
RELATIONSHIP_MAGNITUDE_DELTAS = {"small": 1, "medium": 3, "large": 6}
USER_SUBJECT_KEY = "user"
RELATIONSHIP_LEVEL_VALUES: dict[RelationshipLevel, int] = {
    "none": 0,
    "low": 30,
    "medium": 60,
    "high": 90,
}


class RelationshipProfile(BaseModel):
    """State-external, immutable current relationship facts for one subject."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_key: str = Field(
        strict=True, min_length=1, max_length=64, pattern=SAFE_NAME_PATTERN.pattern
    )
    declared_role: RelationshipRole
    trust: int = Field(strict=True, ge=0, le=100)
    attachment: int = Field(strict=True, ge=0, le=100)
    attraction: int = Field(strict=True, ge=0, le=100)
    friction: int = Field(strict=True, ge=0, le=100)
    updated_tick_id: int | None = Field(default=None, strict=True, gt=0)


class RelationshipFacetProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    facet: RelationshipFacet
    direction: Literal["up", "down"]
    magnitude: Literal["small", "medium", "large"]


class RelationshipRoleEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_role: RelationshipRole


@dataclass(frozen=True)
class RelationshipChange:
    subject_key: str
    target_role: RelationshipRole | None
    facet_deltas: tuple[tuple[RelationshipFacet, int], ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.subject_key, str)
            or len(self.subject_key) > 64
            or SAFE_NAME_PATTERN.fullmatch(self.subject_key) is None
        ):
            raise ValueError("invalid relationship change subject")
        if self.target_role not in {None, "unlabeled", "friend", "lover", "hostile"}:
            raise ValueError("invalid relationship change role")
        if not isinstance(self.facet_deltas, tuple) or any(
            not isinstance(item, tuple) or len(item) != 2 for item in self.facet_deltas
        ):
            raise ValueError("invalid relationship change facets")
        facets = [facet for facet, _ in self.facet_deltas]
        if len(facets) > 2 or len(set(facets)) != len(facets):
            raise ValueError("invalid relationship change facets")
        if not set(facets).issubset(_RELATIONSHIP_FACETS):
            raise ValueError("invalid relationship change facets")
        if self.target_role is None and not facets:
            raise ValueError("relationship change cannot be empty")
        # RelationshipChange stores the applied post-clamp delta. A fixed
        # 1/3/6 proposal can therefore become any non-zero residual up to 6.
        if any(
            type(delta) is not int
            or delta == 0
            or abs(delta) > max(RELATIONSHIP_MAGNITUDE_DELTAS.values())
            for _, delta in self.facet_deltas
        ):
            raise ValueError("invalid relationship change delta")


class RelationshipBootstrap(BaseModel):
    """Qualitative output of the install-time USER-only LLM projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    declared_role: RelationshipRole
    trust: RelationshipLevel
    attachment: RelationshipLevel
    attraction: RelationshipLevel
    friction: RelationshipLevel
    summary: str = Field(strict=True, min_length=1, max_length=240)

    @field_validator("summary", mode="before")
    @classmethod
    def _strip_summary(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


def profile_from_bootstrap(bootstrap: RelationshipBootstrap) -> RelationshipProfile:
    """Map qualitative bootstrap levels to fixed, Host-owned initial values."""
    return RelationshipProfile(
        subject_key=USER_SUBJECT_KEY,
        declared_role=bootstrap.declared_role,
        trust=RELATIONSHIP_LEVEL_VALUES[bootstrap.trust],
        attachment=RELATIONSHIP_LEVEL_VALUES[bootstrap.attachment],
        attraction=RELATIONSHIP_LEVEL_VALUES[bootstrap.attraction],
        friction=RELATIONSHIP_LEVEL_VALUES[bootstrap.friction],
        updated_tick_id=None,
    )
