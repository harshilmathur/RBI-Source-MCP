"""Run the eval suite and dump per-case results as JSONL.

Whichever side of the A/B you want to capture, set the appropriate env
vars before invoking:

    # A — baseline (local bge-small@384):
    RBI_SOURCE_DB=./data/db.sqlite \
        uv run python scripts/eval_dump.py --out /tmp/eval-A.jsonl

    # B — new (cloudflare bge-base@768):
    RBI_EMBEDDING_PROVIDER=cloudflare \
    RBI_EMBEDDING_MODEL=@cf/baai/bge-base-en-v1.5 \
    RBI_EMBEDDING_DIM=768 \
    RBI_SOURCE_DB=./data/db-bge-base.sqlite \
        uv run python scripts/eval_dump.py --out /tmp/eval-B.jsonl

Each line of the output is a JSON object:
    {
      "case_name": str,
      "clause": str,
      "expected_md_id": str,
      "passed": bool,
      "low_confidence": bool,
      "retrieval_mode": str,
      "detail": str,
      "latency_ms": float,
      "top_provisions": [{document_id, paragraph_anchor, quoted_text, ...}, ...]
    }

The top_provisions list preserves order — first item is the system's #1
result. compare_ab.py uses this for top-1 / top-N overlap diffing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Project imports happen after env is read implicitly via embedding_config.
from rbi_source_mcp import embedding_config as cfg  # noqa: E402
from rbi_source_mcp.db import connect  # noqa: E402
from rbi_source_mcp.eval.cases import EVAL_CASES  # noqa: E402
from rbi_source_mcp.mcp.check_compliance import check_compliance  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--top-n", type=int, default=5)
    args = ap.parse_args()

    db_path = Path(os.environ.get("RBI_SOURCE_DB", "./data/db.sqlite")).resolve()
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 2

    print(
        f"[eval_dump] db={db_path} provider={cfg.PROVIDER} "
        f"model={cfg.MODEL} dim={cfg.DIM}",
        file=sys.stderr,
        flush=True,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    n_pass = 0
    n_total = 0
    with connect(db_path) as conn, args.out.open("w") as fp:
        for case in EVAL_CASES:
            if not case.get("enabled", True):
                continue
            n_total += 1
            t0 = time.time()
            response = check_compliance(
                conn,
                case.get("clause", ""),
                topic_hint=case.get("topic_hint"),
                limit=args.top_n,
            )
            latency_ms = (time.time() - t0) * 1000.0
            provisions = response.get("relevant_provisions", []) or []

            # Determine pass exactly the same way the eval runner does, so
            # this dump file is also a faithful eval report.
            expected_md_id = case.get("expected_md_id", "")
            anchor_prefix = case.get("expected_anchor_prefix", "")
            text_substring = (case.get("expected_text_contains", "") or "").lower()
            low_conf = bool(response.get("low_confidence", False))
            passed = False
            detail = ""
            if expected_md_id == "":
                # Negative case: pass iff low_confidence flag is set.
                passed = low_conf
                detail = "negative" if passed else "expected low_confidence=True"
            else:
                for p in provisions:
                    if p.get("document_id", "") != f"rbi:master_direction:{expected_md_id}":
                        continue
                    a = p.get("paragraph_anchor", "") or ""
                    t = (p.get("quoted_text", "") or "").lower()
                    a_ok = (not anchor_prefix) or a.startswith(anchor_prefix)
                    t_ok = (not text_substring) or (text_substring in t)
                    if anchor_prefix and text_substring:
                        if a_ok or t_ok:
                            passed = True
                            detail = f"matched at {a!r}"
                            break
                    elif a_ok and t_ok:
                        passed = True
                        detail = f"matched at {a!r}"
                        break

            n_pass += int(passed)

            slim_provisions = [
                {
                    "document_id": p.get("document_id"),
                    "paragraph_anchor": p.get("paragraph_anchor"),
                    "quoted_text": (p.get("quoted_text") or "")[:200],
                    "fusion_score": p.get("fusion_score"),
                    "sparse_rank": p.get("sparse_rank"),
                    "dense_rank": p.get("dense_rank"),
                }
                for p in provisions[: args.top_n]
            ]

            fp.write(
                json.dumps(
                    {
                        "case_name": case.get("name", "(unnamed)"),
                        "clause": case.get("clause", ""),
                        "expected_md_id": expected_md_id,
                        "passed": passed,
                        "low_confidence": low_conf,
                        "retrieval_mode": response.get("retrieval", ""),
                        "detail": detail,
                        "latency_ms": round(latency_ms, 1),
                        "top_provisions": slim_provisions,
                    }
                )
                + "\n"
            )

    print(
        f"[eval_dump] {n_pass}/{n_total} passed ({n_pass / max(n_total, 1) * 100:.1f}%) → {args.out}",
        file=sys.stderr,
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
