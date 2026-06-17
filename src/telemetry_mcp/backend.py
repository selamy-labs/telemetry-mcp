"""Metrics backend abstraction so the core stays schema-agnostic and offline-testable.

The metrics core never talks to a database directly. It depends only on the
:class:`MetricsBackend` protocol below, which serves read-only telemetry and
returns structured results. Tests inject a fake in-memory backend, so the full
validate/route/shape path is exercised offline with no GCP and no network.

Production injects :class:`BigQueryBackend`, a thin adapter over Google
BigQuery. **It is intentionally not live-wired in this repo** -- it is the
documented integration point that infra completes (dataset, project, and a
keyless WIF service account). See the README "What infra must wire" section.

Two properties live here and nowhere else:

* **Read-only.** The backend issues only read queries. There is no DDL/DML path
  and no method that accepts caller-supplied raw query text -- requests are
  built from a validated source/metric, range, and parameter-bound filters.
* **No embedded credentials.** The backend carries no token. Credentials are
  passed in *per call* (resolved by the core's :class:`CredentialProvider` from
  WIF/GSM/env) or left ``None`` to use ambient identity; nothing is stored.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from telemetry_mcp.core import QueryResult, SourceInfo, SummaryResult, TimeRange


class BackendError(Exception):
    """A backend failed to serve a request for an expected reason.

    Covers missing/unknown source, auth failure, and query failure. The core
    maps it to :class:`telemetry_mcp.core.MetricsError`.
    """


class MetricsBackend(Protocol):
    """Serves read-only telemetry. Credentials are passed per call, never stored.

    Implementations must not expose any write/DDL path and must not accept raw
    query text -- requests are built from a validated source/metric, a range, and
    parameter-bound filters. A query/auth failure is raised as
    :class:`BackendError`.
    """

    def list_sources(self, *, credentials: Any) -> list[SourceInfo]: ...

    def describe(self, source: str, *, credentials: Any) -> SourceInfo: ...

    def query(
        self,
        source: str,
        time_range: TimeRange,
        *,
        filters: dict[str, Any],
        limit: int,
        credentials: Any,
    ) -> QueryResult: ...

    def summary(
        self,
        metric: str,
        time_range: TimeRange,
        agg: str,
        *,
        credentials: Any,
    ) -> SummaryResult: ...


class BigQueryBackend:
    """Production backend: a thin read-only adapter over Google BigQuery.

    **Not live-wired here.** This class documents the integration point and
    fails fast until infra completes it (see the README). The intended shape:

    * Constructed with a ``project`` and a ``dataset``; the source/metric names
      map to tables/views in that dataset (no schema is hard-coded -- the dataset
      defines what exists).
    * ``credentials`` is resolved *at call time* by the core's
      :class:`telemetry_mcp.core.CredentialProvider` from Workload Identity
      Federation (keyless) and handed to the BigQuery client per request; no key
      is read or stored by this module.
    * Every query is read-only and parameterised: the time range and filter
      *values* are passed as BigQuery query parameters, never string-interpolated,
      and ``limit`` is applied as a ``LIMIT`` clause. There is no raw-SQL path.

    Wiring it up is the infra half of this build split; until then this backend
    raises so the offline/fake path is the only one exercised by tests.
    """

    def __init__(self, project: str | None = None, dataset: str | None = None) -> None:
        self._project = project
        self._dataset = dataset

    def _not_wired(self) -> BackendError:
        return BackendError(
            "BigQueryBackend is not wired up in this repo. Infra must supply the "
            "BigQuery project + dataset and a keyless WIF service account, and "
            "implement the read-only parameterised query path (see README)."
        )

    def list_sources(self, *, credentials: Any) -> list[SourceInfo]:
        raise self._not_wired()

    def describe(self, source: str, *, credentials: Any) -> SourceInfo:
        raise self._not_wired()

    def query(
        self,
        source: str,
        time_range: TimeRange,
        *,
        filters: dict[str, Any],
        limit: int,
        credentials: Any,
    ) -> QueryResult:
        raise self._not_wired()

    def summary(
        self,
        metric: str,
        time_range: TimeRange,
        agg: str,
        *,
        credentials: Any,
    ) -> SummaryResult:
        raise self._not_wired()


class Clock(Protocol):
    """A wall clock, injected so timestamps are testable."""

    def now_iso(self) -> str: ...

    def monotonic_ns(self) -> int: ...


class SystemClock:
    """The real clock: UTC ISO timestamps and a monotonic nanosecond counter."""

    def now_iso(self) -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()
