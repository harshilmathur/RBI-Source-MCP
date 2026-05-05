"""Preflight checks for an `rbi-source-mcp` install.

Verifies the environment is set up correctly so first-run failures (HF
model download timeout, cache permission denied, missing sqlite-vec, no
corpus, etc.) get caught with a clear error instead of a runtime crash
in the middle of a Claude Desktop session.

Usage:
    rbi-source-doctor                # all checks, exit non-zero on any failure
    rbi-source-doctor --json         # machine-readable output
    rbi-source-doctor --quick        # skip the slow torch + model checks

Each check is one of:
    OK     all good
    WARN   not ideal but won't break the happy path
    FAIL   blocks the server from running

Exit code is 0 if no FAILs, 1 otherwise. WARNs don't fail.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import __version__


@dataclass(slots=True)
class Check:
    name: str
    status: str  # OK | WARN | FAIL
    detail: str
    fix: str = ""
    elapsed_ms: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Individual checks. Each returns a Check.
# ---------------------------------------------------------------------------


def check_python_version() -> Check:
    t0 = time.perf_counter()
    v = sys.version_info
    elapsed = int((time.perf_counter() - t0) * 1000)
    if v < (3, 11):
        return Check(
            name="python",
            status="FAIL",
            detail=f"Python {v.major}.{v.minor}.{v.micro} (need 3.11+)",
            fix="install Python 3.11 or newer; rbi-source-mcp uses 3.11+ syntax",
            elapsed_ms=elapsed,
        )
    return Check(
        name="python",
        status="OK",
        detail=f"Python {v.major}.{v.minor}.{v.micro}",
        elapsed_ms=elapsed,
    )


def check_sqlite_vec() -> Check:
    """Confirm sqlite-vec loads. Without it, dense retrieval falls back
    to FTS-only — search still works but with worse recall."""
    t0 = time.perf_counter()
    try:
        import sqlite_vec  # noqa: F401
    except ImportError as exc:
        return Check(
            name="sqlite_vec",
            status="FAIL",
            detail=f"sqlite-vec not importable: {exc}",
            fix="`pip install sqlite-vec` (it should already be a hard dep — try a clean reinstall)",
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )
    # Try loading it on a throwaway connection. SQLite must be built
    # with extension loading enabled (this is the default on Python's
    # bundled SQLite, but some distros disable it).
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.enable_load_extension(True)
        except sqlite3.NotSupportedError:
            return Check(
                name="sqlite_vec",
                status="FAIL",
                detail="SQLite built without extension loading support",
                fix=(
                    "your Python's bundled SQLite doesn't allow loadable extensions; "
                    "reinstall Python from python.org or the official Homebrew formula"
                ),
                elapsed_ms=int((time.perf_counter() - t0) * 1000),
            )
        sqlite_vec.load(conn)
        # Verify it actually registered the vec0 module.
        conn.execute("CREATE VIRTUAL TABLE _ping USING vec0(embedding float[4]);")
        conn.close()
    except Exception as exc:  # noqa: BLE001
        return Check(
            name="sqlite_vec",
            status="FAIL",
            detail=f"sqlite-vec failed to load: {exc}",
            fix="reinstall: `pip install --force-reinstall sqlite-vec`",
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )
    return Check(
        name="sqlite_vec",
        status="OK",
        detail="sqlite-vec loads + vec0 virtual table works",
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    )


def check_corpus(corpus_path: Path) -> Check:
    t0 = time.perf_counter()
    if not corpus_path.exists():
        return Check(
            name="corpus",
            status="FAIL",
            detail=f"no corpus at {corpus_path}",
            fix="run `rbi-source-fetch-corpus` to download the latest prebuilt corpus",
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )
    try:
        with sqlite3.connect(str(corpus_path)) as conn:
            doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            schema_row = conn.execute(
                "SELECT value FROM corpus_meta WHERE key = 'schema_version'"
            ).fetchone()
            schema = schema_row[0] if schema_row else None
    except sqlite3.DatabaseError as exc:
        return Check(
            name="corpus",
            status="FAIL",
            detail=f"corpus at {corpus_path} is not a valid SQLite DB: {exc}",
            fix="re-download with `rbi-source-fetch-corpus`",
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )
    elapsed = int((time.perf_counter() - t0) * 1000)
    if doc_count == 0:
        return Check(
            name="corpus",
            status="FAIL",
            detail=f"corpus is empty (0 documents) at {corpus_path}",
            fix="re-download with `rbi-source-fetch-corpus`",
            elapsed_ms=elapsed,
        )
    return Check(
        name="corpus",
        status="OK",
        detail=f"{doc_count:,} documents, {chunk_count:,} chunks, schema_version={schema}",
        elapsed_ms=elapsed,
        extra={"documents": doc_count, "chunks": chunk_count, "schema_version": schema},
    )


def check_torch_import() -> Check:
    """Lazy-import torch — slow on first run after install."""
    t0 = time.perf_counter()
    try:
        import torch  # noqa: F401
    except ImportError as exc:
        return Check(
            name="torch",
            status="WARN",
            detail=f"torch not importable: {exc}",
            fix=(
                "if you plan to use RBI_EMBEDDING_PROVIDER=local (default), "
                "reinstall: `pip install --force-reinstall rbi-source-mcp`. "
                "If you intend to use cloudflare embeddings only, this is harmless."
            ),
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )
    return Check(
        name="torch",
        status="OK",
        detail=f"torch {torch.__version__}",
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    )


def check_hf_cache_writable() -> Check:
    """Confirm we can write to the HuggingFace cache. First-run model
    download will land here; if it's not writable, the local provider
    silently fails on its first query."""
    t0 = time.perf_counter()
    cache = os.environ.get("HF_HOME") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if not cache:
        cache = str(Path("~/.cache/huggingface").expanduser())
    cache_path = Path(cache).expanduser()
    try:
        cache_path.mkdir(parents=True, exist_ok=True)
        # Probe writability with a sentinel file.
        probe = cache_path / ".rbi_doctor_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except PermissionError as exc:
        return Check(
            name="hf_cache",
            status="FAIL",
            detail=f"cannot write to {cache_path}: {exc}",
            fix=(
                "chmod the directory writable, OR set $HF_HOME to a path you own:\n"
                "           export HF_HOME=~/.cache/huggingface"
            ),
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )
    except OSError as exc:
        return Check(
            name="hf_cache",
            status="WARN",
            detail=f"cache path issue at {cache_path}: {exc}",
            fix="check that the parent directory exists and is reachable",
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )
    return Check(
        name="hf_cache",
        status="OK",
        detail=f"writable: {cache_path}",
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    )


def check_local_model_present() -> Check:
    """Soft check — the bge-small-en-v1.5 model directory under HF cache.
    WARN if missing; first query will trigger the ~135 MB download."""
    t0 = time.perf_counter()
    cache = os.environ.get("HF_HOME") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if not cache:
        cache = str(Path("~/.cache/huggingface").expanduser())
    base = Path(cache).expanduser()
    # HuggingFace's cache layout: <cache>/hub/models--BAAI--bge-small-en-v1.5/
    model_dir = base / "hub" / "models--BAAI--bge-small-en-v1.5"
    elapsed = int((time.perf_counter() - t0) * 1000)
    if model_dir.exists() and any(model_dir.iterdir()):
        return Check(
            name="local_model",
            status="OK",
            detail=f"bge-small-en-v1.5 cached at {model_dir}",
            elapsed_ms=elapsed,
        )
    return Check(
        name="local_model",
        status="WARN",
        detail="bge-small-en-v1.5 not yet downloaded",
        fix=(
            "harmless — first query will download it (~135 MB). To prefetch:\n"
            '           python -c "from sentence_transformers import SentenceTransformer; '
            'SentenceTransformer(\\"BAAI/bge-small-en-v1.5\\")"'
        ),
        elapsed_ms=elapsed,
    )


def check_cf_creds_when_provider_cf() -> Check:
    """Only checks if RBI_EMBEDDING_PROVIDER=cloudflare. Otherwise skipped."""
    t0 = time.perf_counter()
    provider = (os.environ.get("RBI_EMBEDDING_PROVIDER") or "local").strip().lower()
    elapsed = int((time.perf_counter() - t0) * 1000)
    if provider != "cloudflare":
        return Check(
            name="cf_creds",
            status="OK",
            detail=f"skipped (provider={provider})",
            elapsed_ms=elapsed,
        )
    if os.environ.get("CF_ACCOUNT_ID") and os.environ.get("CF_API_TOKEN"):
        return Check(
            name="cf_creds",
            status="OK",
            detail="$CF_ACCOUNT_ID + $CF_API_TOKEN both set",
            elapsed_ms=elapsed,
        )
    creds_file = Path("~/.gstack/cloudflare.json").expanduser()
    if creds_file.exists():
        return Check(
            name="cf_creds",
            status="OK",
            detail=f"creds file at {creds_file}",
            elapsed_ms=elapsed,
        )
    return Check(
        name="cf_creds",
        status="FAIL",
        detail="RBI_EMBEDDING_PROVIDER=cloudflare but no CF creds found",
        fix=(
            "set $CF_ACCOUNT_ID + $CF_API_TOKEN, OR write {\"account_id\":..., \"api_token\":...} "
            "to ~/.gstack/cloudflare.json. See https://dash.cloudflare.com/profile/api-tokens"
        ),
        elapsed_ms=elapsed,
    )


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------


def run(*, quick: bool = False) -> list[Check]:
    """Run all checks. Order: cheap first, slow last."""
    from .fetch_corpus import _default_destination

    checks: list[Check] = []
    checks.append(check_python_version())
    checks.append(check_sqlite_vec())
    checks.append(check_corpus(_default_destination()))
    checks.append(check_hf_cache_writable())
    checks.append(check_cf_creds_when_provider_cf())
    if not quick:
        checks.append(check_torch_import())
        checks.append(check_local_model_present())
    return checks


def _print_human(checks: list[Check]) -> int:
    """Pretty-print to stdout. Return shell exit code (0 if no FAILs)."""
    width = max(len(c.name) for c in checks) + 2
    color = sys.stdout.isatty()
    sym = {
        "OK": "\033[32m✓\033[0m" if color else "OK  ",
        "WARN": "\033[33m!\033[0m" if color else "WARN",
        "FAIL": "\033[31m✗\033[0m" if color else "FAIL",
    }
    print(f"rbi-source-mcp doctor (v{__version__})")
    print()
    for c in checks:
        print(f"  {sym[c.status]}  {c.name.ljust(width)} {c.detail}")
        if c.status != "OK" and c.fix:
            for line in c.fix.splitlines():
                print(f"     {line}")
    print()
    failed = [c for c in checks if c.status == "FAIL"]
    warned = [c for c in checks if c.status == "WARN"]
    if failed:
        print(f"  {len(failed)} FAIL, {len(warned)} WARN")
        return 1
    if warned:
        print(f"  all FAIL-checks passed, {len(warned)} WARN")
    else:
        print("  all checks passed")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="rbi-source-doctor",
        description="Preflight checks for an rbi-source-mcp install.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="output machine-readable JSON instead of pretty text",
    )
    parser.add_argument(
        "--quick", action="store_true",
        help="skip slow torch + model presence checks",
    )
    parser.add_argument(
        "--version", action="version", version=f"rbi-source-doctor (rbi-source-mcp {__version__})",
    )
    args = parser.parse_args()

    checks = run(quick=args.quick)

    if args.json:
        print(json.dumps([asdict(c) for c in checks], indent=2))
        sys.exit(1 if any(c.status == "FAIL" for c in checks) else 0)

    sys.exit(_print_human(checks))


if __name__ == "__main__":
    main()
