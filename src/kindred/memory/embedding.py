"""Small embedding boundary and local FastEmbed adapter for Episode retrieval."""

from __future__ import annotations

import math
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Protocol

EmbeddingVector = tuple[float, ...]


class EmbeddingError(RuntimeError):
    """An embedding provider or vector payload violated the retrieval contract."""


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    revision: str
    dimension: int
    normalized: bool = True

    def __post_init__(self) -> None:
        if not self.revision.strip():
            raise ValueError("embedding revision must not be blank")
        if isinstance(self.dimension, bool) or not isinstance(self.dimension, int):
            raise ValueError("embedding dimension must be a positive integer")
        if self.dimension < 1:
            raise ValueError("embedding dimension must be a positive integer")
        if self.normalized is not True:
            raise ValueError("only normalized embeddings are supported")


class EmbeddingProvider(Protocol):
    """Replaceable provider used by explicit sync and vector query paths."""

    @property
    def profile(self) -> EmbeddingProfile: ...

    def embed_documents(self, texts: tuple[str, ...]) -> tuple[EmbeddingVector, ...]: ...

    def embed_query(self, text: str) -> EmbeddingVector: ...


class _FastEmbedModel(Protocol):
    def embed(self, documents: list[str]) -> Iterable[object]: ...


def normalize_embedding(values: object, *, dimension: int) -> EmbeddingVector:
    if not isinstance(values, (list, tuple)) or len(values) != dimension:
        raise EmbeddingError(f"embedding must contain exactly {dimension} values")
    vector: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EmbeddingError("embedding values must be finite numbers")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise EmbeddingError("embedding values must be finite numbers")
        vector.append(numeric)
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        raise EmbeddingError("embedding must have a non-zero norm")
    return tuple(value / norm for value in vector)


def pack_embedding(vector: EmbeddingVector, *, dimension: int) -> bytes:
    normalized = normalize_embedding(vector, dimension=dimension)
    return struct.pack(f"<{dimension}f", *normalized)


def unpack_embedding(payload: object, *, dimension: int) -> EmbeddingVector:
    if not isinstance(payload, bytes) or len(payload) != dimension * 4:
        raise EmbeddingError("stored embedding byte length does not match its dimension")
    return normalize_embedding(struct.unpack(f"<{dimension}f", payload), dimension=dimension)


class FastEmbedEmbeddingProvider:
    """Optional local Chinese embedding adapter backed by ONNX FastEmbed."""

    MODEL = "BAAI/bge-small-zh-v1.5"
    ARTIFACT_REPOSITORY = "Qdrant/bge-small-zh-v1.5"
    ARTIFACT_COMMIT = "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"
    ARTIFACT_FILES = (
        "config.json",
        "model_optimized.onnx",
        "preprocessor_config.json",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
    )
    DIMENSION = 512
    QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
    REVISION = (
        f"fastembed-0.8.0:Qdrant/bge-small-zh-v1.5@{ARTIFACT_COMMIT}:query-instruction-v1:dim-512"
    )

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        threads: int | None = None,
        model: _FastEmbedModel | None = None,
    ) -> None:
        if threads is not None and (
            isinstance(threads, bool) or not isinstance(threads, int) or threads < 1
        ):
            raise ValueError("FastEmbed threads must be a positive integer")
        if model is None:
            try:
                text_embedding = import_module("fastembed").TextEmbedding
                snapshot_download = import_module("huggingface_hub").snapshot_download
            except (ImportError, AttributeError) as exc:
                raise EmbeddingError(
                    "vector memory dependencies are not installed; from the Kindred source "
                    "checkout run: uv sync --no-dev --extra vector (also include "
                    "--extra memory-mcp when using serve-mcp)"
                ) from exc
            download_kwargs: dict[str, object] = {
                "repo_id": self.ARTIFACT_REPOSITORY,
                "revision": self.ARTIFACT_COMMIT,
                "allow_patterns": list(self.ARTIFACT_FILES),
            }
            if cache_dir is not None:
                download_kwargs["cache_dir"] = cache_dir
            try:
                model_path = snapshot_download(**download_kwargs)
            except Exception as exc:
                raise EmbeddingError("pinned FastEmbed model download failed") from exc
            model_kwargs: dict[str, object] = {
                "model_name": self.MODEL,
                "specific_model_path": str(model_path),
            }
            if threads is not None:
                model_kwargs["threads"] = threads
            try:
                model = text_embedding(**model_kwargs)
            except Exception as exc:
                raise EmbeddingError("FastEmbed model initialization failed") from exc
        self._model = model
        self._profile = EmbeddingProfile(
            revision=self.REVISION,
            dimension=self.DIMENSION,
            normalized=True,
        )

    @property
    def profile(self) -> EmbeddingProfile:
        return self._profile

    def embed_documents(self, texts: tuple[str, ...]) -> tuple[EmbeddingVector, ...]:
        return self._embed(texts)

    def embed_query(self, text: str) -> EmbeddingVector:
        return self._embed((f"{self.QUERY_INSTRUCTION}{text}",))[0]

    def _embed(self, texts: tuple[str, ...]) -> tuple[EmbeddingVector, ...]:
        if not texts:
            return ()
        for text in texts:
            if not text.strip():
                raise ValueError("embedding text must not be blank")
        try:
            embeddings = tuple(self._model.embed(list(texts)))
        except Exception as exc:
            raise EmbeddingError("FastEmbed inference failed") from exc
        if len(embeddings) != len(texts):
            raise EmbeddingError("FastEmbed response count is invalid")
        vectors: list[EmbeddingVector] = []
        for embedding in embeddings:
            tolist = getattr(embedding, "tolist", None)
            values = tolist() if callable(tolist) else embedding
            vectors.append(
                normalize_embedding(
                    values,
                    dimension=self.profile.dimension,
                )
            )
        return tuple(vectors)


__all__ = [
    "EmbeddingError",
    "EmbeddingProfile",
    "EmbeddingProvider",
    "EmbeddingVector",
    "FastEmbedEmbeddingProvider",
    "normalize_embedding",
    "pack_embedding",
    "unpack_embedding",
]
