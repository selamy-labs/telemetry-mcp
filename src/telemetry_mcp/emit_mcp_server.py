"""MCP server for opt-in at-will OpenTelemetry emission."""

from __future__ import annotations

from typing import Any, Literal

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
except ModuleNotFoundError as error:  # pragma: no cover - import guard
    raise SystemExit(
        "telemetry-emit-mcp requires the 'mcp' package. Install it with: pip install 'telemetry-mcp[emit]'"
    ) from error

from telemetry_mcp.emit_core import DEFAULT_SERVICE_NAME, EmitError, EmitService
from telemetry_mcp.emit_otlp import OTelSink

INSTRUCTIONS = (
    "At-will OpenTelemetry emission over vendor-neutral OTLP. Route ad-hoc "
    "values to metrics, occurrence markers to events, and bounded operations to "
    "spans only when a W3C traceparent is supplied. The server refuses orphan "
    "spans. Use the read-only telemetry-mcp server for queries."
)

_SERVICE: EmitService | None = None


def _build_service() -> EmitService:
    return EmitService(OTelSink.from_env(), service_name=DEFAULT_SERVICE_NAME)


def set_service(service: EmitService | None) -> None:
    global _SERVICE
    _SERVICE = service


def _service() -> EmitService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = _build_service()
    return _SERVICE


def _run(call: Any) -> dict[str, Any]:
    try:
        return call()
    except EmitError as error:
        raise ToolError(str(error)) from error


def telemetry_emit_metric(
    name: str,
    value: int | float,
    kind: Literal["counter", "gauge", "histogram"] = "gauge",
    unit: str = "1",
    attributes: dict[str, Any] | None = None,
    agent: str | None = None,
) -> dict[str, Any]:
    """Emit an ad-hoc value as an OpenTelemetry metric."""
    service = _service()
    return _run(lambda: service.emit_metric(name, value, kind=kind, unit=unit, attributes=attributes, agent=agent))


def telemetry_emit_event(
    name: str,
    body: str | None = None,
    attributes: dict[str, Any] | None = None,
    traceparent: str | None = None,
    agent: str | None = None,
) -> dict[str, Any]:
    """Emit an occurrence marker as an event/log signal.

    If ``traceparent`` is supplied, the event is attached to that active trace.
    Without it, the event is still valid and no orphan span is created.
    """
    service = _service()
    return _run(
        lambda: service.emit_event(name, body=body, attributes=attributes, traceparent=traceparent, agent=agent)
    )


def telemetry_emit_span(
    name: str,
    traceparent: str,
    attributes: dict[str, Any] | None = None,
    status: Literal["ok", "error"] = "ok",
    agent: str | None = None,
) -> dict[str, Any]:
    """Emit a bounded operation span parented to ``traceparent``.

    A missing or invalid ``traceparent`` is rejected so the server never creates
    an orphan span.
    """
    service = _service()
    return _run(lambda: service.emit_span(name, traceparent, attributes=attributes, status=status, agent=agent))


TOOLS = (
    telemetry_emit_metric,
    telemetry_emit_event,
    telemetry_emit_span,
)


def build_server() -> FastMCP:
    server = FastMCP("telemetry-emit-mcp", instructions=INSTRUCTIONS)
    for tool in TOOLS:
        server.add_tool(tool)
    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
