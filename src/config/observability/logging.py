"""structlog configuration for the application and for third-party logs.

Django, allauth and Celery log through the standard library. Routing those
records through `structlog.stdlib.ProcessorFormatter` means they pass the same
processor chain as our own calls, so every line -- ours or not -- comes out
structured and carries the same context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

import structlog
from opentelemetry import trace

if TYPE_CHECKING:
    from structlog.typing import EventDict
    from structlog.typing import Processor
    from structlog.typing import WrappedLogger

JSON = "json"
CONSOLE = "console"
LOG_FORMATS = (JSON, CONSOLE)

DEFAULT_LOG_LEVEL = "INFO"


def add_otel_context(
    logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Add the active OpenTelemetry trace and span ids to the event.

    This is what joins logs to traces: given a `trace_id` from a log line you
    can open the corresponding trace, and vice versa. Nothing is added when no
    span is recording, which is the case outside an instrumented request or
    task.

    Args:
        logger: The wrapped logger (unused).
        method_name: The log method name (unused).
        event_dict: The event dictionary to enrich.

    Returns:
        The event dictionary, with `trace_id`/`span_id` when a span is active.

    """
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict["trace_id"] = format(span_context.trace_id, "032x")
        event_dict["span_id"] = format(span_context.span_id, "016x")
    return event_dict


def shared_processors() -> list[Processor]:
    """Build the processor chain applied to structlog and stdlib records alike.

    Returns:
        Processors used both by structlog and as the `foreign_pre_chain` of the
        stdlib formatter.

    """
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        add_otel_context,
    ]


def configure_structlog() -> None:
    """Point structlog at the standard library logging pipeline.

    The chain ends in `wrap_for_formatter`, which hands the event dictionary to
    `ProcessorFormatter` on the stdlib handler rather than rendering it here.
    That is what lets one renderer serve both our logs and third-party ones.
    """
    structlog.configure(
        processors=[
            *shared_processors(),
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.UnicodeDecoder(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def resolve_log_format(*, debug: bool, log_format: str | None = None) -> str:
    """Decide between JSON and console rendering.

    Args:
        debug: Whether Django is running with DEBUG on.
        log_format: An explicit choice, usually from `DJANGO_LOG_FORMAT`. An
            unrecognised value is ignored rather than raising, so a typo in the
            environment cannot stop the application from starting.

    Returns:
        Either `json` or `console`. Console is the default under DEBUG because
        it is meant to be read by a person; JSON everywhere else.

    """
    if log_format in LOG_FORMATS:
        return log_format
    return CONSOLE if debug else JSON


def _renderer(log_format: str) -> list[Processor]:
    """Build the final rendering processors for the stdlib formatter.

    Args:
        log_format: Either `json` or `console`.

    Returns:
        The `processors` list for `ProcessorFormatter`.

    """
    if log_format == JSON:
        # Exceptions are rendered as structured data rather than an embedded
        # multi-line string, which keeps a JSON line machine-readable. This is
        # what `structlog.processors.dict_tracebacks` does -- with one
        # difference that is the whole reason it is spelled out: the default
        # transformer prints every frame's *locals*, and a frame on the
        # collector path holds the GitHub credential as a local (`token`,
        # `declared`, `headers`) from `github_token()` down to the transport's
        # `fetch`. An exception raised through any of them -- a soft time limit,
        # a refused construction -- would print the bearer into the log
        # (`CPM-OPERATE-S05`). So locals are off; frames, lines and names stay.
        return [
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.ExceptionRenderer(structlog.tracebacks.ExceptionDictTransformer(show_locals=False)),
            structlog.processors.JSONRenderer(),
        ]
    return [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        structlog.dev.ConsoleRenderer(),
    ]


def build_logging_config(
    *,
    debug: bool,
    log_level: str | None = None,
    log_format: str | None = None,
    extra_handlers: dict[str, Any] | None = None,
    extra_loggers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the Django LOGGING dictConfig around structlog.

    Args:
        debug: Whether Django is running with DEBUG on.
        log_level: Root log level, usually from `DJANGO_LOG_LEVEL`.
        log_format: `json` or `console`; see `resolve_log_format`.
        extra_handlers: Handlers merged in on top, for settings modules that
            need their own (production adds `mail_admins`).
        extra_loggers: Loggers merged in on top.

    Returns:
        A dictConfig mapping suitable for Django's `LOGGING` setting.

    """
    level = (log_level or DEFAULT_LOG_LEVEL).upper()
    resolved_format = resolve_log_format(debug=debug, log_format=log_format)

    handlers: dict[str, Any] = {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "structured",
        },
    }
    handlers.update(extra_handlers or {})

    loggers: dict[str, Any] = {
        # Emitted by django-structlog's RequestMiddleware and Celery hooks.
        "django_structlog": {"level": level},
        "django_service": {"level": level},
    }
    loggers.update(extra_loggers or {})

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "structured": {
                "()": structlog.stdlib.ProcessorFormatter,
                "processors": _renderer(resolved_format),
                # Applied to records that did NOT come from structlog -- Django,
                # allauth, Celery -- so they gain the same timestamp, level and
                # trace context as ours.
                "foreign_pre_chain": shared_processors(),
            },
        },
        "handlers": handlers,
        # Only console on the root logger. Extra handlers are opt-in per logger
        # -- production's mail_admins must not fire for every record.
        "root": {"level": level, "handlers": ["console"]},
        "loggers": loggers,
    }
