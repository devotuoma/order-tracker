"""OpenTelemetry setup: metrics, logs, and traces.

Exporters are chosen by environment:
- OTEL_CONSOLE_EXPORT=true prints every signal to stdout (docker compose logs app).
- OTEL_EXPORTER_OTLP_ENDPOINT=http://host:4318 sends every signal over OTLP/HTTP.
With neither set (for example in tests) the SDK runs without exporting.
"""

import logging
import os
import time

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode
from starlette.middleware.base import BaseHTTPMiddleware

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "order-tracker")
UNINSTRUMENTED_PATHS = {"/healthz"}

logger = logging.getLogger("order_tracker")
tracer = trace.get_tracer("order_tracker")
meter = metrics.get_meter("order_tracker")


def _exporters():
    exporters = []
    if os.getenv("OTEL_CONSOLE_EXPORT", "").lower() in {"1", "true", "yes"}:
        exporters.append((ConsoleSpanExporter(), ConsoleMetricExporter(), ConsoleLogExporter()))
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporters.append((OTLPSpanExporter(), OTLPMetricExporter(), OTLPLogExporter()))
    return exporters


def setup_telemetry():
    resource = Resource.create({"service.name": SERVICE_NAME})
    exporters = _exporters()
    interval = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "5000"))

    tracer_provider = TracerProvider(resource=resource)
    logger_provider = LoggerProvider(resource=resource)
    readers = []
    for span_exporter, metric_exporter, log_exporter in exporters:
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
        readers.append(PeriodicExportingMetricReader(metric_exporter, export_interval_millis=interval))

    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))
    set_logger_provider(logger_provider)

    logger.setLevel(logging.INFO)
    logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
    logger.addHandler(logging.StreamHandler())


setup_telemetry()

request_counter = meter.create_counter(
    "order_tracker.requests",
    description="HTTP requests by route, method, and status code",
)
request_duration = meter.create_histogram(
    "order_tracker.request.duration",
    unit="s",
    description="HTTP request duration",
)


class TelemetryMiddleware(BaseHTTPMiddleware):
    """Records a span, a request metric, and a log line for every request."""

    async def dispatch(self, request, call_next):
        if request.url.path in UNINSTRUMENTED_PATHS:
            return await call_next(request)

        start = time.perf_counter()
        with tracer.start_as_current_span(
            f"{request.method} {request.url.path}", kind=SpanKind.SERVER
        ) as span:
            status_code = 500
            try:
                response = await call_next(request)
                status_code = response.status_code
                return response
            except Exception as error:
                span.record_exception(error)
                logger.exception("Unhandled error on %s %s", request.method, request.url.path)
                raise
            finally:
                route = request.scope.get("route")
                route_path = getattr(route, "path", "unmatched")
                attributes = {
                    "http.route": route_path,
                    "http.request.method": request.method,
                    "http.response.status_code": status_code,
                }
                span.update_name(f"{request.method} {route_path}")
                span.set_attributes({**attributes, "url.path": request.url.path})
                if status_code >= 500:
                    span.set_status(Status(StatusCode.ERROR))
                request_counter.add(1, attributes)
                request_duration.record(time.perf_counter() - start, attributes)
                logger.info(
                    "%s %s -> %s",
                    request.method,
                    request.url.path,
                    status_code,
                    extra={"http.route": route_path, "http.response.status_code": status_code},
                )
