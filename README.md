# telemetry-mcp

[![CI](https://github.com/selamy-labs/telemetry-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/selamy-labs/telemetry-mcp/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)

`telemetry-mcp` is a small, **read-only** [Model Context
Protocol](https://modelcontextprotocol.io) server that exposes a configurable
metrics/telemetry backend as typed tools: *list sources, describe a source's
schema, run a bounded query, and compute a single aggregate*. It turns ad-hoc
"go read the numbers" scripts into constrained, structured tools an agent can
call.

The package also ships an opt-in, write-side sibling entrypoint,
`telemetry-emit-mcp`, for **at-will OpenTelemetry emission**. It is separate
from the default query server so `telemetry-mcp` stays read-only and
zero-dependency by default.

The server is **catalog-driven by design**: it contains no built-in dataset,
table, or metric names. Runtime configuration maps public handles to an explicit
allowlist of BigQuery tables, time columns, filters, projections, and aggregate
columns.

> **Repo structure:** this ships as a per-server repo, following the shipped
> convention (e.g. `reddit-mcp`, `dispatch-mcp`). Whether the fleet's MCP
> servers consolidate into a single `agent-mcp` repo is pending a consolidation decision;
> until that lands, this stays per-server.

## Tools

| Tool | Purpose |
| --- | --- |
| `metrics_list_sources()` | List the telemetry sources (datasets/tables/metrics) the backend exposes. |
| `metrics_describe(source)` | Describe one source: its description and `column -> type` schema. |
| `metrics_query(source, start, end, filters?, limit?)` | Bounded, read-only query over `[start, end)`; returns structured rows. |
| `metrics_summary(metric, start, end, agg)` | A single aggregate (`count`/`sum`/`avg`/`min`/`max`) of a metric over a range. |

## At-will emit server

`telemetry-emit-mcp` is a separate MCP server for agents that need to record a
value or occurrence while they work without managing OpenTelemetry context by
hand. It exports vendor-neutral OTLP using the standard OpenTelemetry SDK and
honors the `OTEL_EXPORTER_OTLP_*` environment variables the runtime already
uses.

| Tool | Signal | Purpose |
| --- | --- | --- |
| `telemetry_emit_metric(name, value, kind, unit?, attributes?, agent?)` | Metric | Emit ad-hoc values such as equity, P&L, queue depth, and counters. This is the primary at-will path and does not require trace context. |
| `telemetry_emit_event(name, body?, attributes?, traceparent?, agent?)` | Event/log | Emit occurrence markers. If a W3C `traceparent` is supplied, the event is attached to that active trace; otherwise no span is created. |
| `telemetry_emit_span(name, traceparent, attributes?, status?, agent?)` | Span | Emit a bounded operation span only when it can be parented to an active trajectory span. Missing or invalid `traceparent` is rejected so the server never creates orphan spans. |

This routing follows the public `otel-emit-at-will` skill:

- values are metrics;
- occurrences are events;
- spans are only for bounded operations that can be auto-parented.

The default read-only query server does not import the OpenTelemetry SDK. Install
the write-side server explicitly:

```bash
pipx install "telemetry-mcp[emit] @ git+https://github.com/selamy-labs/telemetry-mcp@v0.2.0"
```

MCP client config for the emit server:

```json
{
  "mcpServers": {
    "telemetry-emit": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/selamy-labs/telemetry-mcp@v0.2.0#egg=telemetry-mcp[emit]",
        "telemetry-emit-mcp"
      ],
      "env": {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otel-collector.example.internal",
        "OTEL_SERVICE_NAME": "nash-agent"
      }
    }
  }
}
```

`start` / `end` are ISO-8601 instants. `filters` is an optional `column -> value`
mapping (keys are validated; values are bound as query parameters by the backend,
never string-interpolated). `limit` is capped by the core.

## Security model

This server is built so that exposing it does **not** expose arbitrary data
access or command execution. The properties below are enforced in code and
covered by tests.

- **Read-only.** The tools are list/describe/query/summary. There is no write,
  no DDL, and no `run_sql` / raw-query escape hatch — a caller cannot supply
  query text. The optional `telemetry-emit-mcp` entrypoint is a separate
  write-side server and does not register any query tools.
- **Bounded.** Every query is time-ranged and `limit`-capped (`MAX_LIMIT`), so a
  call cannot pull an unbounded result set.
- **No embedded credentials.** Nothing in this package stores a token or key.
  Credentials are resolved **at call time** by an injected `CredentialProvider`
  (backed by WIF/GSM/env in production) and handed to the backend per request;
  they never live in source, in the service, or in a returned payload (tests
  assert the sentinel credential never appears in output).
- **Validated handles.** Source / metric / filter-key names are restricted to a
  conservative identifier shape, so a rejected lookup cannot smuggle injection or
  path traversal into the backend (defence in depth; the backend's own allowlist
  is the real gate).
- **Catalogued identifiers.** Project, dataset, table, time, projection,
  filter, and metric identifiers must pass conservative validation and come
  from the runtime catalog. Caller-controlled values are query parameters.
- **Scan-capped.** Every BigQuery job sets `maximum_bytes_billed` and disables
  legacy SQL. Row queries fetch at most `limit + 1` rows to report truncation.

### Deliberate omissions

- No tool lets the caller supply or override executed query text.
- No tool returns or accepts credentials.
- No mutation/DDL capability — if you need to change data, that is out of scope
  here by design.

## Configuration (environment, resolved at call time)

| Variable | Effect |
| --- | --- |
| `TELEMETRY_BQ_PROJECT` | BigQuery project containing the catalogued tables/views. |
| `TELEMETRY_BQ_DATASET` | BigQuery dataset containing the catalogued tables/views. |
| `TELEMETRY_BQ_CATALOG` | JSON source and metric allowlist; required. |
| `TELEMETRY_BQ_MAXIMUM_BYTES_BILLED` | Per-query scan ceiling in bytes; defaults to `100000000`. |

No credentials are read from the environment by this server; identity is
resolved per call from the runtime (WIF/GSM) by the credential provider.

## BigQuery catalog

The adapter discovers nothing from `INFORMATION_SCHEMA`; only entries in
`TELEMETRY_BQ_CATALOG` are visible. Each source declares its physical table,
mandatory time column, projected schema, and permitted equality filters. Each
metric maps a public handle to one source column:

```json
{
  "sources": {
    "ci.runs": {
      "table": "ci_runs",
      "time_column": "started_at",
      "description": "CI runner job executions.",
      "schema": {
        "started_at": "TIMESTAMP",
        "repo": "STRING",
        "duration_ms": "INT64"
      },
      "filters": ["repo"]
    }
  },
  "metrics": {
    "ci.runs.duration_ms": {"source": "ci.runs", "column": "duration_ms"}
  }
}
```

Deployment still owns the dataset and a keyless runtime identity with read-only
BigQuery access. The adapter creates a client with the credentials resolved for
each call and stores neither clients nor credentials.

The write-side `telemetry-emit-mcp` needs the runtime's OTLP configuration
instead: `OTEL_EXPORTER_OTLP_ENDPOINT`, optional OTLP headers/protocol variables,
and `OTEL_SERVICE_NAME`.

## Install

Run the tagged release directly from GitHub with both required extras:

```bash
uvx --from "git+https://github.com/selamy-labs/telemetry-mcp@v0.3.0#egg=telemetry-mcp[mcp,bigquery]" telemetry-mcp
```

Or with pipx:

```bash
pipx install "telemetry-mcp[mcp,bigquery] @ git+https://github.com/selamy-labs/telemetry-mcp@v0.3.0"
```

## MCP client config

```json
{
  "mcpServers": {
    "telemetry": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/selamy-labs/telemetry-mcp@v0.3.0#egg=telemetry-mcp[mcp,bigquery]",
        "telemetry-mcp"
      ],
      "env": {
        "TELEMETRY_BQ_PROJECT": "speedforge-prod-499002",
        "TELEMETRY_BQ_DATASET": "telemetry",
        "TELEMETRY_BQ_CATALOG": "{\"sources\":{...},\"metrics\":{...}}",
        "TELEMETRY_BQ_MAXIMUM_BYTES_BILLED": "100000000"
      }
    }
  }
}
```

## Architecture

The metrics logic lives once in `telemetry_mcp.core.MetricsService`; the MCP
server in `telemetry_mcp.mcp_server` is a thin wrapper that serialises structured
results to JSON and maps expected failures to `ToolError`. All data access goes
through an **injected backend** (`telemetry_mcp.backend.MetricsBackend`) and all
credential resolution through an **injected `CredentialProvider`**, so the full
validate / route / shape path is exercised offline in tests with a fake
in-memory backend — no GCP, no network. The default backend
(`BigQueryBackend`) lazily imports its optional client dependency, so the core
package has zero runtime dependencies; the `mcp` SDK and
`google-cloud-bigquery` are optional extras.

See the [System Context](docs/architecture/system-context.md) for the runtime
boundaries of the query and emit servers.

## Development

```bash
python -m pip install -e ".[test]"
ruff format --check .
ruff check .
vulture src tests --min-confidence 80
coverage run -m pytest
coverage report --fail-under=95
```

## License

MIT — see [LICENSE](LICENSE).
