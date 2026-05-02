"""3-way A/B/C comparator for the embedding model bake-off.

Sides:
    A — bge-small-en-v1.5 @ 384  (local, current production)
    B — bge-base-en-v1.5  @ 768  (Cloudflare Workers AI)
    C — bge-m3            @ 1024 (Cloudflare Workers AI)

Spawns three eval_dump.py subprocesses, then renders:
    - per-case: pass/fail × {A,B,C}, top-1 chunk match per side, jaccard between A&B, A&C, B&C
    - aggregate: hit-rate per side, agreement matrix, latency mean/p95
    - cost note: CF Workers AI bills per neuron-second; bge-m3 is ~2x slower
      (empirically ~60 chunks/sec vs ~110 for bge-base, vs free-local for A).

Usage:
    uv run python scripts/compare_3way.py \
        --db-a ./data/db.sqlite \
        --db-b ./data/db-bge-base.sqlite \
        --db-c ./data/db-bge-m3.sqlite
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

SIDES = [
    ("A", "bge-small@384", {
        "RBI_EMBEDDING_PROVIDER": "local",
        "RBI_EMBEDDING_MODEL": "BAAI/bge-small-en-v1.5",
        "RBI_EMBEDDING_DIM": "384",
    }),
    ("B", "bge-base@768", {
        "RBI_EMBEDDING_PROVIDER": "cloudflare",
        "RBI_EMBEDDING_MODEL": "@cf/baai/bge-base-en-v1.5",
        "RBI_EMBEDDING_DIM": "768",
    }),
    ("C", "bge-m3@1024", {
        "RBI_EMBEDDING_PROVIDER": "cloudflare",
        "RBI_EMBEDDING_MODEL": "@cf/baai/bge-m3",
        "RBI_EMBEDDING_DIM": "1024",
    }),
]


def _run_side(label: str, db_path: Path, env_overrides: dict[str, str], out: Path) -> None:
    env = os.environ.copy()
    env.update(env_overrides)
    env["RBI_SOURCE_DB"] = str(db_path.resolve())
    print(f"[{label}] running eval_dump → {out}", flush=True)
    proc = subprocess.run(
        ["uv", "run", "python", "scripts/eval_dump.py", "--out", str(out)],
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f"[{label}] FAILED rc={proc.returncode}", file=sys.stderr)
        print(proc.stdout, file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
        sys.exit(proc.returncode)
    for line in proc.stderr.splitlines():
        print(f"  [{label}] {line}", flush=True)


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _idx(rows: list[dict]) -> dict[str, dict]:
    return {r["case_name"]: r for r in rows}


def _key(p: dict) -> str:
    return f"{p.get('document_id', '')}::{p.get('paragraph_anchor', '')}"


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / max(len(a | b), 1)


def _p95(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[int(len(xs) * 0.95)] if xs else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-a", required=True, type=Path)
    ap.add_argument("--db-b", required=True, type=Path)
    ap.add_argument("--db-c", required=True, type=Path)
    ap.add_argument("--keep-tmp", action="store_true")
    args = ap.parse_args()

    db_paths = {"A": args.db_a, "B": args.db_b, "C": args.db_c}
    for label, p in db_paths.items():
        if not p.exists():
            print(f"db-{label.lower()} missing: {p}", file=sys.stderr)
            return 2

    tmp = Path(tempfile.mkdtemp(prefix="rbi-3way-"))
    outs: dict[str, Path] = {}
    for label, name, env in SIDES:
        out = tmp / f"eval-{label}.jsonl"
        outs[label] = out
        _run_side(f"{label}: {name}", db_paths[label], env, out)

    rows = {label: _load(p) for label, p in outs.items()}
    by = {label: _idx(rs) for label, rs in rows.items()}
    names = sorted(set().union(*[set(d) for d in by.values()]))

    print()
    print("=" * 100)
    print("3-WAY COMPARISON   A=bge-small@384   B=bge-base@768   C=bge-m3@1024")
    print("=" * 100)

    pass_count = {"A": 0, "B": 0, "C": 0}
    top1_match = {"AB": 0, "AC": 0, "BC": 0}
    jaccards = {"AB": [], "AC": [], "BC": []}
    latencies = {"A": [], "B": [], "C": []}
    diff_cases = 0

    for name in names:
        a = by["A"].get(name)
        b = by["B"].get(name)
        c = by["C"].get(name)
        if not (a and b and c):
            print(f"\n  ! {name}: missing on one side")
            continue

        for label, row in (("A", a), ("B", b), ("C", c)):
            pass_count[label] += int(row["passed"])
            latencies[label].append(row["latency_ms"])

        ka = [_key(p) for p in a["top_provisions"]]
        kb = [_key(p) for p in b["top_provisions"]]
        kc = [_key(p) for p in c["top_provisions"]]
        sa, sb, sc = set(ka), set(kb), set(kc)

        if ka and kb and ka[0] == kb[0]:
            top1_match["AB"] += 1
        if ka and kc and ka[0] == kc[0]:
            top1_match["AC"] += 1
        if kb and kc and kb[0] == kc[0]:
            top1_match["BC"] += 1
        jaccards["AB"].append(_jaccard(sa, sb))
        jaccards["AC"].append(_jaccard(sa, sc))
        jaccards["BC"].append(_jaccard(sb, sc))

        outcomes = (a["passed"], b["passed"], c["passed"])
        if len(set(outcomes)) > 1:
            diff_cases += 1
            flag = "  ⚠ disagreement"
        else:
            flag = ""

        markers = "".join(("✓" if x else "✗") for x in outcomes)
        print(
            f"\n  {markers}  {name}{flag}\n"
            f"      top-1 A: {ka[0] if ka else '∅'}\n"
            f"      top-1 B: {kb[0] if kb else '∅'}\n"
            f"      top-1 C: {kc[0] if kc else '∅'}\n"
            f"      jaccard5 AB={jaccards['AB'][-1]:.2f} AC={jaccards['AC'][-1]:.2f} "
            f"BC={jaccards['BC'][-1]:.2f}    "
            f"latency A={a['latency_ms']:.0f}  B={b['latency_ms']:.0f}  C={c['latency_ms']:.0f}ms"
        )

    n = len(names)
    print()
    print("=" * 100)
    print("AGGREGATE")
    print("=" * 100)
    print(f"  cases evaluated: {n}    cases with disagreement: {diff_cases}")
    print(f"  hit rate    A={pass_count['A']}/{n}={pass_count['A'] / max(n, 1) * 100:.1f}%   "
          f"B={pass_count['B']}/{n}={pass_count['B'] / max(n, 1) * 100:.1f}%   "
          f"C={pass_count['C']}/{n}={pass_count['C'] / max(n, 1) * 100:.1f}%")
    print(f"  top-1 agreement   A↔B={top1_match['AB']}/{n}   "
          f"A↔C={top1_match['AC']}/{n}   B↔C={top1_match['BC']}/{n}")
    if jaccards["AB"]:
        for pair in ("AB", "AC", "BC"):
            xs = jaccards[pair]
            print(f"  jaccard5 {pair}: mean={statistics.mean(xs):.2f}  "
                  f"median={statistics.median(xs):.2f}  min={min(xs):.2f}")
    for label in ("A", "B", "C"):
        xs = latencies[label]
        if xs:
            print(f"  latency {label}: mean={statistics.mean(xs):.0f}ms  "
                  f"p95={_p95(xs):.0f}ms  min={min(xs):.0f}ms  max={max(xs):.0f}ms")

    print()
    print("  embedding throughput observed during corpus build:")
    print("    A (local bge-small)  — N/A here (corpus pre-built locally)")
    print("    B (CF bge-base)      — ~113 chunks/sec  (8.4 min for 56,653 chunks)")
    print("    C (CF bge-m3)        — ~63 chunks/sec   (~15 min for 56,653 chunks)")
    print()
    print("  CF Workers AI cost (free up to 10k neurons/day):")
    print("    Per inference, neurons scale with model size. bge-m3 is ~2.7x larger")
    print("    than bge-base; expect ~2-3x neuron usage. Both well within free tier")
    print("    for the 56k-chunk weekly refresh + tens-of-thousands of query embeds.")

    if args.keep_tmp:
        print(f"\n  dumps preserved: {tmp}")
    else:
        for p in outs.values():
            p.unlink(missing_ok=True)
        tmp.rmdir()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
