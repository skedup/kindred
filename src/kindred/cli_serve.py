"""Read-only web-serving CLI command."""

from __future__ import annotations

import ipaddress
from pathlib import Path

import click

from kindred.cli_common import cli_compat
from kindred.config import DEFAULT_CONFIG_PATH
from kindred.observability import configure_logging


@click.command(name="serve")
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=f"Kindred YAML 配置路径（默认 {DEFAULT_CONFIG_PATH}，也可用 KINDRED_CONFIG）",
)
@click.option(
    "--life-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="life 根目录；未显式 --db 时派生 SQLite 路径",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="SQLite 数据库路径（CLI override，优先级最高）。",
)
@click.option("--host", default=None, help="监听地址（覆盖 web.host）")
@click.option("--port", default=None, type=int, help="监听端口（覆盖 web.port）")
def serve(
    config_path: Path | None,
    life_root: Path | None,
    db_path: Path | None,
    host: str | None,
    port: int | None,
) -> None:
    """启动 web 后端：只读可视化（观察 ta 的生活）。

    挂只读投影 ``/healthz`` / ``/now`` / ``/stream`` / ``/episodes`` /
    ``/interior/history`` / ``/artifacts`` / ``/api/visual-state``，绝不写库
    （``mode=ro``）。
    需要 web extra：``pip install kindred[web]``。
    """
    config = cli_compat("_load_cli_config")(
        config_path=config_path,
        life_root=life_root,
        db_path=db_path,
        web_host=host,
        web_port=port,
        load_secrets=False,
    )
    # The observation service needs committed identity/state, not Resident or
    # provider credentials.  Keep file-backed secrets out of this HTTP process.
    cli_compat("_require_resident_commit")(config, validate_secrets=False)
    cli_compat("_require_mouth_host_binding")(config)
    configure_logging(config.logging.level)
    resolved_db = config.paths.db
    resolved_host = _normalize_bind_host(config.web.host)
    resolved_port = config.web.port

    try:
        import uvicorn

        from kindred.web.app import create_app
        from kindred.web.assets import WebAssetError, resolve_web_dist
    except ModuleNotFoundError as exc:  # pragma: no cover - 取决于是否装 extra
        raise click.ClickException(
            "缺少 web 依赖。安装：pip install 'kindred[web]'（fastapi + uvicorn）"
        ) from exc

    try:
        static_dir = resolve_web_dist()
    except WebAssetError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Kindred 可视化（只读）：{_bind_url(resolved_host, resolved_port)}")
    if not _is_loopback_bind(resolved_host):
        click.echo(
            "NOTICE: non-loopback bind exposes the full read-only Kindred Web/API "
            "without application authentication or TLS, including relationship, narrative, "
            "history, "
            "artifact, and revealable intimate data; use a trusted network or external HTTPS."
        )
    click.echo(f"db: {resolved_db}（{'存在' if resolved_db.exists() else '尚未创建—空态'}）")
    # N-7：把已解析的 config 注入 app，别让 create_app 二次 load 丢掉 --config 的 web 设置。
    # 显式 --db 是只看任意 SQLite 的调试入口，不猜配套 ArtifactStore。
    # config/life-root 正常入口由 create_app 使用同一 config 装配 db + artifacts/。
    app = create_app(
        config=config,
        db_path=resolved_db if db_path is not None else None,
        static_dir=static_dir,
    )
    uvicorn.run(app, host=resolved_host, port=resolved_port)


def _is_loopback_bind(host: str) -> bool:
    normalized = _normalize_bind_host(host).lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _normalize_bind_host(host: str) -> str:
    """Return the host syntax Uvicorn expects, accepting bracketed IPv6 input."""
    normalized = host.strip()
    if normalized.startswith("[") and normalized.endswith("]"):
        return normalized[1:-1]
    return normalized


def _bind_url(host: str, port: int) -> str:
    """Format one human-readable HTTP URL for an already normalized bind host."""
    authority = f"[{host}]" if ":" in host else host
    return f"http://{authority}:{port}/"
