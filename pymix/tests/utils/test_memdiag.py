"""Tests for the allocator-level memory diagnostics.

The verdict logic is the part worth testing: it is read at 3am against a process whose
true state nobody knows, so it has to be right on cases whose answer we *do* know.
"""
import pathlib
import subprocess
import sys
import textwrap

import pytest

from pymix.utils import memdiag


def test_process_memory_reports_current_rss():
    proc = memdiag.process_memory()

    # A running interpreter is resident; -1.0 is the "couldn't read /proc" sentinel.
    assert proc["rss_mb"] > 0
    assert proc["rss_anon_mb"] > 0


def test_allocator_stats_available_on_glibc():
    stats = memdiag.allocator_stats()

    if not memdiag.HAVE_MALLINFO2:
        pytest.skip("no mallinfo2 (non-glibc libc)")
    assert stats["available"] is True
    assert stats["in_use_mb"] > 0


def test_arena_heaps_parses_maps():
    heaps = memdiag.arena_heaps()

    assert heaps["available"] is True
    assert heaps["heaps"] >= 0


def test_malloc_info_is_readable():
    """Regression guard: without an explicit fdopen restype, ctypes truncates the
    FILE* to 32 bits and malloc_info silently writes nothing."""
    if not memdiag.HAVE_MALLOC_INFO:
        pytest.skip("no malloc_info")

    info = memdiag.malloc_info_xml(include_raw=True)

    assert info["available"] is True
    assert info["arenas"] >= 1
    assert "<malloc" in info["raw_xml"]


# The verdict is a ratio against total RSS, so it can only be asserted in a process
# whose baseline is known. Under pytest it is not: the runner's own footprint plus
# memory retained by earlier tests in the same process drags the ratio into the MIXED
# band and the assertion becomes a statement about test ordering. Each case below
# therefore runs in a fresh interpreter.
_PRELUDE = "import gc, sys; sys.path.insert(0, '/app'); from pymix.utils import memdiag\n"


def _in_fresh_process(body: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", _PRELUDE + textwrap.dedent(body)],
        capture_output=True, text=True, timeout=180,
        cwd=str(pathlib.Path(memdiag.__file__).parents[2]),
    )
    assert result.returncode == 0, f"subprocess failed:\n{result.stderr}"
    return result.stdout.strip()


@pytest.mark.skipif(not memdiag.HAVE_MALLINFO2, reason="no mallinfo2 (non-glibc libc)")
def test_snapshot_calls_genuinely_held_memory_a_leak():
    # >512 bytes each, so these bypass pymalloc and land in glibc where mallinfo2
    # counts them. Held live across the snapshot.
    out = _in_fresh_process("""
        held = [bytearray(900) for _ in range(200_000)]
        snap = memdiag.snapshot()
        print(snap["verdict"])
        print(snap["in_use_fraction_of_rss"])
        assert held  # keep it alive past the snapshot
    """)
    verdict, fraction = out.splitlines()
    assert "NATIVE LEAK" in verdict
    assert float(fraction) > 0.7


@pytest.mark.skipif(not memdiag.HAVE_MALLINFO2, reason="no mallinfo2 (non-glibc libc)")
def test_snapshot_calls_freed_but_unreturned_memory_retention():
    # Python has freed it, but glibc keeps it in free lists, so RSS stays high while
    # in-use collapses. This is exactly prod pymix's signature.
    out = _in_fresh_process("""
        held = [bytearray(900) for _ in range(200_000)]
        del held
        gc.collect()
        snap = memdiag.snapshot()
        print(snap["verdict"])
        print(snap["in_use_fraction_of_rss"])
    """)
    verdict, fraction = out.splitlines()
    assert "RETENTION" in verdict
    assert float(fraction) < 0.3


@pytest.mark.skipif(not memdiag.HAVE_MALLOC_TRIM, reason="no malloc_trim (musl?)")
def test_trim_reclaims_retained_memory():
    out = _in_fresh_process("""
        held = [bytearray(900) for _ in range(200_000)]
        del held
        gc.collect()
        result = memdiag.trim()
        print(result["reclaimed_mb"])
        print(result["rss_before_mb"], result["rss_after_mb"])
    """)
    reclaimed, rss = out.splitlines()
    before, after = (float(v) for v in rss.split())
    assert float(reclaimed) > 100
    assert after < before


def test_snapshot_omits_expensive_object_walk_by_default():
    assert "python_objects" not in memdiag.snapshot()
    assert "python_objects" in memdiag.snapshot(include_objects=True)
