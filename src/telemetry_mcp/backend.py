"""Metrics backend abstraction so the core stays schema-agnostic and offline-testable.

The metrics core never talks to a database directly. It depends only on the
:class:`MetricsBackend` protocol below, which serves read-only telemetry and
returns structured results. Tests inject a fake in-memory backend, so the full
validate/route/shape path is exercised offline with no GCP and no network.

Production injects :class:`BigQueryBackend`, a bounded adapter over Google
BigQuery. Its project, dataset, source catalog, scan ceiling, client, and
credentials are all supplied at runtime; tests inject a fake client and never
contact GCP.

Two properties live here and nowhere else:

* **Read-only.** The backend issues only read queries. There is no DDL/DML path
  and no method that accepts caller-supplied raw query text -- requests are
  built from a validated source/metric, range, and parameter-bound filters.
* **No embedded credentials.** The backend carries no token. Credentials are
  passed in *per call* (resolved by the core's :class:`CredentialProvider` from
  WIF/GSM/env) or left ``None`` to use ambient identity; nothing is stored.
"""

from __future__ import annotations

import base64
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from datetime import time as datetime_time
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

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


@dataclass(frozen=True)
class SourceCatalogEntry:
    """Allowlisted source metadata and its physical BigQuery identifiers."""

    table: str
    time_column: str
    schema: Mapping[str, str]
    description: str = ""
    filters: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class MetricCatalogEntry:
    """A public metric handle mapped to one allowlisted source column."""

    source: str
    column: str


@dataclass(frozen=True)
class BigQueryCatalog:
    """The complete public-to-physical identifier allowlist."""

    sources: Mapping[str, SourceCatalogEntry]
    metrics: Mapping[str, MetricCatalogEntry]


class BigQueryClient(Protocol):
    """Small client surface used by the adapter and implemented by test fakes."""

    def query(self, query: str, *, job_config: Any) -> Any: ...


ClientFactory = Callable[..., BigQueryClient]

DEFAULT_MAXIMUM_BYTES_BILLED = 100_000_000
_BIGQUERY_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")
_PROJECT_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$")
_PUBLIC_HANDLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_TIME_TYPES = frozenset({"DATETIME", "TIMESTAMP"})
_SCALAR_PARAMETER_TYPES = frozenset(
    {
        "BOOL",
        "BOOLEAN",
        "BYTES",
        "DATE",
        "DATETIME",
        "FLOAT",
        "FLOAT64",
        "INT64",
        "INTEGER",
        "NUMERIC",
        "STRING",
        "TIME",
        "TIMESTAMP",
    }
)
_AGGREGATIONS = {"avg": "AVG", "count": "COUNT", "max": "MAX", "min": "MIN", "sum": "SUM"}


def _default_client_factory(*, project: str, credentials: Any) -> BigQueryClient:
    try:
        from google.cloud import bigquery
    except ModuleNotFoundError as error:  # pragma: no cover - depends on installation extras
        raise BackendError(
            "BigQuery support requires the 'bigquery' extra; install 'telemetry-mcp[mcp,bigquery]'"
        ) from error
    return bigquery.Client(project=project, credentials=credentials)


class BigQueryBackend:
    """Allowlisted, bounded, read-only adapter over an injected BigQuery client."""

    def __init__(
        self,
        project: str | None = None,
        dataset: str | None = None,
        catalog: BigQueryCatalog | Mapping[str, Any] | str | None = None,
        maximum_bytes_billed: int | str = DEFAULT_MAXIMUM_BYTES_BILLED,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._project = project
        self._dataset = dataset
        self._catalog_input = catalog
        self._maximum_bytes_input = maximum_bytes_billed
        self._client_factory = client_factory or _default_client_factory
        self._catalog: BigQueryCatalog | None = None

    def list_sources(self, *, credentials: Any) -> list[SourceInfo]:
        catalog = self._configuration()
        return [self._source_info(name, entry) for name, entry in catalog.sources.items()]

    def describe(self, source: str, *, credentials: Any) -> SourceInfo:
        catalog = self._configuration()
        entry = catalog.sources.get(source)
        if entry is None:
            raise BackendError(f"unknown source {source!r}")
        return self._source_info(source, entry)

    def query(
        self,
        source: str,
        time_range: TimeRange,
        *,
        filters: dict[str, Any],
        limit: int,
        credentials: Any,
    ) -> QueryResult:
        catalog = self._configuration()
        entry = catalog.sources.get(source)
        if entry is None:
            raise BackendError(f"unknown source {source!r}")
        if limit < 1 or limit > 10_000:
            raise BackendError("query limit must be between 1 and 10000")

        clauses: list[str] = []
        query_parameters = self._range_parameters(entry, time_range)
        for index, (filter_name, value) in enumerate(sorted(filters.items())):
            if filter_name not in entry.filters:
                raise BackendError(f"unknown filter {filter_name!r} for source {source!r}")
            if value is None or isinstance(value, (dict, list, set, tuple)):
                raise BackendError(f"filter {filter_name!r} requires a non-null scalar value")
            parameter_name = f"filter_{index}"
            clauses.append(f"AND `{filter_name}` = @{parameter_name}")
            query_parameters.append(self._scalar_parameter(parameter_name, entry.schema[filter_name], value))
        query_parameters.append(self._scalar_parameter("row_limit", "INT64", limit + 1))

        projection = ", ".join(f"`{column}`" for column in entry.schema)
        sql_lines = [
            f"SELECT {projection}",
            f"FROM `{self._project}.{self._dataset}.{entry.table}`",
            f"WHERE `{entry.time_column}` >= @range_start AND `{entry.time_column}` < @range_end",
            *clauses,
            f"ORDER BY `{entry.time_column}` ASC",
            "LIMIT @row_limit",
        ]
        rows = self._execute("\n".join(sql_lines), query_parameters, credentials, max_results=limit + 1)
        converted = tuple(self._json_safe_row(row) for row in rows)
        from telemetry_mcp.core import QueryResult

        return QueryResult(source=source, rows=converted[:limit], range=time_range, truncated=len(converted) > limit)

    def summary(
        self,
        metric: str,
        time_range: TimeRange,
        agg: str,
        *,
        credentials: Any,
    ) -> SummaryResult:
        catalog = self._configuration()
        metric_entry = catalog.metrics.get(metric)
        if metric_entry is None:
            raise BackendError(f"unknown metric {metric!r}")
        function = _AGGREGATIONS.get(agg)
        if function is None:
            raise BackendError(f"unsupported aggregation {agg!r}")
        source = catalog.sources[metric_entry.source]
        sql = "\n".join(
            [
                f"SELECT {function}(`{metric_entry.column}`) AS `value`",
                f"FROM `{self._project}.{self._dataset}.{source.table}`",
                f"WHERE `{source.time_column}` >= @range_start AND `{source.time_column}` < @range_end",
                "LIMIT 1",
            ]
        )
        rows = self._execute(sql, self._range_parameters(source, time_range), credentials, max_results=1)
        value = None if not rows else self._summary_value(dict(rows[0])["value"])
        from telemetry_mcp.core import SummaryResult

        return SummaryResult(metric=metric, agg=agg, value=value, range=time_range)

    def _configuration(self) -> BigQueryCatalog:
        if not self._project:
            raise BackendError("missing BigQuery configuration: TELEMETRY_BQ_PROJECT")
        if not _PROJECT_IDENTIFIER.fullmatch(self._project):
            raise BackendError("invalid BigQuery project in TELEMETRY_BQ_PROJECT")
        if not self._dataset:
            raise BackendError("missing BigQuery configuration: TELEMETRY_BQ_DATASET")
        if not _BIGQUERY_IDENTIFIER.fullmatch(self._dataset):
            raise BackendError("invalid BigQuery dataset in TELEMETRY_BQ_DATASET")
        if self._catalog_input is None:
            raise BackendError("missing BigQuery configuration: TELEMETRY_BQ_CATALOG")
        try:
            maximum_bytes = int(self._maximum_bytes_input)
        except (TypeError, ValueError) as error:
            raise BackendError("maximum_bytes_billed must be a positive integer") from error
        if maximum_bytes <= 0:
            raise BackendError("maximum_bytes_billed must be a positive integer")
        self._maximum_bytes_billed = maximum_bytes
        if self._catalog is None:
            try:
                self._catalog = self._parse_catalog(self._catalog_input)
                self._validate_catalog(self._catalog)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise BackendError(f"invalid BigQuery catalog: {error}") from error
        return self._catalog

    @staticmethod
    def _parse_catalog(raw_catalog: BigQueryCatalog | Mapping[str, Any] | str) -> BigQueryCatalog:
        if isinstance(raw_catalog, BigQueryCatalog):
            return raw_catalog
        raw: Any = json.loads(raw_catalog) if isinstance(raw_catalog, str) else raw_catalog
        if not isinstance(raw, Mapping):
            raise TypeError("catalog must be a JSON object")
        raw_sources = raw.get("sources", {})
        raw_metrics = raw.get("metrics", {})
        if not isinstance(raw_sources, Mapping) or not isinstance(raw_metrics, Mapping):
            raise TypeError("catalog sources and metrics must be objects")
        sources = {
            str(name): SourceCatalogEntry(
                table=str(value["table"]),
                time_column=str(value["time_column"]),
                schema=dict(value["schema"]),
                description=str(value.get("description", "")),
                filters=frozenset(value.get("filters", [])),
            )
            for name, value in raw_sources.items()
        }
        metrics = {
            str(name): MetricCatalogEntry(source=str(value["source"]), column=str(value["column"]))
            for name, value in raw_metrics.items()
        }
        return BigQueryCatalog(sources=sources, metrics=metrics)

    @staticmethod
    def _validate_catalog(catalog: BigQueryCatalog) -> None:
        for source_name, entry in catalog.sources.items():
            if not _PUBLIC_HANDLE.fullmatch(source_name):
                raise ValueError(f"invalid source handle {source_name!r}")
            identifiers = [entry.table, entry.time_column, *entry.schema]
            if any(not _BIGQUERY_IDENTIFIER.fullmatch(identifier) for identifier in identifiers):
                raise ValueError(f"invalid identifier in source {source_name!r}")
            if entry.time_column not in entry.schema:
                raise ValueError(f"time column is absent from source {source_name!r} schema")
            if str(entry.schema[entry.time_column]).upper() not in _TIME_TYPES:
                raise ValueError(f"time column for source {source_name!r} must be TIMESTAMP or DATETIME")
            for filter_name in entry.filters:
                if filter_name not in entry.schema:
                    raise ValueError(f"filter {filter_name!r} is absent from source {source_name!r} schema")
                if str(entry.schema[filter_name]).upper() not in _SCALAR_PARAMETER_TYPES:
                    raise ValueError(f"filter {filter_name!r} does not have a scalar BigQuery type")
        for metric_name, metric in catalog.metrics.items():
            if not _PUBLIC_HANDLE.fullmatch(metric_name):
                raise ValueError(f"invalid metric handle {metric_name!r}")
            source = catalog.sources.get(metric.source)
            if source is None or metric.column not in source.schema:
                raise ValueError(f"metric {metric_name!r} does not reference a catalogued source column")

    @staticmethod
    def _source_info(name: str, entry: SourceCatalogEntry) -> SourceInfo:
        from telemetry_mcp.core import SourceInfo

        return SourceInfo(name=name, description=entry.description, schema=dict(entry.schema))

    @staticmethod
    def _scalar_parameter(name: str, type_: str, value: Any) -> Any:
        try:
            from google.cloud.bigquery import ScalarQueryParameter
        except ModuleNotFoundError as error:  # pragma: no cover - depends on installation extras
            raise BackendError(
                "BigQuery support requires the 'bigquery' extra; install 'telemetry-mcp[mcp,bigquery]'"
            ) from error
        return ScalarQueryParameter(name, type_.upper(), value)

    def _range_parameters(self, source: SourceCatalogEntry, time_range: TimeRange) -> list[Any]:
        type_ = str(source.schema[source.time_column]).upper()
        return [
            self._scalar_parameter("range_start", type_, time_range.start),
            self._scalar_parameter("range_end", type_, time_range.end),
        ]

    def _execute(self, sql: str, query_parameters: list[Any], credentials: Any, *, max_results: int) -> list[Any]:
        self._configuration()
        try:
            from google.cloud.bigquery import QueryJobConfig
        except ModuleNotFoundError as error:  # pragma: no cover - depends on installation extras
            raise BackendError(
                "BigQuery support requires the 'bigquery' extra; install 'telemetry-mcp[mcp,bigquery]'"
            ) from error
        try:
            client = self._client_factory(project=self._project, credentials=credentials)
            job_config = QueryJobConfig(
                maximum_bytes_billed=self._maximum_bytes_billed,
                query_parameters=query_parameters,
                use_legacy_sql=False,
            )
            return list(client.query(sql, job_config=job_config).result(max_results=max_results))
        except BackendError:
            raise
        except Exception as error:
            detail = str(error)
            if credentials is not None:
                detail = detail.replace(str(credentials), "[redacted]")
            raise BackendError(f"BigQuery query failed: {detail}") from error

    @classmethod
    def _json_safe_row(cls, row: Any) -> dict[str, Any]:
        return {str(key): cls._json_safe(value) for key, value in dict(row).items()}

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, bool)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, bytes):
            return base64.b64encode(value).decode("ascii")
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                value = value.astimezone(timezone.utc)
            return value.isoformat().replace("+00:00", "Z")
        if isinstance(value, (date, datetime_time, UUID)):
            return value.isoformat() if hasattr(value, "isoformat") else str(value)
        if isinstance(value, Mapping):
            return {str(key): cls._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._json_safe(item) for item in value]
        return str(value)

    @staticmethod
    def _summary_value(value: Any) -> float | int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise BackendError("BigQuery summary returned a non-numeric value")
        if isinstance(value, Decimal):
            return int(value) if value == value.to_integral_value() else float(value)
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value


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
