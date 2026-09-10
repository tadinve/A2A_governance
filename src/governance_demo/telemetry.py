from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Sequence

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from .settings import EVIDENCE_DIR, ensure_directories


class JsonlSpanExporter(SpanExporter):
    def __init__(self, service_name: str) -> None:
        ensure_directories()
        self.path = Path(EVIDENCE_DIR) / f"{service_name}.spans.jsonl"

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self.path.open("a", encoding="utf-8") as stream:
            for span in spans:
                context = span.get_span_context()
                parent_id = f"{span.parent.span_id:016x}" if span.parent else None
                item = {
                    "trace_id": f"{context.trace_id:032x}",
                    "span_id": f"{context.span_id:016x}",
                    "parent_span_id": parent_id,
                    "name": span.name,
                    "service": span.resource.attributes.get("service.name"),
                    "start_ns": span.start_time,
                    "end_ns": span.end_time,
                    "status": span.status.status_code.name,
                    "attributes": dict(span.attributes or {}),
                }
                stream.write(json.dumps(item, default=str, sort_keys=True) + "\n")
        return SpanExportResult.SUCCESS


def configure_telemetry(service_name: str) -> None:
    resource = Resource.create(
        {
            "service.name": service_name,
            "deployment.environment": os.getenv("DEMO_ENVIRONMENT", "local"),
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(service_name)))

    if os.getenv("TRACE_EXPORTER", "file").lower() == "gcp":
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

        provider.add_span_processor(SimpleSpanProcessor(CloudTraceSpanExporter()))

    trace.set_tracer_provider(provider)


def instrument_fastapi(app, service_name: str) -> None:
    configure_telemetry(service_name)
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()

