"""Embedding configuration — provider, model, and vector dimension.

Driven by environment variables so we can A/B test providers without code
changes. Defaults preserve the project's original behaviour (local
sentence-transformers + bge-small-en-v1.5 + 384-dim).

Env vars:
    RBI_EMBEDDING_PROVIDER  local | cloudflare           (default: local)
    RBI_EMBEDDING_MODEL     model id for the provider    (default depends on provider)
    RBI_EMBEDDING_DIM       integer vector dimension     (default: matches model)

Provider notes:
    local
        Uses sentence-transformers. Bundled as a main dep in v0.7.0+ so
        `pip install rbi-source-mcp` is zero-config (no Cloudflare account
        required). Pulls CPU-only torch + the bge-small model (~135 MB)
        on first query.

    cloudflare
        POSTs to https://api.cloudflare.com/client/v4/accounts/<ACCT>/ai/run/<MODEL>.
        Reads creds from $CF_ACCOUNT_ID + $CF_API_TOKEN, OR from
        ~/.gstack/cloudflare.json {"account_id":..., "api_token":...}.
        CF directly serves the BGE family on their GPUs — no third-party
        provider in the path, free at <10k req/day.

Model+dim defaults:
    cloudflare → @cf/baai/bge-base-en-v1.5 (768 dim)   ← target for the A/B
    local      → BAAI/bge-small-en-v1.5  (384 dim)    ← current production
"""

from __future__ import annotations

import json
import os
from pathlib import Path

PROVIDER = (os.environ.get("RBI_EMBEDDING_PROVIDER") or "local").strip().lower()


def _default_model() -> str:
    if PROVIDER == "cloudflare":
        return "@cf/baai/bge-base-en-v1.5"
    return "BAAI/bge-small-en-v1.5"


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
    return 384  # safe default for the legacy path


MODEL: str = os.environ.get("RBI_EMBEDDING_MODEL") or _default_model()
DIM: int = int(os.environ.get("RBI_EMBEDDING_DIM") or _default_dim())

# v0.7 (autoplan review #5): pin the HuggingFace revision so a future model
# update on HF can't silently change query/index embeddings between an old
# corpus and a new runtime. "main" is the default float for OSS users who
# just want to run; production deploys (and the corpus-release.yml workflow)
# should pin to a known-good commit SHA via `RBI_LOCAL_MODEL_REVISION`.
#
# To find the current SHA for BAAI/bge-small-en-v1.5:
#   curl -sSL https://huggingface.co/api/models/BAAI/bge-small-en-v1.5/revision/main \
#     | jq -r .sha
# Update this default once verified, OR set the env var on the build env.
LOCAL_MODEL_REVISION: str = os.environ.get("RBI_LOCAL_MODEL_REVISION", "main")


def cloudflare_creds() -> tuple[str, str]:
    """Return (account_id, api_token). Raises if neither env nor file is set."""
    acct = os.environ.get("CF_ACCOUNT_ID")
    token = os.environ.get("CF_API_TOKEN")
    if acct and token:
        return acct, token
    cred_file = Path.home() / ".gstack" / "cloudflare.json"
    if cred_file.exists():
        data = json.loads(cred_file.read_text())
        if "account_id" in data and "api_token" in data:
            return data["account_id"], data["api_token"]
    raise RuntimeError(
        "Cloudflare creds missing. Set $CF_ACCOUNT_ID + $CF_API_TOKEN, "
        "or write {'account_id': ..., 'api_token': ...} to ~/.gstack/cloudflare.json"
    )
