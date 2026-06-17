"""Tests for the metrics core, fully offline against a fake in-memory backend.

These exercise validation/bounding, routing to the backend, filter and limit
handling, the aggregation allowlist, error mapping, and the credential
contract (resolved per call, passed to the backend, never in the output).
"""

from __future__ import annotations

import pytest

from telemetry_mcp.core import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MetricsError,
    MetricsService,
    TimeRange,
)
from tests.conftest import FakeBackend, FakeClock, RecordingCredentialProvider
from tests.fixtures import backend, sources

RANGE = TimeRange(start="2026-06-16T00:00:00Z", end="2026-06-17T00:00:00Z")


def _service(fake: FakeBackend | None = None, creds: RecordingCredentialProvider | None = None) -> MetricsService:
    return MetricsService(fake or backend(), credentials=creds or RecordingCredentialProvider(), clock=FakeClock())


def test_list_sources_round_trip() -> None:
    out = _service().list_sources()
    assert out["count"] == 2
    names = [source["name"] for source in out["sources"]]
    assert names == sorted(names)
    assert "ci.runs" in names


def test_describe_round_trip() -> None:
    out = _service().describe("ci.runs")
    assert out["name"] == "ci.runs"
    assert out["schema"]["duration_ms"] == "INT64"


def test_query_round_trip_returns_rows() -> None:
    out = _service().query("ci.runs", RANGE)
    assert out["source"] == "ci.runs"
    assert out["row_count"] == 3
    assert out["range"]["start"] == RANGE.start
    assert out["truncated"] is False


def test_query_filters_are_applied() -> None:
    out = _service().query("ci.runs", RANGE, filters={"repo": "nash"})
    assert out["row_count"] == 2
    assert all(row["repo"] == "nash" for row in out["rows"])


def test_query_limit_truncates_and_flags() -> None:
    out = _service().query("ci.runs", RANGE, limit=1)
    assert out["row_count"] == 1
    assert out["truncated"] is True


def test_summary_round_trip() -> None:
    out = _service().summary("ci.runs.duration_ms", RANGE, "avg")
    assert out["metric"] == "ci.runs.duration_ms"
    assert out["agg"] == "avg"
    assert out["value"] == 1200.0


def test_summary_normalises_agg_case() -> None:
    out = _service().summary("ci.runs.duration_ms", RANGE, "AVG")
    assert out["agg"] == "avg"


@pytest.mark.parametrize("agg", ["count", "sum", "avg", "min", "max"])
def test_summary_accepts_each_supported_agg(agg: str) -> None:
    out = _service().summary("ci.runs.duration_ms", RANGE, agg)
    assert out["agg"] == agg


def test_summary_rejects_unsupported_agg() -> None:
    with pytest.raises(MetricsError, match="unsupported agg"):
        _service().summary("ci.runs.duration_ms", RANGE, "median")


def test_unknown_source_raises_metrics_error() -> None:
    with pytest.raises(MetricsError, match="unknown source"):
        _service().query("ghost", RANGE)


def test_unknown_metric_raises_metrics_error() -> None:
    with pytest.raises(MetricsError, match="unknown metric"):
        _service().summary("ghost.metric", RANGE, "sum")


def test_backend_failure_maps_to_metrics_error() -> None:
    svc = _service(backend(fail_with="auth: token expired"))
    with pytest.raises(MetricsError, match="auth: token expired"):
        svc.list_sources()


@pytest.mark.parametrize("handle", ["", "  ", "bad name", "../etc", "a;b"])
def test_invalid_source_handle_rejected(handle: str) -> None:
    with pytest.raises(MetricsError):
        _service().describe(handle)


def test_empty_range_rejected() -> None:
    with pytest.raises(MetricsError, match="range requires both"):
        _service().query("ci.runs", TimeRange(start="", end="2026-06-17T00:00:00Z"))


def test_overlong_range_rejected() -> None:
    with pytest.raises(MetricsError, match="too long"):
        _service().query("ci.runs", TimeRange(start="x" * 100, end="2026-06-17T00:00:00Z"))


def test_limit_below_floor_rejected() -> None:
    with pytest.raises(MetricsError, match="limit must be"):
        _service().query("ci.runs", RANGE, limit=0)


def test_limit_above_ceiling_rejected() -> None:
    with pytest.raises(MetricsError, match="limit too large"):
        _service().query("ci.runs", RANGE, limit=MAX_LIMIT + 1)


def test_default_limit_used_when_unset() -> None:
    fake = backend()
    _service(fake).query("ci.runs", RANGE)
    query_call = next(call for call in fake.calls if call["op"] == "query")
    assert query_call["limit"] == DEFAULT_LIMIT


def test_non_mapping_filters_rejected() -> None:
    with pytest.raises(MetricsError, match="filters must be a mapping"):
        _service().query("ci.runs", RANGE, filters=["not", "a", "dict"])  # type: ignore[arg-type]


def test_invalid_filter_key_rejected() -> None:
    with pytest.raises(MetricsError, match="filter key"):
        _service().query("ci.runs", RANGE, filters={"bad key": "x"})


def test_credentials_resolved_per_call_and_passed_to_backend() -> None:
    creds = RecordingCredentialProvider(sentinel="SECRET-TOKEN")
    fake = backend()
    svc = _service(fake, creds)
    svc.list_sources()
    svc.describe("ci.runs")
    assert creds.resolved == 2
    assert all(call["credentials"] == "SECRET-TOKEN" for call in fake.calls)


def test_credentials_never_appear_in_output() -> None:
    creds = RecordingCredentialProvider(sentinel="SECRET-TOKEN")
    out = _service(backend(), creds).query("ci.runs", RANGE)
    assert "SECRET-TOKEN" not in repr(out)


def test_default_credential_provider_returns_none() -> None:
    from telemetry_mcp.core import EnvCredentialProvider

    assert EnvCredentialProvider().resolve() is None
    # A service with no explicit provider still works (ambient identity / None).
    out = MetricsService(backend()).list_sources()
    assert out["count"] == 2


def test_source_to_public_copies_schema() -> None:
    info = sources()["ci.runs"]
    public = info.to_public()
    public["schema"]["injected"] = "x"
    assert "injected" not in info.schema
