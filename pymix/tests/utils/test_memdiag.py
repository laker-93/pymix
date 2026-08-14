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


# Two glibc arena heaps (each a 64 MB-aligned rw-p + ---p pair covering one 64 MB span),
# plus the mappings that are *not* arena heaps but look similar enough to be swept up:
# a 1 MB pymalloc arena, an 8 MB thread stack, and the main arena's named [heap]. The
# last line is a heap whose rw-p prefix the kernel merged with 4 MB of unrelated
# anonymous memory in front of it, so it no longer starts on the boundary.
_MAPS = """\
7f4c40000000-7f4c41000000 rw-p 00000000 00:00 0
7f4c41000000-7f4c44000000 ---p 00000000 00:00 0
7f4c44000000-7f4c44800000 rw-p 00000000 00:00 0
7f4c44800000-7f4c48000000 ---p 00000000 00:00 0
7f4c50000000-7f4c50100000 rw-p 00000000 00:00 0
7f4c60000000-7f4c60800000 rw-p 00000000 00:00 0
55a1c0000000-55a1c0021000 rw-p 00000000 00:00 0                          [heap]
7f4c6fc00000-7f4c74000000 rw-p 00000000 00:00 0
"""


def test_arena_heaps_counts_only_glibc_heap_spans():
    parsed = memdiag._parse_arena_heaps(_MAPS)

    assert parsed["heaps"] == 2


def test_arena_heaps_committed_excludes_non_arena_mappings():
    """``committed_mb`` used to sum the rw-p side of *every* anonymous mapping >=512 KB,
    so CPython's 1 MB pymalloc arenas and the thread stacks landed in a number labelled
    as glibc's. On prod that reported 258 MB against a real glibc footprint of 140 MB.
    Only the rw-p prefixes of the two heap spans count: 16 MB + 8 MB."""
    parsed = memdiag._parse_arena_heaps(_MAPS)

    assert parsed["committed_mb"] == 24.0


def test_arena_heaps_skips_a_span_merged_with_unrelated_memory():
    """A VMA-merged heap is under-reported rather than counted with its neighbour's
    4 MB attached. Both numbers are lower bounds; over-reporting is the worse failure,
    since crediting glibc with memory it never requested is the original bug."""
    only_merged = memdiag._parse_arena_heaps(_MAPS.splitlines()[-1] + "\n")

    assert only_merged["heaps"] == 0
    assert only_merged["committed_mb"] == 0.0


def test_malloc_info_is_readable():
    """Regression guard: without an explicit fdopen restype, ctypes truncates the
    FILE* to 32 bits and malloc_info silently writes nothing."""
    if not memdiag.HAVE_MALLOC_INFO:
        pytest.skip("no malloc_info")

    info = memdiag.malloc_info_xml(include_raw=True)

    assert info["available"] is True
    assert info["arenas"] >= 1
    assert "<malloc" in info["raw_xml"]


# Captured verbatim from prod pymix (v1.2.9, glibc 2.36) on 2026-08-14, trimmed to the
# per-arena <sizes> block being empty. The attribute shapes are the point: <total> tags
# carry a count before size, <system> tags do not.
_MALLOC_INFO = b"""\
<malloc version="1">
<heap nr="0">
<sizes>
</sizes>
<total type="fast" count="801" size="68848"/>
<total type="rest" count="108" size="32843"/>
<system type="current" size="72908800"/>
<system type="max" size="72908800"/>
<aspace type="total" size="72908800"/>
<aspace type="mprotect" size="72908800"/>
</heap>
<heap nr="1">
<sizes>
</sizes>
<total type="fast" count="0" size="0"/>
<total type="rest" count="1" size="129152"/>
<system type="current" size="135168"/>
<system type="max" size="135168"/>
<aspace type="total" size="135168"/>
<aspace type="mprotect" size="135168"/>
<aspace type="subheaps" size="1"/>
</heap>
<total type="fast" count="927" size="77456"/>
<total type="rest" count="1590" size="58802222"/>
<total type="mmap" count="4" size="6529024"/>
<system type="current" size="146575360"/>
<system type="max" size="152440832"/>
<aspace type="total" size="146575360"/>
<aspace type="mprotect" size="146575360"/>
</malloc>
"""


def test_malloc_info_reports_process_free_total():
    """``free_total_mb`` read 0.0 on every call ever made, on any process, because the
    pattern expected ``<total type="rest" size=...>`` and glibc emits a ``count``
    attribute in between. It must agree with mallinfo2's ``fordblks`` — on the prod
    process this fixture came from, that was 56.16 MB."""
    parsed = memdiag._parse_malloc_info(_MALLOC_INFO)

    assert parsed["free_total_mb"] == 56.08


def test_malloc_info_reports_process_totals_not_the_last_arena():
    """Both totals come from the trailing pair outside every <heap> block. Picking up an
    arena's numbers instead would understate the process by however many arenas exist."""
    parsed = memdiag._parse_malloc_info(_MALLOC_INFO)

    assert parsed["arenas"] == 2
    assert parsed["system_total_mb"] == 139.79


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
