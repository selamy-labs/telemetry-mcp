"""Tests for the backend layer: the production BigQuery adapter is documented but
not live-wired, so it must fail fast on every method; the system clock is the one
real-IO surface and is smoke-checked.
"""

from __future__ import annotations

import pytest

from telemetry_mcp.backend import BackendError, BigQueryBackend, SystemClock
from telemetry_mcp.core import TimeRange

RANGE = TimeRange(start="2026-06-16T00:00:00Z", end="2026-06-17T00:00:00Z")


def test_bigquery_backend_is_not_wired() -> None:
    bq = BigQueryBackend(project="speedforge-prod-499002", dataset="telemetry")
    with pytest.raises(BackendError, match="not wired up"):
        bq.list_sources(credentials=None)
    with pytest.raises(BackendError, match="not wired up"):
        bq.describe("ci.runs", credentials=None)
    with pytest.raises(BackendError, match="not wired up"):
        bq.query("ci.runs", RANGE, filters={}, limit=10, credentials=None)
    with pytest.raises(BackendError, match="not wired up"):
        bq.summary("ci.runs.duration_ms", RANGE, "avg", credentials=None)


def test_bigquery_backend_constructs_without_config() -> None:
    # Constructing it must not require config or touch the network.
    assert isinstance(BigQueryBackend(), BigQueryBackend)


def test_system_clock_now_iso_is_utc() -> None:
    stamp = SystemClock().now_iso()
    assert stamp.endswith("Z")


def test_system_clock_monotonic_advances() -> None:
    clock = SystemClock()
    first = clock.monotonic_ns()
    second = clock.monotonic_ns()
    assert second >= first
