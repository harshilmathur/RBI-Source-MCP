"""Download a prebuilt corpus from GitHub Releases.

The `corpus-release.yml` workflow publishes a compressed corpus
SQLite (`corpus.sqlite.xz`) plus a SHA256 checksum and a sigstore bundle
to two release tags. Cadence: daily diff at 02:00 UTC + monthly full
rebuild on the 1st at 03:00 UTC.
    - `latest-corpus` — moving alias updated in place each run (default)
    - `corpus-YYYY-MM-DD-<run_id>` — immutable timestamped builds

This script downloads the corpus, verifies SHA256, optionally verifies
the sigstore signature, decompresses the SQLite, opens it to confirm
schema_version matches the running package, and atomically renames it
into place.

Usage:
    rbi-source-fetch-corpus                          # latest, default destination
    rbi-source-fetch-corpus --tag corpus-2026-05-04-1234567
    rbi-source-fetch-corpus --to /custom/path/db.sqlite
    rbi-source-fetch-corpus --no-verify-sigstore     # SHA256 only (default)
    rbi-source-fetch-corpus --verify-sigstore        # opt-in: cryptographic verify

Default destination, in order:
    1. $RBI_SOURCE_DB if set
    2. ~/.local/share/rbi-source-mcp/db.sqlite

Error messages spec (autoplan review #8): every failure mode prints
PROBLEM + CAUSE + FIX so the user can act without reading source.

This module is the v0.7.1 implementation. It uses httpx (already a main
dep of `rbi-source-mcp`) so it adds no install cost. Sigstore
verification is opt-in via `pip install rbi-source-mcp[verify]` because
the sigstore Python package is heavy (~25 MB on disk) and most users are
fine with SHA256 + GitHub's transport TLS.
"""

from __future__ import annotations

import argparse
import hashlib
import lzma
import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import structlog

from . import __version__
from .db import CORPUS_SCHEMA_VERSION

logger = structlog.get_logger(__name__)


# Defaults — overridable via args.
DEFAULT_REPO = "harshilmathur/RBI-Source-MCP"
DEFAULT_TAG = "latest-corpus"
ASSET_NAME = "corpus.sqlite.xz"
SHA_ASSET_NAME = "corpus.sqlite.xz.sha256"
SIGSTORE_ASSET_NAME = "corpus.sqlite.xz.sigstore.json"

# 30s open timeout, 5min read timeout. The corpus is ~80 MB; on a poor
# connection that could take several minutes.
HTTP_TIMEOUT = httpx.Timeout(30.0, read=300.0)


def _default_destination() -> Path:
    """Resolve where to install the corpus.

    Priority: $RBI_SOURCE_DB → XDG-style ~/.local/share/rbi-source-mcp/db.sqlite.
    """
    env = os.environ.get("RBI_SOURCE_DB")
    if env:
        return Path(env).expanduser().resolve()
    base = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    return (base / "rbi-source-mcp" / "db.sqlite").resolve()


# ---------------------------------------------------------------------------
# Error helpers — every failure prints PROBLEM/CAUSE/FIX.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FetchError(Exception):
    """Structured fetch failure."""

    problem: str
    cause: str
    fix: str

    def __str__(self) -> str:
        return f"{self.problem}\n  cause: {self.cause}\n  fix:   {self.fix}"


def _abort(problem: str, cause: str, fix: str) -> None:
    """Print a structured error to stderr and exit non-zero."""
    sys.stderr.write(f"\nERROR: {problem}\n")
    sys.stderr.write(f"  cause: {cause}\n")
    sys.stderr.write(f"  fix:   {fix}\n")
    sys.exit(2)


# ---------------------------------------------------------------------------
# GitHub Releases API — fetch the asset URLs for a given tag.
# ---------------------------------------------------------------------------


def _release_assets(repo: str, tag: str, token: str | None = None) -> dict[str, str]:
    """Fetch the GitHub Release for `repo`@`tag` and return {asset_name: download_url}.

    Uses the public REST API. Authenticated requests get a 5000/hour rate
    limit; anonymous gets 60/hour, which is plenty for this script's
    occasional use.
    """
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"rbi-source-mcp/{__version__}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as c:
            r = c.get(url, headers=headers)
    except httpx.HTTPError as exc:
        _abort(
            problem=f"Could not reach GitHub Releases API for {repo}",
            cause=f"network error: {exc}",
            fix="check your internet connection, or pass --tag with a known-good tag",
        )
    if r.status_code == 404:
        _abort(
            problem=f"Release tag '{tag}' not found in {repo}",
            cause="the tag doesn't exist or hasn't been published yet",
            fix=(
                f"check available tags at https://github.com/{repo}/releases\n"
                "         (the moving alias 'latest-corpus' may not exist yet on a fresh repo)"
            ),
        )
    if r.status_code == 403 and "rate limit" in r.text.lower():
        _abort(
            problem="GitHub API rate limit exceeded",
            cause="anonymous requests are capped at 60/hour from your IP",
            fix="set $GITHUB_TOKEN to your personal access token, or wait an hour",
        )
    if r.status_code >= 400:
        _abort(
            problem=f"GitHub Releases API returned HTTP {r.status_code}",
            cause=r.text[:200],
            fix="re-run later, or report the issue at https://github.com/harshilmathur/RBI-Source-MCP/issues",
        )
    payload: dict[str, Any] = r.json()
    return {a["name"]: a["browser_download_url"] for a in payload.get("assets", [])}


def _download_to(url: str, dest: Path, *, label: str) -> None:
    """Stream a URL to disk with a progress dot every 4 MB.

    Atomically writes via .partial then renames so a crashed download
    can't leave a corrupt file at `dest`.
    """
    partial = dest.with_suffix(dest.suffix + ".partial")
    partial.parent.mkdir(parents=True, exist_ok=True)
    bytes_written = 0
    next_dot_at = 4 * 1024 * 1024
    sys.stdout.write(f"  ↓ {label} ")
    sys.stdout.flush()
    try:
        with (
            httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as c,
            c.stream("GET", url) as r,
        ):
            if r.status_code >= 400:
                _abort(
                    problem=f"HTTP {r.status_code} fetching {label}",
                    cause=f"URL: {url}",
                    fix="re-run; if persistent, the release asset may have been removed",
                )
            with partial.open("wb") as f:
                for chunk in r.iter_bytes(64 * 1024):
                    f.write(chunk)
                    bytes_written += len(chunk)
                    while bytes_written >= next_dot_at:
                        sys.stdout.write(".")
                        sys.stdout.flush()
                        next_dot_at += 4 * 1024 * 1024
        sys.stdout.write(f" {bytes_written // 1024} KB\n")
    except httpx.HTTPError as exc:
        partial.unlink(missing_ok=True)
        _abort(
            problem=f"Download failed mid-stream for {label}",
            cause=f"network error after {bytes_written} bytes: {exc}",
            fix="re-run; the partial file has been cleaned up",
        )
    except OSError as exc:
        partial.unlink(missing_ok=True)
        _abort(
            problem=f"Could not write {dest}",
            cause=str(exc),
            fix="check disk space and that the destination is writable",
        )
    partial.rename(dest)


# ---------------------------------------------------------------------------
# Verification.
# ---------------------------------------------------------------------------


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_sha256_line(line: str) -> str:
    """Parse a sha256sum line in either GNU or BSD style.

    GNU style:  ``<hex>  <filename>``
    BSD style:  ``SHA256 (<filename>) = <hex>``  (mac `shasum -p`, BSD `sha256`)

    Returns the lowercase 64-char hex digest. Raises ValueError if neither
    format matches.
    """
    line = line.strip()
    # BSD: SHA256 (file) = hex
    if line.upper().startswith("SHA256 ") and "=" in line:
        return line.rsplit("=", 1)[1].strip().lower()
    # GNU: hex<spaces>file
    parts = line.split()
    if not parts:
        raise ValueError("empty checksum line")
    candidate = parts[0].lower()
    if len(candidate) == 64 and all(c in "0123456789abcdef" for c in candidate):
        return candidate
    raise ValueError(f"unrecognized checksum format: {line[:80]!r}")


def _verify_sha256(asset: Path, sha_file: Path) -> None:
    raw = sha_file.read_text(encoding="utf-8").strip()
    if not raw:
        _abort(
            problem="SHA256 checksum file is empty",
            cause=f"no contents in {sha_file}",
            fix="the release asset is malformed; re-run later or report the bad release",
        )
    try:
        expected = _parse_sha256_line(raw.splitlines()[0])
    except ValueError as exc:
        _abort(
            problem="Could not parse SHA256 checksum file",
            cause=str(exc),
            fix=(
                "expected GNU `<hex>  <file>` or BSD `SHA256 (<file>) = <hex>` format;\n"
                "         the release asset is malformed"
            ),
        )
    actual = _sha256_of(asset)
    if expected != actual.lower():
        _abort(
            problem="SHA256 verification failed",
            cause=f"expected {expected[:16]}... got {actual[:16]}...",
            fix=(
                "the download is corrupt or tampered. Re-run; if the failure persists, the\n"
                "         release asset may have been replaced with a malformed copy."
            ),
        )


def _verify_sigstore(asset: Path, bundle: Path, *, repo: str) -> None:
    """Verify the sigstore bundle. Soft-deps on the `sigstore` package."""
    try:
        from sigstore.models import Bundle  # type: ignore[import-not-found]
        from sigstore.verify import Verifier  # type: ignore[import-not-found]
        from sigstore.verify.policy import Identity  # type: ignore[import-not-found]
    except ImportError:
        _abort(
            problem="--verify-sigstore was passed but the `sigstore` package isn't installed",
            cause="sigstore-python is an opt-in soft dep (heavy: ~25 MB on disk)",
            fix="`pip install 'rbi-source-mcp[verify]'`, or drop --verify-sigstore for SHA256-only",
        )

    try:
        verifier = Verifier.production()  # GitHub Sigstore root
        bundle_obj = Bundle.from_json(bundle.read_text())
        # Identity binds the bundle to "this workflow on this repo".
        # For our daily/monthly corpus release: identity issued by GitHub Actions OIDC
        # for `corpus-release.yml` on the {repo}'s `main` branch.
        policy = Identity(
            identity=f"https://github.com/{repo}/.github/workflows/corpus-release.yml@refs/heads/main",
            oidc_issuer="https://token.actions.githubusercontent.com",
        )
        with asset.open("rb") as f:
            verifier.verify_artifact(f.read(), bundle_obj, policy)
    except Exception as exc:  # noqa: BLE001 — sigstore raises a zoo of types
        _abort(
            problem="Sigstore signature verification failed",
            cause=f"{type(exc).__name__}: {exc}",
            fix=(
                "the artifact is signed but the signature doesn't trace back to a\n"
                "         GitHub Actions run on the official repo. The release may have been\n"
                "         tampered with, or sigstore's trust root may have rotated. Re-run, or\n"
                "         drop --verify-sigstore to fall back to SHA256-only verification."
            ),
        )


# ---------------------------------------------------------------------------
# Decompression + schema version check + atomic rename.
# ---------------------------------------------------------------------------


# Hard cap on decompressed corpus size. The legitimate corpus today is
# ~250-500 MB; 2 GB is generous future headroom. A maliciously crafted
# xz that decompresses to TBs (decompression bomb) would otherwise fill
# the user's disk silently. SHA256 + sigstore prove the artifact came
# from us; this cap prevents OUR build from accidentally shipping
# something obscene, AND defends against a typosquat repo via `--repo`.
MAX_DECOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB


def _decompress_xz(src: Path, dest: Path) -> None:
    """Stream-decompress with a hard size cap (review #6 fix)."""
    bytes_written = 0
    chunk_size = 1 << 20  # 1 MiB
    try:
        with lzma.open(src, "rb") as src_f, dest.open("wb") as dest_f:
            while True:
                chunk = src_f.read(chunk_size)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > MAX_DECOMPRESSED_BYTES:
                    dest_f.close()
                    dest.unlink(missing_ok=True)
                    _abort(
                        problem="Corpus decompression exceeded safety cap",
                        cause=(
                            f"xz output > {MAX_DECOMPRESSED_BYTES // (1 << 30)} GB "
                            "(probable decompression bomb)"
                        ),
                        fix=(
                            "the release asset is malformed or malicious. Verify --repo,\n"
                            "         re-run with --verify-sigstore, or report the bad asset."
                        ),
                    )
                dest_f.write(chunk)
    except OSError as exc:
        dest.unlink(missing_ok=True)
        _abort(
            problem=f"Failed to write decompressed corpus to {dest}",
            cause=str(exc),
            fix="check disk space and that the destination is writable",
        )
    except lzma.LZMAError as exc:
        dest.unlink(missing_ok=True)
        _abort(
            problem="Corpus xz file is corrupt",
            cause=str(exc),
            fix="re-run to download a fresh copy; if persistent, report the bad release asset",
        )


def _check_schema_compatibility(db_path: Path) -> dict[str, str | None]:
    """Open the corpus READ-ONLY, read corpus_meta, refuse on mismatch.

    v0.8.1 review #1 fix: open with SQLite URI `mode=ro` + skip the
    project's `connect()` wrapper. The wrapper would call
    `_stamp_schema_version`, which on a fresh corpus stamps the running
    version BEFORE we read it back — silently passing the missing-stamp
    abort branch and any v0.6 corpus that lacks the stamp.

    v0.8.1 review #3 fix: also validate `corpus_meta.embedding_dim`
    against the runtime `embedding_config.DIM`. v0.8.1 unifies on 768
    everywhere, but a user with leftover `RBI_EMBEDDING_DIM=384` env
    from a v0.7 install would silently break dense retrieval.

    Returns the meta dict (for the success summary line) on the happy
    path.
    """
    from . import embedding_config as _cfg

    uri = f"file:{db_path}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT key, value FROM corpus_meta")
            kv = {row["key"]: row["value"] for row in cur.fetchall()}
            doc_count = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()[0]
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "no such table" in msg and "corpus_meta" in msg:
            _abort(
                problem="Corpus is missing the corpus_meta table",
                cause="the corpus was built by a release older than v0.7.0",
                fix=(
                    "use --tag to pick a corpus built by v0.7.0+ (the latest-corpus alias\n"
                    "         tracks the freshest), or accept the risk via --no-verify-schema"
                ),
            )
        _abort(
            problem=f"Downloaded corpus at {db_path} is not readable",
            cause=str(exc),
            fix="re-run to download a fresh copy; the release asset may be corrupt",
        )
    except sqlite3.DatabaseError as exc:
        _abort(
            problem=f"Downloaded corpus at {db_path} is not a valid SQLite database",
            cause=str(exc),
            fix="re-run to download a fresh copy; the release asset may be corrupt",
        )

    meta: dict[str, str | None] = {
        k: kv.get(k) for k in (
            "schema_version", "embedding_provider", "embedding_model",
            "embedding_dim", "embedding_revision", "build_timestamp",
            "build_commit", "build_mode",
        )
    }
    meta["documents"] = str(doc_count)

    actual = meta["schema_version"]
    if actual is None:
        _abort(
            problem="Corpus is missing the schema_version stamp",
            cause="the corpus was built by a release older than v0.7.0",
            fix=(
                "use --tag to pick a corpus built by v0.7.0+ (the latest-corpus alias\n"
                "         tracks the freshest), or accept the risk via --no-verify-schema"
            ),
        )
    if actual != CORPUS_SCHEMA_VERSION:
        _abort(
            problem=f"Corpus schema_version={actual} but this rbi-source-mcp expects {CORPUS_SCHEMA_VERSION}",
            cause="you have an installed package version that doesn't match the corpus build",
            fix=(
                "upgrade rbi-source-mcp (`pip install -U rbi-source-mcp`) to read this corpus,\n"
                "         OR use --tag to fetch a corpus built for your installed version"
            ),
        )

    # Embedding-dim mismatch detection. v0.8.1 unifies the default to 768
    # across both providers, but a user with a stale RBI_EMBEDDING_DIM=384
    # env (left over from a v0.7 setup) would silently break dense retrieval.
    corpus_dim_str = meta.get("embedding_dim")
    if corpus_dim_str is not None:
        try:
            corpus_dim = int(corpus_dim_str)
        except ValueError:
            corpus_dim = None
        if corpus_dim is not None and corpus_dim != _cfg.DIM:
            _abort(
                problem=f"Embedding dim mismatch: corpus={corpus_dim}, runtime={_cfg.DIM}",
                cause=(
                    f"the corpus was built with {meta.get('embedding_provider')} "
                    f"@ {meta.get('embedding_model')} ({corpus_dim}-dim), but your runtime "
                    f"is configured for {_cfg.DIM}-dim"
                ),
                fix=(
                    "unset RBI_EMBEDDING_DIM and RBI_EMBEDDING_MODEL so they default to\n"
                    "         the v0.8.1 unified bge-base @ 768-dim; OR build the corpus locally\n"
                    "         with your preferred provider"
                ),
            )
    return meta


def _atomic_rename_into_place(staging: Path, final: Path) -> None:
    """Move the verified corpus to its final destination, atomically.

    v0.8.1 review #4 fix: copy to a sibling `<final>.partial` on the
    destination filesystem first, fsync to ensure bytes are durable,
    then `os.replace()` for an atomic rename within the same fs.

    The previous implementation called `staging.rename(final)` which
    works on same-fs but raises EXDEV cross-fs (the common case: staging
    is in /tmp, final is under $HOME); the fallback `shutil.copy2`
    directly into `final` was non-atomic — a crash mid-copy would leave
    a corrupt SQLite at the user's working destination, blowing away an
    existing healthy corpus.

    Same-fs path: rename(staging → partial → final) is atomic.
    Cross-fs path: copy(staging → partial), fsync, rename(partial → final).
    Both paths leave `final` either fully old or fully new, never half.
    """
    final.parent.mkdir(parents=True, exist_ok=True)
    partial = final.parent / (final.name + ".partial")
    # Clean up a stray .partial from a previous crashed run.
    partial.unlink(missing_ok=True)

    try:
        # Try the same-filesystem fast path: move staging → partial. If
        # they're on different filesystems this raises OSError (EXDEV).
        os.rename(staging, partial)
    except OSError:
        # Cross-fs: byte-copy + fsync, then atomic rename within final.parent.
        shutil.copy2(staging, partial)
        try:
            with partial.open("rb") as f:
                os.fsync(f.fileno())
        except OSError:
            # fsync isn't supported on all filesystems (e.g., some FUSE
            # mounts); proceed without it, the rename is still atomic.
            pass
        staging.unlink(missing_ok=True)

    # Atomic on the destination filesystem regardless of how partial got there.
    os.replace(partial, final)


# ---------------------------------------------------------------------------
# Main flow.
# ---------------------------------------------------------------------------


def fetch(
    *,
    repo: str = DEFAULT_REPO,
    tag: str = DEFAULT_TAG,
    destination: Path | None = None,
    verify_sigstore: bool = False,
    verify_schema: bool = True,
    token: str | None = None,
    work_dir: Path | None = None,
) -> dict[str, str | None]:
    """Download + verify + install the corpus. Returns the corpus_meta dict."""
    final_dest = destination or _default_destination()
    print(f"Fetching corpus from {repo}@{tag} → {final_dest}")

    # Pick up a token from env so authenticated requests get a 5000/hr limit.
    token = token or os.environ.get("GITHUB_TOKEN")

    print("→ resolving release assets")
    assets = _release_assets(repo, tag, token=token)
    for required in (ASSET_NAME, SHA_ASSET_NAME):
        if required not in assets:
            _abort(
                problem=f"Release '{tag}' is missing required asset '{required}'",
                cause="the release was published incompletely or the asset was deleted",
                fix=(
                    "use --tag to pick a different release, or report the issue at\n"
                    "         https://github.com/harshilmathur/RBI-Source-MCP/issues"
                ),
            )
    if verify_sigstore and SIGSTORE_ASSET_NAME not in assets:
        _abort(
            problem="--verify-sigstore was passed but the release has no sigstore bundle",
            cause=f"asset '{SIGSTORE_ASSET_NAME}' is not present on tag '{tag}'",
            fix="drop --verify-sigstore for SHA256-only, or use a newer tag with sigstore signing",
        )

    # Use a temp dir for the download so a crash partway never leaves
    # half-baked files at the user's destination.
    with tempfile.TemporaryDirectory(
        prefix="rbi-fetch-", dir=str(work_dir) if work_dir else None
    ) as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        xz_path = tmpdir / ASSET_NAME
        sha_path = tmpdir / SHA_ASSET_NAME
        bundle_path = tmpdir / SIGSTORE_ASSET_NAME

        print("→ downloading")
        _download_to(assets[ASSET_NAME], xz_path, label=ASSET_NAME)
        _download_to(assets[SHA_ASSET_NAME], sha_path, label=SHA_ASSET_NAME)
        if verify_sigstore:
            _download_to(assets[SIGSTORE_ASSET_NAME], bundle_path, label=SIGSTORE_ASSET_NAME)

        print("→ verifying SHA256")
        _verify_sha256(xz_path, sha_path)

        if verify_sigstore:
            print("→ verifying sigstore signature")
            _verify_sigstore(xz_path, bundle_path, repo=repo)

        print("→ decompressing")
        staged = tmpdir / "db.sqlite"
        _decompress_xz(xz_path, staged)

        if verify_schema:
            print("→ checking schema_version")
            meta = _check_schema_compatibility(staged)
        else:
            meta = {"schema_version": "(unverified)"}

        print(f"→ installing to {final_dest}")
        _atomic_rename_into_place(staged, final_dest)

    print()
    print("Done. Corpus installed.")
    print(f"  destination:        {final_dest}")
    print(f"  schema_version:     {meta.get('schema_version')}")
    print(f"  documents:          {meta.get('documents')}")
    print(f"  embedding_provider: {meta.get('embedding_provider')}")
    print(f"  embedding_model:    {meta.get('embedding_model')}")
    print(f"  embedding_dim:      {meta.get('embedding_dim')}")
    print(f"  embedding_revision: {meta.get('embedding_revision')}")
    print(f"  built:              {meta.get('build_timestamp')} (commit {meta.get('build_commit')})")
    print(f"  build_mode:         {meta.get('build_mode')}")
    return meta


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="rbi-source-fetch-corpus",
        description=(
            "Download a prebuilt RBI corpus from GitHub Releases. "
            "Verifies SHA256 (always), sigstore signature (opt-in), and "
            "schema_version compatibility before installing."
        ),
    )
    parser.add_argument(
        "--repo", default=DEFAULT_REPO,
        help=f"GitHub repo to download from (default: {DEFAULT_REPO})",
    )
    parser.add_argument(
        "--tag", default=DEFAULT_TAG,
        help=(
            f"Release tag (default: {DEFAULT_TAG} — moving alias updated daily). "
            "Use a corpus-YYYY-MM-DD-<run_id> tag for an immutable pin."
        ),
    )
    parser.add_argument(
        "--to", "--destination", dest="destination", default=None,
        help=(
            "Destination path for the SQLite corpus. "
            "Default: $RBI_SOURCE_DB or ~/.local/share/rbi-source-mcp/db.sqlite"
        ),
    )
    parser.add_argument(
        "--verify-sigstore", action="store_true",
        help=(
            "Cryptographically verify the corpus signature against the GitHub Actions OIDC "
            "identity. Requires the optional `sigstore` package: pip install 'rbi-source-mcp[verify]'"
        ),
    )
    parser.add_argument(
        "--no-verify-schema", action="store_true",
        help=(
            "Skip the corpus_meta.schema_version compatibility check. Use only if you "
            "know what you are doing — a schema mismatch can cause silent retrieval bugs."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"rbi-source-fetch-corpus (rbi-source-mcp {__version__})",
    )
    args = parser.parse_args()

    fetch(
        repo=args.repo,
        tag=args.tag,
        destination=Path(args.destination).expanduser().resolve() if args.destination else None,
        verify_sigstore=args.verify_sigstore,
        verify_schema=not args.no_verify_schema,
    )


if __name__ == "__main__":
    main()
