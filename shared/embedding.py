"""Text embeddings behind one interface, so the ingest job and the search tool never depend on a model library.

    embed_documents(texts) -> one vector per text          embed_query(text) -> one vector
    name  identifies the model (stored with each document; a different name re-embeds it)      dim  vector width

Providers (EMBEDDING_PROVIDER):
    fastembed  a small local model (default BAAI/bge-small-en-v1.5, ONNX, no PyTorch, no key, no per-call cost)
    hash       deterministic word hashing for tests and offline development: similar WORDS score high; it does not
               understand meaning, so it is never used for real retrieval quality
Vectors are L2-normalised, so cosine similarity is a dot product.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from functools import lru_cache
from typing import Protocol

from shared.config import load_config
from shared.knowledge_schema import EMBED_DIM


def vector_literal(values: list[float]) -> str:
    """A vector as pgvector text, for use as `%s::vector`."""
    return "[" + ",".join(format(x, ".7g") for x in values) + "]"


class Embedder(Protocol):
    name: str
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class HashEmbedder:
    name, dim = "hash-v1", EMBED_DIM

    def _vector(self, text: str) -> list[float]:
        words = re.findall(r"[a-z0-9_]+", text.lower())
        features = [(w, 1.0) for w in words] + [(f"{a}_{b}", 0.5) for a, b in zip(words, words[1:])]
        vec = [0.0] * self.dim
        for feature, weight in features:
            h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
            vec[h % self.dim] += weight if (h >> 40) & 1 else -weight
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def embed_documents(self, texts):
        return [self._vector(t) for t in texts]

    def embed_query(self, text):
        return self._vector(text)


class FastEmbedder:
    dim = EMBED_DIM

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5", cache_dir: str | None = None):
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise RuntimeError("EMBEDDING_PROVIDER=fastembed needs the fastembed package (pip install fastembed); "
                               "use EMBEDDING_PROVIDER=hash for offline tests") from e
        self.name = f"fastembed:{model}"
        self._model = TextEmbedding(model, cache_dir=cache_dir)          # downloads the model on first use

    def embed_documents(self, texts):
        return [v.tolist() for v in self._model.embed(texts)]

    def embed_query(self, text):
        return next(iter(self._model.query_embed(text))).tolist()


@lru_cache(maxsize=4)
def _build(provider: str, model: str, cache_dir: str) -> Embedder:
    if provider == "hash":
        return HashEmbedder()
    if provider == "fastembed":
        return FastEmbedder(model, cache_dir or os.path.expanduser("~/.cache/ai_pricing_fastembed"))
    raise ValueError(f"unknown EMBEDDING_PROVIDER={provider}")


def get_embedder() -> Embedder:
    """The configured embedder, built once per process (loading a model is slow)."""
    cfg = load_config()
    return _build(cfg.embedding_provider, cfg.embedding_model, cfg.embedding_cache_dir)
