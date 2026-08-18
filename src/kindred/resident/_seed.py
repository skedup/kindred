"""Resident 首个 runtime card、State 与 DB seed 构造。"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from kindred.db import KindredDB, TickWriteParams
from kindred.resident._contract import PersonaProjection, ResidentInitRequest, WorldResolution
from kindred.state._derive import derive_time_layer, rederive_layer1
from kindred.state.state import State


def build_initial_state(now: datetime, world: WorldResolution, eros: int) -> State:
    ts = now.astimezone(ZoneInfo(world.timezone)).isoformat(timespec="seconds")
    raw = {
        "interior": {
            "body": {"value": 50, "description": "身体平稳"},
            "mood": {"value": 50, "description": "心绪平静"},
            "inner_pulse": {"value": 50, "description": "没什么非做不可的"},
            "needs": {
                "hunger": 25,
                "energy": 60,
                "fatigue": 30,
                "comfort": 65,
                "social": 60,
                "stimulation": 60,
                "aesthetic": 60,
            },
            "affect": {
                "stress": 20,
                "focus": 50,
                "arousal": round(10 + eros * 0.3),
                "clarity": 60,
            },
            "thoughts": [],
        },
        "embodiment": {"top": None, "bottom": None},
        "bag": {"item": None, "items": []},
        "activity": {
            "name": "rest",
            "desc": "刚在家中醒来",
            "started_at": ts,
            "engagement": 0.0,
            "with_whom": [],
            "for_what": "开始新的生活",
            "step": "settle",
        },
        "location": {
            "name": "家",
            "address": world.address,
            "city": world.city,
            "type": "home",
            "arrived_at": ts,
        },
        "time": derive_time_layer(ts).model_dump(),
        "environment": {
            "city": world.city,
            "weather": "待刷新",
            "temperature": 0,
            "ambience": "刚在家中安顿下来",
            "weather_cached_at": ts,
            "weather_cached_for": "",
        },
        "presence": {"user_present": False, "others": []},
    }
    state = State.model_validate(raw)
    return State.model_validate(
        state.model_copy(update={"interior": rederive_layer1(state.interior)})
    )


def stage_life(
    root: Path,
    request: ResidentInitRequest,
    world: WorldResolution,
    projection: PersonaProjection,
    state: State,
) -> None:
    root.mkdir(mode=0o700)
    for relative in ("data", "state", "run", "debug", "doc", "data/soul-history"):
        (root / relative).mkdir(parents=True, mode=0o700, exist_ok=True)
    traits = projection.traits.model_dump()
    card = {
        "name": request.resident_id,
        "form": "full",
        "traits": {
            "schema": "value_numeric_grade_letter",
            **{key: {"value": value, "grade": _grade(value)} for key, value in traits.items()},
        },
        "home": {
            "name": "家",
            "address": world.address,
            "city": world.city,
            "timezone": world.timezone,
            "weather_location": "",
        },
    }
    card_path = root / "character-card.yaml"
    card_path.write_text(yaml.safe_dump(card, allow_unicode=True), encoding="utf-8")
    os.chmod(card_path, 0o600)
    with KindredDB.open(root / "data/kindred.db") as db, db.transaction():
        db.insert_tick(
            TickWriteParams(
                state=state,
                trigger_source="cold_start",
                triggered_at=state.time.iso,
                note="Bootstrap seed",
                significance=1,
            )
        )
    os.chmod(root / "data/kindred.db", 0o600)


def _grade(value: int) -> str:
    return next(
        grade
        for minimum, grade in ((80, "S"), (60, "A"), (40, "B"), (20, "C"), (0, "D"))
        if value >= minimum
    )


__all__ = ["build_initial_state", "stage_life"]
