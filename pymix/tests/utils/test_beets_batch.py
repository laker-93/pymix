"""
Tests for the batched beets field writer (laker-93/pymix#51) -- the argv it hands
`docker exec` and how it reads the result back.
"""
import pytest

from pymix.utils.beets_batch import (
    DEFAULT_CHUNK_SIZE,
    MATCH_BY_ID,
    build_import_reads_command,
    build_set_field_command,
    chunked,
    parse_applied,
    parse_import_reads,
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


# --- merged post-import read -------------------------------------------------------
#
# `beet duplicates -p` and `beet list -f $id:$path subbox_id::^$` used to be two
# execs, each paying a full interpreter + plugin-chain start to run one query.


def _reads_output(duplicates, unmapped):
    return (
        "---PYMIX-DUPLICATES---\n"
        + "".join(f"{p}\n" for p in duplicates)
        + "---PYMIX-UNMAPPED---\n"
        + "".join(f"{i}:{p}\n" for i, p in unmapped)
        + "---PYMIX-END---\n"
    )


def test_build_import_reads_command_passes_the_markers_the_parser_looks_for():
    command = build_import_reads_command()

    assert command[0] == "python3"
    assert command[1] == "-c"
    # The script prints these as its section delimiters; the parser splits on them.
    assert command[3:] == [
        "---PYMIX-DUPLICATES---",
        "---PYMIX-UNMAPPED---",
        "---PYMIX-END---",
    ]


def test_the_embedded_reads_script_is_valid_python():
    # As with the write script: it only ever runs inside a beets container, so
    # nothing else would catch a syntax error in it before an import did.
    compile(build_import_reads_command()[2], "<beets_batch_reads>", "exec")


def test_parse_import_reads_splits_both_sections():
    output = _reads_output(
        ["/music/A/Album/dup.mp3"],
        [(1, "/music/A/Album/one.mp3"), (2, "/music/A/Album/two.mp3")],
    )

    duplicates, unmapped = parse_import_reads(output)

    assert duplicates == ["/music/A/Album/dup.mp3"]
    assert unmapped == [(1, "/music/A/Album/one.mp3"), (2, "/music/A/Album/two.mp3")]


def test_parse_import_reads_handles_both_sections_empty():
    duplicates, unmapped = parse_import_reads(_reads_output([], []))

    assert duplicates == []
    assert unmapped == []


def test_parse_import_reads_keeps_colons_in_the_path():
    # Only the first colon separates id from path -- a track path may contain more,
    # and splitting on the wrong one would silently mangle the path we then read
    # the SUBBOX_ID tag from.
    output = _reads_output([], [(12, "/music/A/Album/10:15 Saturday Night.mp3")])

    _, unmapped = parse_import_reads(output)

    assert unmapped == [(12, "/music/A/Album/10:15 Saturday Night.mp3")]


def test_parse_import_reads_skips_malformed_item_lines():
    output = (
        "---PYMIX-DUPLICATES---\n"
        "---PYMIX-UNMAPPED---\n"
        "1:/music/A/Album/one.mp3\n"
        "not-an-id-line\n"
        "x:/music/A/Album/two.mp3\n"
        "---PYMIX-END---\n"
    )

    _, unmapped = parse_import_reads(output)

    assert unmapped == [(1, "/music/A/Album/one.mp3")]


@pytest.mark.parametrize("output", [
    "",
    "Traceback (most recent call last):\n  AttributeError: _raw_main\n",
    "---PYMIX-DUPLICATES---\n---PYMIX-UNMAPPED---\n",  # truncated: no END
    "---PYMIX-UNMAPPED---\n---PYMIX-DUPLICATES---\n---PYMIX-END---\n",  # out of order
])
def test_parse_import_reads_rejects_anything_it_cannot_trust(output):
    # An empty library and a failed exec both produce "no duplicates, no unmapped
    # items". Treating the second as the first would silently skip the subbox_id
    # mapping for a whole import, so this must raise and make the caller fall back.
    with pytest.raises(ValueError):
        parse_import_reads(output)
