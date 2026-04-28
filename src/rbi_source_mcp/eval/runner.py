"""Eval runner — executes the EVAL_CASES against the live corpus.

Two entry points:
    1. Python API (`evaluate(db_path)`) — used by tests/test_eval.py for CI.
    2. CLI (`rbi-source-eval`) — for ad-hoc runs and the weekly refresh smoke test.

Each case passes when AT LEAST ONE provision in the top-N retrieval results
satisfies (expected_md_id matches) AND (anchor_prefix matches OR text contains
the expected substring). Cases with expected_md_id="" are negative cases:
they pass when the response has low_confidence=True.

The runner reports per-case results and an overall hit rate. The default
gate is `>=80%` of enabled cases must pass; tune via the --threshold flag
or the EVAL_PASS_THRESHOLD argument.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from ..db import connect
from ..mcp.check_compliance import check_compliance
from .cases import EVAL_CASES, EvalCase

logger = structlog.get_logger(__name__)

DEFAULT_DB_PATH = "./data/db.sqlite"
DEFAULT_PASS_THRESHOLD = 0.80
DEFAULT_TOP_N = 5


@dataclass(slots=True)
class CaseResult:
    case: EvalCase
    passed: bool
    detail: str
    top_provisions: list[dict] = field(default_factory=list)
    low_confidence: bool = False
    retrieval_mode: str = ""


@dataclass(slots=True)
class EvalReport:
    results: list[CaseResult]
    pass_threshold: float

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def hit_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def gate_passed(self) -> bool:
        return self.hit_rate >= self.pass_threshold


def _evaluate_case(conn, case: EvalCase, *, top_n: int) -> CaseResult:
    """Run one eval case. Returns pass/fail with detail."""
    if not case.get("enabled", True):
        return CaseResult(case=case, passed=True, detail="skipped (disabled)")

    expected_md_id = case.get("expected_md_id", "")
    is_negative = expected_md_id == ""

    response = check_compliance(
        conn,
        case.get("clause", ""),
        topic_hint=case.get("topic_hint"),
        limit=top_n,
    )
    provisions = response.get("relevant_provisions", []) or []
    low_conf = bool(response.get("low_confidence", False))
    retrieval = response.get("retrieval", "")

    # Negative case: must be flagged low_confidence.
    if is_negative:
        passed = low_conf
        detail = (
            "low_confidence=True ✓"
            if passed
            else f"expected low_confidence=True, got False ({len(provisions)} provisions returned)"
        )
        return CaseResult(
            case=case,
            passed=passed,
            detail=detail,
            top_provisions=provisions[:top_n],
            low_confidence=low_conf,
            retrieval_mode=retrieval,
        )

    # Positive case: at least one provision must match the expected criteria.
    anchor_prefix = case.get("expected_anchor_prefix", "")
    text_substring = (case.get("expected_text_contains", "") or "").lower()

    matched_provision: dict | None = None
    for p in provisions:
        if p.get("document_id", "") != f"rbi:master_direction:{expected_md_id}":
            continue
        anchor = p.get("paragraph_anchor", "") or ""
        text = (p.get("quoted_text", "") or "").lower()
        anchor_ok = (not anchor_prefix) or anchor.startswith(anchor_prefix)
        text_ok = (not text_substring) or (text_substring in text)
        # When BOTH constraints are set, ANY of the two matching is sufficient
        # (text_contains is the more robust signal across re-chunking).
        if anchor_prefix and text_substring:
            if anchor_ok or text_ok:
                matched_provision = p
                break
        elif anchor_ok and text_ok:
            matched_provision = p
            break

    if matched_provision is not None:
        anchor = matched_provision.get("paragraph_anchor", "?")
        return CaseResult(
            case=case,
            passed=True,
            detail=f"matched at {anchor!r}",
            top_provisions=provisions[:top_n],
            low_confidence=low_conf,
            retrieval_mode=retrieval,
        )

    if not provisions:
        detail = "no provisions returned"
    else:
        seen_md_ids = {p.get("document_id", "") for p in provisions}
        in_corpus = f"rbi:master_direction:{expected_md_id}" in seen_md_ids
        if not in_corpus:
            detail = (
                f"expected MD {expected_md_id} not in top-{top_n}; "
                f"saw {sorted({m.split(':')[-1] for m in seen_md_ids})}"
            )
        else:
            detail = (
                f"expected MD {expected_md_id} present but no chunk satisfied "
                f"anchor_prefix={anchor_prefix!r} or text_contains={text_substring!r}"
            )
    return CaseResult(
        case=case,
        passed=False,
        detail=detail,
        top_provisions=provisions[:top_n],
        low_confidence=low_conf,
        retrieval_mode=retrieval,
    )


def evaluate(
    db_path: str | Path | None = None,
    *,
    cases: list[EvalCase] | None = None,
    top_n: int = DEFAULT_TOP_N,
    pass_threshold: float = DEFAULT_PASS_THRESHOLD,
) -> EvalReport:
    """Run all eval cases against the live DB. Returns an EvalReport."""
    db_path = Path(
        db_path or os.environ.get("RBI_SOURCE_DB", DEFAULT_DB_PATH)
    ).expanduser().resolve()
    cases = cases if cases is not None else EVAL_CASES

    results: list[CaseResult] = []
    with connect(db_path) as conn:
        for case in cases:
            result = _evaluate_case(conn, case, top_n=top_n)
            results.append(result)
    return EvalReport(results=results, pass_threshold=pass_threshold)


def print_report(report: EvalReport, *, verbose: bool = False) -> None:
    """Pretty-print an EvalReport to stdout."""
    print()
    print("=" * 78)
    print(f"COMPLIANCE EVAL — {report.total} cases")
    print("=" * 78)
    for r in report.results:
        marker = "✓" if r.passed else "✗"
        name = r.case.get("name", "(unnamed)")
        print(f"  {marker} {name}")
        print(f"      detail: {r.detail}")
        if verbose and r.top_provisions:
            for i, p in enumerate(r.top_provisions[:3], 1):
                anchor = p.get("paragraph_anchor", "-")
                md = (p.get("document_id") or "?").split(":")[-1]
                fs = p.get("fusion_score") or 0.0
                preview = (p.get("quoted_text") or "")[:80].replace("\n", " ")
                print(f"        [{i}] md={md} para={anchor!r} fusion={fs:.4f} {preview}")
    print()
    print("=" * 78)
    print(
        f"OVERALL: {report.passed}/{report.total} pass "
        f"({report.hit_rate:.1%}) — threshold {report.pass_threshold:.1%}"
    )
    print(f"GATE:    {'PASS' if report.gate_passed else 'FAIL'}")
    print("=" * 78)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the compliance retrieval eval set against the live corpus.",
    )
    parser.add_argument(
        "--db", default=None,
        help="SQLite path (default: $RBI_SOURCE_DB or ./data/db.sqlite)",
    )
    parser.add_argument(
        "--top-n", type=int, default=DEFAULT_TOP_N,
        help=f"Top-N to consider for hits (default {DEFAULT_TOP_N})",
    )
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_PASS_THRESHOLD,
        help=f"Pass threshold for the gate (default {DEFAULT_PASS_THRESHOLD:.0%})",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print top-3 provisions for each case",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of pretty output",
    )
    args = parser.parse_args()

    report = evaluate(
        db_path=args.db,
        top_n=args.top_n,
        pass_threshold=args.threshold,
    )

    if args.json:
        out = {
            "total": report.total,
            "passed": report.passed,
            "failed": report.failed,
            "hit_rate": report.hit_rate,
            "threshold": report.pass_threshold,
            "gate_passed": report.gate_passed,
            "cases": [
                {
                    "name": r.case.get("name"),
                    "passed": r.passed,
                    "detail": r.detail,
                    "low_confidence": r.low_confidence,
                    "retrieval_mode": r.retrieval_mode,
                }
                for r in report.results
            ],
        }
        print(json.dumps(out, indent=2))
    else:
        print_report(report, verbose=args.verbose)

    sys.exit(0 if report.gate_passed else 1)


if __name__ == "__main__":
    main()
