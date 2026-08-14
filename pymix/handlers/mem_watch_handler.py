"""Periodic memory sampling, so an OOM kill leaves a trajectory behind instead of a gap.

``utils/memdiag`` is entirely pull-based: it answers beautifully, but only when someone
is awake to call ``/admin/memory``. #116 was found by hand, and the thing that made it
expensive was that the logs from the growth period said nothing about memory at all —
the last lines before a restart are whatever request happened to be in flight.

Once a ``mem_limit`` exists (laker-93/pymix#125) the kernel will SIGKILL this process at
the ceiling with no warning, no traceback and no chance to flush anything. The only
diagnosis that can survive that is one already written to the log *before* the kill. So
this loop samples on a timer, logs one line per sample, and escalates to WARNING as the
cgroup charge approaches the limit.

Deliberately cheap: ``process_memory`` reads one procfs file, ``allocator_stats`` is a
libc struct copy, ``cgroup_memory`` is two more small reads. None of them walk the heap.
``python_objects``/``include_objects`` is the one genuinely expensive call in memdiag and
is never made here.
"""
import logging

import anyio

from pymix.utils import memdiag

logger = logging.getLogger(__name__)


def _format_sample(proc: dict, alloc: dict, cgroup: dict) -> str:
    parts = [
        f"rss {proc['rss_mb']:.1f} MB",
        f"peak {proc['rss_peak_mb']:.1f} MB",
        f"anon {proc['rss_anon_mb']:.1f} MB",
    ]
    if alloc.get("available"):
        parts.append(f"in_use {alloc['in_use_mb']:.1f} MB")
        parts.append(f"retained {alloc['retained_mb']:.1f} MB")
    if cgroup.get("available") and cgroup.get("limit_mb"):
        parts.append(
            f"cgroup {cgroup['current_mb']:.1f}/{cgroup['limit_mb']:.1f} MB "
            f"({cgroup['used_fraction'] * 100:.0f}%)"
        )
    elif cgroup.get("available"):
        parts.append("cgroup unlimited")
    return "; ".join(parts)


async def mem_watch_loop(interval_s: int, warn_fraction: float):
    """Log a memory sample every ``interval_s`` seconds, at WARNING once the container's
    cgroup charge crosses ``warn_fraction`` of its limit.

    The warning is the useful half. An INFO ramp tells you afterwards what happened; the
    WARNING is what a log search for the last minutes before a restart will actually
    land on, and it fires while the process is still alive and still able to serve
    ``/admin/memory`` for the expensive follow-up questions.

    With no ``mem_limit`` set there is nothing to be a fraction *of*, so every sample
    stays at INFO — the loop is still worth running (it gives the growth-rate history
    that #116 lacked), it just cannot warn.
    """
    while True:
        try:
            proc = memdiag.process_memory()
            alloc = memdiag.allocator_stats()
            cgroup = memdiag.cgroup_memory()
            line = _format_sample(proc, alloc, cgroup)
            fraction = cgroup.get("used_fraction")
            if fraction is not None and fraction >= warn_fraction:
                logger.warning(
                    f"memory: {line} — over {warn_fraction * 100:.0f}% of the container "
                    f"limit; an OOM kill and restart is imminent. Capture "
                    f"GET /admin/memory now, it will not survive the kill."
                )
            else:
                logger.info(f"memory: {line}")
        except Exception:
            # A diagnostic loop must never be the reason the app goes down.
            logger.exception("memory: sampling failed")
        await anyio.sleep(interval_s)
