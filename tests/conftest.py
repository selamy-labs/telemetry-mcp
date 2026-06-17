"""Shared offline test doubles: a fake in-memory backend, clock, and credentials.

Nothing in the test suite touches GCP, BigQuery, or the network. The fake
backend serves canned sources/rows from memory, records every call it received
(including the credentials it was handed), and can be told to raise so the
error path is exercised. The fake clock yields deterministic timestamps. The
recording credential provider lets tests assert credentials are resolved per
call and never leak into returned payloads.
"""

from __future__ import annotations

from typing import Any

from telemetry_mcp.backend import BackendError
from telemetry_mcp.core import QueryResult, SourceInfo, SummaryResult, TimeRange


class FakeBackend:
    """An in-memory metrics backend driven by canned sources and rows.

    Serves whatever ``sources`` / ``rows`` it is built with; records each call
    so tests can assert routing, filter handling, limit application, and that
    credentials were passed through. Set ``fail_with`` to make every call raise
    a :class:`BackendError` (the auth/query-failure path).
    """

    def __init__(
        self,
        sources: dict[str, SourceInfo] | None = None,
        rows: dict[str, list[dict[str, Any]]] | None = None,
        summaries: dict[str, float] | None = None,
        fail_with: str | None = None,
    ) -> None:
        self._sources = sources or {}
        self._rows = rows or {}
        self._summaries = summaries or {}
        self._fail_with = fail_with
        self.calls: list[dict[str, Any]] = []

    def _record(self, **call: Any) -> None:
        self.calls.append(call)

    def _guard(self) -> None:
        if self._fail_with is not None:
            raise BackendError(self._fail_with)

    def list_sources(self, *, credentials: Any) -> list[SourceInfo]:
        self._record(op="list_sources", credentials=credentials)
        self._guard()
        return list(self._sources.values())

    def describe(self, source: str, *, credentials: Any) -> SourceInfo:
        self._record(op="describe", source=source, credentials=credentials)
        self._guard()
        info = self._sources.get(source)
        if info is None:
            raise BackendError(f"unknown source {source!r}")
        return info

    def query(
        self,
        source: str,
        time_range: TimeRange,
        *,
        filters: dict[str, Any],
        limit: int,
        credentials: Any,
    ) -> QueryResult:
        self._record(op="query", source=source, range=time_range, filters=filters, limit=limit, credentials=credentials)
        self._guard()
        rows = self._rows.get(source)
        if rows is None:
            raise BackendError(f"unknown source {source!r}")
        # Apply filters and the limit so tests can assert the backend honoured
        # both (the core bounds the limit; the backend enforces it).
        selected = [row for row in rows if all(row.get(key) == value for key, value in filters.items())]
        truncated = len(selected) > limit
        return QueryResult(source=source, rows=tuple(selected[:limit]), range=time_range, truncated=truncated)

    def summary(
        self,
        metric: str,
        time_range: TimeRange,
        agg: str,
        *,
        credentials: Any,
    ) -> SummaryResult:
        self._record(op="summary", metric=metric, range=time_range, agg=agg, credentials=credentials)
        self._guard()
        if metric not in self._summaries:
            raise BackendError(f"unknown metric {metric!r}")
        return SummaryResult(metric=metric, agg=agg, value=self._summaries[metric], range=time_range)


class FakeClock:
    """A deterministic clock: fixed-format ISO time and a counting monotonic."""

    def __init__(self) -> None:
        self._seq = 0

    def now_iso(self) -> str:
        self._seq += 1
        return f"2026-06-17T00:00:{self._seq:02d}Z"

    def monotonic_ns(self) -> int:
        self._seq += 1
        return self._seq


class RecordingCredentialProvider:
    """A credential provider that hands out a sentinel and counts resolutions.

    Lets tests assert credentials are resolved *per call* and that the sentinel
    is passed to the backend but never appears in a returned payload.
    """

    def __init__(self, sentinel: Any = "FAKE-CREDS") -> None:
        self.sentinel = sentinel
        self.resolved = 0

    def resolve(self) -> Any:
        self.resolved += 1
        return self.sentinel
