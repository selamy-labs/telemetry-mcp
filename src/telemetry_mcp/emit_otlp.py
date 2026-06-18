"""OTLP sink for at-will telemetry emission.

Imported only by ``telemetry-emit-mcp``. The read-only ``telemetry-mcp`` server
does not import OpenTelemetry SDK packages and keeps its zero-dependency runtime
contract.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from telemetry_mcp.emit_core import EventSignal, MetricSignal, SpanSignal, TelemetrySink

LOGGER = logging.getLogger(__name__)


class OTelSink(TelemetrySink):
    """Vendor-neutral OTLP sink using OpenTelemetry's standard SDK/exporters."""

    def __init__(self, *, service_name: str) -> None:
        try:
            from opentelemetry import metrics, trace
            from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.propagate import extract
            from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.trace import Status, StatusCode
        except ModuleNotFoundError as error:  # pragma: no cover - import guard
            raise SystemExit(
                "telemetry-emit-mcp requires emit dependencies. Install with: pip install 'telemetry-mcp[emit]'"
            ) from error

        resource = Resource.create({"service.name": service_name, "telemetry.sdk.language": "python"})

        trace_provider = TracerProvider(resource=resource)
        trace_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(trace_provider)
        self._tracer = trace.get_tracer("telemetry-mcp.emit")
        self._trace = trace
        self._extract = extract
        self._status = Status
        self._status_code = StatusCode

        metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)
        self._meter = metrics.get_meter("telemetry-mcp.emit")
        self._metric_cache: dict[tuple[str, str, str], Any] = {}

        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        self._logging_handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
        self._logger = logging.getLogger("telemetry_mcp.emit.events")
        self._logger.setLevel(logging.INFO)
        self._logger.addHandler(self._logging_handler)
        self._providers = (trace_provider, meter_provider, logger_provider)

    @classmethod
    def from_env(cls) -> OTelSink:
        return cls(service_name=os.environ.get("OTEL_SERVICE_NAME", "telemetry-emit-mcp"))

    def emit_metric(self, signal: MetricSignal) -> None:
        instrument = self._instrument(signal)
        if signal.kind == "counter":
            instrument.add(signal.value, signal.attributes)
        elif signal.kind == "histogram":
            instrument.record(signal.value, signal.attributes)
        else:
            instrument.set(signal.value, signal.attributes)
        self._flush()

    def emit_event(self, signal: EventSignal) -> None:
        extra = {"attributes": signal.attributes}
        if signal.traceparent:
            context = self._extract({"traceparent": signal.traceparent})
            with self._tracer.start_as_current_span("telemetry.event", context=context) as span:
                span.add_event(signal.name, signal.attributes)
                self._logger.info(signal.body or signal.name, extra=extra)
        else:
            self._logger.info(signal.body or signal.name, extra=extra)
        self._flush()

    def emit_span(self, signal: SpanSignal) -> None:
        context = self._extract({"traceparent": signal.traceparent})
        with self._tracer.start_as_current_span(signal.name, context=context) as span:
            for key, value in signal.attributes.items():
                span.set_attribute(key, value)
            if signal.status == "error":
                span.set_status(self._status(self._status_code.ERROR))
        self._flush()

    def _instrument(self, signal: MetricSignal) -> Any:
        key = (signal.kind, signal.name, signal.unit)
        if key not in self._metric_cache:
            if signal.kind == "counter":
                self._metric_cache[key] = self._meter.create_counter(signal.name, unit=signal.unit)
            elif signal.kind == "histogram":
                self._metric_cache[key] = self._meter.create_histogram(signal.name, unit=signal.unit)
            else:
                self._metric_cache[key] = self._meter.create_gauge(signal.name, unit=signal.unit)
        return self._metric_cache[key]

    def _flush(self) -> None:
        # Give the SDK's batch processors a chance to flush in short-lived MCP
        # invocations without blocking for the default batch interval.
        for provider in self._providers:
            force_flush = getattr(provider, "force_flush", None)
            if force_flush is not None:
                force_flush(timeout_millis=5000)
        time.sleep(0)
