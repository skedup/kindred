"""Public contract for the external Relationship authority and projection."""

from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from kindred.db import KindredDB
from kindred.db.relationships import RelationshipDataError
from kindred.graph import (
    NODE_T3_PERSIST_FLUSH_BUNDLE,
    NODE_T3_PERSIST_WRITE_MEMORY,
    NODE_T3_PERSIST_WRITE_STATE,
    build_tick_graph,
)
from kindred.graph.tick.persist import make_persist_nodes
from kindred.graph.tick.sense_llm import _t1_sense_llm_impl
from kindred.llm.client import LlmClient, Role
from kindred.llm.tools import ToolLoopResult
from kindred.mouth_host.model import OpenClawRuntimeModel
from kindred.observability import DebugDumpConfig, PromptDumper
from kindred.openclaw import OpenClawWire
from kindred.relationship import render_relationship_summary
from kindred.relationship.models import (
    RelationshipChange,
    RelationshipFacetProposal,
    RelationshipProfile,
    RelationshipRoleEvent,
)
from kindred.relationship.preflight import require_user_relationship
from kindred.relationship.projector import (
    project_relationship_change,
    project_relationship_evidence,
)
from kindred.resident import PersonaPaths
from kindred.state._seed import make_doc_example_state
from kindred.state.tick import TickState
from kindred.web.app import create_app


def _profile(**updates: object) -> RelationshipProfile:
    values: dict[str, object] = {
        "subject_key": "user",
        "declared_role": "unlabeled",
        "trust": 50,
        "attachment": 50,
        "attraction": 50,
        "friction": 50,
    }
    values.update(updates)
    return RelationshipProfile.model_validate(values)


def _openclaw_model(workspace: Path) -> OpenClawRuntimeModel:
    wire = OpenClawWire.model_validate(
        {
            "transcript_session": "agent:resident:synthetic:direct:peer",
            "approved_peer": {
                "provider": "synthetic",
                "account_id": "account",
                "target": "peer",
            },
            "outbound_route": {
                "channel": "synthetic",
                "account_id": "account",
                "target": "peer",
            },
        }
    )
    return OpenClawRuntimeModel(
        kind="openclaw", wire=wire, agent_id="resident", workspace=workspace
    )


class _Reader:
    def __init__(self, profile: RelationshipProfile) -> None:
        self.profile = profile

    def get_relationship(self, subject_key: str) -> RelationshipProfile | None:
        assert subject_key == "user"
        return self.profile


class _SenseClient:
    def __init__(self) -> None:
        self.prompt = ""

    def complete(self, prompt: str, *, role: Role) -> dict[str, Any]:
        assert role == "sense.llm"
        self.prompt = prompt
        return {
            "observed_user_present": None,
            "affect_event_response": {},
            "relationship_changes": [{"facet": "trust", "direction": "up", "magnitude": "small"}],
            "relationship_role_event": {"target_role": "friend"},
            "note": "这一拍有了新的理解。",
            "significance": 4,
            "act_decision": {
                "act": False,
                "kind": None,
                "target_activity": None,
                "reason": "先安静感受。",
            },
            "thought_diff": {"add": [], "remove": []},
            "ambience": "空气里多了一点确定。",
        }


class _ToolClient:
    def complete(self, prompt: str, *, role: Role) -> dict[str, Any]:
        del prompt, role
        return {}

    def complete_with_tools(self, prompt: str, **_kwargs: Any) -> ToolLoopResult:
        del prompt
        return ToolLoopResult(final={}, tool_events=(), rounds=1)


def _persist_state(change: RelationshipChange) -> TickState:
    state = make_doc_example_state(ts="2026-08-08T12:00:00+08:00").model_dump(mode="json")
    return cast(
        "TickState",
        {
            "trigger_source": "heartbeat",
            "triggered_at": state["time"]["iso"],
            "prev_state": deepcopy(state),
            "next_state": deepcopy(state),
            "note": "relationship transaction",
            "significance": 4,
            "act_decision": {
                "act": False,
                "kind": None,
                "target_activity": None,
                "reason": "quiet",
            },
            "relationship_change": change,
        },
    )


def test_relationship_change_requires_evidence_and_clamps_facets() -> None:
    proposal = RelationshipFacetProposal(
        facet="trust",
        direction="up",
        magnitude="large",
    )
    assert (
        project_relationship_change(
            _profile(),
            project_relationship_evidence((), None),
            [proposal],
            None,
        )
        is None
    )

    change = project_relationship_change(
        _profile(trust=98),
        project_relationship_evidence(("[partner] synthetic statement",), None),
        [proposal],
        RelationshipRoleEvent(target_role="friend"),
    )

    assert change is not None
    assert change.target_role == "friend"
    assert change.facet_deltas == (("trust", 2),)


def test_relationship_db_update_is_atomic_and_tracks_tick_identity(tmp_path) -> None:
    db_path = tmp_path / "kindred.db"
    profile = _profile()
    change = project_relationship_change(
        profile,
        project_relationship_evidence(("[partner] synthetic statement",), None),
        [RelationshipFacetProposal(facet="attachment", direction="up", magnitude="medium")],
        None,
    )
    assert change is not None

    with KindredDB.open(db_path) as db:
        with db.transaction():
            db.create_relationship(profile)
        with db.transaction():
            assert db.apply_relationship_change(change, tick_id=1) is True
        stored = db.get_relationship("user")

    assert stored is not None
    assert stored.attachment == 53
    assert stored.updated_tick_id == 1


def test_relationship_summary_is_qualitative_and_advice_free() -> None:
    summary = render_relationship_summary(
        _profile(declared_role="friend", trust=80, attachment=60, attraction=30, friction=10)
    )

    assert "视为朋友" in summary
    assert all(value not in summary for value in ("80", "60", "30", "10"))
    assert all(word not in summary for word in ("应该", "联系", "等待", "推进", "delta"))


def test_sense_emits_private_change_from_the_same_partner_projection_once() -> None:
    state = make_doc_example_state(ts="2026-08-08T12:00:00+08:00").model_dump(mode="json")
    tick = cast(
        "TickState",
        {
            "trigger_source": "watcher",
            "triggered_at": state["time"]["iso"],
            "chat_window": [
                {
                    "role": "user",
                    "content": "我愿意成为你的朋友。",
                    "is_new": True,
                    "ts": state["time"]["iso"],
                }
            ],
            "prev_state": deepcopy(state),
            "next_state": deepcopy(state),
        },
    )
    client = _SenseClient()

    patch = _t1_sense_llm_impl(
        cast("LlmClient", client),
        tick,
        relationship_reader=_Reader(_profile()),
    )

    assert patch["relationship_change"] == RelationshipChange(
        subject_key="user",
        target_role="friend",
        facet_deltas=(("trust", 1),),
    )
    assert client.prompt.count("我愿意成为你的朋友") == 1
    assert "本拍 Relationship evaluation" in client.prompt


@pytest.mark.parametrize("kind", [None, "start_activity"])
def test_graph_preserves_private_relationship_change_across_act_branch(
    kind: str | None,
) -> None:
    change = RelationshipChange("user", None, (("trust", 1),))
    seen: list[object] = []

    def sense(_state: TickState) -> dict[str, Any]:
        return {
            "relationship_change": change,
            "act_decision": {
                "act": kind is not None,
                "kind": kind,
                "target_activity": "rest" if kind is not None else None,
            },
        }

    def act_node(state: TickState) -> dict[str, Any]:
        assert state["relationship_change"] is change
        return {"act_result": {"committed": False}}

    def persist(state: TickState) -> dict[str, Any]:
        seen.append(state["relationship_change"])
        return {"tick_id": 1}

    graph = build_tick_graph(
        sense_io_node=lambda state: {},
        sense_derive_node=lambda state: {},
        sense_llm_node=sense,
        act_llm_node=act_node,
        persist_nodes={
            NODE_T3_PERSIST_WRITE_STATE: persist,
            NODE_T3_PERSIST_WRITE_MEMORY: lambda state: {},
            NODE_T3_PERSIST_FLUSH_BUNDLE: lambda state: {},
        },
    )
    graph.invoke({"trigger_source": "heartbeat", "triggered_at": "2026-08-11T00:00:00Z"})
    assert seen == [change]


def test_production_graph_injects_one_relationship_authority_into_all_nodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kindred.capability_host.resources import LifeAssetView
    from kindred.config import load_kindred_config
    from kindred.life_assets import ACTIONS_DIR, ACTIVITIES_DIR
    from kindred.runtime.tick_graph import build_client_tick_graph

    card = tmp_path / "character-card.yaml"
    card.write_text(
        "traits:\n  schema: value_numeric_grade_letter\n  eros:\n    value: 50\n",
        encoding="utf-8",
    )
    config = load_kindred_config(
        env={},
        overrides={
            "paths": {"db": tmp_path / "kindred.db", "character_card": card},
            "capabilities": {"xiaohongshu": {"enabled": False}},
        },
    )
    dumper = PromptDumper(DebugDumpConfig(enabled=False, dump_dir=config.paths.debug_dump_dir))
    captured: dict[str, object] = {}
    import kindred.graph as graph_module
    import kindred.graph.tick as node_module

    monkeypatch.setattr(
        "kindred.capability_host.runtime.load_runtime_life_assets",
        lambda: LifeAssetView(ACTIONS_DIR, ACTIVITIES_DIR, ()),
    )
    monkeypatch.setattr(node_module, "make_sense_io_node", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(node_module, "make_sense_derive_node", lambda **_kwargs: object())

    def make_sense(*_args: object, relationship_reader: object, **_kwargs: object) -> object:
        captured["sense"] = relationship_reader
        return object()

    def make_act(*_args: object, relationship_reader: object, **_kwargs: object) -> object:
        captured["act"] = relationship_reader
        return object()

    def make_persist(
        _db: object, _bundle: object, *, relationship_reader: object
    ) -> dict[str, object]:
        captured["persist"] = relationship_reader
        return {}

    monkeypatch.setattr(node_module, "make_sense_llm_node", make_sense)
    monkeypatch.setattr(node_module, "make_act_llm_node", make_act)
    monkeypatch.setattr(node_module, "make_persist_nodes", make_persist)
    monkeypatch.setattr(graph_module, "build_tick_graph", lambda **_kwargs: object())

    with KindredDB.open(config.paths.db) as db, ExitStack() as stack:
        result = build_client_tick_graph(
            _ToolClient(),  # type: ignore[arg-type]
            db,
            config=config,
            prompt_dumper=dumper,
            resource_stack=stack,
        )
        assert result is not None
        assert captured == {"sense": db, "act": db, "persist": db}


def test_t3_commits_tick_and_relationship_then_flushes_post_commit_bundle(tmp_path: Path) -> None:
    db_path = tmp_path / "kindred.db"
    bundle = tmp_path / "context-bundle.md"
    with KindredDB.open(db_path) as db:
        with db.transaction():
            db.create_relationship(_profile())
        nodes = make_persist_nodes(db, bundle, relationship_reader=db)
        state = _persist_state(RelationshipChange("user", "friend", (("trust", 3),)))
        state.update(nodes[NODE_T3_PERSIST_WRITE_STATE](state))
        nodes[NODE_T3_PERSIST_WRITE_MEMORY](state)
        nodes[NODE_T3_PERSIST_FLUSH_BUNDLE](state)
        committed = db.get_relationship("user")

    assert committed is not None
    assert committed.updated_tick_id == state["tick_id"]
    assert committed.declared_role == "friend"
    assert committed.trust == 53
    assert render_relationship_summary(committed) in bundle.read_text(encoding="utf-8")


def test_relationship_failure_rolls_back_tick_and_keeps_input(tmp_path: Path) -> None:
    db_path = tmp_path / "kindred.db"
    change = RelationshipChange("user", "friend", ())
    state = _persist_state(change)
    original = deepcopy(state["next_state"])

    with KindredDB.open(db_path) as db:
        with pytest.raises(RelationshipDataError, match="missing"):
            make_persist_nodes(db, tmp_path / "bundle.md")[NODE_T3_PERSIST_WRITE_STATE](state)
        assert db.count_ticks() == 0
    assert state["next_state"] == original


def test_installer_relationship_stage_bootstraps_missing_user_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kindred.cli import _require_relationship_runtime
    from kindred.config import load_kindred_config
    from kindred.openclaw import install as installer

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("SOUL.md", "IDENTITY.md", "SOUL_excerpt.md"):
        (workspace / name).write_text("synthetic persona\n", encoding="utf-8")
    config = load_kindred_config(
        env={},
        overrides={
            "paths": {
                "db": tmp_path / "kindred.db",
                "soul_full": workspace / "SOUL.md",
                "identity": workspace / "IDENTITY.md",
                "user": workspace / "USER.md",
                "soul_excerpt": workspace / "SOUL_excerpt.md",
            }
        },
    )
    monkeypatch.setattr(installer.click, "echo", lambda *_args, **_kwargs: None)

    persona = PersonaPaths.openclaw(workspace)
    installer._ensure_relationship(config, persona)
    installer._ensure_relationship(config, persona)
    _require_relationship_runtime(config)

    with KindredDB.open_readonly(config.paths.db) as db:
        profile = require_user_relationship(db)
    assert profile == _profile(trust=0, attachment=0, attraction=0, friction=0)


def test_doctor_structural_check_fails_closed_without_relationship(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kindred.config import load_kindred_config
    from kindred.openclaw import doctor

    workspace = tmp_path / "workspace"
    life_root = tmp_path / "life"
    run_dir = life_root / "run"
    workspace.mkdir()
    run_dir.mkdir(parents=True)
    for name in ("SOUL.md", "IDENTITY.md", "SOUL_excerpt.md"):
        (workspace / name).write_text("synthetic persona\n", encoding="utf-8")
    card = tmp_path / "character-card.yaml"
    card.write_text("synthetic card\n", encoding="utf-8")
    db_path = tmp_path / "kindred.db"
    with KindredDB.open(db_path):
        pass
    config = load_kindred_config(
        env={},
        overrides={
            "paths": {
                "life_root": life_root,
                "run_dir": run_dir,
                "db": db_path,
                "character_card": card,
                "soul_full": workspace / "SOUL.md",
                "identity": workspace / "IDENTITY.md",
                "user": workspace / "USER.md",
                "soul_excerpt": workspace / "SOUL_excerpt.md",
            }
        },
    )
    config = replace(config, mouth_host=_openclaw_model(workspace))

    def inject(context: object) -> str:
        context.config = config  # type: ignore[attr-defined]
        return "synthetic config"

    monkeypatch.setattr(doctor, "_config", inject)
    monkeypatch.setattr(doctor, "require_committed_resident", lambda _config: None)
    for name in ("_mouth_host_local", "_capabilities", "_llm_config", "_services"):
        monkeypatch.setattr(doctor, name, lambda _context: "synthetic ok")
    monkeypatch.setattr(doctor, "_life_assets", lambda: "synthetic ok")

    missing = doctor.run_doctor(tmp_path / "config.yaml")
    missing_resident = next(
        check for check in missing.checks if check["check_id"] == "resident.runtime"
    )
    assert missing_resident["status"] == "failed"
    assert "Relationship" in str(missing_resident["hint"])

    with KindredDB.open(db_path) as db, db.transaction():
        db.create_relationship(_profile())
    present = doctor.run_doctor(tmp_path / "config.yaml")
    present_resident = next(
        check for check in present.checks if check["check_id"] == "resident.runtime"
    )
    assert present_resident["status"] == "warning"
    assert "context bundle" in str(present_resident["hint"])


def test_loopback_web_reads_the_same_strict_profile(tmp_path) -> None:
    db_path = tmp_path / "kindred.db"
    with KindredDB.open(db_path) as db:
        with db.transaction():
            db.create_relationship(
                _profile(
                    declared_role="lover",
                    trust=81,
                    attachment=72,
                    attraction=63,
                    friction=14,
                )
            )

    response = TestClient(create_app(db_path=db_path)).get("/relationship")

    assert response.status_code == 200
    assert response.json() == {
        "declared_role": "lover",
        "trust": 81,
        "attachment": 72,
        "attraction": 63,
        "friction": 14,
    }
