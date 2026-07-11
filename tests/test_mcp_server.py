"""Tests for the thin MCP wrapper, fully offline.

The server module is a thin adapter over :class:`telemetry_mcp.core.MetricsService`:
it builds the service, maps :class:`MetricsError` to ``ToolError``, and registers
the tools. Tests inject a fake-backend-backed service via ``set_service`` so
nothing touches GCP, and assert the wrapper's mapping and registration.
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from telemetry_mcp import mcp_server
from telemetry_mcp.core import MetricsService
from tests.conftest import FakeClock, RecordingCredentialProvider
from tests.fixtures import backend

START = "2026-06-16T00:00:00Z"
END = "2026-06-17T00:00:00Z"


@pytest.fixture(autouse=True)
def _reset_service() -> None:
    mcp_server.set_service(None)
    yield
    mcp_server.set_service(None)


def _install_fake(fail_with: str | None = None) -> None:
    service = MetricsService(backend(fail_with=fail_with), credentials=RecordingCredentialProvider(), clock=FakeClock())
    mcp_server.set_service(service)


def test_list_sources_tool_round_trip() -> None:
    _install_fake()
    out = mcp_server.metrics_list_sources()
    assert out["count"] == 2


def test_describe_tool_round_trip() -> None:
    _install_fake()
    out = mcp_server.metrics_describe("ci.runs")
    assert out["name"] == "ci.runs"


def test_query_tool_round_trip() -> None:
    _install_fake()
    out = mcp_server.metrics_query("ci.runs", START, END, filters={"repo": "nash"}, limit=10)
    assert out["row_count"] == 2


def test_query_tool_defaults_optional_args() -> None:
    _install_fake()
    out = mcp_server.metrics_query("ci.runs", START, END)
    assert out["row_count"] == 3


def test_summary_tool_round_trip() -> None:
    _install_fake()
    out = mcp_server.metrics_summary("ci.runs.duration_ms", START, END, "avg")
    assert out["value"] == 1200.0


def test_unknown_source_maps_to_tool_error() -> None:
    _install_fake()
    with pytest.raises(ToolError, match="unknown source"):
        mcp_server.metrics_query("ghost", START, END)


def test_bad_agg_maps_to_tool_error() -> None:
    _install_fake()
    with pytest.raises(ToolError, match="unsupported agg"):
        mcp_server.metrics_summary("ci.runs.duration_ms", START, END, "median")


def test_backend_failure_maps_to_tool_error() -> None:
    _install_fake(fail_with="auth: token expired")
    with pytest.raises(ToolError, match="token expired"):
        mcp_server.metrics_list_sources()


def test_build_server_registers_all_tools() -> None:
    _install_fake()
    server = mcp_server.build_server()
    assert server.name == "telemetry-mcp"
    names = {tool.name for tool in server._tool_manager.list_tools()}
    assert {"metrics_list_sources", "metrics_describe", "metrics_query", "metrics_summary"} <= names


def test_service_is_built_from_env_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # No service pre-installed -> the server builds one with the (not-wired)
    # BigQuery backend, which fails fast. No GCP/network is touched because the
    # backend raises before any client is created.
    monkeypatch.setenv("TELEMETRY_BQ_PROJECT", "speedforge-prod-499002")
    monkeypatch.setenv("TELEMETRY_BQ_DATASET", "telemetry")
    mcp_server.set_service(None)
    with pytest.raises(ToolError, match="not wired up"):
        mcp_server.metrics_list_sources()


def test_main_runs_the_built_server(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[bool] = []
    monkeypatch.setattr(FastMCP, "run", lambda self, *_args, **_kwargs: ran.append(True))
    mcp_server.main()
    assert ran == [True]
