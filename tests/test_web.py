import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_segment


@pytest.fixture
def client(settings, recordings):
    from coeus.security import throttle
    throttle._fails.clear()
    make_segment(recordings, "Main Gate", datetime(2025, 5, 16, 12, 27, 34))
    from coeus.main import create_app
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def csrf(client, path="/login"):
    html = client.get(path).text
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client, password="secret-pass"):
    tok = csrf(client)
    return client.post("/login", data={"username": "admin", "password": password,
                                       "csrf_token": tok, "next": "/"}, follow_redirects=False)


def test_redirects_to_login_when_anonymous(client):
    r = client.get("/cameras", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.get("/api/status").status_code == 401
    assert client.get("/healthz").json() == {"ok": True}


def test_login_logout_and_bad_password(client):
    assert "Invalid username or password" in login(client, "wrong").text
    r = login(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "Dashboard" in client.get("/").text
    tok = csrf(client, "/")
    client.post("/logout", data={"csrf_token": tok})
    assert client.get("/", follow_redirects=False).status_code == 303


def test_lockout_after_failures(client):
    for _ in range(5):
        login(client, "nope")
    assert "Too many failed attempts" in login(client).text


def test_post_without_csrf_rejected(client):
    login(client)
    assert client.post("/persons", data={"name": "X"}).status_code == 403


def test_all_pages_render(client):
    login(client)
    for path in ["/", "/cameras", "/cameras/new", "/recordings", "/recordings/Main Gate",
                 "/jobs", "/persons", "/rules", "/rules/new", "/events", "/settings", "/users",
                 "/youtube", "/youtube/new", "/library", "/library?path=Main Gate"]:
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:300])


def add_account(name="Home"):
    from coeus.db import session_scope
    from coeus.models import YouTubeAccount
    with session_scope() as db:
        a = YouTubeAccount(name=name, client_config={"installed": {"client_id": "c1"}},
                           credentials={"token": "x"})
        db.add(a)
        db.flush()
        return a.id


CAM = {"name": "Main Gate", "recording_folder": "Main Gate", "rtsp_url": "",
       "archive_enabled": "on", "yt_privacy": "unlisted", "yt_title_template": "{camera} {date}",
       "face_mode": "local", "detect_fps": "5", "cooldown_s": "30"}


def test_archiving_requires_an_account(client):
    login(client)
    tok = csrf(client, "/cameras/new")
    r = client.post("/cameras/save", data={"csrf_token": tok, **CAM}, follow_redirects=False)
    assert r.headers["location"] == "/youtube/new?next=/cameras/1/edit"  # none exist: create
    page = client.get("/youtube/new?next=/cameras/1/edit").text
    assert "there is no YouTube account yet" in page and 'value="/cameras/1/edit"' in page
    aid = add_account()
    r = client.post("/cameras/save", data={"csrf_token": tok, "id": "1", **CAM})
    assert "choose which YouTube account" in r.text  # exists but not chosen
    r = client.post("/cameras/save", data={"csrf_token": tok, "id": "1", **CAM,
                                           "yt_account_id": str(aid)})
    assert "Saved camera Main Gate" in r.text


def test_recordings_quick_add_asks_for_account(client):
    login(client)
    tok = csrf(client, "/recordings")
    assert "Add a YouTube account" in client.get("/recordings").text
    aid = add_account()
    page = client.get("/recordings").text
    assert "Upload to which YouTube account?" in page
    r = client.post("/recordings/Main Gate/add", data={"csrf_token": tok})
    assert "Choose which YouTube account" in r.text
    r = client.post("/recordings/Main Gate/add", data={"csrf_token": tok,
                                                       "yt_account_id": str(aid)})
    assert "archived daily to Home" in r.text


def test_account_connect_and_test_flow(client, monkeypatch):
    from coeus.youtube import uploader as yt
    monkeypatch.setattr(yt, "finish_authorization",
                        lambda cfg, verifier, pasted: {"token": "t", "refresh_token": "r"})
    monkeypatch.setattr(yt, "test_credentials", lambda creds, cb=None: {
        "ok": True, "detail": "Connected to channel “Xenos Cams”", "channel_title": "Xenos Cams",
        "channel_id": "UC1"})
    login(client)
    tok = csrf(client, "/youtube/new")
    cj = ('{"installed": {"client_id": "abc.apps.googleusercontent.com", "client_secret": "s",'
          ' "auth_uri": "https://accounts.google.com/o/oauth2/auth",'
          ' "token_uri": "https://oauth2.googleapis.com/token"}}')
    r = client.post("/youtube/new", data={"csrf_token": tok, "name": "Home", "client_json": cj,
                                          "next": "/cameras/new?folder=Main Gate"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/youtube/1/connect?next=/cameras/new%3Ffolder")
    page = client.get(r.headers["location"]).text
    assert "accounts.google.com" in page
    r = client.post("/youtube/1/connect", data={"csrf_token": tok, "next": "/cameras/new?folder=Main Gate",
                                                "redirect_url": "http://localhost:1/?code=4/x"},
                    follow_redirects=False)
    assert r.headers["location"] == "/cameras/new?folder=Main%20Gate"
    assert "Xenos Cams" in client.get("/youtube").text
    j = client.post("/youtube/1/test", headers={"X-CSRF-Token": tok,
                                               "Accept": "application/json"}).json()
    assert j["ok"] and j["quota"] == "0/6 uploads today"
    # second account can reuse the first one's OAuth client (shared quota)
    r = client.post("/youtube/new", data={"csrf_token": tok, "name": "Office",
                                          "reuse_from": "1"}, follow_redirects=False)
    assert r.headers["location"].startswith("/youtube/2/connect")


def test_library_picker(client, settings):
    login(client)
    page = client.get("/library").text
    assert "Detected automatically" in page and "Main Gate" in client.get("/library?path=").text
    tok = csrf(client, "/library")
    r = client.post("/library/select", data={"csrf_token": tok, "path": "Main Gate"})
    assert "single camera folder" in r.text  # picked a camera: parent (the mount) is used
    r = client.post("/library/select", data={"csrf_token": tok, "path": "../.."})
    assert "outside the mounted" in r.text


def test_camera_rule_and_settings_flow(client):
    login(client)
    tok = csrf(client, "/cameras/new")
    aid = add_account()
    r = client.post("/cameras/save", data={"csrf_token": tok, **CAM, "yt_account_id": str(aid)})
    assert r.status_code == 200 and "Saved camera Main Gate" in r.text
    assert "create camera" not in client.get("/recordings").text

    r = client.post("/cameras/1/zones", json={"zones": [
        {"name": "gate", "kind": "include", "points": [[0, 0], [0.5, 0], [0.5, 0.5]],
         "min_motion_ratio": 0.02}, {"name": "bad", "points": [[0, 0]]}]},
        headers={"X-CSRF-Token": tok})
    assert r.json() == {"ok": True, "count": 1}

    r = client.post("/persons", data={"csrf_token": tok, "name": "Akash"})
    assert "Akash" in r.text

    r = client.post("/rules/save", data={
        "csrf_token": tok, "name": "Known at gate", "enabled": "on",
        "event_types": ["face_known"], "camera_ids": ["1"], "zone_names": ["gate"],
        "person_ids": ["1"], "days": ["0", "6"], "time_start": "22:00", "time_end": "06:00",
        "cooldown_s": "60", "actions": '[{"type": "webhook", "url": "http://127.0.0.1:9/x"}]'})
    assert "Saved rule Known at gate" in r.text
    r = client.post("/rules/save", data={"csrf_token": tok, "name": "Bad", "actions": "{nope"})
    assert "Invalid actions JSON" in r.text

    r = client.post("/settings", data={"csrf_token": tok, "archive__schedule_time": "02:15",
                                       "llm__provider": "anthropic", "llm__api_key": "sk-ant-123456789",
                                       "faces__threshold": "0.4"})
    assert "Settings saved" in r.text
    page = client.get("/settings").text
    assert "sk-ant-123456789" not in page and "6789" in page
    assert 'value="02:15"' in page


def test_archive_queue_from_recordings(client, settings):
    login(client)
    tok = csrf(client, "/recordings/Main Gate")
    aid = add_account()
    client.post("/recordings/Main Gate/add", data={"csrf_token": tok, "yt_account_id": str(aid)})
    r = client.post("/recordings/Main Gate/2025-05-16/archive", data={"csrf_token": tok})
    assert r.status_code == 200 and "Queued 1 part(s)" in r.text
    assert "Main Gate" in client.get("/jobs").text
