"""Packaged Web asset discovery and narrow integrity checks."""

from __future__ import annotations

import re
from pathlib import Path

WEB_DIST_DIR = Path(__file__).resolve().parent / "static"
_ASSET_PATTERN = re.compile(r"""(?:src|href)=["'](/assets/[^"']+)["']""")


class WebAssetError(RuntimeError):
    """The packaged Web distribution is absent or incomplete."""


def resolve_web_dist(directory: Path | None = None) -> Path:
    """Return a validated dist directory without exposing its path in errors."""
    root = directory if directory is not None else WEB_DIST_DIR
    index = root / "index.html"
    try:
        html = index.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise WebAssetError("packaged Web index is unavailable") from exc
    if any(root.rglob("*.map")):
        raise WebAssetError("packaged Web source maps are not allowed")
    references = _ASSET_PATTERN.findall(html)
    referenced_files = [root / ref.removeprefix("/") for ref in references]
    if not references or any(
        not path.is_file() or path.stat().st_size == 0 for path in referenced_files
    ):
        raise WebAssetError("packaged Web assets are incomplete")
    return root
