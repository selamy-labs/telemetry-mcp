"""Read-only telemetry/metrics core.

This module holds the metrics-query logic exactly once. The MCP server in
:mod:`telemetry_mcp.mcp_server` is a thin wrapper that serialises these
structured results to JSON; nothing here imports the MCP SDK.

The capability exposed is narrow on purpose: *read* metrics from a configurable
backend. There is no write, no DDL, and no free-form execution path -- a caller
chooses a source/metric and a bounded query, never raw SQL or a shell.

Schema-agnostic by design
-------------------------
This package does **not** hard-code any dataset, table, or metric. The shape of
the available telemetry is supplied entirely by the injected
:class:`MetricsBackend`, so the same server works against whatever dataset infra
wires up (see :mod:`telemetry_mcp.backend`). The core only validates, bounds,
and structures requests; it never assumes a particular schema.

Security model
--------------
* **Read-only.** The tools are query/list/describe/summary. There is no mutation
  tool and no ``run_sql`` escape hatch; a caller cannot supply raw query text.
* **Bounded.** Every query is capped (``limit``) and time-ranged so a call
  cannot pull an unbounded result set.
* **No embedded credentials.** Nothing here stores a token or key. Credentials
  are resolved at *call time* from an injected :class:`CredentialProvider`
  (backed by WIF/GSM/env in production) and handed to the backend; they never
  live in this module, in source, or in returned payloads.
* **Validated handles.** Source / metric / filter keys are restricted to a
  conservative identifier shape so a rejected lookup can never smuggle injection
  or traversal into the backend.

All data access goes through the injected :class:`MetricsBackend`, and all
timing through the injected :class:`Clock`, so the full validate/route/shape path
is exercised offline in tests with a fake in-memory backend -- no GCP, no
network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from telemetry_mcp.backend import BackendError, Clock, MetricsBackend, SystemClock

# Supported aggregations for :meth:`MetricsService.summary`. Kept as a small,
# closed set so a caller cannot pass an arbitrary function name through to the
# backend.
AGG_COUNT = "count"
AGG_SUM = "sum"
AGG_AVG = "avg"
AGG_MIN = "min"
AGG_MAX = "max"
SUPPORTED_AGGS = frozenset({AGG_COUNT, AGG_SUM, AGG_AVG, AGG_MIN, AGG_MAX})

# Bounds on a query result set. The floor stops a zero/negative page; the ceiling
# stops an unbounded pull regardless of what a caller asks for.
MIN_LIMIT = 1
MAX_LIMIT = 10_000
DEFAULT_LIMIT = 100

# Source / metric / filter-key handles are restricted to a conservative shape so
# a rejected lookup can never smuggle injection or path traversal into the
# backend. (The backend's own allowlist is the real gate; this is defence in
# depth.) Dotted names are allowed so a backend may namespace e.g.
# ``ci.runs.duration_ms``.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")

# A time range is two ISO-8601 instants. We only check non-empty shape here; the
# backend interprets and enforces them against its own clock.
MAX_RANGE_LEN = 64


class MetricsError(Exception):
    """A metrics request failed for an expected, user-facing reason.

    The MCP layer maps this to a ``ToolError`` so clients get a clean message
    instead of a stack trace.
    """


def _validate_handle(value: str, name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise MetricsError(f"{name} must not be empty")
    if not _HANDLE_RE.match(cleaned):
        raise MetricsError(
            f"invalid {name} {value!r}: must match {_HANDLE_RE.pattern} (letters, digits, dot, dash, underscore)"
        )
    return cleaned


def _validate_range(time_range: TimeRange) -> TimeRange:
    start = time_range.start.strip()
    end = time_range.end.strip()
    if not start or not end:
        raise MetricsError("range requires both start and end (ISO-8601 instants)")
    if len(start) > MAX_RANGE_LEN or len(end) > MAX_RANGE_LEN:
        raise MetricsError("range bounds are too long to be ISO-8601 instants")
    return TimeRange(start=start, end=end)


def _validate_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if limit < MIN_LIMIT:
        raise MetricsError(f"limit must be >= {MIN_LIMIT}")
    if limit > MAX_LIMIT:
        raise MetricsError(f"limit too large: {limit} > {MAX_LIMIT}")
    return limit


def _validate_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
    """Validate filter keys to the handle shape; values pass through as data.

    Keys are restricted so they cannot smuggle injection into the backend. The
    *values* are carried as structured data (the backend binds them as
    parameters, never string-interpolated), so they are left as-is.
    """
    if not filters:
        return {}
    if not isinstance(filters, dict):
        raise MetricsError("filters must be a mapping of column -> value")
    validated: dict[str, Any] = {}
    for key, value in filters.items():
        validated[_validate_handle(str(key), "filter key")] = value
    return validated


@dataclass(frozen=True)
class TimeRange:
    """A query window: two ISO-8601 instants (``start`` inclusive, ``end`` exclusive).

    Interpreted by the backend against its own clock; the core only checks that
    both bounds are present and plausibly shaped.
    """

    start: str
    end: str


@dataclass(frozen=True)
class SourceInfo:
    """One available telemetry source (dataset/table/metric family) and its schema.

    ``schema`` maps column name -> type string as the backend reports it. The
    core does not interpret the types; they are surfaced to the caller verbatim.
    """

    name: str
    description: str = ""
    schema: dict[str, str] = field(default_factory=dict)

    def to_public(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "schema": dict(self.schema)}


@dataclass(frozen=True)
class QueryResult:
    """A bounded, read-only query result: structured rows plus echoed query shape."""

    source: str
    rows: tuple[dict[str, Any], ...]
    range: TimeRange
    truncated: bool = False

    def to_public(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "row_count": len(self.rows),
            "rows": [dict(row) for row in self.rows],
            "range": {"start": self.range.start, "end": self.range.end},
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class SummaryResult:
    """A single aggregate of one metric over a range."""

    metric: str
    agg: str
    value: float | int | None
    range: TimeRange

    def to_public(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "agg": self.agg,
            "value": self.value,
            "range": {"start": self.range.start, "end": self.range.end},
        }


class CredentialProvider(Protocol):
    """Resolves backend credentials at call time; never stores them in the core.

    Production implementations resolve from Workload Identity Federation / GSM /
    the process environment when a query runs. The returned object is opaque to
    the core -- it is handed straight to the backend and never logged, returned,
    or persisted.
    """

    def resolve(self) -> Any: ...


class EnvCredentialProvider:
    """Default provider: defers entirely to the backend's own ambient auth.

    Returns ``None`` so the backend uses its environment-resolved identity (e.g.
    Application Default Credentials / WIF). It deliberately reads and stores no
    secret value itself -- there is nothing here to leak.
    """

    def resolve(self) -> Any:
        return None


class MetricsService:
    """Serves read-only metrics from an injected backend.

    Every method validates and bounds its inputs, resolves credentials at call
    time via the injected :class:`CredentialProvider`, routes the request to the
    injected :class:`MetricsBackend`, and returns a structured, credential-free
    result. The service holds no schema knowledge of its own.
    """

    def __init__(
        self,
        backend: MetricsBackend,
        *,
        credentials: CredentialProvider | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._backend = backend
        self._credentials = credentials or EnvCredentialProvider()
        self._clock = clock or SystemClock()

    def list_sources(self) -> dict[str, Any]:
        """List the telemetry sources the backend exposes (no schema bodies)."""
        sources = self._call(lambda creds: self._backend.list_sources(credentials=creds))
        infos = [info.to_public() for info in sources]
        infos.sort(key=lambda item: item["name"])
        return {"count": len(infos), "sources": infos}

    def describe(self, source: str) -> dict[str, Any]:
        """Describe one source: its description and column -> type schema."""
        name = _validate_handle(source, "source")
        info = self._call(lambda creds: self._backend.describe(name, credentials=creds))
        return info.to_public()

    def query(
        self,
        source: str,
        time_range: TimeRange,
        *,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Run a bounded, read-only query against ``source`` over ``time_range``.

        ``filters`` is an optional column -> value mapping (keys validated, values
        bound as parameters by the backend). ``limit`` is capped at
        :data:`MAX_LIMIT`. Returns structured rows.
        """
        name = _validate_handle(source, "source")
        window = _validate_range(time_range)
        bounded = _validate_limit(limit)
        clean_filters = _validate_filters(filters)
        result = self._call(
            lambda creds: self._backend.query(
                name,
                window,
                filters=clean_filters,
                limit=bounded,
                credentials=creds,
            )
        )
        return result.to_public()

    def summary(self, metric: str, time_range: TimeRange, agg: str) -> dict[str, Any]:
        """Return a single aggregate (``agg``) of ``metric`` over ``time_range``."""
        name = _validate_handle(metric, "metric")
        window = _validate_range(time_range)
        operation = agg.strip().lower()
        if operation not in SUPPORTED_AGGS:
            raise MetricsError(f"unsupported agg {agg!r}: choose one of {sorted(SUPPORTED_AGGS)}")
        result = self._call(lambda creds: self._backend.summary(name, window, operation, credentials=creds))
        return result.to_public()

    # -- internals -------------------------------------------------------------

    def _call(self, backend_call: Any) -> Any:
        """Resolve credentials at call time and route to the backend.

        Backend failures (auth, missing source, query error) surface as
        :class:`MetricsError` so the MCP layer maps them to a clean ``ToolError``.
        Credentials are resolved per call and never retained on the service.
        """
        credentials = self._credentials.resolve()
        try:
            return backend_call(credentials)
        except BackendError as error:
            raise MetricsError(str(error)) from error
