"""Deterministic text normalization shared by memory projection and retrieval."""

from __future__ import annotations

import unicodedata

NORMALIZATION_REVISION = "nfkc-casefold-whitespace-v1"


def canonical_text(value: str) -> str:
    """Return display text with Unicode and whitespace normalized."""

    return " ".join(unicodedata.normalize("NFKC", value).split())


def normalize_text(value: str) -> str:
    """Return the retrieval form: NFKC, casefold, and collapsed whitespace."""

    return canonical_text(value).casefold()


__all__ = ["NORMALIZATION_REVISION", "canonical_text", "normalize_text"]
