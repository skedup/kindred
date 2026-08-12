"""OPEN2 Resident 初始化、no-clobber 发布与 marker 提交。"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

import yaml

from kindred.character_card import load_card_manifest, load_interior_trait_profile
from kindred.config import KindredConfig, load_kindred_config
from kindred.config.secrets import (
    KindredSecretError,
    read_secrets_file,
    serialize_secrets,
    validate_secret_values,
)
from kindred.db import KindredDB
from kindred.db.ticks import STATE_LAYERS
from kindred.resident._contract import (
    INSTALL_CONTRACT_VERSION,
    MARKER_SCHEMA_VERSION,
    PERSONA_PROJECTION_SCHEMA,
    PersonaProjection,
    ResidentInitError,
    ResidentInitRequest,
    ResidentInitResult,
    WorldResolution,
)
from kindred.resident._seed import build_initial_state, stage_life
from kindred.state.state import State


def read_owned_persona_file(path: Path, *, name: str, optional: bool = False) -> str | None:
    """Read one workspace Persona file using the OPEN2 ownership contract."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise ResidentInitError(f"{name} is unavailable") from None
    except OSError as exc:
        raise ResidentInitError(f"{name} is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or not os.access(path, os.R_OK | os.W_OK)
    ):
        raise ResidentInitError(f"{name} must be an owned regular file")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ResidentInitError(f"{name} is unavailable") from exc
    if not text.strip():
        raise ResidentInitError(f"{name} must not be empty")
    return text


def initialize_resident(
    request: ResidentInitRequest,
    *,
    project_persona: Callable[[str, str], PersonaProjection],
    resolve_world: Callable[[str, str, str | None, datetime], WorldResolution],
    phase_hook: Callable[[str], None] | None = None,
) -> ResidentInitResult:
    """先完整计算，再按 excerpt/life/secrets/config/marker 顺序发布。"""
    config_dir = request.xdg_config_home / "kindred"
    config_path, secrets_path = config_dir / "config.yaml", config_dir / "secrets.env"
    marker_path, excerpt_path = (
        request.life_root / ".kindred-resident.json",
        request.workspace / "SOUL_excerpt.md",
    )
    committed = _committed(request, config_path, marker_path)
    if committed:
        return committed
    soul, identity = _preflight(request, excerpt_path, config_path, secrets_path)
    secrets = _secrets(request)
    try:
        projection = PersonaProjection.model_validate(project_persona(soul, identity))
        world = WorldResolution.model_validate(
            resolve_world(
                request.home_address,
                secrets["BAIDU_MAP_AK"],
                secrets.get("BAIDU_MAP_SK"),
                request.install_now,
            )
        )
        state = build_initial_state(request.install_now, world, projection.traits.eros)
    except Exception as exc:
        raise ResidentInitError("resident projection or world resolution failed") from exc

    install_id = uuid.uuid4().hex
    config_text = _config(request, world, install_id, marker_path, secrets_path)
    marker = {
        "schema_version": MARKER_SCHEMA_VERSION,
        "install_id": install_id,
        "resident_id": request.resident_id,
        "persona_projection_schema": PERSONA_PROJECTION_SCHEMA,
        "install_contract_version": INSTALL_CONTRACT_VERSION,
        "completed_at": request.install_now.isoformat(),
    }
    with tempfile.TemporaryDirectory(prefix="kindred-open2-") as temp:
        stage = Path(temp)
        staged_life = stage / "life"
        stage_life(staged_life, request, world, projection, state)
        staged_secret = _stage_file(stage / "secrets.env", serialize_secrets(secrets))
        staged_config = _config(request, world, install_id, marker_path, staged_secret)
        load_kindred_config(_stage_file(stage / "config.yaml", staged_config), env={})
        _exclusive(excerpt_path, projection.soul_excerpt + "\n")
        _phase(phase_hook, "excerpt")
        shutil.copytree(staged_life, request.life_root)
        _phase(phase_hook, "life_root")
        config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        _exclusive(secrets_path, serialize_secrets(secrets))
        _phase(phase_hook, "secrets")
        _exclusive(config_path, config_text)
        _phase(phase_hook, "config")
        _composition_preflight(config_path)
        _phase(phase_hook, "precommit")
        _exclusive(marker_path, json.dumps(marker, sort_keys=True) + "\n")
    return ResidentInitResult("created", install_id)


def require_committed_resident(config: KindredConfig) -> None:
    """有 OPEN2 resident 身份时，marker 必须存在并与 config 对账。"""
    resident = config.resident
    fields = (
        resident.install_id,
        resident.resident_id,
        resident.agent_id,
        resident.workspace,
        resident.marker_path,
        resident.secrets_file,
    )
    if not any(fields):
        return
    marker_path = resident.marker_path
    if not all(fields) or marker_path is None:
        raise ResidentInitError("resident installation is incomplete")
    try:
        info = marker_path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if not isinstance(marker, Mapping):
            raise ValueError
        expected = (
            marker.get("schema_version") == MARKER_SCHEMA_VERSION,
            marker.get("install_id") == resident.install_id,
            marker.get("resident_id") == resident.resident_id,
            marker.get("persona_projection_schema") == PERSONA_PROJECTION_SCHEMA,
            marker.get("install_contract_version") == INSTALL_CONTRACT_VERSION,
        )
        if not all(expected):
            raise ValueError
    except (OSError, ValueError) as exc:
        raise ResidentInitError("resident installation is incomplete") from exc
    try:
        _composition_preflight_config(config, read_only=True)
    except ResidentInitError:
        raise
    except Exception as exc:
        raise ResidentInitError("resident composition preflight failed") from exc


def _committed(
    request: ResidentInitRequest, config_path: Path, marker_path: Path
) -> ResidentInitResult | None:
    if not marker_path.exists():
        return None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        config = load_kindred_config(config_path, env={})
        expected = (
            marker.get("schema_version") == MARKER_SCHEMA_VERSION,
            marker.get("resident_id") == request.resident_id,
            marker.get("persona_projection_schema") == PERSONA_PROJECTION_SCHEMA,
            marker.get("install_contract_version") == INSTALL_CONTRACT_VERSION,
            config.resident.install_id == marker.get("install_id"),
            config.resident.resident_id == request.resident_id,
            config.resident.agent_id == request.agent_id,
            config.resident.workspace == request.workspace,
            config.resident.marker_path == marker_path,
            config.paths.life_root == request.life_root,
        )
        if not all(expected):
            raise ValueError
        require_committed_resident(config)
        return ResidentInitResult("already_committed", marker["install_id"])
    except Exception as exc:
        raise ResidentInitError("committed resident identity is invalid") from exc


def _preflight(
    request: ResidentInitRequest, excerpt: Path, config: Path, secrets: Path
) -> tuple[str, str]:
    if not request.persona_write_consent:
        raise ResidentInitError("Persona read and Dream write consent is required")
    if request.install_now.tzinfo is None:
        raise ResidentInitError("install_now must be timezone-aware")
    for field in ("resident_id", "agent_id", "home_address", "llm_model"):
        value = getattr(request, field)
        if not isinstance(value, str) or not value.strip():
            raise ResidentInitError(f"{field} must not be empty")
    existing = [
        role
        for role, path in (
            ("life_root", request.life_root),
            ("soul_excerpt", excerpt),
            ("config", config),
            ("secrets", secrets),
        )
        if path.exists()
    ]
    if existing:
        raise ResidentInitError(f"incomplete resident installation exists: {','.join(existing)}")
    try:
        workspace = request.workspace.resolve(strict=True)
    except OSError as exc:
        raise ResidentInitError("workspace is unavailable") from exc
    if (
        workspace != request.workspace
        or not workspace.is_dir()
        or not os.access(workspace, os.W_OK)
    ):
        raise ResidentInitError("workspace must be canonical and writable")
    soul = read_owned_persona_file(workspace / "SOUL.md", name="SOUL.md")
    identity = read_owned_persona_file(workspace / "IDENTITY.md", name="IDENTITY.md")
    assert soul is not None and identity is not None
    return soul, identity


def _secrets(request: ResidentInitRequest) -> dict[str, str]:
    try:
        result = validate_secret_values(request.secrets)
    except KindredSecretError as exc:
        raise ResidentInitError(str(exc)) from exc
    if "BAIDU_MAP_AK" not in result:
        raise ResidentInitError("missing secret: BAIDU_MAP_AK")
    required = {
        "anthropic": ("ANTHROPIC_API_KEY",),
        "deepseek": ("DEEPSEEK_API_KEY",),
        "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "openai": ("OPENAI_API_KEY",),
    }.get(request.llm_provider)
    if required and not any(key in result for key in required):
        raise ResidentInitError(f"missing credential for provider: {request.llm_provider}")
    return result


def _config(
    request: ResidentInitRequest,
    world: WorldResolution,
    install_id: str,
    marker: Path,
    secrets: Path,
) -> str:
    life, workspace = request.life_root, request.workspace
    raw = {
        "paths": {
            "life_root": str(life),
            "soul_excerpt": str(workspace / "SOUL_excerpt.md"),
            "soul_full": str(workspace / "SOUL.md"),
            "identity": str(workspace / "IDENTITY.md"),
            "user": str(workspace / "USER.md"),
            "character_card": str(life / "character-card.yaml"),
        },
        "llm": {
            "provider": request.llm_provider,
            "model": request.llm_model,
        },
        "world": {
            "location_provider": "baidu",
            "weather_provider": "wttr",
            "weather_location": "",
            "timezone": world.timezone,
        },
        "resident": {
            "install_id": install_id,
            "resident_id": request.resident_id,
            "agent_id": request.agent_id,
            "workspace": str(workspace),
            "marker_path": str(marker),
            "secrets_file": str(secrets),
        },
    }
    return yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)


def _composition_preflight(config_path: Path) -> None:
    _composition_preflight_config(load_kindred_config(config_path, env={}))


def _composition_preflight_config(
    config: KindredConfig,
    *,
    read_only: bool = False,
) -> None:
    load_card_manifest(config.paths.character_card)
    load_interior_trait_profile(config.paths.character_card)
    secret = config.resident.secrets_file
    if secret is None:
        raise ResidentInitError("resident secrets_file is missing or too permissive")
    try:
        read_secrets_file(secret)
    except KindredSecretError as exc:
        raise ResidentInitError(str(exc)) from exc
    opener = KindredDB.open_readonly if read_only else KindredDB.open
    with opener(config.paths.db) as db:
        latest = db.get_state_latest()
    if latest is None:
        raise ResidentInitError("resident seed state is missing")
    State.model_validate({layer: latest[layer] for layer in STATE_LAYERS})


def _stage_file(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _exclusive(path: Path, text: str) -> None:
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(path, 0o600)
    except FileExistsError as exc:
        raise ResidentInitError(f"final already exists: {path.name}") from exc


def _phase(hook: Callable[[str], None] | None, phase: str) -> None:
    if hook:
        hook(phase)


__all__ = ["initialize_resident", "require_committed_resident"]
