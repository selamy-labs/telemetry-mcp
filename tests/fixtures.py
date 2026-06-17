"""Canned telemetry the fake backend serves offline.

A minimal, hand-built dataset shaped like generic telemetry (sources with a
column -> type schema, some rows, and a couple of scalar metrics). It is not
copied from any live dataset and contains no credentials. The point is to prove
the server is schema-agnostic: the core knows none of these names ahead of time.
"""

from __future__ import annotations

from typing import Any

from telemetry_mcp.core import SourceInfo
from tests.conftest import FakeBackend


def sources() -> dict[str, SourceInfo]:
    """Two sources with distinct schemas, to prove no schema is hard-coded."""
    return {
        "ci.runs": SourceInfo(
            name="ci.runs",
            description="CI runner job executions.",
            schema={"started_at": "TIMESTAMP", "repo": "STRING", "duration_ms": "INT64", "status": "STRING"},
        ),
        "api.requests": SourceInfo(
            name="api.requests",
            description="Inbound API request log.",
            schema={"ts": "TIMESTAMP", "route": "STRING", "latency_ms": "INT64"},
        ),
    }


def rows() -> dict[str, list[dict[str, Any]]]:
    """Rows for each source; ``ci.runs`` has both a passing and failing repo so
    filter handling is observable."""
    return {
        "ci.runs": [
            {"started_at": "2026-06-16T10:00:00Z", "repo": "nash", "duration_ms": 1200, "status": "success"},
            {"started_at": "2026-06-16T11:00:00Z", "repo": "reid", "duration_ms": 900, "status": "success"},
            {"started_at": "2026-06-16T12:00:00Z", "repo": "nash", "duration_ms": 1500, "status": "failure"},
        ],
        "api.requests": [
            {"ts": "2026-06-16T10:00:00Z", "route": "/v1/query", "latency_ms": 42},
            {"ts": "2026-06-16T10:00:01Z", "route": "/v1/query", "latency_ms": 51},
        ],
    }


def summaries() -> dict[str, float]:
    """Scalar metrics for the summary path."""
    return {"ci.runs.duration_ms": 1200.0, "api.requests.latency_ms": 46.5}


def backend(fail_with: str | None = None) -> FakeBackend:
    """The fake backend most tests query against."""
    return FakeBackend(sources=sources(), rows=rows(), summaries=summaries(), fail_with=fail_with)
