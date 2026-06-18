from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from telemetry_mcp.emit_core import EmitError, EmitService, EventSignal, MetricSignal, SpanSignal

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@dataclass
class RecordingSink:
    metrics: list[MetricSignal] = field(default_factory=list)
    events: list[EventSignal] = field(default_factory=list)
    spans: list[SpanSignal] = field(default_factory=list)

    def emit_metric(self, signal: MetricSignal) -> None:
        self.metrics.append(signal)

    def emit_event(self, signal: EventSignal) -> None:
        self.events.append(signal)

    def emit_span(self, signal: SpanSignal) -> None:
        self.spans.append(signal)


def service() -> tuple[EmitService, RecordingSink]:
    sink = RecordingSink()
    return EmitService(sink), sink


def test_metric_routes_ad_hoc_value_without_trace_context() -> None:
    emit, sink = service()
    out = emit.emit_metric(
        "nash.equity.total_usd",
        125000.50,
        kind="gauge",
        unit="USD",
        attributes={"venue": "kalshi"},
        agent="nash",
    )
    assert out == {"ok": True, "signal": "metric", "name": "nash.equity.total_usd", "kind": "gauge"}
    assert sink.metrics == [
        MetricSignal(
            name="nash.equity.total_usd",
            value=125000.50,
            kind="gauge",
            unit="USD",
            attributes={"venue": "kalshi", "gen_ai.agent.name": "nash", "telemetry.source": "at_will"},
        )
    ]


def test_counter_rejects_negative_values() -> None:
    emit, _ = service()
    with pytest.raises(EmitError, match="non-negative"):
        emit.emit_metric("agent.errors", -1, kind="counter")


def test_metric_rejects_invalid_inputs() -> None:
    emit, _ = service()
    with pytest.raises(EmitError, match="metric name"):
        emit.emit_metric("bad name", 1)
    with pytest.raises(EmitError, match="kind"):
        emit.emit_metric("agent.metric", 1, kind="summary")  # type: ignore[arg-type]
    with pytest.raises(EmitError, match="numeric"):
        emit.emit_metric("agent.metric", "1")  # type: ignore[arg-type]


def test_event_can_attach_to_trace_when_available() -> None:
    emit, sink = service()
    out = emit.emit_event("nash.kill_switch", body="risk halted", traceparent=TRACEPARENT)
    assert out["attached_to_trace"] is True
    assert sink.events[0].traceparent == TRACEPARENT


def test_event_without_traceparent_does_not_create_span() -> None:
    emit, sink = service()
    out = emit.emit_event("nash.kill_switch", body="risk halted")
    assert out["attached_to_trace"] is False
    assert sink.events[0].traceparent is None
    assert sink.spans == []


def test_span_requires_traceparent_to_avoid_orphans() -> None:
    emit, sink = service()
    with pytest.raises(EmitError, match="orphan span"):
        emit.emit_span("nash.rebalance", "")
    assert sink.spans == []


def test_span_rejects_invalid_traceparent_and_status() -> None:
    emit, _ = service()
    with pytest.raises(EmitError, match="version 00"):
        emit.emit_span("nash.rebalance", "not-a-traceparent")
    with pytest.raises(EmitError, match="non-zero"):
        emit.emit_span("nash.rebalance", "00-00000000000000000000000000000000-00f067aa0ba902b7-01")
    with pytest.raises(EmitError, match="status"):
        emit.emit_span("nash.rebalance", TRACEPARENT, status="unknown")  # type: ignore[arg-type]


def test_span_records_only_when_parented() -> None:
    emit, sink = service()
    out = emit.emit_span("nash.rebalance", TRACEPARENT, attributes={"gen_ai.operation.name": "rebalance"})
    assert out == {"ok": True, "signal": "span", "name": "nash.rebalance", "parented": True, "status": "ok"}
    assert sink.spans == [
        SpanSignal(
            name="nash.rebalance",
            traceparent=TRACEPARENT,
            attributes={"gen_ai.operation.name": "rebalance", "telemetry.source": "at_will"},
            status="ok",
        )
    ]


@pytest.mark.parametrize(
    ("attributes", "match"),
    [
        ({"bad key": "value"}, "invalid attribute"),
        ({f"k{i}": i for i in range(65)}, "too many attributes"),
    ],
)
def test_attribute_validation(attributes: dict[str, Any], match: str) -> None:
    emit, _ = service()
    with pytest.raises(EmitError, match=match):
        emit.emit_metric("agent.metric", 1, attributes=attributes)


def test_text_and_attribute_length_limits() -> None:
    emit, _ = service()
    with pytest.raises(EmitError, match="body is too long"):
        emit.emit_event("agent.notice", body="x" * 4097)
    with pytest.raises(EmitError, match="attribute 'note' is too long"):
        emit.emit_metric("agent.metric", 1, attributes={"note": "x" * 4097})
