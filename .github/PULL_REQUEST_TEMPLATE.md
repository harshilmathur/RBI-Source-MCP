<!-- Thanks for the PR. Keep it small, focused, and clear about the why. -->

## What this changes

<!-- One short paragraph. The diff shows what; explain why. -->

## Test coverage

<!-- Tick whichever applies. Delete the rest. -->

- [ ] Added unit tests covering the change (`tests/test_*.py`)
- [ ] Pre-existing tests still pass: `uv run pytest`
- [ ] Lint clean: `uv run ruff check src/ tests/`
- [ ] Eval gate passes at ≥80%: `uv run rbi-source-eval`
- [ ] N/A — docs / config / non-code change

## Eval impact (only if you touched retrieval)

<!-- If you changed chunkers, fusion logic, topic hints, ranking thresholds,
embedder, or the corpus pipeline — paste before/after eval scores. -->

```
before: __ / 25 (__%)
after:  __ / 25 (__%)
```

## Anything reviewers should know

<!-- Migration steps, deploy concerns, follow-ups intentionally deferred, etc. -->
