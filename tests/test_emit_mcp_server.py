from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from telemetry_mcp import emit_mcp_server
from telemetry_mcp.emit_core import EmitService
from tests.test_emit_core import TRACEPARENT, RecordingSink


@pytest.fixture(autouse=True)
def _reset_service() -> None:
    emit_mcp_server.set_service(None)
    yield
    emit_mcp_server.set_service(None)


def _install_fake() -> RecordingSink:
    sink = RecordingSink()
    emit_mcp_server.set_service(EmitService(sink))
    return sink


def test_metric_tool_round_trip() -> None:
    sink = _install_fake()
    out = emit_mcp_server.telemetry_emit_metric("agent.value", 42, kind="histogram", unit="ms")
    assert out["signal"] == "metric"
    assert sink.metrics[0].kind == "histogram"


def test_event_tool_round_trip() -> None:
    sink = _install_fake()
    out = emit_mcp_server.telemetry_emit_event("agent.notice", body="observed", traceparent=TRACEPARENT)
    assert out["attached_to_trace"] is True
    assert sink.events[0].body == "observed"


def test_span_tool_rejects_orphans_as_tool_error() -> None:
    _install_fake()
    with pytest.raises(ToolError, match="orphan span"):
        emit_mcp_server.telemetry_emit_span("agent.operation", "")


def test_build_server_registers_emit_tools() -> None:
    _install_fake()
    server = emit_mcp_server.build_server()
    assert server.name == "telemetry-emit-mcp"
    names = {tool.name for tool in server._tool_manager.list_tools()}
    assert {"telemetry_emit_metric", "telemetry_emit_event", "telemetry_emit_span"} <= names


def test_main_runs_the_built_server(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[bool] = []
    monkeypatch.setattr(FastMCP, "run", lambda self, *a, **k: ran.append(True))
    emit_mcp_server.main()
    assert ran == [True]
