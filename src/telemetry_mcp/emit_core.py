"""At-will OpenTelemetry emission core.

This module is the write-side sibling to the read-only metrics query service.
It routes by signal type:

* ad-hoc values become metrics;
* occurrence markers become events/log-like records;
* spans are accepted only when a parent ``traceparent`` is supplied, so the
  service never creates orphan spans.

The core has no dependency on the OpenTelemetry SDK. It validates and shapes the
request, then hands it to an injected :class:`TelemetrySink`. Production uses an
OTLP sink from :mod:`telemetry_mcp.emit_otlp`; tests use a recording sink.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

SignalKind = Literal["counter", "gauge", "histogram"]

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$")
_ATTR_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$")
_TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")

DEFAULT_SERVICE_NAME = "selamy-agent"
MAX_ATTRIBUTES = 64
MAX_TEXT_LEN = 4096


class EmitError(Exception):
    """An expected at-will telemetry emission error."""


class TelemetrySink(Protocol):
    """Destination for validated telemetry signals."""

    def emit_metric(self, signal: MetricSignal) -> None: ...

    def emit_event(self, signal: EventSignal) -> None: ...

    def emit_span(self, signal: SpanSignal) -> None: ...


@dataclass(frozen=True)
class MetricSignal:
    name: str
    value: float
    kind: SignalKind
    unit: str = "1"
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EventSignal:
    name: str
    attributes: dict[str, str] = field(default_factory=dict)
    body: str = ""
    traceparent: str | None = None


@dataclass(frozen=True)
class SpanSignal:
    name: str
    traceparent: str
    attributes: dict[str, str] = field(default_factory=dict)
    status: Literal["ok", "error"] = "ok"


class NoopTelemetrySink:
    """Default sink for dry local use; records nothing and exports nowhere."""

    def emit_metric(self, signal: MetricSignal) -> None:
        return None

    def emit_event(self, signal: EventSignal) -> None:
        return None

    def emit_span(self, signal: SpanSignal) -> None:
        return None


def _validate_name(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise EmitError(f"{label} must not be empty")
    if not _NAME_RE.match(cleaned):
        raise EmitError(f"invalid {label} {value!r}: must match {_NAME_RE.pattern}")
    return cleaned


def _validate_text(value: str | None, label: str) -> str:
    if value is None:
        return ""
    if len(value) > MAX_TEXT_LEN:
        raise EmitError(f"{label} is too long: {len(value)} > {MAX_TEXT_LEN}")
    return value


def _validate_traceparent(traceparent: str | None, *, required: bool) -> str | None:
    if traceparent is None or not traceparent.strip():
        if required:
            raise EmitError("traceparent is required for span emission; refusing to create an orphan span")
        return None
    cleaned = traceparent.strip().lower()
    if not _TRACEPARENT_RE.match(cleaned):
        raise EmitError("traceparent must match W3C version 00 format")
    if cleaned[3:35] == "0" * 32 or cleaned[36:52] == "0" * 16:
        raise EmitError("traceparent trace-id and parent-id must be non-zero")
    return cleaned


def _validate_attributes(attributes: dict[str, Any] | None, *, agent: str | None = None) -> dict[str, str]:
    if not attributes:
        out: dict[str, str] = {}
    elif not isinstance(attributes, dict):
        raise EmitError("attributes must be a mapping")
    else:
        if len(attributes) > MAX_ATTRIBUTES:
            raise EmitError(f"too many attributes: {len(attributes)} > {MAX_ATTRIBUTES}")
        out = {}
        for key, value in attributes.items():
            name = _validate_name(str(key), "attribute")
            if not _ATTR_RE.match(name):
                raise EmitError(f"invalid attribute name {key!r}")
            text = str(value)
            if len(text) > MAX_TEXT_LEN:
                raise EmitError(f"attribute {name!r} is too long")
            out[name] = text

    if agent:
        out.setdefault("gen_ai.agent.name", agent)
    out.setdefault("telemetry.source", "at_will")
    return out


class EmitService:
    """Validates and routes at-will telemetry to an injected sink."""

    def __init__(self, sink: TelemetrySink | None = None, *, service_name: str = DEFAULT_SERVICE_NAME) -> None:
        self._sink = sink or NoopTelemetrySink()
        self._service_name = service_name

    def emit_metric(
        self,
        name: str,
        value: int | float,
        *,
        kind: SignalKind = "gauge",
        unit: str = "1",
        attributes: dict[str, Any] | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        metric_name = _validate_name(name, "metric name")
        if kind not in {"counter", "gauge", "histogram"}:
            raise EmitError("kind must be one of: counter, gauge, histogram")
        if not isinstance(value, int | float):
            raise EmitError("metric value must be numeric")
        if kind == "counter" and value < 0:
            raise EmitError("counter value must be non-negative")
        signal = MetricSignal(
            name=metric_name,
            value=float(value),
            kind=kind,
            unit=_validate_text(unit, "unit") or "1",
            attributes=_validate_attributes(attributes, agent=agent),
        )
        self._sink.emit_metric(signal)
        return {"ok": True, "signal": "metric", "name": signal.name, "kind": signal.kind}

    def emit_event(
        self,
        name: str,
        *,
        body: str | None = None,
        attributes: dict[str, Any] | None = None,
        traceparent: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        event_name = _validate_name(name, "event name")
        parent = _validate_traceparent(traceparent, required=False)
        signal = EventSignal(
            name=event_name,
            body=_validate_text(body, "body"),
            attributes=_validate_attributes(attributes, agent=agent),
            traceparent=parent,
        )
        self._sink.emit_event(signal)
        return {"ok": True, "signal": "event", "name": signal.name, "attached_to_trace": parent is not None}

    def emit_span(
        self,
        name: str,
        traceparent: str,
        *,
        attributes: dict[str, Any] | None = None,
        status: Literal["ok", "error"] = "ok",
        agent: str | None = None,
    ) -> dict[str, Any]:
        span_name = _validate_name(name, "span name")
        parent = _validate_traceparent(traceparent, required=True)
        if status not in {"ok", "error"}:
            raise EmitError("status must be ok or error")
        signal = SpanSignal(
            name=span_name,
            traceparent=parent,
            attributes=_validate_attributes(attributes, agent=agent),
            status=status,
        )
        self._sink.emit_span(signal)
        return {"ok": True, "signal": "span", "name": signal.name, "parented": True, "status": signal.status}
