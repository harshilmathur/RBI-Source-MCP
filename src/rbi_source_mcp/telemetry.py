"""Optional PostHog telemetry for hosted instances.

Off by default. Activates only when `POSTHOG_API_KEY` is set in the env —
the maintainer's hosted instance sets this; self-host OSS installs never
phone home unless the operator opts in.

What we capture (hosted only):
    - tool name (rbi_search / rbi_check_compliance / rbi_get_document / rbi_check_current)
    - latency_ms
    - status (ok / error / input_too_large / db_unavailable / unknown_tool)
    - error_type (exception class name, when status=error)
    - shape-only properties: limit, has_filters, include_text, topic_hint enum value,
      text_length_bucket, query_length_bucket
    - server version, instance id (MCP_INSTANCE_ID or FLY_MACHINE_ID, when present)

What we do NOT capture:
    - query text, clause text, document IDs, response bodies, URLs from rbi_check_current,
    - IP, user-agent, or any client identifier beyond the server's own instance id.

The module is structured so that when `POSTHOG_API_KEY` is unset:
    - posthog is never imported (it's not in pyproject.toml deps)
    - capture_tool_call() is a no-op returning immediately
    - zero allocations, zero network, zero log noise

Distinct ID strategy:
    Anonymous events. We use the server instance id (MCP_INSTANCE_ID, falling back
    to FLY_MACHINE_ID for the Fly-hosted maintainer instance, or a per-process UUID
    when neither is set) as the distinct_id and pass `$process_person_profile: false`
    so PostHog does NOT build user profiles. The dashboard treats events as
    anonymous server activity. We are counting tool calls and tracking
    error/latency, not users.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Sentinels: resolved on first use, then cached.
_initialized = False
_client: Any = None  # posthog.Posthog instance, or None
_distinct_id: str = ""


def _length_bucket(n: int, *, small: bool = False) -> str:
    """Coarse length bucket — keeps cardinality low in PostHog properties.

    `small=True` is for query-style fields (search query). The default is for
    clause-style fields (check_compliance text).
    """
    if small:
        if n < 50:
            return "0-50"
        if n < 200:
            return "50-200"
        if n < 500:
            return "200-500"
        return "500+"
    if n < 500:
        return "0-500"
    if n < 2000:
        return "500-2000"
    if n < 10_000:
        return "2000-10000"
    return "10000+"


def text_length_bucket(n: int) -> str:
    return _length_bucket(n, small=False)


def query_length_bucket(n: int) -> str:
    return _length_bucket(n, small=True)


def _init_once() -> None:
    """Lazy init. Idempotent. Cheap when disabled."""
    global _initialized, _client, _distinct_id
    if _initialized:
        return
    _initialized = True

    api_key = os.environ.get("POSTHOG_API_KEY", "").strip()
    if not api_key:
        # Disabled mode — leave _client = None, _distinct_id = "".
        return

    try:
        # Imported lazily so OSS installs without posthog don't fail.
        # The `--extra hosted` dev path has posthog and types resolve;
        # main installs don't, so we need the import to be guarded.
        from posthog import Posthog  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        logger.warning(
            "telemetry_posthog_not_installed",
            hint="POSTHOG_API_KEY is set but posthog package not installed; telemetry disabled",
        )
        return

    host = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com").strip()
    try:
        _client = Posthog(
            project_api_key=api_key,
            host=host,
            # Don't auto-send geoip data — we only want server-side counts.
            disable_geoip=True,
            # Background queue + flush thread; capture() is non-blocking.
            sync_mode=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("telemetry_init_failed", error=str(exc))
        _client = None
        return

    # Stable per-instance ID. We prefer MCP_INSTANCE_ID (vendor-neutral) so
    # operators on any platform can group events from the same VM/machine.
    # FLY_MACHINE_ID is kept as a transparent fallback so existing Fly-hosted
    # deployments don't have to change anything to keep the same distinct_id
    # they had before v0.7.0. Falls back to a per-process random UUID with a
    # `local-` prefix when neither is set (typical for laptop self-hosts).
    _distinct_id = (
        os.environ.get("MCP_INSTANCE_ID")
        or os.environ.get("FLY_MACHINE_ID")
        or f"local-{uuid.uuid4().hex[:12]}"
    )
    logger.info("telemetry_enabled", host=host, distinct_id=_distinct_id)


def is_enabled() -> bool:
    _init_once()
    return _client is not None


def capture_tool_call(
    *,
    tool_name: str,
    status: str,
    latency_ms: int,
    properties: dict[str, Any] | None = None,
) -> None:
    """Capture a single MCP tool invocation. No-op when telemetry disabled."""
    _init_once()
    if _client is None:
        return

    from . import __version__

    props: dict[str, Any] = {
        "tool_name": tool_name,
        "status": status,
        "latency_ms": latency_ms,
        "version": __version__,
        # Tell PostHog not to build user profiles — these are anonymous server events.
        "$process_person_profile": False,
    }
    # Region: prefer MCP_REGION (vendor-neutral); fall back to FLY_REGION for
    # back-compat with the Fly-hosted instance. Either populates the same
    # `region` property in PostHog so dashboards don't need to know the source.
    region = os.environ.get("MCP_REGION") or os.environ.get("FLY_REGION")
    if region:
        props["region"] = region
    if properties:
        props.update(properties)

    try:
        _client.capture(
            distinct_id=_distinct_id,
            event="mcp_tool_called",
            properties=props,
        )
    except Exception as exc:  # noqa: BLE001
        # Telemetry must never break the server.
        logger.debug("telemetry_capture_failed", error=str(exc))


def flush() -> None:
    """Flush the queue. Call before process exit."""
    _init_once()
    if _client is None:
        return
    try:
        _client.flush()
    except Exception as exc:  # noqa: BLE001
        logger.debug("telemetry_flush_failed", error=str(exc))


def shutdown() -> None:
    """Flush + close. Call from server lifespan teardown."""
    _init_once()
    if _client is None:
        return
    try:
        _client.shutdown()
    except Exception as exc:  # noqa: BLE001
        logger.debug("telemetry_shutdown_failed", error=str(exc))
