import pytest

from coeus.recordings.merge import fmt_ts
from coeus.youtube import uploader as yt


def test_extract_code():
    assert yt.extract_code("http://localhost:1/?state=x&code=4/abc&scope=y") == "4/abc"
    assert yt.extract_code("  4/raw ") == "4/raw"
    with pytest.raises(ValueError):
        yt.extract_code("http://localhost:1/?error=access_denied")


def test_title_template():
    t = yt.render_title("{camera} — {date} (part {part}/{parts}) <x>", camera="Main Gate",
                        date="2025-05-16", part=1, parts=2)
    assert t == "Main Gate — 2025-05-16 (part 1/2) x"
    assert yt.render_title("{oops}", camera="C", date="D", part=1) == "C D part 1"
    assert len(yt.render_title("x" * 300)) == 100


def test_description_chapters():
    chapters = [(0, "00:27:34"), (1800, "00:57:34"), (3600, "01:27:34")]
    d = yt.build_description("Main Gate", "2025-05-16", 1, 2, chapters,
                             [["2025-05-16T03:00:00", 900]], fmt_ts)
    assert "00:00 00:27:34" in d and "30:00 00:57:34" in d and "1:00:00 01:27:34" in d
    assert "15 min missing" in d
    short = yt.build_description("C", "D", 1, 1, chapters[:2], [], fmt_ts)
    assert "Chapters" not in short  # YouTube needs at least 3


def test_client_config_validation():
    with pytest.raises(ValueError):
        yt.normalise_client_config('{"foo": 1}')
    assert "installed" in yt.normalise_client_config('{"installed": {"client_id": "x"}}')


def test_authorization_url_has_pkce_and_offline():
    cfg = {"installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": "s",
                         "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                         "token_uri": "https://oauth2.googleapis.com/token",
                         "redirect_uris": ["http://localhost"]}}
    url, state, verifier = yt.start_authorization(cfg)
    assert "access_type=offline" in url and "code_challenge=" in url and verifier
