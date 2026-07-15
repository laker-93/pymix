import pytest

from pymix.services import musicbrainz_match_service as mb_module
from pymix.services.musicbrainz_match_service import MusicBrainzMatchService


def _recording(title, artist_credit, score, releases=None):
    rec = {"title": title, "artist-credit": artist_credit, "ext:score": str(score)}
    if releases is not None:
        rec["release-list"] = releases
    return rec


def _credit(name):
    return [{"artist": {"name": name}}]


def _returns(*recordings):
    return lambda query, limit: {"recording-list": list(recordings)}


@pytest.mark.anyio
async def test_match_returns_best_scoring_recording(monkeypatch):
    monkeypatch.setattr(
        mb_module.musicbrainzngs,
        "search_recordings",
        _returns(
            _recording("Wrong", _credit("Nope"), 60),
            _recording("Lite Spots", _credit("KAYTRANADA"), 99, releases=[{"title": "99.9%"}]),
        ),
    )
    result = await MusicBrainzMatchService().match("kaytranada lite spots")

    assert result == {
        "artist": "KAYTRANADA",
        "title": "Lite Spots",
        "album": "99.9%",
        "score": 99,
        "similarity": 100.0,
    }


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
        _returns(_recording("Song", credit, 95)),
    )
    result = await MusicBrainzMatchService().match("artist a feat artist b song")

    assert result["artist"] == "Artist A feat. Artist B"


@pytest.mark.anyio
async def test_match_returns_none_for_no_results(monkeypatch):
    monkeypatch.setattr(mb_module.musicbrainzngs, "search_recordings", _returns())
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
        mb_module.musicbrainzngs, "search_recordings", _returns(_recording("Song", [], 100))
    )
    assert await MusicBrainzMatchService().match("x") is None


# --- Retrieval: the query we build ------------------------------------------------------


@pytest.mark.anyio
async def test_match_fields_fuzzes_both_fields_by_term_length(monkeypatch):
    captured = {}

    def fake_search(query, limit):
        captured["query"] = query
        return {"recording-list": [_recording("Abattoir", _credit("Kahn"), 100)]}

    monkeypatch.setattr(mb_module.musicbrainzngs, "search_recordings", fake_search)
    result = await MusicBrainzMatchService().match_fields(artist="Kahn", title="Abatoir")

    # The artist is fuzzed too, so a mistyped artist is still retrievable. Short terms get
    # ~1: a bare ~ is edit-distance 2, which on a 4-letter word matches half the database.
    assert captured["query"] == "artist:(Kahn~1) AND recording:(Abatoir~2)"
    assert result["artist"] == "Kahn"
    assert result["title"] == "Abattoir"


@pytest.mark.anyio
async def test_match_fields_fuzzes_each_term_and_escapes(monkeypatch):
    captured = {}

    def fake_search(query, limit):
        captured["query"] = query
        return {"recording-list": []}

    monkeypatch.setattr(mb_module.musicbrainzngs, "search_recordings", fake_search)
    await MusicBrainzMatchService().match_fields(artist="A:B", title="two words")

    # Reserved ":" is escaped; every term is individually fuzzed by its own length.
    assert captured["query"] == r"artist:(A\:B~1) AND recording:(two~1 words~2)"


@pytest.mark.anyio
async def test_match_fields_title_only(monkeypatch):
    captured = {}

    def fake_search(query, limit):
        captured["query"] = query
        return {"recording-list": []}

    monkeypatch.setattr(mb_module.musicbrainzngs, "search_recordings", fake_search)
    await MusicBrainzMatchService().match_fields(title="solo")

    assert captured["query"] == "recording:(solo~1)"


@pytest.mark.anyio
async def test_match_fields_returns_none_when_no_fields():
    # No network call should happen when there's nothing to search on.
    assert await MusicBrainzMatchService().match_fields(artist="  ", title="") is None


# --- Verification: what we accept (regressions for #31) ---------------------------------
#
# Every "rejects" case below scored 100 on MusicBrainz's ext:score in the live evidence on
# #31 — that's the whole point. A top hit always scores ~100, so these are only separable
# by comparing the candidate to what the user actually typed.


@pytest.mark.anyio
async def test_match_fields_accepts_genuine_typo(monkeypatch):
    monkeypatch.setattr(
        mb_module.musicbrainzngs,
        "search_recordings",
        _returns(_recording("Abattoir", _credit("Kahn"), 100)),
    )
    result = await MusicBrainzMatchService().match_fields(artist="Kahn", title="Abatoir")

    # The feature working as intended: one mistyped character, corrected.
    assert (result["artist"], result["title"]) == ("Kahn", "Abattoir")


@pytest.mark.anyio
async def test_match_fields_accepts_mistyped_artist(monkeypatch):
    monkeypatch.setattr(
        mb_module.musicbrainzngs,
        "search_recordings",
        _returns(_recording("Lite Spots", _credit("KAYTRANADA"), 100)),
    )
    # Previously unfixable: the artist was matched exactly, so a typo in it returned
    # nothing and the resolve loop recorded a *terminal* nomatch.
    result = await MusicBrainzMatchService().match_fields(artist="Kaytranda", title="Lite Spots")

    assert result["artist"] == "KAYTRANADA"


@pytest.mark.anyio
async def test_match_fields_accepts_extra_credited_artists(monkeypatch):
    monkeypatch.setattr(
        mb_module.musicbrainzngs,
        "search_recordings",
        _returns(
            _recording("Damager (Hamdi edit)", _credit("Sammy Virji & Interplanetary Criminal"), 100)
        ),
    )
    # MusicBrainz credits collaborators the user didn't type. Still the right recording,
    # so the artist gate only runs typed -> candidate, never the reverse.
    result = await MusicBrainzMatchService().match_fields(
        artist="Sammy Virji", title="Damager (Hamdi Edit)"
    )

    assert result["artist"] == "Sammy Virji & Interplanetary Criminal"


@pytest.mark.anyio
async def test_match_fields_rejects_unrelated_artist(monkeypatch):
    monkeypatch.setattr(
        mb_module.musicbrainzngs,
        "search_recordings",
        _returns(_recording("Yeah", _credit("Criminal Manne"), 100)),
    )
    result = await MusicBrainzMatchService().match_fields(
        artist="Interplanetary Criminal", title="Yeah Yeah (VIP Mix)"
    )

    assert result is None


@pytest.mark.anyio
async def test_match_fields_rejects_title_that_drops_typed_words(monkeypatch):
    monkeypatch.setattr(
        mb_module.musicbrainzngs,
        "search_recordings",
        _returns(_recording("Yeah", _credit("Interplanetary Criminal"), 100)),
    )
    # The DJ case: the VIP mix isn't in MusicBrainz, and collapsing it onto the unrelated
    # original would send the Soulseek downloader after the wrong track.
    result = await MusicBrainzMatchService().match_fields(
        artist="Interplanetary Criminal", title="Yeah Yeah (VIP Mix)"
    )

    assert result is None


@pytest.mark.anyio
async def test_match_fields_rejects_title_that_adds_words(monkeypatch):
    monkeypatch.setattr(
        mb_module.musicbrainzngs,
        "search_recordings",
        _returns(_recording("Abattoir Blues", _credit("Kahn"), 100)),
    )
    # A different recording, not a typo fix — so the title gate runs both directions.
    assert await MusicBrainzMatchService().match_fields(artist="Kahn", title="Abatoir") is None


@pytest.mark.anyio
async def test_match_fields_rejects_placeholder_metadata(monkeypatch):
    monkeypatch.setattr(
        mb_module.musicbrainzngs,
        "search_recordings",
        _returns(_recording("Probe", _credit("[no artist]"), 100, releases=[{"title": "Vaya Con Tioz"}])),
    )
    # Observed end-to-end on dev: a hand-typed item was rewritten to "[no artist] - Probe"
    # and logged as a success.
    result = await MusicBrainzMatchService().match_fields(
        artist="QA Scratch Artist 1784020563445", title="QA Wishlist Probe 1784020563445"
    )

    assert result is None


@pytest.mark.anyio
async def test_match_rejects_unrelated_top_scoring_freetext_result(monkeypatch):
    monkeypatch.setattr(
        mb_module.musicbrainzngs,
        "search_recordings",
        _returns(_recording("Title", _credit("Wandering Artist"), 100)),
    )
    # The nonsense query from #31: 35 results, all 91+. The old min_score gate passed this.
    assert await MusicBrainzMatchService().match("Zzqqxx Nonexistent Title 99999") is None


@pytest.mark.anyio
async def test_match_tolerates_noise_in_freetext_query(monkeypatch):
    monkeypatch.setattr(
        mb_module.musicbrainzngs,
        "search_recordings",
        _returns(_recording("Lite Spots", _credit("KAYTRANADA"), 100)),
    )
    # A raw video title carries tokens MusicBrainz will never hold, so free text is gated
    # candidate -> query: the match may not invent anything, but noise is forgiven.
    result = await MusicBrainzMatchService().match("Kaytranada - Lite Spots (Official Video) [HD]")

    assert result["title"] == "Lite Spots"


@pytest.mark.anyio
async def test_match_rejects_candidate_whose_artist_is_non_latin(monkeypatch):
    monkeypatch.setattr(
        mb_module.musicbrainzngs,
        "search_recordings",
        _returns(_recording("Aphex Twin", _credit("МОРЭ&РЭЛЬСЫ"), 100)),
    )
    # Live MusicBrainz really returns this for "Aphex Twin Windowlicker" — a track *named*
    # "Aphex Twin" by an unrelated Russian band. Tokenising to an a-z0-9 allowlist deleted
    # the Cyrillic artist entirely, leaving nothing to fail the gate.
    assert await MusicBrainzMatchService().match("Aphex Twin Windowlicker [HD]") is None


@pytest.mark.anyio
async def test_match_fields_matches_non_latin_artist(monkeypatch):
    monkeypatch.setattr(
        mb_module.musicbrainzngs,
        "search_recordings",
        _returns(_recording("Тишина", _credit("МОРЭ&РЭЛЬСЫ"), 100)),
    )
    # The flip side: a non-Latin artist must still be matchable on their own name.
    result = await MusicBrainzMatchService().match_fields(artist="МОРЭ РЭЛЬСЫ", title="Тишина")

    assert result["artist"] == "МОРЭ&РЭЛЬСЫ"


@pytest.mark.anyio
async def test_match_fields_ignores_accents_and_case(monkeypatch):
    monkeypatch.setattr(
        mb_module.musicbrainzngs,
        "search_recordings",
        _returns(_recording("Déjà Vu", _credit("Beyoncé"), 100)),
    )
    result = await MusicBrainzMatchService().match_fields(artist="beyonce", title="Deja Vu")

    assert (result["artist"], result["similarity"]) == ("Beyoncé", 100.0)


@pytest.mark.anyio
async def test_match_fields_ext_score_cannot_promote_a_rejected_candidate(monkeypatch):
    monkeypatch.setattr(
        mb_module.musicbrainzngs,
        "search_recordings",
        _returns(
            _recording("Abattoir Blues", _credit("Kahn"), 100),
            _recording("Abattoir", _credit("Kahn"), 80),
        ),
    )
    # #31 in miniature: MusicBrainz ranks the wrong recording top. ext:score is applied
    # only *underneath* the similarity gate, so the top hit can't win by out-scoring.
    result = await MusicBrainzMatchService().match_fields(artist="Kahn", title="Abatoir")

    assert result["title"] == "Abattoir"
    assert result["score"] == 80


@pytest.mark.anyio
async def test_match_fields_breaks_similarity_ties_on_ext_score(monkeypatch):
    monkeypatch.setattr(
        mb_module.musicbrainzngs,
        "search_recordings",
        _returns(
            _recording("Abattoir", _credit("Kahn & Neek"), 100),
            _recording("Abattoir", _credit("Kahn"), 80),
        ),
    )
    # Both are equally similar to "Kahn" — the artist gate allows extra credited artists,
    # so it can't separate them. Ranking within one result set is the one thing ext:score
    # is good for, so it decides here.
    result = await MusicBrainzMatchService().match_fields(artist="Kahn", title="Abatoir")

    assert result["artist"] == "Kahn & Neek"
