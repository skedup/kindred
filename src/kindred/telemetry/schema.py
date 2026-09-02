"""Neutral telemetry schema metadata shared by readers and writers."""

from typing import Final

CURRENT_TELEMETRY_SCHEMA_VERSION: Final[int] = 1
TELEMETRY_GRAPH_NODE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "T1.sense.io",
        "T1.sense.derive",
        "T1.sense.llm",
        "T2.act.llm",
        "T3.persist.write_state",
        "T3.persist.write_memory",
        "T3.persist.flush_bundle",
        "Step 1.summarize",
        "Step 2.reset_stack",
        "Step 3.reflect",
        "Step 4.gate",
        "Step 5.land",
    }
)
TELEMETRY_LLM_ROLES: Final[frozenset[str]] = frozenset(
    {
        "sense.llm",
        "act.llm",
        "dream.summarize",
        "dream.reflect",
        "dream.gate",
        "dream.excerpt",
    }
)
SAFE_TELEMETRY_TOOL_NAME_PATTERN: Final[str] = r"^[a-z][a-z0-9_]{0,63}$"

__all__ = ["CURRENT_TELEMETRY_SCHEMA_VERSION"]
