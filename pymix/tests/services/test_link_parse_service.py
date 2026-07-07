import pytest

from pymix.services.link_parse_service import (
    LinkParseService,
    _is_youtube_auto_mix,
    _split_artist_title,
    _strip_noise,
    detect_link_source,
)


class _StubMusicBrainz:
    """Records queries and returns a canned match (or None) for the given query."""

    def __init__(self, result=None):
        self._result = result
        self.queries = []

    async def match(self, query):
        self.queries.append(query)
        return self._result


def _service(mb=None):
    return LinkParseService(mb or _StubMusicBrainz())


def test_detect_link_source():
    assert detect_link_source("https://www.youtube.com/watch?v=abc") == "youtube"
    assert detect_link_source("https://youtu.be/abc") == "youtube"
    assert detect_link_source("https://artist.bandcamp.com/track/x") == "bandcamp"
    assert detect_link_source("https://soundcloud.com/forss/flickermood") == "soundcloud"
    assert detect_link_source("https://m.soundcloud.com/forss/flickermood") == "soundcloud"
    assert detect_link_source("https://open.spotify.com/track/x") is None


def test_is_youtube_auto_mix_true_for_rd_list_with_video():
    # watch?v=X&list=RD... — a link copied while a YouTube Mix auto-plays.
    assert _is_youtube_auto_mix(
        "https://www.youtube.com/watch?v=2g7CjPjrKNk&list=RDAnwiOdtLF3E&index=4"
    )


def test_is_youtube_auto_mix_false_for_real_playlist():
    # A real user playlist (PL...) should still expand.
    assert not _is_youtube_auto_mix(
        "https://www.youtube.com/watch?v=abc&list=PL1234567890"
    )
    # Auto-generated album release playlist (OLAK5uy_) is not a Mix either.
    assert not _is_youtube_auto_mix(
        "https://www.youtube.com/watch?v=abc&list=OLAK5uy_1234"
    )


def test_is_youtube_auto_mix_false_without_video_id():
    # A bare mix playlist URL has no single video to fall back to.
    assert not _is_youtube_auto_mix("https://www.youtube.com/playlist?list=RDAnwiOdtLF3E")


def test_is_youtube_auto_mix_false_for_plain_video_and_non_youtube():
    assert not _is_youtube_auto_mix("https://www.youtube.com/watch?v=abc")
    assert not _is_youtube_auto_mix("https://soundcloud.com/x/y?list=RDabc")


def test_track_from_info_soundcloud_sets_soundcloud_url():
    svc = _service()
    info = {
        "track": "Flickermood",
        "artist": "Forss",
        "title": "Flickermood",
        "uploader": "Forss",
        "webpage_url": "https://soundcloud.com/forss/flickermood",
        "id": "293",
    }
    result = svc._track_from_info(info, "soundcloud", fallback_url="http://x")
    assert result["artist"] == "Forss"
    assert result["title"] == "Flickermood"
    assert result["soundcloud_url"] == "https://soundcloud.com/forss/flickermood"
    assert result["youtube_url"] is None
    assert result["bandcamp_url"] is None


def test_split_artist_title_basic():
    assert _split_artist_title("Kaytranada - Lite spots") == ("Kaytranada", "Lite spots")


def test_split_artist_title_en_and_em_dash():
    assert _split_artist_title("Artist – Song") == ("Artist", "Song")
    assert _split_artist_title("Artist — Song") == ("Artist", "Song")


def test_split_artist_title_first_separator_only():
    assert _split_artist_title("Artist - Song - Remix") == ("Artist", "Song - Remix")


def test_split_artist_title_strips_trailing_noise():
    assert _split_artist_title("Kaytranada - Lite spots (Official Video)") == (
        "Kaytranada",
        "Lite spots",
    )
    assert _split_artist_title("Artist - Song [HD]") == ("Artist", "Song")


def test_split_artist_title_keeps_meaningful_parenthetical():
    assert _split_artist_title("Artist - Song (Live)") == ("Artist", "Song (Live)")
    assert _split_artist_title("Artist - Song (Acoustic)") == ("Artist", "Song (Acoustic)")


def test_split_artist_title_no_separator():
    assert _split_artist_title("Just A Title") == (None, "Just A Title")


def test_strip_noise_removes_only_trailing_group():
    assert _strip_noise("Lite spots (Official Video)") == "Lite spots"


def test_strip_noise_never_blanks_an_all_noise_title():
    # A title that is *only* a descriptor is kept verbatim rather than emptied.
    assert _strip_noise("[HD]") == "[HD]"
    assert _strip_noise("(Official Video)") == "(Official Video)"


def test_split_artist_title_all_noise_falls_back_to_raw():
    # No usable name survives stripping -> keep the original rather than blank it.
    assert _split_artist_title("(Official Video)") == (None, "(Official Video)")


def test_split_artist_title_noise_only_title_is_preserved():
    # e.g. a (contrived) "Artist - [HD]" keeps the bracket as the title rather
    # than producing an empty title.
    assert _split_artist_title("Artist - [HD]") == ("Artist", "[HD]")


def test_track_from_info_uses_structured_metadata_when_present():
    svc = _service()
    info = {
        "track": "Lite Spots",
        "artist": "Kaytranada",
        "title": "Kaytranada - Lite Spots (Official Video)",
        "uploader": "KaytranadaVEVO",
        "id": "abc",
    }
    result = svc._track_from_info(info, "youtube", fallback_url="http://x")
    assert result["artist"] == "Kaytranada"
    assert result["title"] == "Lite Spots"


def test_track_from_info_parses_title_when_metadata_missing():
    svc = _service()
    info = {
        "track": None,
        "artist": None,
        "title": "Kaytranada - Lite spots",
        "uploader": "Renard Hugo",
        "id": "KzMrQ-Lkxbo",
    }
    result = svc._track_from_info(info, "youtube", fallback_url="http://x")
    assert result["artist"] == "Kaytranada"
    assert result["title"] == "Lite spots"


def test_track_from_info_falls_back_to_uploader_without_separator():
    svc = _service()
    info = {
        "track": None,
        "artist": None,
        "title": "My DJ Set 2024",
        "uploader": "Some Channel",
        "id": "xyz",
    }
    result = svc._track_from_info(info, "youtube", fallback_url="http://x")
    assert result["artist"] == "Some Channel"
    assert result["title"] == "My DJ Set 2024"


def test_track_from_info_marks_structured_source():
    svc = _service()
    info = {"track": "Lite Spots", "artist": "Kaytranada", "title": "x", "id": "abc"}
    result = svc._track_from_info(info, "youtube", fallback_url="http://x")
    assert result["match_source"] == "structured"
    assert result["confidence"] is None


def test_track_from_info_marks_string_source():
    svc = _service()
    info = {"track": None, "artist": None, "title": "Kaytranada - Lite spots", "id": "abc"}
    result = svc._track_from_info(info, "youtube", fallback_url="http://x")
    assert result["match_source"] == "string"


@pytest.mark.anyio
async def test_refine_upgrades_string_guess_with_musicbrainz():
    mb = _StubMusicBrainz(
        {"artist": "KAYTRANADA", "title": "Lite Spots", "album": "99.9%", "score": 100}
    )
    svc = _service(mb)
    info = {"track": None, "artist": None, "title": "kaytranada lite spots (Official Audio)", "id": "abc"}
    track = svc._track_from_info(info, "youtube", fallback_url="http://x")

    refined = await svc._refine_with_musicbrainz(track, info)

    assert refined["artist"] == "KAYTRANADA"
    assert refined["title"] == "Lite Spots"
    assert refined["album"] == "99.9%"
    assert refined["match_source"] == "musicbrainz"
    assert refined["confidence"] == 100
    # MusicBrainz is queried with the noise-stripped raw video title, not the guessed artist.
    assert mb.queries == ["kaytranada lite spots"]


@pytest.mark.anyio
async def test_refine_is_noop_for_structured_metadata():
    mb = _StubMusicBrainz({"artist": "Wrong", "title": "Wrong", "album": None, "score": 100})
    svc = _service(mb)
    info = {"track": "Flickermood", "artist": "Forss", "title": "x", "id": "abc"}
    track = svc._track_from_info(info, "youtube", fallback_url="http://x")

    refined = await svc._refine_with_musicbrainz(track, info)

    assert refined["artist"] == "Forss"
    assert refined["title"] == "Flickermood"
    assert refined["match_source"] == "structured"
    assert mb.queries == []  # never called


@pytest.mark.anyio
async def test_refine_keeps_string_guess_when_no_match():
    svc = _service(_StubMusicBrainz(None))
    info = {"track": None, "artist": None, "title": "Kaytranada - Lite spots", "id": "abc"}
    track = svc._track_from_info(info, "youtube", fallback_url="http://x")

    refined = await svc._refine_with_musicbrainz(track, info)

    assert refined["artist"] == "Kaytranada"
    assert refined["title"] == "Lite spots"
    assert refined["match_source"] == "string"
    assert refined["confidence"] is None


@pytest.mark.anyio
async def test_refine_does_not_overwrite_existing_album():
    mb = _StubMusicBrainz({"artist": "A", "title": "T", "album": "MB Album", "score": 95})
    svc = _service(mb)
    info = {"track": None, "artist": None, "title": "A - T", "album": "Original Album", "id": "abc"}
    track = svc._track_from_info(info, "youtube", fallback_url="http://x")

    refined = await svc._refine_with_musicbrainz(track, info)

    assert refined["album"] == "Original Album"
