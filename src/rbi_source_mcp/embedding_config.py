"""Embedding configuration — provider, model, and vector dimension.

Driven by environment variables so we can A/B test providers without code
changes.

Env vars:
    RBI_EMBEDDING_PROVIDER  local | cloudflare           (default: local)
    RBI_EMBEDDING_MODEL     model id for the provider    (default depends on provider)
    RBI_EMBEDDING_DIM       integer vector dimension     (default: matches model)

Provider notes:
    local
        Uses sentence-transformers. Bundled as a main dep in v0.7.0+ so
        `pip install rbi-source-mcp` is zero-config (no Cloudflare account
        required). Pulls CPU-only torch + the bge-base model (~440 MB)
        on first query.

    cloudflare
        POSTs to https://api.cloudflare.com/client/v4/accounts/<ACCT>/ai/run/<MODEL>.
        Reads creds from $CF_ACCOUNT_ID + $CF_API_TOKEN.
        CF directly serves the BGE family on their GPUs — no third-party
        provider in the path, free at <10k req/day.

v0.8.1 unification (review #3 fix):
    Both providers default to bge-base-en-v1.5 @ 768-dim so the prebuilt
    corpus (built in CI with provider=cloudflare, dim=768) can be queried
    by ANY runtime without dim mismatch. Local users pay ~440 MB extra
    model size + ~50 ms more per CPU embed call vs the old bge-small
    default; tradeoff is ONE corpus, ONE config, no two-variants logic.

Model+dim defaults:
    local      → BAAI/bge-base-en-v1.5      (768 dim)
    cloudflare → @cf/baai/bge-base-en-v1.5  (768 dim)
"""

from __future__ import annotations

import os

PROVIDER = (os.environ.get("RBI_EMBEDDING_PROVIDER") or "local").strip().lower()


def _default_model() -> str:
    if PROVIDER == "cloudflare":
        return "@cf/baai/bge-base-en-v1.5"
    return "BAAI/bge-base-en-v1.5"


def _default_dim() -> int:
    """Match the dimension the model actually emits.

    bge-small-en-v1.5 → 384, bge-base-en-v1.5 → 768, bge-large/m3 → 1024.
    Anything else, the user must set RBI_EMBEDDING_DIM explicitly.
    """
    m = MODEL.lower()
    if "bge-small" in m:
        return 384
    if "bge-base" in m:
        return 768
    if "bge-large" in m or "bge-m3" in m:
        return 1024
    return 768  # v0.8.1 default — bge-base


MODEL: str = os.environ.get("RBI_EMBEDDING_MODEL") or _default_model()
DIM: int = int(os.environ.get("RBI_EMBEDDING_DIM") or _default_dim())

# v0.7 (autoplan review #5): pin the HuggingFace revision so a future model
# update on HF can't silently change query/index embeddings between an old
# corpus and a new runtime. "main" is the default float for OSS users who
# just want to run; production deploys (and the corpus-release.yml workflow)
# should pin to a known-good commit SHA via `RBI_LOCAL_MODEL_REVISION`.
#
# To find the current SHA for BAAI/bge-base-en-v1.5:
#   curl -sSL https://huggingface.co/api/models/BAAI/bge-base-en-v1.5/revision/main \
#     | jq -r .sha
# Update this default once verified, OR set the env var on the build env.
LOCAL_MODEL_REVISION: str = os.environ.get("RBI_LOCAL_MODEL_REVISION", "main")


def cloudflare_creds() -> tuple[str, str]:
    """Return (account_id, api_token) from the environment. Raises if unset.

    Only consulted when RBI_EMBEDDING_PROVIDER=cloudflare. The default
    `local` provider needs no credentials.
    """
    acct = os.environ.get("CF_ACCOUNT_ID")
    token = os.environ.get("CF_API_TOKEN")
    if acct and token:
        return acct, token
    raise RuntimeError(
        "Cloudflare creds missing. Set $CF_ACCOUNT_ID and $CF_API_TOKEN "
        "(required only when RBI_EMBEDDING_PROVIDER=cloudflare)."
    )
