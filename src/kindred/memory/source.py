"""Read-only canonical episode source and deterministic document projection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kindred.db.connection import connect_readonly
from kindred.memory.contracts import EpisodeDocument
from kindred.memory.normalization import canonical_text

PROJECTION_REVISION = "canonical-episode-v1"


class EpisodeSourceError(RuntimeError):
    """Canonical episode data is unavailable or violates the projection contract."""


def _clean_catalog(activity_descriptions: Mapping[str, str]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for raw_name, raw_description in activity_descriptions.items():
        name = canonical_text(raw_name)
        description = canonical_text(raw_description)
        if not name or not description:
            raise ValueError("activity catalog names and descriptions must not be blank")
        if name in cleaned:
            raise ValueError(f"duplicate normalized activity name: {name}")
        cleaned[name] = description
    return cleaned


def activity_catalog_digest(activity_descriptions: Mapping[str, str]) -> str:
    """Hash the exact catalog projection input used by the memory index."""

    payload = json.dumps(
        sorted(_clean_catalog(activity_descriptions).items()),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _json_object(row: sqlite3.Row, column: str, tick_id: int) -> dict[str, Any]:
    raw = row[column]
    if not isinstance(raw, str):
        raise EpisodeSourceError(f"episode:{tick_id} {column} must be JSON text")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EpisodeSourceError(f"episode:{tick_id} {column} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise EpisodeSourceError(f"episode:{tick_id} {column} must be a JSON object")
    return value


def _required_text(value: object, field_name: str, tick_id: int) -> str:
    if not isinstance(value, str):
        raise EpisodeSourceError(f"episode:{tick_id} {field_name} must be text")
    cleaned = canonical_text(value)
    if not cleaned:
        raise EpisodeSourceError(f"episode:{tick_id} {field_name} must not be blank")
    return cleaned


def _optional_text(value: object, field_name: str, tick_id: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EpisodeSourceError(f"episode:{tick_id} {field_name} must be text or null")
    cleaned = canonical_text(value)
    return cleaned or None


def project_episode_row(
    row: sqlite3.Row,
    *,
    activity_descriptions: Mapping[str, str],
) -> EpisodeDocument:
    """Project one row from the canonical ``episode`` view without an LLM call."""

    tick_id = row["id"]
    significance = row["significance"]
    if isinstance(tick_id, bool) or not isinstance(tick_id, int) or tick_id < 1:
        raise EpisodeSourceError("episode id must be a positive integer")
    if (
        isinstance(significance, bool)
        or not isinstance(significance, int)
        or not 7 <= significance <= 10
    ):
        raise EpisodeSourceError(f"episode:{tick_id} significance must be between 7 and 10")

    activity = _json_object(row, "activity", tick_id)
    location = _json_object(row, "location", tick_id)
    interior = _json_object(row, "interior", tick_id)
    mood = interior.get("mood")
    if not isinstance(mood, dict):
        raise EpisodeSourceError(f"episode:{tick_id} interior.mood must be an object")

    activity_name = _required_text(activity.get("name"), "activity.name", tick_id)
    activity_desc = _required_text(activity.get("desc"), "activity.desc", tick_id)
    catalog_description = activity_descriptions.get(activity_name)
    if catalog_description is not None:
        catalog_description = _required_text(
            catalog_description, "activity.catalog_description", tick_id
        )
    location_name = _required_text(location.get("name"), "location.name", tick_id)
    location_city = _optional_text(location.get("city"), "location.city", tick_id)
    location_address = _optional_text(location.get("address"), "location.address", tick_id)
    mood_description = _required_text(mood.get("description"), "interior.mood.description", tick_id)
    note = _optional_text(row["note"], "note", tick_id)
    occurred_at = _required_text(row["ts"], "ts", tick_id)

    text_parts: list[str] = []
    if note is not None:
        text_parts.append(note)
    text_parts.append(f"活动：{activity_name}")
    if catalog_description is not None:
        text_parts.append(f"活动说明：{catalog_description}")
    text_parts.extend(
        (
            f"本次经历：{activity_desc}",
            f"地点：{location_name}",
        )
    )
    if location_city is not None:
        text_parts.append(f"城市：{location_city}")
    if location_address is not None:
        text_parts.append(f"地址：{location_address}")
    text_parts.append(f"心情：{mood_description}")

    return EpisodeDocument(
        id=f"episode:{tick_id}",
        source_tick_id=tick_id,
        text="\n".join(text_parts),
        occurred_at=occurred_at,
        activity=activity_name,
        activity_description=catalog_description,
        location=location_name,
        location_city=location_city,
        location_address=location_address,
        mood_description=mood_description,
        significance=significance,
    )


class CanonicalEpisodeSource:
    """Read episodes from a canonical DB through SQLite ``mode=ro``."""

    def __init__(self, db_path: Path, *, activity_descriptions: Mapping[str, str]) -> None:
        self._db_path = db_path
        self._activity_descriptions = _clean_catalog(activity_descriptions)

    def read_after(self, after_tick_id: int, *, limit: int = 500) -> tuple[EpisodeDocument, ...]:
        if after_tick_id < 0:
            raise ValueError("after_tick_id must be non-negative")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        try:
            conn = connect_readonly(self._db_path)
            try:
                rows = conn.execute(
                    "SELECT id, ts, note, significance, interior, activity, location "
                    "FROM episode WHERE id > :after_tick_id ORDER BY id ASC LIMIT :limit",
                    {"after_tick_id": after_tick_id, "limit": limit},
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise EpisodeSourceError("canonical episode source is unavailable") from exc
        return tuple(
            project_episode_row(row, activity_descriptions=self._activity_descriptions)
            for row in rows
        )

    def max_tick_id(self) -> int | None:
        try:
            conn = connect_readonly(self._db_path)
            try:
                row = conn.execute("SELECT MAX(id) AS max_tick_id FROM episode").fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise EpisodeSourceError("canonical episode source is unavailable") from exc
        if row is None or row["max_tick_id"] is None:
            return None
        value = row["max_tick_id"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise EpisodeSourceError("canonical episode watermark is invalid")
        return value


__all__ = [
    "PROJECTION_REVISION",
    "CanonicalEpisodeSource",
    "EpisodeSourceError",
    "activity_catalog_digest",
    "project_episode_row",
]
