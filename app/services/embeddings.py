"""Text embeddings. Runs locally (ONNX), so no API key and no student text leaves the machine."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

from app.config import settings

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"  # 384 dimensions
MAX_CHARS = 2000  # the model reads about 256 tokens; more text is ignored anyway


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    """all-MiniLM-L6-v2 through fastembed. The model downloads once (about 90 MB), then runs offline."""

    def __init__(self, model_name: str = MODEL_NAME) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name)
        self.dim = settings.embedding_dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.embed([t[:MAX_CHARS] for t in texts])
        return [[float(x) for x in v] for v in vectors]


class HashingEmbedder:
    """Deterministic bag-of-words vectors with no model download. For tests only."""

    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim or settings.embedding_dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                h = int(hashlib.md5(word.encode()).hexdigest(), 16)
                vec[h % self.dim] += 1.0 if (h >> 8) % 2 else -1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = FastEmbedder()
    return _embedder


def set_embedder(embedder: Embedder | None) -> None:
    """Swap the embedder (tests use HashingEmbedder)."""
    global _embedder
    _embedder = embedder


def course_text(title: str, description: str | None, outcomes: list[str] | None) -> str:
    """The text that represents a course. Used for catalog courses and for matching."""
    parts = [title.strip()]
    if description:
        parts.append(description.strip())
    if outcomes:
        parts.append("Outcomes: " + "; ".join(outcomes))
    return ". ".join(parts)
