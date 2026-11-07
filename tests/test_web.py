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
                 "/jobs", "/persons", "/rules", "/rules/new", "/events", "/settings", "/users"]:
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:300])


def test_camera_rule_and_settings_flow(client):
    login(client)
    tok = csrf(client, "/cameras/new")
    r = client.post("/cameras/save", data={
        "csrf_token": tok, "name": "Main Gate", "recording_folder": "Main Gate",
        "rtsp_url": "", "archive_enabled": "on", "yt_privacy": "unlisted",
        "yt_title_template": "{camera} {date}", "face_mode": "local", "detect_fps": "5",
        "cooldown_s": "30"})
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
    r = client.post("/recordings/Main Gate/2025-05-16/archive", data={"csrf_token": tok})
    assert r.status_code == 200 and "Queued 1 part(s)" in r.text
    assert "Main Gate" in client.get("/jobs").text
