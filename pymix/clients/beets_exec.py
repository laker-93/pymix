import logging
import threading
import time
from contextlib import contextmanager
from typing import Dict, Iterator, List, Union

from python_on_whales import docker

from pymix.services import metrics

logger = logging.getLogger(__name__)


class BeetsExec:
    """
    Owns every `docker exec` against a beets container (one per user, plus the
    shared "beets" public container), and a lock per container name that
    serializes beets *writes* against that container.

    Reads (`beet stats`, `beet list`, `beet duplicates -p`) are exempt from the
    lock — concurrent readers are safe, and the import progress bar polls
    `beet stats` for the whole duration of an import, so a lock that covered
    reads too would serialize it behind the import it's trying to report on.
    `beet import`, `modify`, `rm`, and `duplicates -d` are writes.

    `execute` never takes the lock itself. A composite job (an import, a
    watch cycle) is made of several execs plus non-beets work in between
    (parsing beet list output, tagging files); locking inside `execute` would
    only cover one exec at a time and let another writer interleave between
    steps. Callers take the lock explicitly with `write_lock(container_name)`
    around the whole logical operation instead.

    A plain dict of `threading.Lock` (not `asyncio.Lock`) because every call
    site is synchronous and runs on a worker thread via
    `anyio.to_thread.run_sync` — nothing here ever awaits.
    """

    def __init__(self):
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, container_name: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(container_name)
            if lock is None:
                lock = threading.Lock()
                self._locks[container_name] = lock
            return lock

    @contextmanager
    def write_lock(self, container_name: str) -> Iterator[None]:
        """Serialize beets writes against `container_name`. Blocks until any
        other writer for that same container finishes — no reject/retry.

        The wait is measured because it is otherwise completely invisible: a user
        whose import is queued behind their own watch-directory cycle sees a slow
        import, while every other metric in the service says the work was fast. Time
        spent here is contention, not work, and it is the one bottleneck that gets
        worse purely because more people showed up.
        """
        lock = self._lock_for(container_name)
        waiting_since = time.monotonic()
        lock.acquire()
        metrics.beets_write_lock_wait_seconds.observe(time.monotonic() - waiting_since)
        try:
            yield
        finally:
            lock.release()

    def execute(self, container_name: str, command: Union[str, List[str]], stream: bool = False):
        if isinstance(command, str):
            command = command.split()
        logger.info(f'running beets command {command} on container {container_name}')

        if stream:
            # Deliberately not timed. A streamed exec returns a generator immediately
            # and the work happens as the caller consumes it, so anything measured
            # here would be the setup cost and would report a ten-minute import as
            # instantaneous — worse than no number at all. The streamed calls are the
            # imports, and those are timed end-to-end by `pymix_job_duration_seconds`.
            return docker.execute(container_name, command, stream=True)

        started_at = time.monotonic()
        ok = False
        try:
            result = docker.execute(container_name, command, stream=False)
            ok = True
            return result
        finally:
            metrics.observe_beets_exec(
                metrics.beets_command_label(command), started_at, ok
            )
