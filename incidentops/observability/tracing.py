from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager, nullcontext
from typing import Iterator

from incidentops.config.settings import Settings

logger = logging.getLogger("incidentops.observability.tracing")

_configured = False
_enabled = False


def make_trace_id() -> str:
    return uuid.uuid4().hex


def configure_tracing(settings: Settings, app=None) -> None:
    global _configured, _enabled
    if _configured:
        return
    _configured = True
    _enabled = bool(settings.enable_otel)
    if not _enabled:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

        provider = TracerProvider(resource=Resource.create({"service.name": settings.otel_service_name}))
        if settings.otel_exporter_otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
                )
            except Exception:
                logger.exception("OTLP exporter unavailable; falling back to console span exporter")
                provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        else:
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(provider)
        if app is not None:
            try:
                from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

                FastAPIInstrumentor.instrument_app(app)
            except Exception:
                logger.info("FastAPI OpenTelemetry instrumentation package not installed")
    except Exception:
        _enabled = False
        logger.exception("OpenTelemetry setup failed; continuing without tracing")


@contextmanager
def traced(name: str) -> Iterator[None]:
    if not _enabled:
        with nullcontext():
            yield
        return
    try:
        from opentelemetry import trace

        tracer = trace.get_tracer("incidentops-core")
        with tracer.start_as_current_span(name):
            yield
    except Exception:
        logger.exception("OpenTelemetry span failed name=%s", name)
        yield
