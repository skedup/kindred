"""SQLite facade helpers for current Relationship profiles."""

from __future__ import annotations

import sqlite3

from pydantic import ValidationError

from kindred.relationship.models import RelationshipChange, RelationshipProfile

_COLUMNS = "subject_key, declared_role, trust, attachment, attraction, friction, updated_tick_id"


class RelationshipDataError(RuntimeError):
    """A stored Relationship row violates the strict domain contract."""


def _row_to_profile(row: sqlite3.Row) -> RelationshipProfile:
    try:
        return RelationshipProfile.model_validate(dict(row))
    except (TypeError, ValidationError):
        raise RelationshipDataError("relationship row validation_failed") from None


def get_relationship(conn: sqlite3.Connection, subject_key: str) -> RelationshipProfile | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM relationship_profile WHERE subject_key = ?",  # noqa: S608
        (subject_key,),
    ).fetchone()
    return None if row is None else _row_to_profile(row)


def _create_relationship_impl(conn: sqlite3.Connection, profile: RelationshipProfile) -> None:
    conn.execute(
        f"INSERT INTO relationship_profile ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",  # noqa: S608
        (
            profile.subject_key,
            profile.declared_role,
            profile.trust,
            profile.attachment,
            profile.attraction,
            profile.friction,
            profile.updated_tick_id,
        ),
    )


def _apply_relationship_change_impl(
    conn: sqlite3.Connection,
    change: RelationshipChange,
    tick_id: int,
) -> bool:
    current = get_relationship(conn, change.subject_key)
    if current is None:
        raise RelationshipDataError("relationship target row missing")
    values = {
        "trust": current.trust,
        "attachment": current.attachment,
        "attraction": current.attraction,
        "friction": current.friction,
    }
    for facet, delta in change.facet_deltas:
        values[facet] = max(0, min(100, values[facet] + delta))
    target_role = change.target_role or current.declared_role
    changed = target_role != current.declared_role or any(
        values[name] != getattr(current, name) for name in values
    )
    if not changed:
        return False
    projected = RelationshipProfile(
        subject_key=current.subject_key,
        declared_role=target_role,
        updated_tick_id=tick_id,
        **values,
    )
    conn.execute(
        "UPDATE relationship_profile SET declared_role = ?, trust = ?, attachment = ?, "
        "attraction = ?, friction = ?, updated_tick_id = ? WHERE subject_key = ?",
        (
            projected.declared_role,
            projected.trust,
            projected.attachment,
            projected.attraction,
            projected.friction,
            projected.updated_tick_id,
            projected.subject_key,
        ),
    )
    return True
