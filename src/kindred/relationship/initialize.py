"""One-time Relationship initialization used internally by the OpenClaw installer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kindred.db import KindredDB
from kindred.relationship.models import (
    USER_SUBJECT_KEY,
    RelationshipBootstrap,
    RelationshipProfile,
    profile_from_bootstrap,
)
from kindred.resident import ResidentInitError, read_owned_persona_file


class RelationshipInitError(RuntimeError):
    """The bounded bootstrap could not be committed."""


@dataclass(frozen=True)
class RelationshipInitResult:
    status: Literal["created", "existing"]
    profile: RelationshipProfile


def initialize_user_relationship(
    *,
    db_path: Path,
    user_path: Path,
    project_user: Callable[[str], RelationshipBootstrap],
    confirm_identity: Callable[[], bool],
    confirm_projection: Callable[[RelationshipBootstrap], bool],
) -> RelationshipInitResult:
    """Create the reserved user row exactly once; an existing row wins unchanged."""
    with KindredDB.open(db_path) as db:
        existing = db.get_relationship(USER_SUBJECT_KEY)
    if existing is not None:
        return RelationshipInitResult(status="existing", profile=existing)

    try:
        user_text = read_owned_persona_file(user_path, name="USER.md", optional=True)
    except ResidentInitError as exc:
        raise RelationshipInitError("relationship bootstrap USER input invalid") from exc
    if user_text is not None:
        if not confirm_identity():
            raise RelationshipInitError("relationship bootstrap cancelled")
        try:
            bootstrap = project_user(user_text)
        except Exception as exc:
            raise RelationshipInitError("relationship bootstrap projection failed") from exc
        if not confirm_projection(bootstrap):
            raise RelationshipInitError("relationship bootstrap cancelled")
        profile = profile_from_bootstrap(bootstrap)
    else:
        profile = RelationshipProfile(
            subject_key=USER_SUBJECT_KEY,
            declared_role="unlabeled",
            trust=0,
            attachment=0,
            attraction=0,
            friction=0,
            updated_tick_id=None,
        )

    try:
        with KindredDB.open(db_path) as db, db.transaction():
            db.create_relationship(profile)
    except Exception as exc:
        raise RelationshipInitError("relationship bootstrap commit failed") from exc
    return RelationshipInitResult(status="created", profile=profile)
