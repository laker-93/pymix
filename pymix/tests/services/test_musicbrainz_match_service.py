import pytest

from pymix.services import musicbrainz_match_service as mb_module
from pymix.services.musicbrainz_match_service import MusicBrainzMatchService


def _recording(title, artist_credit, score, releases=None):
    rec = {"title": title, "artist-credit": artist_credit, "ext:score": str(score)}
    if releases is not None:
        rec["release-list"] = releases
    return rec


@pytest.mark.anyio
async def test_match_returns_best_scoring_recording(monkeypatch):
    def fake_search(query, limit):
        return {
            "recording-list": [
                _recording("Wrong", [{"artist": {"name": "Nope"}}], 60),
                _recording(
                    "Lite Spots",
                    [{"artist": {"name": "KAYTRANADA"}}],
                    99,
                    releases=[{"title": "99.9%"}],
                ),
            ]
        }

    monkeypatch.setattr(mb_module.musicbrainzngs, "search_recordings", fake_search)
    result = await MusicBrainzMatchService().match("kaytranada lite spots")

    assert result == {"artist": "KAYTRANADA", "title": "Lite Spots", "album": "99.9%", "score": 99}


@pytest.mark.anyio
async def test_match_joins_multi_artist_credit(monkeypatch):
    credit = [
        {"artist": {"name": "Artist A"}},
        " feat. ",
        {"artist": {"name": "Artist B"}},
    ]
    monkeypatch.setattr(
        mb_module.musicbrainzngs,
        "search_recordings",
        lambda query, limit: {"recording-list": [_recording("Song", credit, 95)]},
    )
    result = await MusicBrainzMatchService().match("something")

    assert result["artist"] == "Artist A feat. Artist B"


@pytest.mark.anyio
async def test_match_discards_below_threshold(monkeypatch):
    monkeypatch.setattr(
        mb_module.musicbrainzngs,
        "search_recordings",
        lambda query, limit: {"recording-list": [_recording("Song", [{"artist": {"name": "A"}}], 50)]},
    )
    result = await MusicBrainzMatchService(min_score=90).match("something")

    assert result is None


@pytest.mark.anyio
async def test_match_returns_none_for_no_results(monkeypatch):
    monkeypatch.setattr(
        mb_module.musicbrainzngs,
        "search_recordings",
        lambda query, limit: {"recording-list": []},
    )
    assert await MusicBrainzMatchService().match("nothing here") is None


@pytest.mark.anyio
async def test_match_returns_none_on_empty_query():
    # No network call should happen for blank input.
    assert await MusicBrainzMatchService().match("   ") is None


@pytest.mark.anyio
async def test_match_swallows_search_errors(monkeypatch):
    def boom(query, limit):
        raise RuntimeError("network down")

    monkeypatch.setattr(mb_module.musicbrainzngs, "search_recordings", boom)
    # A failing search must never propagate — the caller falls back to its own guess.
    assert await MusicBrainzMatchService().match("anything") is None


@pytest.mark.anyio
async def test_match_drops_result_missing_artist(monkeypatch):
    monkeypatch.setattr(
        mb_module.musicbrainzngs,
        "search_recordings",
        lambda query, limit: {"recording-list": [_recording("Song", [], 100)]},
    )
    assert await MusicBrainzMatchService().match("x") is None


@pytest.mark.anyio
async def test_match_fields_builds_fielded_fuzzy_query(monkeypatch):
    # The reported bug: artist "Kahn" + title typo "Abatoir". A free-text search matches
    # the exact typo'd title by unrelated artists; a fielded query keyed on the artist,
    # with the title fuzzed, resolves to the right recording instead.
    captured = {}

    def fake_search(query, limit):
        captured["query"] = query
        return {"recording-list": [_recording("Abattoir", [{"artist": {"name": "Kahn"}}], 100)]}

    monkeypatch.setattr(mb_module.musicbrainzngs, "search_recordings", fake_search)
    result = await MusicBrainzMatchService().match_fields(artist="Kahn", title="Abatoir")

    assert captured["query"] == "artist:(Kahn) AND recording:(Abatoir~)"
    assert result == {"artist": "Kahn", "title": "Abattoir", "album": None, "score": 100}


@pytest.mark.anyio
async def test_match_fields_fuzzes_each_title_term_and_escapes(monkeypatch):
    captured = {}

    def fake_search(query, limit):
        captured["query"] = query
        return {"recording-list": []}

    monkeypatch.setattr(mb_module.musicbrainzngs, "search_recordings", fake_search)
    await MusicBrainzMatchService().match_fields(artist="A:B", title="two words")

    # Reserved ":" is escaped in the artist; every title term is individually fuzzed.
    assert captured["query"] == r"artist:(A\:B) AND recording:(two~ words~)"


@pytest.mark.anyio
async def test_match_fields_title_only(monkeypatch):
    captured = {}

    def fake_search(query, limit):
        captured["query"] = query
        return {"recording-list": []}

    monkeypatch.setattr(mb_module.musicbrainzngs, "search_recordings", fake_search)
    await MusicBrainzMatchService().match_fields(title="solo")

    assert captured["query"] == "recording:(solo~)"


@pytest.mark.anyio
async def test_match_fields_returns_none_when_no_fields():
    # No network call should happen when there's nothing to search on.
    assert await MusicBrainzMatchService().match_fields(artist="  ", title="") is None
