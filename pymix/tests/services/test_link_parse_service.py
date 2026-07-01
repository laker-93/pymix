from pymix.services.link_parse_service import (
    LinkParseService,
    _split_artist_title,
    _strip_noise,
    detect_link_source,
)


def test_detect_link_source():
    assert detect_link_source("https://www.youtube.com/watch?v=abc") == "youtube"
    assert detect_link_source("https://youtu.be/abc") == "youtube"
    assert detect_link_source("https://artist.bandcamp.com/track/x") == "bandcamp"
    assert detect_link_source("https://soundcloud.com/forss/flickermood") == "soundcloud"
    assert detect_link_source("https://m.soundcloud.com/forss/flickermood") == "soundcloud"
    assert detect_link_source("https://open.spotify.com/track/x") is None


def test_track_from_info_soundcloud_sets_soundcloud_url():
    svc = LinkParseService()
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
    svc = LinkParseService()
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
    svc = LinkParseService()
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
    svc = LinkParseService()
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
