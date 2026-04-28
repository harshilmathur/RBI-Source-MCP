"""Pytest integration for the compliance retrieval eval set.

Runs only when a populated corpus DB is available. In CI without a corpus
(default `pytest` run), the test is skipped — the eval is meant to gate the
weekly refresh, not every push. To run locally:

    rbi-source-eval                           # CLI
    pytest tests/test_eval.py -v              # via pytest
    RBI_EVAL_RUN=1 pytest tests/test_eval.py  # force-run even with no DB

A test "fails" when overall hit-rate drops below 80%, or when any of the
three locked regression cases fails individually.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rbi_source_mcp.eval.runner import (
    DEFAULT_PASS_THRESHOLD,
    DEFAULT_TOP_N,
    evaluate,
)

# Three locked regression cases that MUST pass — every refresh, no exceptions.
# Failure of any of these is a P0 corpus-quality regression.
LOCKED_REGRESSION_CASES = {
    "PA: minimum net-worth requirement",
    "Bank KYC: periodic updation cadence",
    "PPI: loading limit (THE headline regression — was buried under audit text in FTS5-only)",
}


def _has_corpus(db_path: Path) -> bool:
    """True if the DB exists and has at least the 7 v0.5 MDs indexed."""
    if not db_path.exists():
        return False
    import sqlite3

    try:
        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute(
                "SELECT count(DISTINCT md_id) FROM chunks"
            ).fetchone()[0]
            return count >= 7
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _db_path() -> Path:
    return Path(
        os.environ.get("RBI_SOURCE_DB", "./data/db.sqlite")
    ).expanduser().resolve()


@pytest.fixture(scope="module")
def report():
    db = _db_path()
    if not _has_corpus(db) and not os.environ.get("RBI_EVAL_RUN"):
        pytest.skip(
            f"corpus not populated at {db}; "
            "run `rbi-source-index-all` or set RBI_EVAL_RUN=1 to force"
        )
    return evaluate(db, top_n=DEFAULT_TOP_N, pass_threshold=DEFAULT_PASS_THRESHOLD)


def test_eval_overall_hit_rate_meets_threshold(report) -> None:
    """The overall eval gate: at least 80% of cases must pass."""
    assert report.gate_passed, (
        f"Eval gate failed: {report.passed}/{report.total} ({report.hit_rate:.1%}) "
        f"below threshold {report.pass_threshold:.1%}.\n"
        f"Failed cases:\n"
        + "\n".join(
            f"  ✗ {r.case.get('name')}: {r.detail}"
            for r in report.results
            if not r.passed
        )
    )


def test_eval_locked_regressions_pass(report) -> None:
    """Three named regression cases must individually pass (no aggregate hiding)."""
    failures: list[str] = []
    for r in report.results:
        name = r.case.get("name", "")
        if name in LOCKED_REGRESSION_CASES and not r.passed:
            failures.append(f"  ✗ {name}: {r.detail}")
    assert not failures, (
        "Locked regression cases failed:\n" + "\n".join(failures)
    )


def test_eval_negative_cases_flag_low_confidence(report) -> None:
    """Out-of-scope inputs (recipe, empty) must trip low_confidence."""
    failures: list[str] = []
    for r in report.results:
        case = r.case
        if case.get("expected_md_id", "") != "":
            continue  # not a negative case
        if not r.passed:
            failures.append(f"  ✗ {case.get('name')}: {r.detail}")
    assert not failures, (
        "Negative (low_confidence) cases failed:\n" + "\n".join(failures)
    )
