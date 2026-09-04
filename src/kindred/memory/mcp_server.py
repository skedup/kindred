"""Thin stdio MCP adapter for the host-neutral Kindred memory search contract."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import asdict
from typing import Annotated

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from kindred import __version__
from kindred.memory.contracts import (
    MemoryFilters,
    MemorySearch,
    MemorySearchRequest,
    TimeOrder,
)
from kindred.memory.embedding import EmbeddingError
from kindred.memory.search import MemorySearchError
from kindred.memory.source import EpisodeSourceError

MCP_TOOL_NAME = "kindred_memory_search"
_LOGGER = logging.getLogger(__name__)


class MemoryMcpFailure(RuntimeError):
    """Expected adapter failure with a stable, non-sensitive Mouth-facing code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MemoryToolFilters(BaseModel):
    """Nested MCP input matching the host-neutral memory filter contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    occurred_after: str | None = Field(default=None, min_length=1)
    occurred_before: str | None = Field(default=None, min_length=1)
    activity: str | None = Field(default=None, min_length=1)
    location: str | None = Field(default=None, min_length=1)


def _tool_result(payload: dict[str, object], *, is_error: bool = False) -> CallToolResult:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=payload,
        is_error=is_error,
    )


def _tool_error(code: str, message: str) -> CallToolResult:
    return _tool_result({"error": {"code": code, "message": message}}, is_error=True)


def create_memory_mcp_server(search_factory: Callable[[], MemorySearch]) -> MCPServer:
    """Expose exactly one read-only memory tool over any MCPServer transport."""

    server = MCPServer(
        name="kindred-memory",
        version=__version__,
        instructions=(
            "The Mouth host's native memory remains primary. Use this optional tool only when a "
            "question needs Kindred's high-significance lived Episode history; an empty result is "
            "a valid answer."
        ),
    )

    @server.tool(
        name=MCP_TOOL_NAME,
        description=(
            "Search the resident's read-only Kindred Episode memory only when a question needs "
            "high-significance lived history. The Mouth host's native memory remains primary for "
            "user and conversation facts. This tool excludes ordinary ticks, may abstain, and "
            "returns provenance plus index freshness."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        structured_output=False,
    )
    def kindred_memory_search(
        query: Annotated[str, Field(min_length=1, max_length=512)],
        top_k: Annotated[int, Field(ge=1, le=5)] = 3,
        token_budget: Annotated[int, Field(ge=128, le=1200)] = 800,
        time_order: TimeOrder = "relevance",
        filters: MemoryToolFilters | None = None,
    ) -> CallToolResult:
        try:
            memory_filters = MemoryFilters(**({} if filters is None else filters.model_dump()))
        except ValueError:
            return _tool_error("INVALID_FILTER", "Memory filters are invalid.")
        try:
            request = MemorySearchRequest(
                query=query,
                top_k=top_k,
                token_budget=token_budget,
                time_order=time_order,
                filters=memory_filters,
            )
        except ValueError:
            return _tool_error("INVALID_QUERY", "The memory search request is invalid.")
        try:
            response = search_factory().search(request)
        except MemoryMcpFailure as exc:
            return _tool_error(exc.code, exc.message)
        except EmbeddingError:
            return _tool_error(
                "EMBEDDING_UNAVAILABLE",
                "Kindred vector embedding is unavailable.",
            )
        except (EpisodeSourceError, MemorySearchError, sqlite3.Error):
            return _tool_error(
                "SEARCH_UNAVAILABLE",
                "Kindred memory search is temporarily unavailable.",
            )
        except Exception:
            _LOGGER.exception("unexpected Kindred memory MCP search failure")
            return _tool_error("INTERNAL_ERROR", "Kindred memory search failed.")
        return _tool_result(asdict(response))

    return server


async def serve_memory_mcp_stdio(search_factory: Callable[[], MemorySearch]) -> None:
    """Run the single-tool adapter on stdin/stdout without another service process."""

    await create_memory_mcp_server(search_factory).run_stdio_async()


__all__ = [
    "MCP_TOOL_NAME",
    "MemoryMcpFailure",
    "MemoryToolFilters",
    "create_memory_mcp_server",
    "serve_memory_mcp_stdio",
]
