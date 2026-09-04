from __future__ import annotations

import asyncio
import builtins
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner
from mcp import Client

import kindred.memory.embedding as embedding_module
from kindred.cli import cli
from kindred.memory import EpisodeDocument, MemorySearchRequest
from kindred.memory.embedding import EmbeddingError, EmbeddingProfile, FastEmbedEmbeddingProvider
from kindred.memory.index import IndexSpec, MemoryIndex
from kindred.memory.mcp_server import MCP_TOOL_NAME, create_memory_mcp_server
from kindred.memory.search import SqliteHybridMemorySearch
from kindred.memory.sync import sync_episode_index


def _episode(tick_id: int, text: str) -> EpisodeDocument:
    return EpisodeDocument(
        id=f"episode:{tick_id}",
        source_tick_id=tick_id,
        text=text,
        occurred_at=f"2026-08-{tick_id:02d}T18:00:00+08:00",
        activity="take_a_walk",
        activity_description="Take a quiet walk outside.",
        location="Harbor path",
        location_city="Example City",
        location_address=None,
        mood_description="Calm and curious",
        significance=8,
    )


@dataclass(frozen=True)
class _Source:
    documents: tuple[EpisodeDocument, ...]

    def read_after(
        self,
        after_tick_id: int,
        *,
        limit: int = 500,
    ) -> tuple[EpisodeDocument, ...]:
        return tuple(
            document for document in self.documents if document.source_tick_id > after_tick_id
        )[:limit]

    def max_tick_id(self) -> int | None:
        return self.documents[-1].source_tick_id if self.documents else None


class _EmbeddingProvider:
    profile = EmbeddingProfile(revision="public-memory-test-v1", dimension=2)

    def embed_documents(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple((1.0, 0.0) if "blue waves" in text else (0.0, 1.0) for text in texts)

    def embed_query(self, text: str) -> tuple[float, ...]:
        return (1.0, 0.0) if "blue waves" in text else (0.0, 1.0)


def _build_search(tmp_path: Path) -> SqliteHybridMemorySearch:
    provider = _EmbeddingProvider()
    spec = IndexSpec(
        activity_catalog_digest="public-memory-catalog-v1",
        embedding_revision=provider.profile.revision,
        embedding_dimension=provider.profile.dimension,
        embedding_normalized=provider.profile.normalized,
    )
    source = _Source(
        (
            _episode(1, "I saw blue waves glowing along the shore."),
            _episode(2, "I made breakfast while rain tapped the window."),
        )
    )
    index_path = tmp_path / "memory-index.db"
    with MemoryIndex.open(index_path, spec=spec, embedding_provider=provider) as index:
        first = sync_episode_index(source, index)
        second = sync_episode_index(source, index)
    assert first.indexed_documents == 2
    assert second.indexed_documents == 0
    return SqliteHybridMemorySearch(
        index_path,
        spec=spec,
        embedding_provider=provider,
    )


def test_public_cli_exposes_memory_commands() -> None:
    result = CliRunner().invoke(cli, ["memory", "--help"])

    assert result.exit_code == 0
    assert "search" in result.output
    assert "serve-mcp" in result.output
    assert "sync" in result.output


def test_public_memory_optional_dependency_errors_use_source_checkout_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "kindred.memory.mcp_server":
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    mcp_result = CliRunner().invoke(cli, ["memory", "serve-mcp"])

    assert mcp_result.exit_code != 0
    assert "uv sync --no-dev --extra memory-mcp" in mcp_result.output
    assert "pip install" not in mcp_result.output

    def missing_module(name: str) -> object:
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(embedding_module, "import_module", missing_module)
    with pytest.raises(EmbeddingError) as failure:
        FastEmbedEmbeddingProvider()

    message = str(failure.value)
    assert "uv sync --no-dev --extra vector" in message
    assert "--extra memory-mcp when using serve-mcp" in message
    assert "pip install" not in message


def test_public_memory_index_is_idempotent_and_hybrid_searches(tmp_path: Path) -> None:
    search = _build_search(tmp_path)

    response = search.search(MemorySearchRequest(query="blue waves", top_k=1))

    assert [hit.id for hit in response.results] == ["episode:1"]
    assert response.trace.active_channels == ("lexical", "vector")
    assert response.trace.abstained is False


def test_public_memory_mcp_exposes_one_read_only_tool(tmp_path: Path) -> None:
    async def exercise() -> None:
        search = _build_search(tmp_path)
        async with Client(create_memory_mcp_server(lambda: search)) as client:
            tools = (await client.list_tools()).tools
            result = await client.call_tool(MCP_TOOL_NAME, {"query": "blue waves", "top_k": 1})

        assert [tool.name for tool in tools] == [MCP_TOOL_NAME]
        assert tools[0].annotations is not None
        assert tools[0].annotations.read_only_hint is True
        assert result.is_error is False
        assert result.structured_content is not None
        assert result.structured_content["results"][0]["id"] == "episode:1"

    asyncio.run(exercise())
