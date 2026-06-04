"""Tests for crawler._common.get_with_retry — the retry/backoff wrapper that
rides through RBI's intermittent WAF blocks (418/403), rate limits (429), and
5xx, which otherwise break scheduled CI corpus builds from datacenter IPs.

A scripted fake client returns a queued sequence of status codes / exceptions,
and `sleep` is injected so the tests never actually wait.
"""

from __future__ import annotations

import httpx
import pytest

from rbi_source_mcp.crawler._common import get_with_retry

URL = "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx"


class _FakeClient:
    """Duck-typed httpx.Client. Each `.get` pops the next scripted outcome:
    an int → an httpx.Response with that status; an Exception → raised."""

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append((url, kwargs))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(status_code=int(outcome), request=httpx.Request("GET", url))


def _recording_sleep() -> tuple[list[float], object]:
    delays: list[float] = []
    return delays, delays.append


def test_returns_immediately_on_200() -> None:
    client = _FakeClient([200])
    delays, sleep = _recording_sleep()
    resp = get_with_retry(client, URL, sleep=sleep)
    assert resp.status_code == 200
    assert len(client.calls) == 1
    assert delays == []  # no retry, no sleep


def test_retries_retryable_status_then_succeeds() -> None:
    # 418 (RBI WAF) then 200 — the exact shape that broke CI.
    client = _FakeClient([418, 200])
    delays, sleep = _recording_sleep()
    resp = get_with_retry(client, URL, sleep=sleep)
    assert resp.status_code == 200
    assert len(client.calls) == 2
    assert delays == [2.0]  # backoff_base * 2**0


def test_exhausts_on_persistent_retryable_then_raises() -> None:
    client = _FakeClient([418, 418, 418])
    delays, sleep = _recording_sleep()
    with pytest.raises(httpx.HTTPStatusError):
        get_with_retry(client, URL, attempts=3, sleep=sleep)
    assert len(client.calls) == 3
    # slept before attempts 2 and 3 (not after the final failure)
    assert delays == [2.0, 4.0]


def test_non_retryable_status_raises_immediately() -> None:
    client = _FakeClient([404])
    delays, sleep = _recording_sleep()
    with pytest.raises(httpx.HTTPStatusError):
        get_with_retry(client, URL, sleep=sleep)
    assert len(client.calls) == 1  # no retry on 404
    assert delays == []


def test_retries_transport_error_then_succeeds() -> None:
    client = _FakeClient([httpx.ConnectError("conn reset"), 200])
    delays, sleep = _recording_sleep()
    resp = get_with_retry(client, URL, sleep=sleep)
    assert resp.status_code == 200
    assert len(client.calls) == 2
    assert delays == [2.0]


def test_persistent_transport_error_raises() -> None:
    client = _FakeClient([httpx.ConnectError("x"), httpx.ConnectError("y"), httpx.ConnectError("z")])
    delays, sleep = _recording_sleep()
    with pytest.raises(httpx.TransportError):
        get_with_retry(client, URL, attempts=3, sleep=sleep)
    assert len(client.calls) == 3
    assert delays == [2.0, 4.0]


def test_backoff_schedule_is_exponential() -> None:
    client = _FakeClient([429, 429, 429, 429, 200])
    delays, sleep = _recording_sleep()
    resp = get_with_retry(client, URL, attempts=5, backoff_base=1.5, sleep=sleep)
    assert resp.status_code == 200
    # 1.5 * 2**0, **1, **2, **3
    assert delays == [1.5, 3.0, 6.0, 12.0]


def test_forwards_kwargs_to_get() -> None:
    client = _FakeClient([200])
    delays, sleep = _recording_sleep()
    get_with_retry(client, URL, sleep=sleep, headers={"X-Test": "1"})
    assert client.calls[0][1]["headers"] == {"X-Test": "1"}
