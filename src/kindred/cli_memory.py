"""Explicit maintenance commands for Kindred's derived episode memory index."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import cast

import click

from kindred.activity import ActivitySkillError, list_registered_activities, load_activity_skill
from kindred.cli_common import cli_compat
from kindred.config import DEFAULT_CONFIG_PATH
from kindred.memory.contracts import MemoryFilters, MemorySearch, MemorySearchRequest, TimeOrder
from kindred.memory.embedding import (
    EmbeddingError,
    EmbeddingProvider,
    FastEmbedEmbeddingProvider,
)
from kindred.memory.index import IndexSpec, MemoryIndex, MemoryIndexError
from kindred.memory.search import (
    MemorySearchError,
    SqliteHybridMemorySearch,
    SqliteMemorySearch,
    SqliteVectorMemorySearch,
)
from kindred.memory.source import (
    CanonicalEpisodeSource,
    EpisodeSourceError,
    activity_catalog_digest,
)
from kindred.memory.sync import MemorySyncError, sync_episode_index
from kindred.observability import configure_logging


def _activity_descriptions() -> dict[str, str]:
    return {name: load_activity_skill(name).description for name in list_registered_activities()}


def _embedding_provider(channel: str) -> EmbeddingProvider | None:
    if channel == "lexical":
        return None
    return FastEmbedEmbeddingProvider()


def _index_spec(
    activity_descriptions: dict[str, str],
    embedding_provider: EmbeddingProvider | None,
) -> IndexSpec:
    profile = None if embedding_provider is None else embedding_provider.profile
    return IndexSpec(
        activity_catalog_digest=activity_catalog_digest(activity_descriptions),
        embedding_revision=None if profile is None else profile.revision,
        embedding_dimension=None if profile is None else profile.dimension,
        embedding_normalized=None if profile is None else profile.normalized,
    )


@click.group()
def memory() -> None:
    """Episode 记忆派生索引命令。"""


@memory.command(name="sync")
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
    help="life 根目录；未显式 --db/--index 时派生数据库路径。",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="canonical Kindred SQLite 路径（CLI override）。",
)
@click.option(
    "--index",
    "index_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="派生 memory index 路径（默认 <life-root>/data/memory-index.db）。",
)
@click.option(
    "--rebuild",
    is_flag=True,
    help="按当前投影/catalog 规则原子替换全部 Episode 索引。",
)
@click.option(
    "--channel",
    type=click.Choice(("lexical", "vector", "hybrid")),
    default="lexical",
    show_default=True,
    help=(
        "构建 lexical 或带本地 FastEmbed 的 vector/hybrid index；"
        "lexical 与 embedding index 间切换需要 --rebuild。"
    ),
)
def memory_sync(
    config_path: Path | None,
    life_root: Path | None,
    db_path: Path | None,
    index_path: Path | None,
    *,
    rebuild: bool,
    channel: str,
) -> None:
    """从已提交 canonical Episode 显式同步派生检索索引。"""

    config = cli_compat("_load_cli_config")(
        config_path=config_path,
        life_root=life_root,
        db_path=db_path,
        load_secrets=False,
    )
    configure_logging(config.logging.level)
    canonical_path = config.paths.db
    derived_path = index_path or config.paths.life_root / "data" / "memory-index.db"

    if not canonical_path.exists():
        raise click.ClickException(
            f"canonical database does not exist: {canonical_path}; run `kindred db migrate` first"
        )
    if canonical_path.resolve() == derived_path.resolve():
        raise click.ClickException("memory index path must differ from the canonical database path")

    try:
        activity_descriptions = _activity_descriptions()
        embedding_provider = _embedding_provider(channel)
        spec = _index_spec(activity_descriptions, embedding_provider)
        source = CanonicalEpisodeSource(
            canonical_path,
            activity_descriptions=activity_descriptions,
        )
        with MemoryIndex.open(
            derived_path,
            spec=spec,
            embedding_provider=embedding_provider,
            allow_rebuild=rebuild,
        ) as index:
            result = sync_episode_index(source, index, rebuild=rebuild)
    except (
        ActivitySkillError,
        EmbeddingError,
        EpisodeSourceError,
        MemoryIndexError,
        MemorySyncError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        raise click.ClickException(f"memory sync failed: {exc}") from exc

    source_max = result.source_max_episode_tick_id
    click.echo(
        f"OK    memory sync channel={channel} mode={result.mode} "
        f"documents={result.indexed_documents} "
        f"watermark={result.before.indexed_through_tick_id}"
        f"->{result.after.indexed_through_tick_id} stale={'yes' if result.stale else 'no'}"
    )
    click.echo(f"canonical_db: {canonical_path}")
    click.echo(f"index_db:     {derived_path}")
    click.echo(f"source_max_episode_tick_id: {source_max if source_max is not None else '-'}")
    click.echo(f"index_version: {result.after.index_version}")


@memory.command(name="search")
@click.argument("query", type=str)
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=f"Kindred YAML 配置路径（默认 {DEFAULT_CONFIG_PATH}，也可用 KINDRED_CONFIG）",
)
@click.option(
    "--channel",
    type=click.Choice(("lexical", "vector", "hybrid")),
    default="lexical",
    show_default=True,
    help="使用 lexical、vector 或 lexical+vector RRF hybrid 检索通道。",
)
@click.option(
    "--life-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="life 根目录；未显式 --db/--index 时派生数据库路径。",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="可选 canonical Kindred SQLite 路径，仅用于报告索引 freshness。",
)
@click.option(
    "--index",
    "index_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="派生 memory index 路径（默认 <life-root>/data/memory-index.db）。",
)
@click.option("--top-k", type=click.IntRange(1, 5), default=3, show_default=True)
@click.option(
    "--token-budget",
    type=click.IntRange(128, 1200),
    default=800,
    show_default=True,
)
@click.option(
    "--time-order",
    type=click.Choice(("relevance", "earliest", "latest")),
    default="relevance",
    show_default=True,
)
@click.option("--activity", default=None, help="canonical Activity name 精确过滤。")
@click.option(
    "--location",
    default=None,
    help="canonical Location name/city/address 精确过滤。",
)
@click.option("--occurred-after", default=None, help="occurred_at 下界（包含）。")
@click.option("--occurred-before", default=None, help="occurred_at 上界（包含）。")
def memory_search(
    query: str,
    config_path: Path | None,
    life_root: Path | None,
    db_path: Path | None,
    index_path: Path | None,
    channel: str,
    top_k: int,
    token_budget: int,
    time_order: str,
    activity: str | None,
    location: str | None,
    occurred_after: str | None,
    occurred_before: str | None,
) -> None:
    """只读检索已完成同步的 Episode 派生索引。"""

    config = cli_compat("_load_cli_config")(
        config_path=config_path,
        life_root=life_root,
        db_path=db_path,
        load_secrets=False,
    )
    configure_logging(config.logging.level)
    canonical_path = config.paths.db
    derived_path = index_path or config.paths.life_root / "data" / "memory-index.db"
    if not derived_path.exists():
        raise click.ClickException("memory index does not exist; run `kindred memory sync` first")

    try:
        activity_descriptions = _activity_descriptions()
        embedding_provider = _embedding_provider(channel)
        spec = _index_spec(activity_descriptions, embedding_provider)
        source = (
            CanonicalEpisodeSource(
                canonical_path,
                activity_descriptions=activity_descriptions,
            )
            if canonical_path.exists()
            else None
        )
        request = MemorySearchRequest(
            query=query,
            top_k=top_k,
            token_budget=token_budget,
            time_order=cast(TimeOrder, time_order),
            filters=MemoryFilters(
                occurred_after=occurred_after,
                occurred_before=occurred_before,
                activity=activity,
                location=location,
            ),
        )
        if channel == "hybrid":
            assert embedding_provider is not None
            result = SqliteHybridMemorySearch(
                derived_path,
                spec=spec,
                embedding_provider=embedding_provider,
                source=source,
            ).search(request)
        elif channel == "vector":
            assert embedding_provider is not None
            result = SqliteVectorMemorySearch(
                derived_path,
                spec=spec,
                embedding_provider=embedding_provider,
                source=source,
            ).search(request)
        else:
            result = SqliteMemorySearch(
                derived_path,
                spec=spec,
                source=source,
            ).search(request)
    except (
        ActivitySkillError,
        EmbeddingError,
        EpisodeSourceError,
        MemoryIndexError,
        MemorySearchError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        raise click.ClickException(f"memory search failed: {exc}") from exc

    click.echo(json.dumps(asdict(result), ensure_ascii=False, indent=2, sort_keys=True))


@memory.command(name="serve-mcp")
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=f"Kindred YAML 配置路径（默认 {DEFAULT_CONFIG_PATH}，也可用 KINDRED_CONFIG）",
)
@click.option(
    "--channel",
    type=click.Choice(("lexical", "vector", "hybrid")),
    default="hybrid",
    show_default=True,
    help="MCP 工具使用的固定 lexical/vector/hybrid 通道；不暴露给 Mouth 动态选择。",
)
@click.option(
    "--life-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="life 根目录；未显式 --db/--index 时派生数据库路径。",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="可选 canonical Kindred SQLite 路径，仅用于报告索引 freshness。",
)
@click.option(
    "--index",
    "index_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="派生 memory index 路径（默认 <life-root>/data/memory-index.db）。",
)
def memory_serve_mcp(
    config_path: Path | None,
    life_root: Path | None,
    db_path: Path | None,
    index_path: Path | None,
    channel: str,
) -> None:
    """通过 stdio 暴露单个只读 Kindred Episode memory tool。"""

    try:
        from kindred.memory.mcp_server import MemoryMcpFailure, serve_memory_mcp_stdio
    except ModuleNotFoundError as exc:
        raise click.ClickException(
            "memory MCP dependencies are not installed; from the Kindred source checkout run: "
            "uv sync --no-dev --extra memory-mcp"
        ) from exc

    config = cli_compat("_load_cli_config")(
        config_path=config_path,
        life_root=life_root,
        db_path=db_path,
        load_secrets=False,
    )
    configure_logging(config.logging.level)
    canonical_path = config.paths.db
    derived_path = index_path or config.paths.life_root / "data" / "memory-index.db"
    backend: MemorySearch | None = None

    def resolve_search() -> MemorySearch:
        nonlocal backend
        if backend is not None:
            return backend
        if not derived_path.exists():
            raise MemoryMcpFailure(
                "INDEX_NOT_READY",
                "Kindred memory index is not ready; run kindred memory sync with the configured "
                "channel first.",
            )
        try:
            activity_descriptions = _activity_descriptions()
            embedding_provider = _embedding_provider(channel)
            spec = _index_spec(activity_descriptions, embedding_provider)
            source = (
                CanonicalEpisodeSource(
                    canonical_path,
                    activity_descriptions=activity_descriptions,
                )
                if canonical_path.exists()
                else None
            )
            if channel == "hybrid":
                assert embedding_provider is not None
                backend = SqliteHybridMemorySearch(
                    derived_path,
                    spec=spec,
                    embedding_provider=embedding_provider,
                    source=source,
                )
            elif channel == "vector":
                assert embedding_provider is not None
                backend = SqliteVectorMemorySearch(
                    derived_path,
                    spec=spec,
                    embedding_provider=embedding_provider,
                    source=source,
                )
            else:
                backend = SqliteMemorySearch(
                    derived_path,
                    spec=spec,
                    source=source,
                )
        except EmbeddingError as exc:
            raise MemoryMcpFailure(
                "EMBEDDING_UNAVAILABLE",
                "Kindred vector embedding is unavailable.",
            ) from exc
        except (
            ActivitySkillError,
            EpisodeSourceError,
            MemoryIndexError,
            sqlite3.Error,
            ValueError,
        ) as exc:
            raise MemoryMcpFailure(
                "SEARCH_UNAVAILABLE",
                "Kindred memory search is temporarily unavailable.",
            ) from exc
        return backend

    asyncio.run(serve_memory_mcp_stdio(resolve_search))


__all__ = ["memory", "memory_search", "memory_serve_mcp", "memory_sync"]
