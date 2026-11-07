import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from coeus.actions.engine import (ActionEngine, EventContext, RuleView, in_time_window, render,
                                  rule_matches, run_homeassistant, run_webhook)

TZ = ZoneInfo("Asia/Kolkata")


def ev(**kw):
    base = dict(event_id=1, event_type="face_known", camera_id=2, camera="Main Gate",
                zone="gate", person_id=5, person="Akash", confidence=0.71,
                time=datetime(2025, 5, 16, 22, 30, tzinfo=TZ))
    base.update(kw)
    return EventContext(**base)


def rule(**kw):
    base = dict(id=1, name="r", enabled=True, event_types=[], camera_ids=[], zone_names=[],
                person_ids=[], time_start="", time_end="", days=[], cooldown_s=0, actions=[])
    base.update(kw)
    return RuleView(**base)


def test_filters():
    assert rule_matches(rule(), ev())
    assert not rule_matches(rule(enabled=False), ev())
    assert not rule_matches(rule(event_types=["motion"]), ev())
    assert rule_matches(rule(camera_ids=[2], zone_names=["gate"], person_ids=[5]), ev())
    assert not rule_matches(rule(person_ids=[6]), ev())
    assert not rule_matches(rule(days=[0]), ev())  # 2025-05-16 is a Friday (4)
    assert rule_matches(rule(days=[4]), ev())


def test_time_windows_including_midnight_wrap():
    t = datetime(2025, 1, 1, 23, 0)
    assert in_time_window(t, "22:00", "06:00")
    assert in_time_window(t.replace(hour=5), "22:00", "06:00")
    assert not in_time_window(t.replace(hour=12), "22:00", "06:00")
    assert in_time_window(t.replace(hour=12), "09:00", "17:00")
    assert in_time_window(t, "", "")


def test_cooldown():
    now = datetime.now(timezone.utc)
    r = rule(cooldown_s=60, last_fired_at=now - timedelta(seconds=30))
    assert not rule_matches(r, ev(), now)
    r.last_fired_at = now - timedelta(seconds=90)
    assert rule_matches(r, ev(), now)


def test_render_keeps_unknown_placeholders():
    assert render("{person} at {camera} {nope}", ev().vars()) == "Akash at Main Gate {nope}"
    assert render("bad {", {}) == "bad {"


def test_webhook_renders_json_body():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"], seen["body"], seen["auth"] = str(req.url), json.loads(req.content), req.headers.get("x-key")
        return httpx.Response(200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    action = {"type": "webhook", "url": "http://n8n.local/hook/{camera_id}",
              "headers": {"X-Key": "abc"},
              "body": '{"who": "{person}", "conf": "{confidence}"}'}
    res = run_webhook(action, ev().vars(), client)
    assert res.ok
    assert seen == {"url": "http://n8n.local/hook/2", "body": {"who": "Akash", "conf": "0.71"},
                    "auth": "abc"}


def test_homeassistant_service_and_event():
    calls = []

    def handler(req):
        calls.append((req.url.path, req.headers["authorization"], json.loads(req.content)))
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    svc = {"type": "homeassistant", "service": "notify.mobile_app_s921b",
           "data": {"message": "{person} at {zone}"}}
    assert run_homeassistant(svc, ev().vars(), client, "http://ha:8123/", "tok").ok
    assert run_homeassistant({"mode": "event"}, ev().vars(), client, "http://ha:8123", "tok").ok
    assert calls[0] == ("/api/services/notify/mobile_app_s921b", "Bearer tok",
                        {"message": "Akash at gate"})
    assert calls[1][0] == "/api/events/coeus_event" and calls[1][2]["person"] == "Akash"
    assert not run_homeassistant(svc, ev().vars(), client, "", "").ok


def test_engine_runs_describe_before_webhook(monkeypatch):
    bodies, logged = [], []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(204)

    real_client = httpx.Client

    def fake_client(**kw):
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "Client", fake_client)
    r = rule(actions=[{"type": "llm_describe"},
                      {"type": "webhook", "url": "http://x/", "body": {"d": "{description}"}}])
    eng = ActionEngine(lambda: [r], lambda *a: None, lambda *a: logged.append(a), lambda: {},
                       describe=lambda e: "A person at the gate")
    results = eng._handle(ev())
    assert [x.ok for x in results] == [True, True]
    assert bodies == [{"d": "A person at the gate"}]
    assert len(logged) == 2
