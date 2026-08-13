"""Custom OpenTelemetry instrumentation for the Valkey client.

There's no official `opentelemetry-instrumentation-valkey` package -- the
existing redis-py instrumentor doesn't patch this library's classes, since
they're a separate (if API-compatible) hierarchy. Every command this app
sends -- `.get`, `.set`, `.hset`, `.mget`, and the raw `.execute_command`
calls in cache.py/notify.py for MSETEX/HGETDEL -- routes through one
method: `Valkey.execute_command`. Patching that one spot is enough to trace
every Valkey command the app issues, with no per-call-site changes needed.
"""

import logging
from collections.abc import Callable
from typing import Any, cast

import valkey
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

logger = logging.getLogger(__name__)

_tracer = trace.get_tracer("fire-map.valkey")
_original_execute_command: Callable[..., Any] | None = None

# Span attributes get shipped to whatever's consuming the trace -- keep a
# large payload (an MSETEX batch of detections, say) from ballooning it.
MAX_STATEMENT_LENGTH = 512

# Blocking commands time out by design when there's nothing to pop -- e.g.
# notify.next_scan_id()'s BLPOP, polled every 5s by the notifier while idle
# (see notifier.run()). notify.py already catches that TimeoutError and
# treats it as "nothing showed up", not a failure -- so a span for it
# shouldn't be marked as an error either, or every idle poll cycle shows up
# as an ERROR span and buries real Valkey failures.
_BLOCKING_COMMANDS = {
    "BLPOP",
    "BRPOP",
    "BLMPOP",
    "BRPOPLPUSH",
    "BLMOVE",
    "BZPOPMIN",
    "BZPOPMAX",
    "BZMPOP",
}


def _format_statement(args: tuple[Any, ...]) -> str:
    statement = " ".join(str(arg) for arg in args)
    if len(statement) > MAX_STATEMENT_LENGTH:
        return statement[:MAX_STATEMENT_LENGTH] + "…"
    return statement


def instrument() -> None:
    """Patch Valkey.execute_command to emit one CLIENT span per command."""
    global _original_execute_command
    if _original_execute_command is not None:
        return  # already instrumented

    original_execute_command = valkey.Valkey.execute_command
    _original_execute_command = original_execute_command

    def traced_execute_command(self: valkey.Valkey, *args: Any, **kwargs: Any) -> Any:
        command_name = str(args[0]) if args else "UNKNOWN"
        with _tracer.start_as_current_span(
            f"VALKEY {command_name}",
            kind=SpanKind.CLIENT,
            attributes={
                "db.system": "valkey",
                "db.operation": command_name,
                "db.statement": _format_statement(args),
            },
            # A blocking command's TimeoutError is expected (see below) and
            # must not become an ERROR span. The context manager's default
            # __exit__ auto-records/auto-errors on ANY exception that
            # escapes the block though, regardless of what the handlers
            # below do -- so that has to be turned off here, with the
            # non-blocking error path recording/setting status explicitly.
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                return original_execute_command(self, *args, **kwargs)
            except valkey.exceptions.TimeoutError as exc:
                if command_name.upper() in _BLOCKING_COMMANDS:
                    # Expected: the block timed out with nothing to pop.
                    # Leave the span unmarked rather than flag it as an
                    # error the caller doesn't treat as one.
                    span.set_attribute("valkey.blocking_timed_out", True)
                    raise
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise

    valkey.Valkey.execute_command = traced_execute_command
    logger.info("Valkey commands instrumented for OpenTelemetry tracing")


def uninstrument() -> None:
    """Undo `instrument()`. Mainly useful for tests."""
    global _original_execute_command
    if _original_execute_command is None:
        return
    # The stored callable is exactly what came off the class in instrument()
    # (an unbound method), but by the time it's back in a plain variable the
    # type checker only sees `Callable[..., Any]` -- cast it back to what
    # the class attribute is declared as.
    valkey.Valkey.execute_command = cast("Any", _original_execute_command)
    _original_execute_command = None
