"""MCP server exposing read-only telemetry/metrics queries as typed tools.

This is an optional integration: install it with ``pip install telemetry-mcp[mcp]``.
The core package keeps its runtime dependencies minimal (stdlib only); the
``mcp`` SDK is required only to run this server.

Every tool is a thin wrapper over :class:`telemetry_mcp.core.MetricsService`, so
validation, bounding, credential resolution, and backend routing live in exactly
one place. Tools take structured inputs and return JSON objects. Expected
failures (unknown source/metric, bad agg, backend/auth error) surface as
``ToolError`` with a clean message.

Backend selection (deliberate)
------------------------------
By default this server constructs the production :class:`BigQueryBackend`, which
is **not live-wired** and fails fast until infra completes it. Tests (and any
offline use) inject a fake in-memory backend via :func:`set_service`, so nothing
here touches GCP or the network unless infra has wired the real adapter.

Configuration is resolved at call time from the environment:
``TELEMETRY_BQ_PROJECT`` and ``TELEMETRY_BQ_DATASET`` (consumed by the BigQuery
backend once infra wires it). No credential is ever read or stored here;
identity is resolved per call from WIF/GSM by the credential provider.
"""

from __future__ import annotations

import os
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
except ModuleNotFoundError as error:  # pragma: no cover - import guard
    raise SystemExit(
        "telemetry-mcp server requires the 'mcp' package. Install it with: pip install 'telemetry-mcp[mcp]'"
    ) from error

from telemetry_mcp.backend import BigQueryBackend
from telemetry_mcp.core import MetricsError, MetricsService, TimeRange

INSTRUCTIONS = (
    "Read-only telemetry/metrics access over a configurable backend. The only "
    "capability is querying metrics; there is no write, DDL, or raw-SQL path. "
    "Use metrics_list_sources to discover available datasets/metrics, "
    "metrics_describe(source) to see a source's schema, metrics_query(...) to "
    "pull bounded rows over a time range with optional filters, and "
    "metrics_summary(metric, range, agg) for a single aggregate. The metrics "
    "schema is supplied by the backend (not hard-coded). Credentials are resolved "
    "at call time from the runtime identity and never accepted or returned."
)

# A single service per process. The backend and config are resolved once at
# build time from the environment; credentials are never read or stored here.
_SERVICE: MetricsService | None = None


def _build_service() -> MetricsService:
    """Construct the service from environment config. Separated so tests can
    inject a fake-backed service instead."""
    backend = BigQueryBackend(
        project=os.environ.get("TELEMETRY_BQ_PROJECT"),
        dataset=os.environ.get("TELEMETRY_BQ_DATASET"),
    )
    return MetricsService(backend)


def set_service(service: MetricsService | None) -> None:
    """Install the service the tools use (tests inject a fake-backed one)."""
    global _SERVICE
    _SERVICE = service


def _service() -> MetricsService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = _build_service()
    return _SERVICE


def _run(call: Any) -> dict[str, Any]:
    """Execute a service call, mapping expected failures to ``ToolError``."""
    try:
        return call()
    except MetricsError as error:
        raise ToolError(str(error)) from error


def metrics_list_sources() -> dict[str, Any]:
    """List the telemetry sources (datasets/tables/metrics) the backend exposes."""
    service = _service()
    return _run(service.list_sources)


def metrics_describe(source: str) -> dict[str, Any]:
    """Describe one source: its description and column -> type schema."""
    service = _service()
    return _run(lambda: service.describe(source))


def metrics_query(
    source: str,
    start: str,
    end: str,
    filters: dict[str, Any] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Run a bounded, read-only query against ``source`` over ``[start, end)``.

    ``start`` / ``end`` are ISO-8601 instants. ``filters`` is an optional
    column -> value mapping (values are parameter-bound by the backend, never
    string-interpolated). ``limit`` is capped by the core. Returns structured
    rows.
    """
    service = _service()
    return _run(lambda: service.query(source, TimeRange(start=start, end=end), filters=filters, limit=limit))


def metrics_summary(metric: str, start: str, end: str, agg: str) -> dict[str, Any]:
    """Return a single aggregate (``count``/``sum``/``avg``/``min``/``max``) of
    ``metric`` over ``[start, end)``."""
    service = _service()
    return _run(lambda: service.summary(metric, TimeRange(start=start, end=end), agg))


TOOLS = (
    metrics_list_sources,
    metrics_describe,
    metrics_query,
    metrics_summary,
)


def build_server() -> FastMCP:
    """Build the telemetry-mcp server with every metrics tool registered."""
    server = FastMCP("telemetry-mcp", instructions=INSTRUCTIONS)
    for tool in TOOLS:
        server.add_tool(tool)
    return server


def main() -> None:
    """Run the telemetry-mcp server over stdio."""
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
