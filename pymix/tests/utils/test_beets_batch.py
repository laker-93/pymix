"""
Tests for the batched beets field writer (laker-93/pymix#51) -- the argv it hands
`docker exec` and how it reads the result back.
"""
import pytest

from pymix.utils.beets_batch import (
    DEFAULT_CHUNK_SIZE,
    MATCH_BY_ID,
    build_set_field_command,
    chunked,
    parse_applied,
)


def test_build_set_field_command_passes_every_pair_as_one_argv():
    command = build_set_field_command(
        "subbox_id", MATCH_BY_ID, [(1, "SBX-1"), (2, "SBX-2")], write_tags=False
    )

    assert command[0] == "python3"
    assert command[1] == "-c"
    # field, match_field, write mode, then the pairs
    assert command[3:] == ["subbox_id", "id", "nowrite", "1=SBX-1", "2=SBX-2"]


def test_build_set_field_command_marks_media_fields_as_written_back():
    command = build_set_field_command(
        "bpm", "subbox_id", [("SBX-1", 128)], write_tags=True
    )

    assert command[3:] == ["bpm", "subbox_id", "write", "SBX-1=128"]


def test_the_embedded_script_is_valid_python():
    # The script only ever runs inside a beets container, so nothing else here
    # would catch a syntax error in it before an import did.
    script = build_set_field_command("bpm", "id", [], write_tags=False)[2]
    compile(script, "<beets_batch>", "exec")


def test_chunked_keeps_a_realistic_import_to_a_single_exec():
    pairs = [(i, f"SBX-{i}") for i in range(1000)]

    assert [len(c) for c in chunked(pairs)] == [1000]
    assert DEFAULT_CHUNK_SIZE >= 1000


def test_chunked_splits_beyond_the_chunk_size():
    pairs = [(i, f"SBX-{i}") for i in range(5)]

    assert [list(c) for c in chunked(pairs, size=2)] == [
        [(0, "SBX-0"), (1, "SBX-1")],
        [(2, "SBX-2"), (3, "SBX-3")],
        [(4, "SBX-4")],
    ]


def test_parse_applied_reads_the_summary_line():
    output = "MISSING 7\nAPPLIED 2 MISSING 1\n"

    assert parse_applied(output) == 2


def test_parse_applied_rejects_output_with_no_summary():
    # Absence of the summary means the exec didn't run the script to completion,
    # so the caller must fall back rather than assume the writes landed.
    with pytest.raises(ValueError):
        parse_applied("Traceback (most recent call last):\n  ImportError\n")
