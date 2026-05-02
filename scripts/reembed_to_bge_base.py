"""Re-embed an existing corpus DB against a different embedding model.

For the bge-small (384) → bge-base (768) A/B test. Strategy:

  1. Copy data/db.sqlite → data/db-bge-base.sqlite (preserve original).
  2. Drop the existing chunks_vec (which is float[384]).
  3. Open via project's connect() with RBI_EMBEDDING_DIM=768 set — that
     auto-creates a fresh chunks_vec at float[768].
  4. Stream every row from `chunks` (rowid, text), embed in batches of 100
     against the configured provider (Cloudflare Workers AI for this run),
     INSERT into chunks_vec(rowid, embedding).

Resume-friendly: skips rowids already present in chunks_vec, so a crash
mid-way only loses the in-flight batch. Set --restart to force from scratch.

Usage:
    RBI_EMBEDDING_PROVIDER=cloudflare \
    RBI_EMBEDDING_MODEL=@cf/baai/bge-base-en-v1.5 \
    RBI_EMBEDDING_DIM=768 \
        uv run python scripts/reembed_to_bge_base.py \
            --src data/db.sqlite \
            --dst data/db-bge-base.sqlite

This re-embed approach is roughly 10x faster than re-crawling because
crawl/extract/chunk/dedupe overhead is skipped — we only pay the
embedding-API time. With 56k chunks at 100/batch and ~600ms/batch, the
whole thing runs in ~5-7 minutes against CF.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

# Force the env so embedding_config picks the right defaults if the caller
# didn't already export them. We respect anything pre-set.
os.environ.setdefault("RBI_EMBEDDING_PROVIDER", "cloudflare")
os.environ.setdefault("RBI_EMBEDDING_MODEL", "@cf/baai/bge-base-en-v1.5")
os.environ.setdefault("RBI_EMBEDDING_DIM", "768")

# Project imports happen *after* env is set so embedding_config reads it.
import httpx  # noqa: E402

from rbi_source_mcp import embedding_config as cfg  # noqa: E402
from rbi_source_mcp.db import _load_sqlite_vec, connect  # noqa: E402
from rbi_source_mcp.indexer.embed import embed_texts, to_sqlite_vec_bytes  # noqa: E402

# CF bge-m3 enforces a 60K-token TOTAL-batch cap (server-side). One big
# chunk + 29 small ones can blow it. We halve-and-retry on that error.

# CF Workers AI imposes a ~60K-token TOTAL-batch cap on bge-m3 (server-side
# aggregation limit, NOT per-input). bge-base is fine at 100/batch because
# CF auto-truncates each input at 512 tokens (100 × 512 = 51K). For bge-m3
# we drop to ~30/batch so even chunks averaging ~1.5K tokens stay under cap.
DEFAULT_BATCH = 100


def _drop_old_vec_table(db_path: Path) -> None:
    """Drop the legacy chunks_vec virtual table so connect() can recreate it
    at the new dimension. SQLite requires the vec0 module to be loaded
    even for DROP, so we load sqlite-vec on this raw connection first."""
    raw = sqlite3.connect(db_path)
    try:
        _load_sqlite_vec(raw)
        raw.execute("DROP TABLE IF EXISTS chunks_vec")
        raw.commit()
    finally:
        raw.close()


def _already_done(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT rowid FROM chunks_vec").fetchall()
    return {r[0] for r in rows}


def _embed_with_split(texts: list[str]) -> list[list[float]]:
    """embed_texts() but with halve-and-retry on 400 'Max context reached'.

    CF's bge-m3 endpoint caps total tokens per batch at 60K. We can't predict
    tokens cheaply (no tokenizer locally), so on that specific 400 we split
    the batch in half and recurse. Singletons that still 400 raise — they're
    inputs the model genuinely can't take.
    """
    try:
        arr = embed_texts(texts)
        return arr.tolist()
    except httpx.HTTPStatusError as exc:
        if exc.response is None or exc.response.status_code != 400:
            raise
        body = exc.response.text
        if "Max context reached" not in body:
            raise
        if len(texts) <= 1:
            print(
                f"[warn] dropping single chunk that exceeds model context "
                f"(len={len(texts[0]) if texts else 0}): {body[:200]}",
                flush=True,
            )
            return []
        half = len(texts) // 2
        print(
            f"[adaptive] 400 on batch of {len(texts)}; splitting → {half} + {len(texts) - half}",
            flush=True,
        )
        return _embed_with_split(texts[:half]) + _embed_with_split(texts[half:])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, type=Path, help="Existing DB to copy from")
    ap.add_argument("--dst", required=True, type=Path, help="Target DB to write to")
    ap.add_argument(
        "--restart",
        action="store_true",
        help="Overwrite dst from src even if dst exists (else: resume)",
    )
    ap.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH,
        help="Embeddings per CF request. Drop to ~30 for bge-m3 (60K-token cap)",
    )
    args = ap.parse_args()
    batch = args.batch_size

    if not args.src.exists():
        print(f"src not found: {args.src}", file=sys.stderr)
        return 2

    if args.restart or not args.dst.exists():
        args.dst.parent.mkdir(parents=True, exist_ok=True)
        print(f"[copy] {args.src} → {args.dst}")
        shutil.copy2(args.src, args.dst)
        _drop_old_vec_table(args.dst)
    else:
        print(f"[resume] using existing {args.dst}")

    print(
        f"[config] provider={cfg.PROVIDER} model={cfg.MODEL} dim={cfg.DIM}",
        flush=True,
    )

    with connect(args.dst) as conn:
        total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        done = _already_done(conn)
        print(f"[corpus] {total} chunks total, {len(done)} already embedded", flush=True)

        rows = conn.execute(
            "SELECT rowid, text FROM chunks ORDER BY rowid"
        ).fetchall()
        pending = [(r["rowid"], r["text"]) for r in rows if r["rowid"] not in done]
        print(f"[corpus] {len(pending)} pending", flush=True)

        t0 = time.time()
        n_done = 0
        for i in range(0, len(pending), batch):
            chunk_batch = pending[i : i + batch]
            rowids = [b[0] for b in chunk_batch]
            texts = [b[1] for b in chunk_batch]
            t1 = time.time()
            vecs = _embed_with_split(texts)
            t_embed = time.time() - t1
            if len(vecs) != len(rowids):
                # _embed_with_split dropped a chunk that exceeded model context.
                # Realign: it always preserves order, so zip-trimming would
                # mis-key. Better: re-embed surviving inputs one at a time.
                print(
                    f"[recover] {len(rowids) - len(vecs)} chunks dropped; "
                    f"re-embedding remainder one-by-one to realign",
                    flush=True,
                )
                vecs = []
                for txt in texts:
                    try:
                        v = _embed_with_split([txt])
                        vecs.append(v[0] if v else None)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[skip] embed failed for chunk: {exc}", flush=True)
                        vecs.append(None)
            import numpy as _np

            for rid, vec in zip(rowids, vecs, strict=True):
                if vec is None:
                    continue
                conn.execute(
                    "INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                    (rid, to_sqlite_vec_bytes(_np.asarray(vec, dtype="float32"))),
                )
            conn.commit()
            n_done += len(chunk_batch)
            elapsed = time.time() - t0
            rate = n_done / elapsed if elapsed > 0 else 0.0
            eta = (len(pending) - n_done) / rate if rate > 0 else 0.0
            print(
                f"[batch {i // batch + 1}/{(len(pending) + batch - 1) // batch}] "
                f"+{len(chunk_batch)} chunks in {t_embed:.2f}s "
                f"({n_done}/{len(pending)} = {n_done / max(len(pending), 1) * 100:.1f}%, "
                f"rate={rate:.1f}/s, eta={eta / 60:.1f}min)",
                flush=True,
            )

        final_count = conn.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0]
        print(
            f"[done] {final_count}/{total} embedded in {(time.time() - t0) / 60:.2f} min",
            flush=True,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
