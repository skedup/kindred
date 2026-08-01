"""Draw package-private provider types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

Layout: TypeAlias = Literal["portrait", "square", "landscape"]


@dataclass(frozen=True)
class DrawSettings:
    provider: Literal["google", "openai"]
    model: str
    timeout_s: float


@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    layout: Layout


@dataclass(frozen=True)
class GeneratedImage:
    content: bytes
    media_type: Literal["image/png"]


class ImageProvider(Protocol):
    def generate(self, request: ImageRequest) -> GeneratedImage: ...

    def close(self) -> None: ...


class ImageProviderRejected(Exception):
    """The provider completed without one usable final image."""


class ImageProviderError(Exception):
    """The provider transport or response violated the package contract."""


__all__ = [
    "DrawSettings",
    "GeneratedImage",
    "ImageProvider",
    "ImageProviderError",
    "ImageProviderRejected",
    "ImageRequest",
    "Layout",
]
