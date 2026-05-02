"""A/B comparator for the bge-small@384 vs bge-base@768 embedding switch.

Driver script. Spawns two `eval_dump.py` subprocesses (one per side), each
writing per-case JSONL to /tmp, then renders a side-by-side report:

    - per-case: pass/fail, top-1 chunk match, top-5 overlap (Jaccard),
      retrieval mode, latency
    - aggregate: hit-rate delta, mean/p95 latency, total disagreements

Usage:
    uv run python scripts/compare_ab.py \
        --db-a ./data/db.sqlite \
        --db-b ./data/db-bge-base.sqlite

The script does NOT modify either DB. It re-uses the shipped EVAL_CASES.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path


def _run_side(label: str, env_overrides: dict[str, str], out: Path) -> None:
    env = os.environ.copy()
    env.update(env_overrides)
    print(f"[{label}] running eval_dump → {out}", flush=True)
    cmd = [
        "uv",
        "run",
        "python",
        "scripts/eval_dump.py",
        "--out",
        str(out),
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"[{label}] FAILED rc={proc.returncode}", file=sys.stderr)
        print(proc.stdout, file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
        sys.exit(proc.returncode)
    # Forward eval_dump's progress lines so the user sees them inline.
    for line in proc.stderr.splitlines():
        print(f"  [{label}] {line}", flush=True)


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _index_by_name(rows: list[dict]) -> dict[str, dict]:
    return {r["case_name"]: r for r in rows}


def _provision_key(p: dict) -> str:
    """Stable key for set-overlap math. document_id+paragraph_anchor uniquely
    identifies a chunk-ish unit even if quoted_text differs across embeddings
    (which it shouldn't here — chunks are the same; only their embeddings differ)."""
    return f"{p.get('document_id', '')}::{p.get('paragraph_anchor', '')}"


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / max(len(a | b), 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-a", required=True, type=Path, help="baseline DB (bge-small@384)")
    ap.add_argument("--db-b", required=True, type=Path, help="new DB (bge-base@768)")
    ap.add_argument(
        "--keep-tmp",
        action="store_true",
        help="don't delete the per-side JSONL dumps after comparing",
    )
    args = ap.parse_args()

    if not args.db_a.exists():
        print(f"db-a missing: {args.db_a}", file=sys.stderr)
        return 2
    if not args.db_b.exists():
        print(f"db-b missing: {args.db_b}", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="rbi-ab-"))
    out_a = tmp / "eval-A.jsonl"
    out_b = tmp / "eval-B.jsonl"

    _run_side(
        "A: bge-small@384",
        {
            "RBI_SOURCE_DB": str(args.db_a.resolve()),
            # Explicitly clear any leaked CF env vars from caller so A
            # really is the local-default path.
            "RBI_EMBEDDING_PROVIDER": "local",
            "RBI_EMBEDDING_MODEL": "BAAI/bge-small-en-v1.5",
            "RBI_EMBEDDING_DIM": "384",
        },
        out_a,
    )
    _run_side(
        "B: bge-base@768",
        {
            "RBI_SOURCE_DB": str(args.db_b.resolve()),
            "RBI_EMBEDDING_PROVIDER": "cloudflare",
            "RBI_EMBEDDING_MODEL": "@cf/baai/bge-base-en-v1.5",
            "RBI_EMBEDDING_DIM": "768",
        },
        out_b,
    )

    rows_a = _load(out_a)
    rows_b = _load(out_b)
    by_a = _index_by_name(rows_a)
    by_b = _index_by_name(rows_b)
    names = sorted(set(by_a) | set(by_b))

    print()
    print("=" * 92)
    print("A/B COMPARISON — bge-small@384 (A) vs bge-base@768 (B)")
    print("=" * 92)

    n_a_pass = n_b_pass = 0
    only_a_pass = only_b_pass = both_pass = both_fail = 0
    top1_agree = 0
    top5_overlaps: list[float] = []
    lat_a: list[float] = []
    lat_b: list[float] = []

    for name in names:
        a = by_a.get(name)
        b = by_b.get(name)
        if not a or not b:
            print(f"\n  ! {name}: missing on one side (A={bool(a)}, B={bool(b)})")
            continue

        n_a_pass += int(a["passed"])
        n_b_pass += int(b["passed"])
        if a["passed"] and b["passed"]:
            both_pass += 1
        elif a["passed"]:
            only_a_pass += 1
        elif b["passed"]:
            only_b_pass += 1
        else:
            both_fail += 1

        keys_a = [_provision_key(p) for p in a["top_provisions"]]
        keys_b = [_provision_key(p) for p in b["top_provisions"]]
        top1_match = bool(keys_a) and bool(keys_b) and keys_a[0] == keys_b[0]
        top1_agree += int(top1_match)
        jaccard5 = _jaccard(set(keys_a), set(keys_b))
        top5_overlaps.append(jaccard5)

        lat_a.append(a["latency_ms"])
        lat_b.append(b["latency_ms"])

        marker_a = "✓" if a["passed"] else "✗"
        marker_b = "✓" if b["passed"] else "✗"
        flag = ""
        if a["passed"] != b["passed"]:
            flag = "  ⚠ DIFFERENT OUTCOME"
        elif not top1_match:
            flag = "  · top-1 differs"
        print(
            f"\n  A:{marker_a} B:{marker_b}  {name}{flag}\n"
            f"      top-1 A: {keys_a[0] if keys_a else '∅'}\n"
            f"      top-1 B: {keys_b[0] if keys_b else '∅'}\n"
            f"      top-5 jaccard: {jaccard5:.2f}    "
            f"latency A={a['latency_ms']:.0f}ms  B={b['latency_ms']:.0f}ms    "
            f"retrieval A={a['retrieval_mode']}  B={b['retrieval_mode']}"
        )

    n = len(names)
    print()
    print("=" * 92)
    print("AGGREGATE")
    print("=" * 92)
    print(f"  cases evaluated: {n}")
    print(f"  hit rate    A: {n_a_pass}/{n} = {n_a_pass / max(n, 1) * 100:.1f}%")
    print(f"  hit rate    B: {n_b_pass}/{n} = {n_b_pass / max(n, 1) * 100:.1f}%")
    print(f"  delta (B-A): {n_b_pass - n_a_pass:+d} cases")
    print(f"  both pass: {both_pass}   both fail: {both_fail}   "
          f"only-A pass: {only_a_pass}   only-B pass: {only_b_pass}")
    print(f"  top-1 agreement: {top1_agree}/{n} = {top1_agree / max(n, 1) * 100:.1f}%")
    if top5_overlaps:
        print(
            f"  top-5 jaccard mean={statistics.mean(top5_overlaps):.2f}  "
            f"median={statistics.median(top5_overlaps):.2f}  "
            f"min={min(top5_overlaps):.2f}"
        )
    if lat_a and lat_b:

        def _p95(xs: list[float]) -> float:
            xs = sorted(xs)
            return xs[int(len(xs) * 0.95)] if xs else 0.0

        print(
            f"  latency A: mean={statistics.mean(lat_a):.0f}ms  p95={_p95(lat_a):.0f}ms\n"
            f"  latency B: mean={statistics.mean(lat_b):.0f}ms  p95={_p95(lat_b):.0f}ms"
        )

    if args.keep_tmp:
        print(f"\n  dumps preserved: {out_a}  {out_b}")
    else:
        out_a.unlink(missing_ok=True)
        out_b.unlink(missing_ok=True)
        tmp.rmdir()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
