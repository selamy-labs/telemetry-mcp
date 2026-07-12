"""Offline contract tests for the bounded BigQuery backend and system clock."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from telemetry_mcp.backend import (
    BackendError,
    BigQueryBackend,
    BigQueryCatalog,
    MetricCatalogEntry,
    SourceCatalogEntry,
    SystemClock,
)
from telemetry_mcp.core import TimeRange

RANGE = TimeRange(start="2026-06-16T00:00:00Z", end="2026-06-17T00:00:00Z")
MAXIMUM_BYTES_BILLED = 25_000_000


class FakeQueryJob:
    def __init__(self, rows: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self.rows = rows or []
        self.error = error
        self.max_results: list[int | None] = []

    def result(self, *, max_results: int | None = None) -> list[dict[str, Any]]:
        self.max_results.append(max_results)
        if self.error is not None:
            raise self.error
        return self.rows


class FakeBigQueryClient:
    def __init__(self, jobs: list[FakeQueryJob] | None = None) -> None:
        self.jobs = jobs or []
        self.queries: list[tuple[str, Any]] = []

    def query(self, sql: str, *, job_config: Any) -> FakeQueryJob:
        self.queries.append((sql, job_config))
        return self.jobs.pop(0)


class RecordingClientFactory:
    def __init__(self, client: FakeBigQueryClient) -> None:
        self.client = client
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *, project: str, credentials: Any) -> FakeBigQueryClient:
        self.calls.append({"project": project, "credentials": credentials})
        return self.client


@pytest.fixture
def catalog() -> BigQueryCatalog:
    return BigQueryCatalog(
        sources={
            "ci.runs": SourceCatalogEntry(
                table="ci_runs",
                time_column="started_at",
                description="CI runner job executions.",
                schema={
                    "started_at": "TIMESTAMP",
                    "repo": "STRING",
                    "duration_ms": "INT64",
                    "payload": "JSON",
                },
                filters=frozenset({"repo"}),
            )
        },
        metrics={"ci.runs.duration_ms": MetricCatalogEntry(source="ci.runs", column="duration_ms")},
    )


def backend(
    catalog: BigQueryCatalog,
    *jobs: FakeQueryJob,
) -> tuple[BigQueryBackend, FakeBigQueryClient, RecordingClientFactory]:
    client = FakeBigQueryClient(list(jobs))
    factory = RecordingClientFactory(client)
    adapter = BigQueryBackend(
        project="telemetry-prod",
        dataset="telemetry",
        catalog=catalog,
        maximum_bytes_billed=MAXIMUM_BYTES_BILLED,
        client_factory=factory,
    )
    return adapter, client, factory


def parameters_by_name(job_config: Any) -> dict[str, Any]:
    return {parameter.name: parameter for parameter in job_config.query_parameters}


def test_catalog_drives_list_and_describe_without_metadata_queries(catalog: BigQueryCatalog) -> None:
    adapter, client, _factory = backend(catalog)

    assert [source.name for source in adapter.list_sources(credentials="sentinel")] == ["ci.runs"]
    assert adapter.describe("ci.runs", credentials="sentinel").schema["repo"] == "STRING"
    assert client.queries == []


def test_query_uses_only_catalogued_identifiers_and_bound_values(catalog: BigQueryCatalog) -> None:
    hostile_value = "nash' OR TRUE --"
    adapter, client, factory = backend(catalog, FakeQueryJob([{"started_at": RANGE.start, "repo": hostile_value}]))

    result = adapter.query("ci.runs", RANGE, filters={"repo": hostile_value}, limit=10, credentials="sentinel")

    sql, job_config = client.queries[0]
    assert sql == (
        "SELECT `started_at`, `repo`, `duration_ms`, `payload`\n"
        "FROM `telemetry-prod.telemetry.ci_runs`\n"
        "WHERE `started_at` >= @range_start AND `started_at` < @range_end\n"
        "AND `repo` = @filter_0\n"
        "ORDER BY `started_at` ASC\n"
        "LIMIT @row_limit"
    )
    assert hostile_value not in sql
    parameters = parameters_by_name(job_config)
    assert {name: parameter.value for name, parameter in parameters.items()} == {
        "range_start": datetime(2026, 6, 16, tzinfo=timezone.utc),
        "range_end": datetime(2026, 6, 17, tzinfo=timezone.utc),
        "filter_0": hostile_value,
        "row_limit": 11,
    }
    assert parameters["range_start"].type_ == "TIMESTAMP"
    assert parameters["filter_0"].type_ == "STRING"
    assert parameters["row_limit"].type_ == "INT64"
    assert job_config.maximum_bytes_billed == MAXIMUM_BYTES_BILLED
    assert job_config.use_legacy_sql is False
    assert factory.calls == [{"project": "telemetry-prod", "credentials": "sentinel"}]
    assert result.rows[0]["repo"] == hostile_value


def test_query_returns_at_most_limit_and_marks_truncation(catalog: BigQueryCatalog) -> None:
    job = FakeQueryJob([{"repo": "a"}, {"repo": "b"}, {"repo": "c"}])
    adapter, _client, _factory = backend(catalog, job)

    result = adapter.query("ci.runs", RANGE, filters={}, limit=2, credentials=None)

    assert result.rows == ({"repo": "a"}, {"repo": "b"})
    assert result.truncated is True
    assert job.max_results == [3]


def test_query_converts_rows_to_strict_json_safe_values(catalog: BigQueryCatalog) -> None:
    row = {
        "decimal": Decimal("12.50"),
        "timestamp": datetime(2026, 6, 16, 1, 2, 3, tzinfo=timezone.utc),
        "date": date(2026, 6, 16),
        "bytes": b"binary",
        "nested": [{"amount": Decimal("2")}],
        "time": time(1, 2, 3),
        "uuid": UUID("12345678-1234-5678-1234-567812345678"),
        "not_finite": float("nan"),
    }
    adapter, _client, _factory = backend(catalog, FakeQueryJob([row]))

    result = adapter.query("ci.runs", RANGE, filters={}, limit=1, credentials=None)

    assert result.rows == (
        {
            "decimal": "12.50",
            "timestamp": "2026-06-16T01:02:03Z",
            "date": "2026-06-16",
            "bytes": "YmluYXJ5",
            "nested": [{"amount": "2"}],
            "time": "01:02:03",
            "uuid": "12345678-1234-5678-1234-567812345678",
            "not_finite": None,
        },
    )
    json.dumps(result.to_public(), allow_nan=False)


def test_summary_uses_catalogued_metric_and_fixed_single_row_select(catalog: BigQueryCatalog) -> None:
    adapter, client, _factory = backend(catalog, FakeQueryJob([{"value": Decimal("1200.5")}]))

    result = adapter.summary("ci.runs.duration_ms", RANGE, "avg", credentials=None)

    sql, job_config = client.queries[0]
    assert sql == (
        "SELECT AVG(`duration_ms`) AS `value`\n"
        "FROM `telemetry-prod.telemetry.ci_runs`\n"
        "WHERE `started_at` >= @range_start AND `started_at` < @range_end\n"
        "LIMIT 1"
    )
    assert {name: parameter.value for name, parameter in parameters_by_name(job_config).items()} == {
        "range_start": datetime(2026, 6, 16, tzinfo=timezone.utc),
        "range_end": datetime(2026, 6, 17, tzinfo=timezone.utc),
    }
    assert job_config.maximum_bytes_billed == MAXIMUM_BYTES_BILLED
    assert result.value == 1200.5


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda adapter: adapter.describe("ci.runs`; DROP TABLE x --", credentials=None), "unknown source"),
        (lambda adapter: adapter.query("unknown", RANGE, filters={}, limit=1, credentials=None), "unknown source"),
        (
            lambda adapter: adapter.query(
                "ci.runs", RANGE, filters={"repo`; DROP TABLE x --": "nash"}, limit=1, credentials=None
            ),
            "unknown filter",
        ),
        (lambda adapter: adapter.summary("ci.runs.secret", RANGE, "sum", credentials=None), "unknown metric"),
    ],
)
def test_unknown_or_hostile_handles_are_rejected(catalog: BigQueryCatalog, call: Any, message: str) -> None:
    adapter, _client, _factory = backend(catalog)
    with pytest.raises(BackendError, match=message):
        call(adapter)


@pytest.mark.parametrize(
    "entry",
    [
        SourceCatalogEntry(table="runs`; DROP TABLE x --", time_column="ts", schema={"ts": "TIMESTAMP"}),
        SourceCatalogEntry(table="runs", time_column="ts OR TRUE", schema={"ts OR TRUE": "TIMESTAMP"}),
        SourceCatalogEntry(table="runs", time_column="ts", schema={"ts": "TIMESTAMP", "bad-name": "STRING"}),
        SourceCatalogEntry(
            table="runs", time_column="ts", schema={"ts": "TIMESTAMP", "repo": "STRING"}, filters=frozenset({"missing"})
        ),
    ],
)
def test_hostile_or_inconsistent_catalog_identifiers_fail_configuration(entry: SourceCatalogEntry) -> None:
    configured = BigQueryBackend(
        project="telemetry-prod",
        dataset="telemetry",
        catalog=BigQueryCatalog(sources={"ci.runs": entry}, metrics={}),
        maximum_bytes_billed=1,
        client_factory=RecordingClientFactory(FakeBigQueryClient()),
    )
    with pytest.raises(BackendError, match="invalid BigQuery catalog"):
        configured.list_sources(credentials=None)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"project": None, "dataset": "telemetry", "catalog": {}}, "TELEMETRY_BQ_PROJECT"),
        ({"project": "telemetry-prod", "dataset": None, "catalog": {}}, "TELEMETRY_BQ_DATASET"),
        ({"project": "telemetry-prod", "dataset": "telemetry", "catalog": None}, "TELEMETRY_BQ_CATALOG"),
        (
            {"project": "telemetry-prod", "dataset": "telemetry", "catalog": {}, "maximum_bytes_billed": 0},
            "maximum_bytes_billed",
        ),
    ],
)
def test_missing_or_invalid_configuration_fails_clearly(kwargs: dict[str, Any], message: str) -> None:
    configured = BigQueryBackend(client_factory=RecordingClientFactory(FakeBigQueryClient()), **kwargs)
    with pytest.raises(BackendError, match=message):
        configured.list_sources(credentials=None)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"project": "INVALID", "dataset": "telemetry", "catalog": {}}, "invalid BigQuery project"),
        ({"project": "telemetry-prod", "dataset": "bad-dataset", "catalog": {}}, "invalid BigQuery dataset"),
        (
            {"project": "telemetry-prod", "dataset": "telemetry", "catalog": {}, "maximum_bytes_billed": "many"},
            "positive integer",
        ),
        ({"project": "telemetry-prod", "dataset": "telemetry", "catalog": "[]"}, "JSON object"),
        (
            {"project": "telemetry-prod", "dataset": "telemetry", "catalog": {"sources": [], "metrics": {}}},
            "sources and metrics",
        ),
    ],
)
def test_malformed_configuration_is_rejected(kwargs: dict[str, Any], message: str) -> None:
    configured = BigQueryBackend(client_factory=RecordingClientFactory(FakeBigQueryClient()), **kwargs)
    with pytest.raises(BackendError, match=message):
        configured.list_sources(credentials=None)


@pytest.mark.parametrize(
    "catalog",
    [
        BigQueryCatalog(
            sources={"bad source": SourceCatalogEntry(table="runs", time_column="ts", schema={"ts": "TIMESTAMP"})},
            metrics={},
        ),
        BigQueryCatalog(
            sources={"runs": SourceCatalogEntry(table="runs", time_column="missing", schema={"ts": "TIMESTAMP"})},
            metrics={},
        ),
        BigQueryCatalog(
            sources={"runs": SourceCatalogEntry(table="runs", time_column="ts", schema={"ts": "STRING"})},
            metrics={},
        ),
        BigQueryCatalog(
            sources={
                "runs": SourceCatalogEntry(
                    table="runs",
                    time_column="ts",
                    schema={"ts": "TIMESTAMP", "labels": "JSON"},
                    filters=frozenset({"labels"}),
                )
            },
            metrics={},
        ),
        BigQueryCatalog(
            sources={"runs": SourceCatalogEntry(table="runs", time_column="ts", schema={"ts": "TIMESTAMP"})},
            metrics={"bad metric": MetricCatalogEntry(source="runs", column="ts")},
        ),
        BigQueryCatalog(
            sources={"runs": SourceCatalogEntry(table="runs", time_column="ts", schema={"ts": "TIMESTAMP"})},
            metrics={"runs.value": MetricCatalogEntry(source="missing", column="value")},
        ),
    ],
)
def test_catalog_relationships_are_validated(catalog: BigQueryCatalog) -> None:
    configured = BigQueryBackend(
        project="telemetry-prod",
        dataset="telemetry",
        catalog=catalog,
        client_factory=RecordingClientFactory(FakeBigQueryClient()),
    )
    with pytest.raises(BackendError, match="invalid BigQuery catalog"):
        configured.list_sources(credentials=None)


@pytest.mark.parametrize("limit", [0, 10_001])
def test_backend_enforces_limit_defensively(catalog: BigQueryCatalog, limit: int) -> None:
    adapter, _client, _factory = backend(catalog)
    with pytest.raises(BackendError, match="query limit"):
        adapter.query("ci.runs", RANGE, filters={}, limit=limit, credentials=None)


def test_filter_values_must_be_scalar(catalog: BigQueryCatalog) -> None:
    adapter, _client, _factory = backend(catalog)
    with pytest.raises(BackendError, match="non-null scalar"):
        adapter.query("ci.runs", RANGE, filters={"repo": ["nash"]}, limit=1, credentials=None)


def test_query_errors_are_wrapped_without_credentials(catalog: BigQueryCatalog) -> None:
    adapter, _client, _factory = backend(catalog, FakeQueryJob(error=RuntimeError("permission denied for sentinel")))

    with pytest.raises(BackendError, match="BigQuery query failed: permission denied") as caught:
        adapter.query("ci.runs", RANGE, filters={}, limit=1, credentials="sentinel")
    assert "sentinel" not in str(caught.value)


def test_backend_errors_from_client_factory_remain_backend_errors(catalog: BigQueryCatalog) -> None:
    def fail_factory(**_kwargs: Any) -> FakeBigQueryClient:
        raise BackendError("client unavailable")

    adapter = BigQueryBackend(
        project="telemetry-prod",
        dataset="telemetry",
        catalog=catalog,
        client_factory=fail_factory,
    )
    with pytest.raises(BackendError, match="client unavailable"):
        adapter.query("ci.runs", RANGE, filters={}, limit=1, credentials=None)


def test_default_client_factory_forwards_project_and_credentials(
    catalog: BigQueryCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    from google.cloud import bigquery

    client = FakeBigQueryClient([FakeQueryJob([])])
    calls: list[dict[str, Any]] = []

    def fake_client(**kwargs: Any) -> FakeBigQueryClient:
        calls.append(kwargs)
        return client

    monkeypatch.setattr(bigquery, "Client", fake_client)
    adapter = BigQueryBackend(project="telemetry-prod", dataset="telemetry", catalog=catalog)

    adapter.query("ci.runs", RANGE, filters={}, limit=1, credentials="sentinel")

    assert calls == [{"project": "telemetry-prod", "credentials": "sentinel"}]


@pytest.mark.parametrize(
    ("rows", "expected"),
    [([], None), ([{"value": Decimal("2")}], 2), ([{"value": float("inf")}], None)],
)
def test_summary_handles_empty_and_json_numeric_results(
    catalog: BigQueryCatalog, rows: list[dict[str, Any]], expected: float | int | None
) -> None:
    adapter, _client, _factory = backend(catalog, FakeQueryJob(rows))
    assert adapter.summary("ci.runs.duration_ms", RANGE, "sum", credentials=None).value == expected


def test_summary_rejects_unsupported_aggregation_and_non_numeric_result(catalog: BigQueryCatalog) -> None:
    adapter, _client, _factory = backend(catalog)
    with pytest.raises(BackendError, match="unsupported aggregation"):
        adapter.summary("ci.runs.duration_ms", RANGE, "median", credentials=None)

    adapter, _client, _factory = backend(catalog, FakeQueryJob([{"value": "not a number"}]))
    with pytest.raises(BackendError, match="non-numeric"):
        adapter.summary("ci.runs.duration_ms", RANGE, "sum", credentials=None)


def test_catalog_can_be_loaded_from_json() -> None:
    catalog_json = json.dumps(
        {
            "sources": {
                "ci.runs": {
                    "table": "ci_runs",
                    "time_column": "started_at",
                    "schema": {"started_at": "TIMESTAMP", "repo": "STRING"},
                    "filters": ["repo"],
                }
            },
            "metrics": {"ci.runs.count": {"source": "ci.runs", "column": "repo"}},
        }
    )
    configured = BigQueryBackend(
        project="telemetry-prod",
        dataset="telemetry",
        catalog=catalog_json,
        maximum_bytes_billed=1,
        client_factory=RecordingClientFactory(FakeBigQueryClient()),
    )

    assert configured.describe("ci.runs", credentials=None).schema["repo"] == "STRING"


def test_system_clock_now_iso_is_utc() -> None:
    stamp = SystemClock().now_iso()
    assert stamp.endswith("Z")


def test_system_clock_monotonic_advances() -> None:
    clock = SystemClock()
    first = clock.monotonic_ns()
    second = clock.monotonic_ns()
    assert second >= first
