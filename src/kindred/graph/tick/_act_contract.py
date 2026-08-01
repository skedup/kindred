"""T2.act.llm 内部共享契约。"""

from __future__ import annotations

import re

from pydantic import ValidationError

from kindred.graph._shared._errors import NodeContractError

_SAFE_VALIDATION_PATH_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")


class ActLlmContractError(NodeContractError):
    """T2.act.llm 契约错误。"""


def safe_state_path(path: object) -> str:
    """只保留稳定的 ASCII state path；其余细节留给 gated debug dump。"""
    if isinstance(path, str) and _SAFE_VALIDATION_PATH_PATTERN.fullmatch(path):
        return path
    return "<invalid>"


def safe_validation_error_paths(exc: ValidationError) -> list[str]:
    """只投影稳定 schema path，不把 Pydantic input_value 带出验证边界。"""
    paths: set[str] = set()
    for error in exc.errors():
        path = ".".join(str(part) for part in error["loc"])
        if not path:
            paths.add("<root>")
        else:
            paths.add(safe_state_path(path))
    return sorted(paths)


__all__ = ["ActLlmContractError", "safe_state_path", "safe_validation_error_paths"]
