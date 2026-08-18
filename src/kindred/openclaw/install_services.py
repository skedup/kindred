"""Resident preparation and managed service installation stages."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from kindred.config import KindredConfig, merge_runtime_secrets
from kindred.mouth_host.model import OpenClawRuntimeModel
from kindred.openclaw.install_plugin import OpenClawInstallError
from kindred.relationship.initialize import initialize_user_relationship
from kindred.relationship.models import RelationshipBootstrap
from kindred.resident import (
    PersonaPaths,
    ResidentInitRequest,
    WorldResolution,
    initialize_resident,
    read_owned_persona_file,
)


def _ops() -> Any:
    """Resolve the stable facade lazily so its patch seams stay authoritative."""
    from kindred.openclaw import install

    return install


def _config_home() -> Path:
    ops = _ops()
    root = ops.os.environ.get("XDG_CONFIG_HOME")
    return cast(
        Path,
        ops.Path(root).expanduser() if root else ops.Path.home() / ".config",
    )


def _secret(name: str, *, optional: bool = False) -> str:
    ops = _ops()
    value = ops.os.environ.get(name, "")
    if value:
        return cast(str, value)
    return cast(
        str,
        ops.click.prompt(
            name,
            hide_input=True,
            default="" if optional else None,
            show_default=False,
        ),
    )


def _ensure_resident(
    persona: PersonaPaths,
    *,
    resident_id: str,
    config_path: Path,
    require_gateway: bool,
) -> KindredConfig:
    ops = _ops()
    if config_path.exists():
        config = cast(KindredConfig, ops.load_kindred_config(config_path))
        ops.require_committed_resident(config)
        ops._require_persona(config, persona)
        return config
    ops.click.echo("将读取 SOUL.md / IDENTITY.md，并允许 Dream 更新该 workspace。")
    if not ops.click.confirm("确认继续 Persona 投影？"):
        raise OpenClawInstallError("Persona consent is required")
    data_home = ops.Path(ops.os.environ.get("XDG_DATA_HOME", ops.Path.home() / ".local/share"))
    life_root = (
        ops.Path(ops.click.prompt("Kindred life_root", default=str(data_home / "kindred/life")))
        .expanduser()
        .resolve()
    )
    home_address = ops.click.prompt("ta 的 home address").strip()
    ops.click.echo("当前公开安装适配仅支持 Heart provider=google。")
    model = ops.click.prompt("Heart Gemini model", default="gemini-2.5-flash").strip()
    secrets = {
        "BAIDU_MAP_AK": ops._secret("BAIDU_MAP_AK"),
        "GEMINI_API_KEY": ops._secret("GEMINI_API_KEY"),
    }
    if require_gateway:
        secrets["KINDRED_GATEWAY_TOKEN"] = ops._secret("KINDRED_GATEWAY_TOKEN")
    baidu_sk = ops._secret("BAIDU_MAP_SK", optional=True)
    if baidu_sk:
        secrets["BAIDU_MAP_SK"] = baidu_sk
    projector = ops.make_google_persona_projector(api_key=secrets["GEMINI_API_KEY"], model=model)

    def resolve_world(address: str, ak: str, sk: str | None, now: datetime) -> WorldResolution:
        resolved = ops.BaiduLocationProvider(ak=ak, sk=sk or "").resolve_home(address, at=now)
        return WorldResolution(address=resolved[0], city=resolved[1], timezone=resolved[2])

    initialize_resident(
        ResidentInitRequest(
            resident_id=ops.click.prompt("resident id", default=resident_id).strip(),
            persona=persona,
            life_root=life_root,
            xdg_config_home=ops._config_home(),
            home_address=home_address,
            llm_provider="google",
            llm_model=model,
            secrets=secrets,
            install_now=datetime.now(timezone.utc),
            persona_write_consent=True,
        ),
        project_persona=projector,
        resolve_world=resolve_world,
    )
    config = cast(KindredConfig, ops.load_kindred_config(config_path))
    ops._require_persona(config, persona)
    return config


def _require_agent(config: KindredConfig, agent: Any) -> None:
    model = config.mouth_host
    if model is not None and (
        not isinstance(model, OpenClawRuntimeModel)
        or model.agent_id != agent.agent_id
        or model.workspace != agent.workspace
    ):
        raise OpenClawInstallError("OpenClaw agent/workspace does not match Resident")
    _require_persona(config, PersonaPaths.openclaw(agent.workspace))


def _require_persona(config: KindredConfig, persona: PersonaPaths) -> None:
    if (
        config.paths.soul_full,
        config.paths.identity,
        config.paths.user,
        config.paths.soul_excerpt,
    ) != (persona.soul_full, persona.identity, persona.user, persona.soul_excerpt):
        raise OpenClawInstallError("OpenClaw Persona paths do not match Resident")


def _ensure_relationship(config: KindredConfig, persona: PersonaPaths) -> None:
    """Run the internal, one-time Relationship stage after approved-peer selection."""

    ops = _ops()
    expected = (persona.soul_full, persona.identity, persona.user, persona.soul_excerpt)
    actual = (
        config.paths.soul_full,
        config.paths.identity,
        config.paths.user,
        config.paths.soul_excerpt,
    )
    if actual != expected:
        raise OpenClawInstallError("Relationship Persona authority does not match Resident")
    try:
        read_owned_persona_file(config.paths.soul_full, name="SOUL.md")
        read_owned_persona_file(config.paths.identity, name="IDENTITY.md")
        read_owned_persona_file(config.paths.soul_excerpt, name="SOUL_excerpt.md")
    except ops.ResidentInitError as exc:
        raise OpenClawInstallError("Relationship prerequisite preflight failed") from exc

    def project_user(user_text: str) -> RelationshipBootstrap:
        if config.resident.secrets_file is None:
            raise OpenClawInstallError("Relationship bootstrap requires configured LLM secrets")
        secrets = merge_runtime_secrets(config.resident.secrets_file, env=ops.os.environ)
        if config.llm.provider == "google":
            api_key = secrets.get("GEMINI_API_KEY") or secrets.get("GOOGLE_API_KEY")
            projector = ops.make_google_relationship_projector
            base_url = ops.os.environ.get("GEMINI_BASE_URL")
        elif config.llm.provider == "openai":
            api_key = secrets.get("OPENAI_API_KEY")
            projector = ops.make_openai_relationship_projector
            base_url = ops.os.environ.get("OPENAI_BASE_URL")
        elif config.llm.provider == "xai":
            api_key = secrets.get("XAI_API_KEY")
            projector = ops.make_xai_relationship_projector
            base_url = ops.os.environ.get("XAI_BASE_URL")
        else:
            raise OpenClawInstallError(
                "Relationship bootstrap requires configured Google, OpenAI, or xAI provider"
            )
        if not api_key:
            raise OpenClawInstallError("Relationship bootstrap credential is missing")
        return cast(
            RelationshipBootstrap,
            projector(api_key=api_key, model=config.llm.model, base_url=base_url)(user_text),
        )

    def confirm_projection(bootstrap: RelationshipBootstrap) -> bool:
        ops.click.echo(
            "Relationship bootstrap: "
            f"role={bootstrap.declared_role}; trust={bootstrap.trust}; "
            f"attachment={bootstrap.attachment}; attraction={bootstrap.attraction}; "
            f"friction={bootstrap.friction}"
        )
        ops.click.echo(f"摘要：{bootstrap.summary}")
        return bool(ops.click.confirm("接受整份 Relationship 初始投影？"))

    result = initialize_user_relationship(
        db_path=config.paths.db,
        user_path=config.paths.user,
        project_user=project_user,
        confirm_identity=lambda: bool(
            ops.click.confirm("确认 USER.md 描述的是刚选择的 approved peer？")
        ),
        confirm_projection=confirm_projection,
    )
    ops.click.echo(f"Relationship initialization: {result.status}")


def _install_platform_services(config_path: Path) -> None:
    ops = _ops()
    try:
        web_available = bool(__import__("fastapi") and __import__("uvicorn"))
    except ImportError:
        web_available = False
    if not web_available:
        ops.click.echo("Web 依赖未安装；需要时安装 kindred[web]。")
    include_web = web_available and ops.click.confirm(
        "安装 Kindred Web 后台服务？",
        default=False,
    )
    ops.platform_service.install_services(config_path, include_web=include_web)
    if ops.click.confirm("现在启动 Kindred？", default=False):
        ops.platform_service.control_services(config_path, action="start")
