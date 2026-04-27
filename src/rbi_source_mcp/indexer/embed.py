"""Embeddings for RBI Source MCP — bge-small-en-v1.5 via sentence-transformers.

Lazy-loaded singleton so the model is downloaded/loaded only once per process.
First load downloads ~135 MB to the HuggingFace cache; subsequent loads are
in-memory.

Output: float32 numpy array of shape (N, 384), L2-normalized so cosine
similarity = dot product (and so sqlite-vec's L2 distance directly reflects
semantic similarity).

Usage:
    from rbi_source_mcp.indexer.embed import embed_texts, embed_query
    arr = embed_texts(["chunk 1 text", "chunk 2 text"])  # (2, 384)
    q = embed_query("compliance question")               # (384,)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import numpy as np
    from sentence_transformers import SentenceTransformer

logger = structlog.get_logger(__name__)

MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384

_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    """Lazy-load the embedding model. Subsequent calls return the same instance."""
    global _model
    if _model is None:
        # Import here so module import is cheap; loading the model is the
        # expensive step, deferred until first use.
        from sentence_transformers import SentenceTransformer

        logger.info("embed.model.load", model=MODEL_NAME)
        _model = SentenceTransformer(MODEL_NAME)
        logger.info("embed.model.ready", dim=EMBEDDING_DIM)
    return _model


def embed_texts(texts: list[str], *, batch_size: int = 32) -> np.ndarray:
    """Embed a batch of texts. Returns (len(texts), 384) float32, L2-normalized."""
    if not texts:
        import numpy as np

        return np.zeros((0, EMBEDDING_DIM), dtype="float32")
    model = _get_model()
    arr = model.encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    # sentence-transformers returns float32 already, but enforce defensively.
    return arr.astype("float32")


def embed_query(text: str) -> np.ndarray:
    """Embed a single query string. Returns shape (384,) float32, L2-normalized.

    For BGE retrieval, queries should be prefixed with the model's instruction.
    BGE-v1.5 recommends "Represent this sentence for searching relevant
    passages: " for English queries.
    """
    if not text or not text.strip():
        import numpy as np

        return np.zeros((EMBEDDING_DIM,), dtype="float32")
    instruction = "Represent this sentence for searching relevant passages: "
    arr = embed_texts([instruction + text])
    return arr[0]


def to_sqlite_vec_bytes(vector: np.ndarray) -> bytes:
    """Serialize a float32 vector for sqlite-vec INSERT/MATCH.

    sqlite-vec accepts vectors as raw little-endian float32 bytes. The vector
    must be exactly EMBEDDING_DIM dimensions.
    """
    import sqlite_vec

    flat = vector.astype("float32").reshape(-1)
    if flat.shape[0] != EMBEDDING_DIM:
        raise ValueError(
            f"expected {EMBEDDING_DIM}-dim vector, got {flat.shape[0]}"
        )
    return sqlite_vec.serialize_float32(flat.tolist())
