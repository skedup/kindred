"""Read-only startup check for the reserved user Relationship profile."""

from __future__ import annotations

from typing import Protocol

from kindred.relationship.models import USER_SUBJECT_KEY, RelationshipProfile


class RelationshipReader(Protocol):
    def get_relationship(self, subject_key: str) -> RelationshipProfile | None: ...


class RelationshipPreflightError(RuntimeError):
    """The reserved user profile is missing or cannot be strictly read."""


def require_user_relationship(reader: RelationshipReader) -> RelationshipProfile:
    try:
        profile = reader.get_relationship(USER_SUBJECT_KEY)
    except Exception:
        raise RelationshipPreflightError(
            "relationship preflight path=relationship_profile.user reason=invalid_or_unreadable"
        ) from None
    if profile is None:
        raise RelationshipPreflightError(
            "relationship preflight path=relationship_profile.user reason=missing"
        )
    return profile
