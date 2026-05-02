"""Embeddings for RBI Source MCP.

Dispatches on the configured provider (see ../embedding_config.py):

    local       sentence-transformers, run on this process's CPU
    cloudflare  POST to Cloudflare Workers AI

Both paths return float32 numpy arrays of shape (N, DIM), L2-normalized
so cosine similarity == dot product (and so sqlite-vec's L2 distance
directly reflects semantic similarity).

Public API stays the same regardless of provider:
    arr = embed_texts(["chunk 1", "chunk 2"])   # (2, DIM)
    q   = embed_query("compliance question")    # (DIM,)
    bts = to_sqlite_vec_bytes(q)                # bytes for SQLite INSERT/MATCH
"""

from __future__ import annotations

import os
import time
from functools import lru_cache
from typing import TYPE_CHECKING

import structlog

from .. import embedding_config as cfg

if TYPE_CHECKING:
    import numpy as np
    from sentence_transformers import SentenceTransformer

logger = structlog.get_logger(__name__)

# Re-exported for callers that import from .embed (legacy import sites:
# indexer/persist.py and a few tests).
MODEL_NAME = cfg.MODEL
EMBEDDING_DIM = cfg.DIM


# ---------------------------------------------------------------------------
# Local backend (sentence-transformers). Loaded lazily so module import is
# cheap; ~135 MB model download + ~1-3s load cost is paid on first use.
# ---------------------------------------------------------------------------
_local_model: SentenceTransformer | None = None


def _get_local_model() -> SentenceTransformer:
    global _local_model
    if _local_model is None:
        from sentence_transformers import SentenceTransformer

        logger.info("embed.local.load", model=MODEL_NAME)
        _local_model = SentenceTransformer(MODEL_NAME)
        logger.info("embed.local.ready", dim=EMBEDDING_DIM)
    return _local_model


def _local_embed(texts: list[str], *, batch_size: int = 32) -> np.ndarray:
    model = _get_local_model()
    arr = model.encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    return arr.astype("float32")


# ---------------------------------------------------------------------------
# Cloudflare Workers AI backend.
# Endpoint: POST /accounts/<ACCT>/ai/run/<MODEL_ID>
# Body:     {"text": [...]}     (CF accepts 1..N strings)
# Response: {"success": true, "result": {"shape": [N, D], "data": [[...]]}}
# We L2-normalize ourselves (CF's vectors are already unit-norm for BGE,
# but we enforce defensively in case of provider drift).
# ---------------------------------------------------------------------------
_CF_BATCH_SIZE = 100  # CF accepts more, but smaller batches retry-friendly
_CF_BASE_URL = "https://api.cloudflare.com/client/v4"


def _cf_endpoint() -> str:
    acct, _ = cfg.cloudflare_creds()
    return f"{_CF_BASE_URL}/accounts/{acct}/ai/run/{MODEL_NAME}"


def _cf_embed(texts: list[str]) -> np.ndarray:
    """POST to CF Workers AI. Returns (len(texts), DIM) float32, L2-normalized."""
    import httpx
    import numpy as np

    _, token = cfg.cloudflare_creds()
    url = _cf_endpoint()
    out: list[list[float]] = []

    for i in range(0, len(texts), _CF_BATCH_SIZE):
        batch = texts[i : i + _CF_BATCH_SIZE]
        # Two attempts with exponential backoff. CF occasionally 5xx on
        # bursty traffic; embeddings are pure inference so idempotent retry
        # is safe.
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                with httpx.Client(timeout=60.0) as client:
                    r = client.post(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        json={"text": batch},
                    )
                if r.status_code == 200:
                    data = r.json()
                    if not data.get("success"):
                        raise RuntimeError(
                            f"CF Workers AI returned success=false: {data.get('errors')}"
                        )
                    out.extend(data["result"]["data"])
                    break
                # 429/5xx → backoff + retry once
                if r.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                    time.sleep(1.5)
                    continue
                r.raise_for_status()
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == 0:
                    time.sleep(1.5)
                    continue
                raise
        else:  # noqa: PLW0120 — runs if for-else exhausts without break
            if last_exc is not None:
                raise last_exc

    arr = np.asarray(out, dtype="float32")
    # Defensive L2-normalize (BGE-family models from CF already are, but
    # we don't trust silent provider changes).
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    return arr / np.clip(norms, 1e-12, None)


# ---------------------------------------------------------------------------
# Public API (provider-agnostic)
# ---------------------------------------------------------------------------


def embed_texts(texts: list[str], *, batch_size: int = 32) -> np.ndarray:
    """Embed a batch of texts. Returns (len(texts), DIM) float32, L2-normalized."""
    if not texts:
        import numpy as np

        return np.zeros((0, EMBEDDING_DIM), dtype="float32")

    if cfg.PROVIDER == "cloudflare":
        return _cf_embed(texts)
    return _local_embed(texts, batch_size=batch_size)


# Process-local LRU cache for query embeddings. BGE is deterministic at
# fp32 — same input → exact same 768-dim vector — so caching is safe and
# correct, no staleness concern. Saves a CF round-trip (~250 ms each)
# when the same query repeats: watchdog probes, popular user questions,
# load tests. 10,000 entries × ~3 KB = ~30 MB max, fits trivially in our
# 1 GB Fly machine.
_QUERY_CACHE_MAXSIZE = 10_000
_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "


@lru_cache(maxsize=_QUERY_CACHE_MAXSIZE)
def _cached_query_embed(text: str) -> np.ndarray:
    """Cache layer behind embed_query. Keyed on raw user text (the BGE
    instruction prefix is a constant, applied uniformly downstream)."""
    arr = embed_texts([_QUERY_INSTRUCTION + text])
    v = arr[0]
    # Mark the cached array read-only so a downstream `.astype()` /
    # `.reshape()` that returns a view doesn't accidentally let a caller
    # mutate the cached vector. Numpy will raise on direct in-place
    # writes; copy()/astype() with copy=True still produce mutable arrays.
    v.setflags(write=False)
    return v


def embed_query(text: str) -> np.ndarray:
    """Embed a single query. Returns shape (DIM,) float32, L2-normalized.

    BGE retrieval expects queries to be prefixed with the model's instruction
    (`Represent this sentence for searching relevant passages: `). The
    cloudflare path applies the prefix here too — CF's @cf/baai/bge-* models
    are the same checkpoints HuggingFace serves, so the same instruction
    convention applies.

    Process-local LRU cache: identical text returns the same cached vector
    without re-embedding. The returned array is read-only; callers that
    need a mutable buffer should `.copy()`. `to_sqlite_vec_bytes()` and
    sqlite-vec MATCH both work fine on read-only arrays — they re-cast.
    """
    if not text or not text.strip():
        import numpy as np

        return np.zeros((EMBEDDING_DIM,), dtype="float32")
    return _cached_query_embed(text)


def embed_query_cache_info() -> dict[str, int]:
    """Cache stats for observability (e.g. surfacing on /health). Returns
    a plain dict so the caller doesn't need to import functools.CacheInfo."""
    info = _cached_query_embed.cache_info()
    return {
        "hits": info.hits,
        "misses": info.misses,
        "currsize": info.currsize,
        "maxsize": info.maxsize,
    }


def embed_query_cache_clear() -> None:
    """Drop the query-embedding cache. Useful for tests and after a
    runtime model swap (which we don't currently support, but it's cheap
    insurance)."""
    _cached_query_embed.cache_clear()


def to_sqlite_vec_bytes(vector: np.ndarray) -> bytes:
    """Serialize a float32 vector for sqlite-vec INSERT/MATCH.

    sqlite-vec accepts vectors as raw little-endian float32 bytes. The
    vector must be exactly EMBEDDING_DIM dimensions.
    """
    import sqlite_vec

    flat = vector.astype("float32").reshape(-1)
    if flat.shape[0] != EMBEDDING_DIM:
        raise ValueError(
            f"expected {EMBEDDING_DIM}-dim vector, got {flat.shape[0]} "
            f"(provider={cfg.PROVIDER}, model={MODEL_NAME})"
        )
    return sqlite_vec.serialize_float32(flat.tolist())


# Re-export for diagnostics — callers (e.g. /health) may want to know which
# backend is active without touching cfg directly.
def active_backend() -> dict[str, object]:
    return {
        "provider": cfg.PROVIDER,
        "model": MODEL_NAME,
        "dim": EMBEDDING_DIM,
        "cf_account_id_set": bool(os.environ.get("CF_ACCOUNT_ID"))
        or (
            __import__("pathlib").Path("~/.gstack/cloudflare.json").expanduser().exists()
        ),
    }
