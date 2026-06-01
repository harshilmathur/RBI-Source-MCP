"""Tests for the optional PostHog telemetry shim.

Pin three contracts:

1. **Disabled-mode invariant.** When `POSTHOG_API_KEY` is unset,
   `posthog` is never imported, no client is constructed, and every
   capture path is a no-op. This is the "self-host OSS never phones
   home" promise; a regression would silently activate telemetry on
   every install.

2. **Bucket boundaries.** `text_length_bucket` and `query_length_bucket`
   feed PostHog property cardinality; an off-by-one would silently
   mis-bucket dashboards. Pin the documented boundary values.

3. **Instance-ID precedence.** `MCP_INSTANCE_ID` > `FLY_MACHINE_ID` >
   per-process UUID. The first two are the back-compat contract; the
   last is the local-self-host default.
"""

from __future__ import annotations

import importlib
import sys

import pytest

from rbi_source_mcp import telemetry


@pytest.fixture(autouse=True)
def _reset_telemetry_state(monkeypatch):
    """Clear cached module state between tests so each starts disabled."""
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_client", None)
    monkeypatch.setattr(telemetry, "_distinct_id", "")
    yield


def test_disabled_mode_does_not_import_posthog(monkeypatch) -> None:
    """When POSTHOG_API_KEY is unset, the posthog package must not appear
    in sys.modules after telemetry initializes."""
    monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
    sys.modules.pop("posthog", None)
    sys.modules.pop("rbi_source_mcp.telemetry", None)
    mod = importlib.import_module("rbi_source_mcp.telemetry")
    # Trigger lazy init explicitly.
    assert mod.is_enabled() is False
    assert "posthog" not in sys.modules, (
        "posthog must NOT be imported when POSTHOG_API_KEY is unset — "
        "this is the load-bearing 'self-host never phones home' contract"
    )


def test_capture_is_no_op_when_disabled(monkeypatch) -> None:
    """capture_tool_call returns immediately when disabled, with no errors."""
    monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
    # Calling with arbitrary args must NOT raise even if posthog is missing.
    telemetry.capture_tool_call(
        tool_name="rbi_search",
        status="ok",
        latency_ms=12,
        properties={"limit": 5},
    )
    telemetry.flush()
    telemetry.shutdown()


@pytest.mark.parametrize(
    "n,expected",
    [
        # Clause-style boundaries: 500, 2000, 10000
        (0, "0-500"),
        (499, "0-500"),
        (500, "500-2000"),
        (1999, "500-2000"),
        (2000, "2000-10000"),
        (9999, "2000-10000"),
        (10_000, "10000+"),
        (1_000_000, "10000+"),
    ],
)
def test_text_length_bucket_boundaries(n: int, expected: str) -> None:
    """Pin the exact boundary values surfaced as PostHog properties."""
    assert telemetry.text_length_bucket(n) == expected


@pytest.mark.parametrize(
    "n,expected",
    [
        # Query-style boundaries: 50, 200, 500
        (0, "0-50"),
        (49, "0-50"),
        (50, "50-200"),
        (199, "50-200"),
        (200, "200-500"),
        (499, "200-500"),
        (500, "500+"),
        (10_000, "500+"),
    ],
)
def test_query_length_bucket_boundaries(n: int, expected: str) -> None:
    assert telemetry.query_length_bucket(n) == expected


def test_instance_id_precedence_mcp_over_fly(monkeypatch) -> None:
    """When both MCP_INSTANCE_ID and FLY_MACHINE_ID are set, MCP wins.

    Pinning the documented precedence: vendor-neutral env first, Fly
    fallback for back-compat. A future refactor swapping the order
    would silently relabel events in the maintainer's dashboards.
    """
    monkeypatch.setenv("MCP_INSTANCE_ID", "vendor-neutral-id")
    monkeypatch.setenv("FLY_MACHINE_ID", "fly-id")
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_fake_key_for_test")

    # Stub Posthog to avoid real network setup; we only care about distinct_id.
    class _FakePosthog:
        def __init__(self, **_kwargs):
            pass

    fake_module = type(sys)("posthog")
    fake_module.Posthog = _FakePosthog  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "posthog", fake_module)

    telemetry._init_once()
    assert telemetry._distinct_id == "vendor-neutral-id"


def test_instance_id_falls_back_to_fly_then_uuid(monkeypatch) -> None:
    """No MCP_INSTANCE_ID but FLY_MACHINE_ID set → use Fly. Neither set →
    synthesized local-* UUID."""
    monkeypatch.delenv("MCP_INSTANCE_ID", raising=False)
    monkeypatch.setenv("FLY_MACHINE_ID", "fly-id-xyz")
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_fake_key_for_test")

    class _FakePosthog:
        def __init__(self, **_kwargs):
            pass

    fake_module = type(sys)("posthog")
    fake_module.Posthog = _FakePosthog  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "posthog", fake_module)

    telemetry._init_once()
    assert telemetry._distinct_id == "fly-id-xyz"

    # Now clear both and re-init: must synthesize a `local-*` UUID.
    monkeypatch.delenv("FLY_MACHINE_ID", raising=False)
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_client", None)
    monkeypatch.setattr(telemetry, "_distinct_id", "")
    telemetry._init_once()
    assert telemetry._distinct_id.startswith("local-")


def test_capture_swallows_client_exceptions(monkeypatch) -> None:
    """Telemetry must never break the server — capture exceptions are
    swallowed and logged at debug level only."""
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_fake_key_for_test")

    class _ExplodingClient:
        def capture(self, **_kwargs):
            raise RuntimeError("simulated posthog outage")

        def flush(self):
            raise RuntimeError("simulated flush failure")

    # Force-init the module state directly.
    monkeypatch.setattr(telemetry, "_initialized", True)
    monkeypatch.setattr(telemetry, "_client", _ExplodingClient())
    monkeypatch.setattr(telemetry, "_distinct_id", "test-id")

    # Must not raise.
    telemetry.capture_tool_call(
        tool_name="rbi_search",
        status="ok",
        latency_ms=42,
    )
    telemetry.flush()
