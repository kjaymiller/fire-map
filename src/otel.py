"""OpenTelemetry bootstrap, shared by every service in this app (web,
scheduler).

Call `setup_telemetry(service_name)` once, as early as possible -- before
any Postgres connection or Valkey command is made, so the DB instrumentors
below have a chance to patch things first.

Both traces and logs export to an OTLP endpoint if `OTEL_EXPORTER_OTLP_ENDPOINT`
is set, otherwise they're dumped to stdout via the console exporters. That
means this works out of the box locally with no collector to stand up --
point the env var at a real OTLP endpoint (Aiven or otherwise) when you're
ready to ship telemetry somewhere durable. Every Postgres query and every
Valkey command (see otel_valkey.py) gets its own span either way, so once a
collector is configured, both services' activity shows up there alongside
the app's own logs.
"""

import logging
import os

from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    ConsoleLogExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)

from . import otel_valkey

logger = logging.getLogger(__name__)

_initialized = False


def setup_telemetry(service_name: str) -> None:
    """Configure tracing, log export, and trace-correlated logging for
    `service_name`.

    Idempotent -- safe to call more than once (e.g. under a dev
    autoreloader); only the first call takes effect.
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    resource = Resource.create({SERVICE_NAME: service_name})

    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

    trace_provider = TracerProvider(resource=resource)
    log_provider = LoggerProvider(resource=resource)

    if otlp_endpoint:
        trace_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        log_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        logger.info(
            f"OpenTelemetry: exporting traces and logs for {service_name!r} to {otlp_endpoint}"
        )
    else:
        trace_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        log_provider.add_log_record_processor(SimpleLogRecordProcessor(ConsoleLogExporter()))
        logger.info(
            f"OpenTelemetry: OTEL_EXPORTER_OTLP_ENDPOINT not set, logging "
            f"{service_name!r}'s spans and logs to stdout"
        )

    trace.set_tracer_provider(trace_provider)
    set_logger_provider(log_provider)

    # Every log record gets the active trace_id/span_id appended, so a log
    # line and the DB-command/route span it happened inside of can be
    # correlated after the fact.
    LoggingInstrumentor().instrument(set_logging_format=True)

    # Also hand every log record to the OTLP log pipeline above, so logs
    # (not just traces) reach the collector -- e.g. the "Inserted N fire
    # detections into Postgres" / "Caching N detections in Valkey" lines
    # from src/db/postgres.py and src/db/cache.py.
    logging.getLogger().addHandler(LoggingHandler(logger_provider=log_provider))

    # DB command tracing: one span per Postgres query (db.statement is the
    # SQL) and one span per Valkey command (see otel_valkey.py -- there's
    # no official instrumentor for this client, so it's hand-rolled).
    PsycopgInstrumentor().instrument()
    otel_valkey.instrument()
