"""Allocator-level memory introspection, for diagnosing RSS growth in a live process.

Why this exists: on 2026-08-12 prod pymix held ~1.0 GB RSS of the droplet's 1.92 GB and
climbed ~27-38 MB/h, while every Python-level tool said nothing was wrong. That is not a
contradiction — it is the signature of memory that has left Python's view but not the
process. There are two candidates, and they need opposite fixes:

  (a) RETENTION / FRAGMENTATION — Python freed it, glibc kept it in per-arena free lists
      rather than returning it to the kernel. ``malloc_trim(0)`` reclaims it.
  (b) A TRUE NATIVE LEAK — a C/C++/Rust extension (pytaglib, psycopg2, rapidfuzz,
      watchfiles, pydantic-core) malloc'd and never freed. ``malloc_trim`` does nothing.

Neither ``tracemalloc`` nor ``gc`` can tell them apart: tracemalloc only hooks CPython's
``PyMem_*`` domains, so a raw ``malloc`` inside an extension is invisible to it, and gc
only sees Python objects. Both report "flat" in either case.

``mallinfo2().uordblks`` is the discriminator. It is the number of bytes the allocator
has handed out and not had returned:

    uordblks ~= RSS   -> (b) something genuinely holds it; go get native call stacks
    uordblks << RSS   -> (a) free-list retention; malloc_trim is the right fix

Everything here is read-only and cheap except :func:`trim`, which is the one call that
mutates allocator state (it only ever returns free pages to the kernel, never touches
live data). glibc-specific: musl (Alpine) has no ``mallinfo2``/``malloc_trim``, so every
entry point degrades to ``None``/``available: false`` rather than raising.
"""
import ctypes
import ctypes.util
import gc
import logging
import os
import re
import sys
import tempfile
from collections import Counter

logger = logging.getLogger(__name__)

_MB = 1024.0 * 1024.0


class _MallInfo2(ctypes.Structure):
    """glibc ``struct mallinfo2``. All fields are ``size_t`` — the legacy ``mallinfo``
    used ``int``, which silently overflows past 2 GB and is useless here."""

    _fields_ = [
        ("arena", ctypes.c_size_t),     # non-mmapped space allocated (sbrk + heaps)
        ("ordblks", ctypes.c_size_t),   # number of free chunks
        ("smblks", ctypes.c_size_t),    # number of free fastbin blocks
        ("hblks", ctypes.c_size_t),     # number of mmapped regions
        ("hblkhd", ctypes.c_size_t),    # bytes in mmapped regions
        ("usmblks", ctypes.c_size_t),   # always 0 in modern glibc
        ("fsmblks", ctypes.c_size_t),   # bytes in freed fastbin blocks
        ("uordblks", ctypes.c_size_t),  # bytes handed out and not returned  <-- key
        ("fordblks", ctypes.c_size_t),  # bytes free but retained by the allocator
        ("keepcost", ctypes.c_size_t),  # top-most releasable space
    ]


def _load_libc():
    try:
        return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    except OSError:  # pragma: no cover - non-glibc / unusual platform
        logger.warning("memdiag: could not load libc; allocator stats unavailable")
        return None


_libc = _load_libc()

HAVE_MALLINFO2 = bool(_libc) and hasattr(_libc, "mallinfo2")
HAVE_MALLOC_INFO = bool(_libc) and hasattr(_libc, "malloc_info")
HAVE_MALLOC_TRIM = bool(_libc) and hasattr(_libc, "malloc_trim")

if HAVE_MALLINFO2:
    _libc.mallinfo2.restype = _MallInfo2
    _libc.mallinfo2.argtypes = []
if HAVE_MALLOC_TRIM:
    _libc.malloc_trim.restype = ctypes.c_int
    _libc.malloc_trim.argtypes = [ctypes.c_size_t]
if HAVE_MALLOC_INFO:
    _libc.malloc_info.restype = ctypes.c_int
    _libc.malloc_info.argtypes = [ctypes.c_int, ctypes.c_void_p]
    # These restypes are not optional: ctypes defaults to int, which truncates the
    # returned FILE* to 32 bits, and malloc_info then writes through a garbage pointer.
    _libc.fdopen.restype = ctypes.c_void_p
    _libc.fdopen.argtypes = [ctypes.c_int, ctypes.c_char_p]
    _libc.fflush.argtypes = [ctypes.c_void_p]
    _libc.fclose.argtypes = [ctypes.c_void_p]


def _proc_status_kb(key: str) -> float:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith(key):
                    return float(line.split()[1])
    except OSError:
        pass
    return -1.0


def process_memory() -> dict:
    """Kernel's view: what is actually resident, and how much is anonymous (heap-ish)
    rather than file-backed. Current values, not the ``ru_maxrss`` high-water mark —
    a peak counter cannot show a sawtooth and will happily read flat through a leak."""
    return {
        "rss_mb": round(_proc_status_kb("VmRSS:") / 1024.0, 2),
        "rss_anon_mb": round(_proc_status_kb("RssAnon:") / 1024.0, 2),
        "rss_file_mb": round(_proc_status_kb("RssFile:") / 1024.0, 2),
        "vm_data_mb": round(_proc_status_kb("VmData:") / 1024.0, 2),
        "threads": int(_proc_status_kb("Threads:")) if _proc_status_kb("Threads:") > 0 else None,
    }


def allocator_stats() -> dict:
    """glibc's own accounting, aggregated across every arena.

    ``in_use_mb`` vs ``rss_mb`` is the whole diagnosis (see module docstring).
    ``retained_mb`` is memory glibc is holding in free lists on Python's behalf.
    """
    if not HAVE_MALLINFO2:
        return {"available": False, "reason": "mallinfo2 not present (non-glibc libc?)"}
    m = _libc.mallinfo2()
    return {
        "available": True,
        "in_use_mb": round(m.uordblks / _MB, 2),
        "retained_mb": round(m.fordblks / _MB, 2),
        "mmapped_mb": round(m.hblkhd / _MB, 2),
        "arena_mb": round(m.arena / _MB, 2),
        "releasable_top_mb": round(m.keepcost / _MB, 2),
        "free_chunks": m.ordblks,
        "mmapped_regions": m.hblks,
    }


def arena_heaps() -> dict:
    """Count glibc arena heaps by their mapping signature in ``/proc/self/maps``.

    ``new_heap()`` mmaps HEAP_MAX_SIZE (64 MB) as PROT_NONE at a 64 MB-aligned address
    and then mprotects an ``rw-p`` prefix, so each heap appears as an ``rw-p`` + ``---p``
    pair summing to exactly 65536 KB inside one aligned span. Counting these separates
    glibc arenas from CPython's own 1 MB pymalloc arenas.
    """
    spans: dict[int, int] = {}
    committed_kb = 0
    try:
        with open("/proc/self/maps") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) > 5:  # has a pathname -> file-backed, not an arena heap
                    continue
                lo, hi = parts[0].split("-")
                start, end = int(lo, 16), int(hi, 16)
                kb = (end - start) // 1024
                if kb < 512:
                    continue
                base = start - (start % (64 * 1024 * 1024))
                spans[base] = spans.get(base, 0) + kb
                if parts[1].startswith("rw"):
                    committed_kb += kb
    except OSError as exc:
        return {"available": False, "reason": str(exc)}
    return {
        "available": True,
        "heaps": sum(1 for kb in spans.values() if 65000 <= kb <= 66000),
        "committed_mb": round(committed_kb / 1024.0, 2),
    }


_HEAP_NR_RE = re.compile(rb'<heap nr="(\d+)"')
_SYSTEM_CUR_RE = re.compile(rb'<system type="current" size="(\d+)"/>')
_TOTAL_REST_RE = re.compile(rb'<total type="rest" size="(\d+)"/>')


def malloc_info_xml(include_raw: bool = False) -> dict:
    """Per-arena breakdown straight from glibc's ``malloc_info()``.

    ``mallinfo2`` aggregates every arena into one number; this shows how many arenas
    exist and how the totals distribute, which is what tells you whether one arena is
    pathological or the growth is spread evenly.
    """
    if not HAVE_MALLOC_INFO:
        return {"available": False, "reason": "malloc_info not present"}
    tmp = None
    try:
        with tempfile.TemporaryFile() as tf:
            # malloc_info needs a FILE*. fdopen a dup so the fclose below closes our
            # duplicate and leaves tempfile's own descriptor intact.
            fp = _libc.fdopen(os.dup(tf.fileno()), b"w")
            if not fp:
                return {"available": False, "reason": "fdopen failed"}
            _libc.malloc_info(0, fp)
            _libc.fflush(fp)
            _libc.fclose(fp)
            tf.seek(0)
            tmp = tf.read()
    except OSError as exc:
        return {"available": False, "reason": str(exc)}

    system_cur = [int(v) for v in _SYSTEM_CUR_RE.findall(tmp)]
    rest = [int(v) for v in _TOTAL_REST_RE.findall(tmp)]
    out = {
        "available": True,
        "arenas": len(set(_HEAP_NR_RE.findall(tmp))),
        # The final <system>/<total> pair sits outside every <heap> block: process totals.
        "system_total_mb": round((system_cur[-1] if system_cur else 0) / _MB, 2),
        "free_total_mb": round((rest[-1] if rest else 0) / _MB, 2),
    }
    if include_raw:
        out["raw_xml"] = tmp.decode("utf-8", "replace")
    return out


def python_objects(top: int = 15) -> dict:
    """CPython's view: how many tracked objects exist and of what type.

    Included to *rule out* a Python-level leak rather than to find this one — if RSS is
    1 GB while this histogram is small and steady, the growth is below Python entirely.
    """
    objects = gc.get_objects()
    counts = Counter(type(o).__name__ for o in objects)
    return {
        "gc_tracked_objects": len(objects),
        "gc_counts": gc.get_count(),
        "top_types": [{"type": n, "count": c} for n, c in counts.most_common(top)],
    }


_tracemalloc_baseline = None


def tracemalloc_start(frames: int = 20) -> dict:
    """Begin recording Python-level allocation sites, and take a baseline to diff against.

    Scope, so the results are not over-read: tracemalloc hooks CPython's allocator APIs,
    so it sees every Python object — including the >512-byte ones that fall through
    pymalloc into glibc, which is the churn that feeds arena growth. It does *not* see a
    raw ``malloc`` inside a C extension. So it can name the site that churns memory, and
    it stays silent on a genuinely native leak. Read a flat result as "not Python-level",
    never as "no leak".

    Growth here is continuous (~27-38 MB/h), so this can be started at any point on a
    running process — no restart needed — and :func:`tracemalloc_top` read an hour later.
    Costs roughly 2x memory overhead per traced frame while active.
    """
    global _tracemalloc_baseline
    import tracemalloc

    if tracemalloc.is_tracing():
        return {"started": False, "reason": "already tracing",
                "traced_mb": round(tracemalloc.get_traced_memory()[0] / _MB, 2)}
    tracemalloc.start(frames)
    _tracemalloc_baseline = tracemalloc.take_snapshot()
    return {"started": True, "frames": frames}


def tracemalloc_top(limit: int = 20) -> dict:
    """Allocation sites that have grown most since :func:`tracemalloc_start`."""
    import tracemalloc

    if not tracemalloc.is_tracing():
        return {"available": False, "reason": "not tracing; POST /admin/memory/tracemalloc/start first"}

    current, peak = tracemalloc.get_traced_memory()
    snap = tracemalloc.take_snapshot()
    if _tracemalloc_baseline is None:
        stats = snap.statistics("lineno")[:limit]
        growth = None
    else:
        stats = snap.compare_to(_tracemalloc_baseline, "lineno")[:limit]
        growth = True
    return {
        "available": True,
        "traced_current_mb": round(current / _MB, 2),
        "traced_peak_mb": round(peak / _MB, 2),
        "since_baseline": bool(growth),
        "top": [
            {
                "site": str(s.traceback[0]) if s.traceback else "?",
                "size_mb": round(getattr(s, "size_diff", s.size) / _MB, 3),
                "blocks": getattr(s, "count_diff", s.count),
            }
            for s in stats
        ],
    }


def trim() -> dict:
    """Return entirely-free pages from the allocator's free lists to the kernel.

    The one mutating call here, and a safe one: it moves no live data and frees nothing
    that is still referenced. Its limit is that it can only release pages that are
    *wholly* unused — a single live object pins its whole 4 KB page — so a long-lived,
    fragmented heap reclaims less than a clean benchmark suggests.
    """
    if not HAVE_MALLOC_TRIM:
        return {"available": False, "reason": "malloc_trim not present (musl?)"}
    before = process_memory()
    released = bool(_libc.malloc_trim(0))
    after = process_memory()
    return {
        "available": True,
        "glibc_reported_release": released,
        "rss_before_mb": before["rss_mb"],
        "rss_after_mb": after["rss_mb"],
        "reclaimed_mb": round(before["rss_mb"] - after["rss_mb"], 2),
    }


def snapshot(include_objects: bool = False, include_raw_xml: bool = False) -> dict:
    """Every view at once — the single call a diagnosis should start from."""
    snap = {
        "process": process_memory(),
        "allocator": allocator_stats(),
        "arena_heaps": arena_heaps(),
        "malloc_info": malloc_info_xml(include_raw=include_raw_xml),
        "python": {
            "version": sys.version.split()[0],
            "pid": os.getpid(),
        },
    }
    if include_objects:
        snap["python_objects"] = python_objects()

    # State the conclusion rather than making the reader do the division at 3am.
    alloc, proc = snap["allocator"], snap["process"]
    if alloc.get("available") and proc["rss_mb"] > 0:
        in_use = alloc["in_use_mb"]
        ratio = in_use / proc["rss_mb"]
        if ratio > 0.7:
            verdict = ("NATIVE LEAK: most of RSS is memory malloc still considers handed "
                       "out. malloc_trim will not help — get native call stacks.")
        elif ratio < 0.3:
            verdict = ("RETENTION: most of RSS is memory Python freed but glibc kept in "
                       "free lists. malloc_trim should reclaim it.")
        else:
            verdict = "MIXED: neither retention nor a leak dominates; sample over time."
        snap["verdict"] = verdict
        snap["in_use_fraction_of_rss"] = round(ratio, 3)
    return snap
