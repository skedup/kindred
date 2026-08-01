from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any


class ArtifactCommitDataError(ValueError): ...


@dataclass(frozen=True)
class ArtifactCommitRow:
    tick_id: int
    artifact_ordinal: int
    artifact_ref: str
    producer: str
    profile: str
    activity_name: str
    activity_started_at: str
    created_at: str


_COLUMNS = "tick_id, artifact_ordinal, artifact_ref, producer, profile, activity_name, activity_started_at, created_at"  # noqa: E501


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactCommitDataError(f"{path}: expected non-empty string")
    return value


def _descriptor(value: object, path: str) -> tuple[str, str, str]:
    if not isinstance(value, dict):
        raise ArtifactCommitDataError(f"{path}: expected object")
    keys = ("artifact_ref", "producer", "profile")
    result = [_text(value.get(key), f"{path}.{key}") for key in keys]
    return result[0], result[1], result[2]


def maybe_insert_artifact_commits(
    conn: sqlite3.Connection,
    *,
    tick_id: int,
    created_at: str,
    activity: dict[str, Any],
    act_result: dict[str, Any] | None,
) -> None:
    if not isinstance(act_result, dict) or act_result.get("committed") is not True:
        return
    artifacts = act_result.get("artifacts")
    if artifacts in (None, []):
        return
    if not isinstance(artifacts, list):
        raise ArtifactCommitDataError("act_result.artifacts: expected list")
    activity_values = (
        _text(activity.get("name"), "activity.name"),
        _text(activity.get("started_at"), "activity.started_at"),
        _text(created_at, "created_at"),
    )
    rows = [
        (
            tick_id,
            ordinal,
            *_descriptor(value, f"act_result.artifacts[{ordinal}]"),
            *activity_values,
        )
        for ordinal, value in enumerate(artifacts)
    ]
    conn.executemany(
        "INSERT INTO artifact_commit VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def backfill_artifact_commits(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        WITH candidates AS (
            SELECT source.id AS tick_id, CAST(i.key AS INTEGER) AS ordinal,
                   CASE i.type WHEN 'object' THEN json_extract(i.value,'$.artifact_ref') END ref,
                   CASE i.type WHEN 'object' THEN json_extract(i.value,'$.producer') END producer,
                   CASE i.type WHEN 'object' THEN json_extract(i.value,'$.profile') END profile,
                   json_extract(source.activity, '$.name') AS activity_name,
                   json_extract(source.activity, '$.started_at') AS started_at, source.ts
            FROM (
                SELECT id, ts, CASE WHEN json_valid(activity) THEN activity END AS activity,
                       CASE WHEN json_valid(act_result) THEN act_result END AS act_result
                FROM tick
            ) AS source
            JOIN json_each(source.act_result, '$.artifacts') AS i
            WHERE json_type(source.act_result, '$.committed') = 'true'
              AND json_type(source.act_result, '$.artifacts') = 'array'
              AND i.type = 'object'
        )
        INSERT OR IGNORE INTO artifact_commit
        SELECT tick_id, ordinal, ref, producer, profile, activity_name, started_at, ts
        FROM candidates
        WHERE typeof(ref) = 'text' AND trim(ref) <> ''
          AND typeof(producer) = 'text' AND trim(producer) <> ''
          AND typeof(profile) = 'text' AND trim(profile) <> ''
          AND typeof(activity_name) = 'text' AND trim(activity_name) <> ''
          AND typeof(started_at) = 'text' AND trim(started_at) <> ''
        """
    )


def _row(row: sqlite3.Row) -> ArtifactCommitRow:
    return ArtifactCommitRow(*(row[column] for column in _COLUMNS.split(", ")))


def list_committed_artifacts(
    conn: sqlite3.Connection,
    *,
    limit: int,
    cursor: tuple[int, int] | None = None,
) -> list[ArtifactCommitRow]:
    where = "" if cursor is None else "WHERE tick_id < ? OR (tick_id = ? AND artifact_ordinal > ?)"
    params = (limit,) if cursor is None else (cursor[0], cursor[0], cursor[1], limit)
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM artifact_commit {where} "  # noqa: S608
        "ORDER BY tick_id DESC, artifact_ordinal ASC LIMIT ?",
        params,
    ).fetchall()
    return [_row(row) for row in rows]


def get_committed_artifact(
    conn: sqlite3.Connection,
    *,
    tick_id: int,
    artifact_ordinal: int,
) -> ArtifactCommitRow | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM artifact_commit "  # noqa: S608
        "WHERE tick_id = ? AND artifact_ordinal = ?",
        (tick_id, artifact_ordinal),
    ).fetchone()
    return None if row is None else _row(row)


def get_activity_artifacts(
    conn: sqlite3.Connection,
    *,
    activity_name: str,
    started_at: str,
) -> list[dict[str, str]]:
    rows = conn.execute(
        "SELECT artifact_ref, producer, profile FROM artifact_commit "
        "WHERE activity_name = ? AND activity_started_at = ? "
        "ORDER BY tick_id, artifact_ordinal",
        (activity_name, started_at),
    ).fetchall()
    return [{key: row[key] for key in ("artifact_ref", "producer", "profile")} for row in rows]
