"""Synthetic smoke checks retained in the clean public root."""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import tarfile
import zipfile
from copy import deepcopy
from email.parser import Parser
from importlib import metadata
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from kindred.activity import list_registered_actions, list_registered_activities
from kindred.config import DEFAULT_CONFIG, DEFAULT_WEB_HOST, DEFAULT_WEB_PORT
from kindred.config.schema import KindredConfig
from kindred.db import KindredDB
from kindred.db.places import derive_place_visit
from kindred.graph.dream.summarize import step1_summarize
from kindred.graph.tick.sense_derive import t1_sense_derive
from kindred.llm import build_llm_client
from kindred.llm.anthropic_client import AnthropicLlmClient
from kindred.llm.claude_code_client import ClaudeCodeLlmClient
from kindred.llm.deepseek_client import DeepSeekLlmClient
from kindred.llm.gemini_client import GeminiLlmClient
from kindred.llm.openai_client import OpenAILlmClient
from kindred.llm.real_client import parse_llm_json, text_fingerprint
from kindred.llm.xai_client import XaiLlmClient
from kindred.location.models import LocationOrigin, LocationQuery
from kindred.providers.environment import VirtualEnvironmentProvider
from kindred.providers.location import VirtualLocationProvider
from kindred.runtime.pidfile import acquire, read_pid
from kindred.runtime.scheduler import Debouncer
from kindred.runtime.watcher import MessageCursor, MessageWatcher
from kindred.state._seed import make_doc_example_state
from kindred.web.app import create_app
from kindred.web.service import VisualStateProjector, derive_visual_source_id

_PUBLIC_DISTRIBUTIONS = {
    "kindred",
    "kindred-capability-sdk",
    "kindred-capability-compose",
    "kindred-capability-draw",
    "kindred-capability-inventory",
    "kindred-capability-send",
}


def test_public_life_assets_are_complete() -> None:
    assert len(list_registered_actions()) == 13
    assert len(list_registered_activities()) == 7


def test_portable_capability_entry_points_remain_loadable() -> None:
    entries = metadata.entry_points().select(group="kindred.capability.v1")
    by_name = {entry.name: entry for entry in entries}

    assert {"compose", "inventory", "send"} <= by_name.keys()
    assert all(callable(by_name[name].load()) for name in ("compose", "inventory", "send"))


def test_portable_capability_packages_carry_the_root_license() -> None:
    root = Path(__file__).resolve().parents[2]
    expected = (root / "LICENSE").read_bytes()

    for package in (
        "kindred-capability-sdk",
        "kindred-capability-compose",
        "kindred-capability-draw",
        "kindred-capability-inventory",
        "kindred-capability-send",
    ):
        assert (root / "packages" / package / "LICENSE").read_bytes() == expected
        metadata_text = (root / "packages" / package / "pyproject.toml").read_text(encoding="utf-8")
        assert 'license = "Apache-2.0"' in metadata_text
        assert 'license-files = ["LICENSE"]' in metadata_text


def test_installed_root_distribution_declares_its_license_file() -> None:
    distribution = metadata.distribution("kindred")

    assert distribution.metadata.get_all("License-File") == ["LICENSE"]
    assert any(str(path).endswith("licenses/LICENSE") for path in distribution.files or ())


def test_built_public_distributions_carry_apache_license() -> None:
    artifact_dir_value = os.environ.get("KINDRED_PUBLIC_ARTIFACT_DIR")
    if artifact_dir_value is None:
        pytest.skip("set KINDRED_PUBLIC_ARTIFACT_DIR when validating clean build artifacts")
    artifact_dir = Path(artifact_dir_value)
    wheels: dict[str, Path] = {}
    sdists: dict[str, Path] = {}

    for wheel in artifact_dir.glob("*.whl"):
        with zipfile.ZipFile(wheel) as archive:
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            assert len(metadata_names) == 1
            package_metadata = Parser().parsestr(archive.read(metadata_names[0]).decode())
            name = package_metadata["Name"]
            assert name not in wheels
            assert package_metadata["License-Expression"] == "Apache-2.0"
            assert package_metadata.get_all("License-File") == ["LICENSE"]
            assert any(path.endswith(".dist-info/licenses/LICENSE") for path in archive.namelist())
            wheels[name] = wheel

    for sdist in artifact_dir.glob("*.tar.gz"):
        with tarfile.open(sdist) as archive:
            members = archive.getmembers()
            metadata_members = [member for member in members if member.name.endswith("/PKG-INFO")]
            assert len(metadata_members) == 1
            stream = archive.extractfile(metadata_members[0])
            assert stream is not None
            package_metadata = Parser().parsestr(stream.read().decode())
            name = package_metadata["Name"]
            assert name not in sdists
            assert package_metadata["License-Expression"] == "Apache-2.0"
            assert package_metadata.get_all("License-File") == ["LICENSE"]
            assert any(
                len(Path(member.name).parts) == 2 and Path(member.name).name == "LICENSE"
                for member in members
            )
            sdists[name] = sdist

    assert wheels.keys() == _PUBLIC_DISTRIBUTIONS
    assert sdists.keys() == _PUBLIC_DISTRIBUTIONS


def test_built_root_distribution_contains_precompiled_web_without_source_maps() -> None:
    artifact_dir_value = os.environ.get("KINDRED_PUBLIC_ARTIFACT_DIR")
    if artifact_dir_value is None:
        pytest.skip("set KINDRED_PUBLIC_ARTIFACT_DIR when validating clean build artifacts")
    artifact_dir = Path(artifact_dir_value)
    root_wheel = next(artifact_dir.glob("kindred-[0-9]*.whl"))
    root_sdist = next(artifact_dir.glob("kindred-[0-9]*.tar.gz"))

    with zipfile.ZipFile(root_wheel) as archive:
        names = set(archive.namelist())
        assert "kindred/web/static/index.html" in names
        assert "kindred/web/static/kindred-web-build.json" in names
        assert any(name.startswith("kindred/web/static/assets/") for name in names)
        assert not any(name.endswith(".map") for name in names)

    with tarfile.open(root_sdist) as archive:
        names = {member.name for member in archive.getmembers()}
        assert any(name.endswith("/src/kindred/web/static/index.html") for name in names)
        assert not any(name.endswith(".map") for name in names)


@pytest.mark.parametrize(
    ("provider", "client_type"),
    [
        ("claude_code", ClaudeCodeLlmClient),
        ("anthropic", AnthropicLlmClient),
        ("google", GeminiLlmClient),
        ("deepseek", DeepSeekLlmClient),
        ("openai", OpenAILlmClient),
        ("xai", XaiLlmClient),
    ],
)
def test_all_public_llm_providers_route_to_their_client(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    client_type: type[Any],
) -> None:
    sentinel = object()
    monkeypatch.setattr(client_type, "from_config", lambda _config: sentinel)
    config = dataclasses.replace(
        DEFAULT_CONFIG,
        llm=dataclasses.replace(DEFAULT_CONFIG.llm, provider=provider),
    )

    assert build_llm_client(config) is sentinel


def test_public_llm_parser_and_fingerprint_contract() -> None:
    private_text = "synthetic response body"

    assert parse_llm_json('prefix\n```json\n{"ok": true}\n```\n') == {"ok": True}
    fingerprint = text_fingerprint(private_text)
    assert fingerprint.startswith(f"bytes={len(private_text.encode())} sha256=")
    assert private_text not in fingerprint


def test_public_openai_response_json_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": '{"ok":true}'}],
                    }
                ],
            },
        )

    client = OpenAILlmClient(
        api_key="EXAMPLE_API_KEY",
        model="openai-contract-test",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.complete("synthetic input", role="sense.llm") == {"ok": True}
    finally:
        client.close()


def test_public_runtime_configuration_keeps_typed_world_defaults() -> None:
    assert isinstance(DEFAULT_CONFIG, KindredConfig)
    assert DEFAULT_CONFIG.world.weather_provider == "virtual"
    assert DEFAULT_CONFIG.world.weather_ttl_minutes > 0


def test_public_visual_state_foundation_is_stable_and_action_only() -> None:
    assert (DEFAULT_WEB_HOST, DEFAULT_WEB_PORT) == ("127.0.0.1", 8787)
    install_id = "synthetic-public-install"
    db = MagicMock()
    db.get_state_latest.return_value = {
        "id": 7,
        "ts": "2026-08-17T12:00:00+08:00",
        "activity": {
            "started_at": "2026-08-17T11:55:00+08:00",
            "step": "walk",
        },
    }
    db.get_motion_instance_start_id.return_value = 5

    snapshot = VisualStateProjector(
        install_id,
        registered_actions={"walk"},
    ).project(db)

    assert snapshot.model_dump(mode="json") == {
        "schema_version": 1,
        "source_id": derive_visual_source_id(install_id),
        "status": "ready",
        "revision": 7,
        "committed_at": "2026-08-17T12:00:00+08:00",
        "motion_instance_id": "tick:5",
        "action": {"name": "walk"},
    }
    assert install_id not in snapshot.source_id


def test_public_visual_state_v1_is_the_only_published_version_surface(tmp_path: Path) -> None:
    config = dataclasses.replace(
        DEFAULT_CONFIG,
        resident=dataclasses.replace(
            DEFAULT_CONFIG.resident,
            install_id="synthetic-public-install",
        ),
    )
    client = TestClient(create_app(config=config, db_path=tmp_path / "missing.db"))

    openapi = client.get("/openapi.json").json()
    visual_paths = sorted(path for path in openapi["paths"] if "visual-state" in path)
    component_schemas = openapi["components"]["schemas"]
    response_schema = openapi["paths"]["/api/visual-state"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]

    assert visual_paths == ["/api/visual-state"]
    assert response_schema["oneOf"] == [
        {"$ref": "#/components/schemas/VisualStateEmptyV1"},
        {"$ref": "#/components/schemas/VisualStateReadyV1"},
    ]
    assert response_schema["discriminator"] == {
        "propertyName": "status",
        "mapping": {
            "empty": "#/components/schemas/VisualStateEmptyV1",
            "ready": "#/components/schemas/VisualStateReadyV1",
        },
    }
    assert set(component_schemas["VisualStateEmptyV1"]["properties"]) == {
        "schema_version",
        "source_id",
        "status",
    }
    assert set(component_schemas["VisualStateReadyV1"]["properties"]) == {
        "schema_version",
        "source_id",
        "status",
        "revision",
        "committed_at",
        "motion_instance_id",
        "action",
    }
    assert set(component_schemas["VisualActionV1"]["properties"]) == {"name"}
    assert all(
        component_schemas[name]["additionalProperties"] is False
        for name in ("VisualStateEmptyV1", "VisualStateReadyV1", "VisualActionV1")
    )


def test_public_visual_state_http_contract_fails_closed_on_incomplete_schema(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "visual-state.db"
    config = dataclasses.replace(
        DEFAULT_CONFIG,
        resident=dataclasses.replace(
            DEFAULT_CONFIG.resident,
            install_id="synthetic-public-install",
        ),
    )
    with KindredDB.open(db_path) as db:
        client = TestClient(create_app(config=config, db_path=db_path))
        response = client.get("/api/visual-state")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["status"] == "empty"

        db._conn.execute("DROP VIEW state_latest")  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            "CREATE VIEW state_latest AS SELECT id, ts, activity FROM tick LIMIT 1"
        )
        db._conn.commit()  # noqa: SLF001

    unavailable = client.get("/api/visual-state")
    assert unavailable.status_code == 503
    assert unavailable.headers["cache-control"] == "no-store"
    assert unavailable.json() == {"detail": "Visual state is unavailable"}


def test_public_place_visit_contract_derives_committed_arrival() -> None:
    visit = derive_place_visit(
        tick_id=7,
        act_result={
            "committed": True,
            "location_arrival": {
                "place_key": "virtual:library",
                "name": "Synthetic Library",
                "source": "virtual",
            },
        },
        location={"arrived_at": "2026-08-15T12:00:00+08:00"},
        activity={"name": "visit_cultural_place"},
    )

    assert visit is not None
    assert (visit.place_key, visit.tick_id, visit.activity_name) == (
        "virtual:library",
        7,
        "visit_cultural_place",
    )


def test_public_virtual_location_provider_is_deterministic_and_offline() -> None:
    query = LocationQuery(
        origin=LocationOrigin(name="Home", city="Synthetic City"),
        query="find a quiet cafe",
        categories=("cafe",),
        limit=2,
    )
    provider = VirtualLocationProvider()

    first = provider.find_nearby(query)
    assert first == provider.find_nearby(query)
    assert len(first) == 2
    assert all(item.source == "virtual" and item.type == "cafe" for item in first)


def test_public_dream_summary_has_honest_no_database_fallback() -> None:
    result = step1_summarize({"dream_date": "2026-08-14"})

    assert "2026-08-14" in result["messages_summary"]
    assert "没有可回顾" in result["messages_summary"]


def test_public_sense_derive_advances_time_without_external_io() -> None:
    previous = make_doc_example_state(ts="2026-08-15T11:55:00+08:00").model_dump()
    untouched = deepcopy(previous)
    result = t1_sense_derive(
        {
            "trigger_source": "heartbeat",
            "triggered_at": "2026-08-15T12:00:00+08:00",
            "prev_state": previous,
            "next_state": deepcopy(previous),
        }
    )

    assert result["next_state"]["time"]["iso"] == "2026-08-15T12:00:00+08:00"
    assert "interior" in result["next_state"]
    assert previous == untouched


def test_public_virtual_weather_is_deterministic_and_offline() -> None:
    now = dt.datetime(2026, 8, 15, 12, tzinfo=dt.timezone.utc)
    provider = VirtualEnvironmentProvider(now_fn=lambda: now)

    first = provider.get_weather("Synthetic City")
    second = provider.get_weather("Synthetic City")

    assert first == second
    assert 0 <= first.humidity <= 100
    assert 0 <= first.uv_index <= 12
    assert not hasattr(first, "ambience")


def test_public_pid_lease_records_owner_and_releases_lock(tmp_path: Path) -> None:
    pid_file = tmp_path / "kindred.pid"
    lease = acquire(pid_file)
    try:
        assert read_pid(pid_file) == os.getpid()
    finally:
        lease.release()

    assert pid_file.is_file()
    second_lease = acquire(pid_file)
    second_lease.release()


def test_public_watcher_fires_without_mutating_cursor() -> None:
    class Source:
        saved: list[MessageCursor] = []

        def load_cursor(self, *, session_key: str) -> MessageCursor:
            assert session_key == "synthetic-session"
            return MessageCursor(1000, -1, 1)

        def exists_partner_after(self, *, session_key: str, cursor: MessageCursor | None) -> bool:
            assert session_key == "synthetic-session"
            assert cursor == MessageCursor(1000, -1, 1)
            return True

        def save_cursor(self, *, session_key: str, cursor: MessageCursor) -> None:
            del session_key
            self.saved.append(cursor)

    source = Source()
    watcher = MessageWatcher(source, Debouncer(), session_key="synthetic-session")  # type: ignore[arg-type]

    event = watcher.poll_once(now=1.0, triggered_at="2026-08-15T12:00:00+08:00")

    assert event is not None
    assert event.to_invoke_input() == {
        "trigger_source": "watcher",
        "triggered_at": "2026-08-15T12:00:00+08:00",
    }
    assert source.saved == []
