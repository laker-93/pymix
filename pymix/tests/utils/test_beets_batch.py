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
    parse_missing,
    strip_duplicates_count,
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


# --- duplicates output shape (laker-93/pymix#65) -----------------------------------
#
# `beetsplug/duplicates.py` appends `: <count>` to every record unconditionally on
# 2.10.0, and only under `--count` from 2.13.1. pymix never passes `-c`, so which
# shape it gets depends on the container's frozen beets version.


@pytest.mark.parametrize("record, expected", [
    # 2.10.0: the count is appended after the extension.
    ("/music/A/Album/01 - Artist - Track.1.flac: 1", "/music/A/Album/01 - Artist - Track.1.flac"),
    ("/music/A/Album/02 - Artist - Track.4.flac: 5", "/music/A/Album/02 - Artist - Track.4.flac"),
    # Multi-digit counts, and non-ASCII paths (the en dash was the other suspect
    # in #65 and is not one -- it must survive untouched).
    ("/music/A/Album/track.mp3: 137", "/music/A/Album/track.mp3"),
    ("/music/Aphex Twin/Selected Ambient Works 85–92/Xtal.flac: 2",
     "/music/Aphex Twin/Selected Ambient Works 85–92/Xtal.flac"),
    # 2.13.1: already clean, must pass through byte-identical.
    ("/music/A/Album/01 - Artist - Track.1.flac", "/music/A/Album/01 - Artist - Track.1.flac"),
])
def test_strip_duplicates_count_normalises_both_beets_output_shapes(record, expected):
    assert strip_duplicates_count(record) == expected


@pytest.mark.parametrize("record", [
    # A colon in the middle is part of the path.
    "/music/A/Album 2: The Sequel/track.mp3",
    # The plugin's separator is ": " -- no space means it is not one.
    "/music/A/Album/track.mp3:1",
    # Only an integer is a count.
    "/music/A/Album/track.mp3: one",
    "/music/A/Album/Disc: A",
    "",
])
def test_strip_duplicates_count_leaves_everything_else_alone(record):
    assert strip_duplicates_count(record) == record


# --- the write script against a real library (#243) --------------------------------
#
# Matching by a flexattr used to run `lib.items('subbox_id::^key$')` per pair, which
# beets can't push down to SQL: every pair loaded and filtered the whole library,
# O(pairs x library) -- 47 minutes for a 1.9k-track bpm write on prod.


def _run_set_field_script(monkeypatch, tmp_path, library_path, field, match_field, pairs):
    from beets import config
    from beets.library import Library

    # The script reads the container's beets config; point it at this library
    # instead, without touching whatever config this machine has.
    monkeypatch.setattr(config, "read", lambda *a, **k: None)
    monkeypatch.setattr(config, "sources", list(config.sources))
    config.set({"library": str(library_path), "directory": str(tmp_path / "music")})

    calls = []
    real_items = Library.items

    def counting_items(self, *args, **kwargs):
        calls.append(args)
        return real_items(self, *args, **kwargs)

    monkeypatch.setattr(Library, "items", counting_items)
    command = build_set_field_command(field, match_field, pairs, write_tags=False)
    monkeypatch.setattr("sys.argv", ["-c", *command[3:]])
    import contextlib
    import io
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(command[2], "<beets_batch>", "exec"), {"__name__": "__main__"})
    return out.getvalue(), calls


def _library_with(tmp_path, count):
    from beets.library import Item, Library

    library_path = tmp_path / "library.db"
    lib = Library(str(library_path), str(tmp_path / "music"))
    for i in range(count):
        item = Item(path=str(tmp_path / f"music/{i}.mp3"), title=f"t{i}")
        item["subbox_id"] = f"SBX-{i}"
        lib.add(item)
    # An item with no subbox_id must not match anything.
    lib.add(Item(path=str(tmp_path / "music/none.mp3"), title="none"))
    lib._close()
    return library_path


def test_set_field_by_flexattr_reads_the_library_once(monkeypatch, tmp_path):
    from beets.library import Library

    count = 50
    library_path = _library_with(tmp_path, count)
    pairs = [(f"SBX-{i}", 100 + i) for i in range(count)] + [("SBX-absent", 1)]

    output, calls = _run_set_field_script(
        monkeypatch, tmp_path, library_path, "bpm", "subbox_id", pairs
    )

    # One library pass for all 51 pairs, not one per pair.
    assert len(calls) == 1
    assert parse_applied(output) == count
    assert parse_missing(output) == ["SBX-absent"]
    lib = Library(str(library_path), str(tmp_path / "music"))
    bpms = {item["subbox_id"]: item.bpm for item in lib.items("subbox_id:SBX-")}
    assert bpms == {f"SBX-{i}": 100 + i for i in range(count)}


def test_set_field_by_flexattr_matches_exactly_not_by_prefix(monkeypatch, tmp_path):
    from beets.library import Library

    library_path = _library_with(tmp_path, 12)

    output, _ = _run_set_field_script(
        monkeypatch, tmp_path, library_path, "bpm", "subbox_id", [("SBX-1", 99)]
    )

    assert parse_applied(output) == 1
    lib = Library(str(library_path), str(tmp_path / "music"))
    assert [i["subbox_id"] for i in lib.items("bpm:99")] == ["SBX-1"]


def test_set_field_by_id_uses_the_primary_key(monkeypatch, tmp_path):
    from beets.library import Library

    library_path = _library_with(tmp_path, 3)

    output, calls = _run_set_field_script(
        monkeypatch, tmp_path, library_path, "subbox_id", MATCH_BY_ID, [(2, "NEW"), (999, "X")]
    )

    assert calls == []
    assert parse_applied(output) == 1
    assert parse_missing(output) == ["999"]
    assert Library(str(library_path), str(tmp_path / "music")).get_item(2)["subbox_id"] == "NEW"
