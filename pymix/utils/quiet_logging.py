"""Context-scoped suppression of chatty per-item log records.

The wishlist reconcile sweep runs the Navidrome track matcher over every open item for
every user on a fixed interval, then emits a single aggregated summary line per cycle.
Without help, the matcher's per-item diagnostics ("no matches querying by ...", "No good
match ...") would drown that summary.

Rather than thread a ``log``/``quiet`` boolean down through the matcher API (a caller's
logging concern leaking into every method signature), a logger is made *suppressible*
with :func:`make_logger_suppressible`, and callers that want silence wrap the work in
:func:`suppress_match_logging`. The flag lives in a :class:`contextvars.ContextVar`, so
it propagates across ``await`` and ``anyio.to_thread.run_sync`` and is scoped to the
task that set it — a concurrent import keeps full logging.

Records at ``ERROR`` and above are never suppressed: a real failure (e.g. a Navidrome
search that raised) must always surface, even mid-sweep.
"""
import contextvars
import logging
from contextlib import contextmanager
from typing import Iterator

_suppressed: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "match_logging_suppressed", default=False
)


class _SuppressionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Errors always surface; only sub-ERROR per-item chatter is dropped.
        if record.levelno >= logging.ERROR:
            return True
        return not _suppressed.get()


def make_logger_suppressible(logger: logging.Logger) -> None:
    """Attach the suppression filter to ``logger`` (idempotent).

    Once attached, ``logger``'s sub-ERROR records are dropped whenever the calling task
    is inside a :func:`suppress_match_logging` block.
    """
    if not any(isinstance(f, _SuppressionFilter) for f in logger.filters):
        logger.addFilter(_SuppressionFilter())


@contextmanager
def suppress_match_logging() -> Iterator[None]:
    """Within this block, sub-ERROR records from suppressible loggers are dropped."""
    token = _suppressed.set(True)
    try:
        yield
    finally:
        _suppressed.reset(token)
