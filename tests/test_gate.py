import numpy as np
import pytest

from coeus.actions.engine import ActionResult
from coeus.gate import GateConfig, decide
from coeus.llm.providers import Identification
from coeus.models import Camera, GateCheck, Person
from coeus.vision.faces import DetectedFace, Match, normalise

AKASH = normalise(np.random.default_rng(1).normal(size=128).astype(np.float32))
STRANGER = normalise(np.random.default_rng(9).normal(size=128).astype(np.float32))


# --- decide(): pure decision table ------------------------------------------------

def m(pid, name, score=0.6):
    return Match(pid, name, score)


@pytest.mark.parametrize("method,allowed,local,llm,expect", [
    ("local", None, m(1, "Akash"), None, "opened"),
    ("local", {2}, m(1, "Akash"), None, "denied"),
    ("local", None, Match(None, "unknown", .1), None, None),
    ("llm", None, None, (1, "Akash", .9), "opened"),
    ("llm", None, None, (1, "Akash", .5), None),          # below min confidence
    ("local+llm", None, Match(None, "unknown", .1), (1, "Akash", .9), "opened"),
    ("local+llm", None, m(1, "Akash"), None, "opened"),   # no LLM needed
    ("both", None, m(1, "Akash"), (1, "Akash", .9), "opened"),
    ("both", None, m(1, "Akash"), (2, "Riya", .9), "no_match"),
    ("both", None, m(1, "Akash"), None, None),            # waits for the LLM
])
def test_decide(method, allowed, local, llm, expect):
    v = decide(method, allowed, local, llm, 0.75)
    assert (v.outcome if v else None) == expect


# --- GateController.check with fakes ------------------------------------------------

class Backend:
    name = "fake"

    def __init__(self, emb):
        self.emb = emb

    def detect(self, img):
        return [] if self.emb is None else [DetectedFace((10, 10, 40, 40), .9, np.zeros(15))]

    def embed(self, img, face):
        return self.emb


class LLM:
    name, model = "fake", "fake"

    def __init__(self, name, conf):
        self.answer, self.calls = Identification(name, conf, "looks like them"), 0

    def identify(self, face, refs):
        self.calls += 1
        return self.answer


@pytest.fixture
def gate_env(settings, db, monkeypatch):
    from coeus.services import Runtime
    db.add_all([Person(name="Akash"), Person(name="Riya"), Camera(name="Main Gate")])
    db.commit()
    rt = Runtime(settings)
    rt.gallery.load([(1, "Akash", AKASH.tobytes())])
    ctl = rt.gate
    frame = np.full((120, 160, 3), 80, np.uint8)
    monkeypatch.setattr(ctl, "_frame_source", lambda cam_id: (lambda: frame, lambda: None))
    opened = []
    monkeypatch.setattr(ctl, "open_gate",
                        lambda cfg, person, conf: opened.append(person) or ActionResult(True, "ok"))
    state = {"rt": rt, "ctl": ctl, "opened": opened}

    def setup(emb, **cfg):
        monkeypatch.setattr(rt, "face_backend", lambda: Backend(emb))
        base = dict(enabled=True, camera_id=1, window_s=0.5, interval_s=0.01, cooldown_s=0)
        base.update(cfg)
        ctl.save(GateConfig(**base))
    state["setup"] = setup
    return state


def test_known_person_opens_gate_and_is_logged(gate_env, db):
    gate_env["setup"](AKASH)
    res = gate_env["ctl"].check("webhook")
    assert res["outcome"] == "opened" and res["opened"] and res["person"] == "Akash"
    assert gate_env["opened"] == ["Akash"]
    row = db.query(GateCheck).one()
    assert row.trigger == "webhook" and row.source == "on-device" and row.snapshot_path


def test_not_allowed_and_stranger_and_empty(gate_env):
    gate_env["setup"](AKASH, person_ids=[2])
    assert gate_env["ctl"].check("manual")["outcome"] == "denied"
    gate_env["setup"](STRANGER)
    assert gate_env["ctl"].check("manual")["outcome"] == "no_match"
    gate_env["setup"](None)
    assert gate_env["ctl"].check("manual")["outcome"] == "no_face"
    assert gate_env["opened"] == []


def test_llm_identifies_from_people_list(gate_env, monkeypatch):
    llm = LLM("Riya", 0.88)
    monkeypatch.setattr(gate_env["rt"], "llm", lambda: llm)
    gate_env["setup"](STRANGER, method="local+llm", max_llm_calls=2)
    res = gate_env["ctl"].check("webhook")
    assert (res["outcome"], res["person"], res["source"]) == ("opened", "Riya", "llm")
    assert res["confidence"] == 0.88 and llm.calls == 1


def test_llm_low_confidence_and_call_budget(gate_env, monkeypatch):
    llm = LLM("Riya", 0.5)
    monkeypatch.setattr(gate_env["rt"], "llm", lambda: llm)
    gate_env["setup"](None, method="llm", llm_image="frame", max_llm_calls=2)
    assert gate_env["ctl"].check("webhook")["outcome"] == "no_face"
    assert llm.calls == 2  # never more than the budget


def test_both_requires_agreement(gate_env, monkeypatch):
    monkeypatch.setattr(gate_env["rt"], "llm", lambda: LLM("Riya", 0.95))
    gate_env["setup"](AKASH, method="both", max_llm_calls=1)
    res = gate_env["ctl"].check("manual")
    assert res["outcome"] == "no_match" and "LLM says Riya" in res["detail"]


def test_cooldown_and_disabled(gate_env):
    gate_env["setup"](AKASH, cooldown_s=60)
    assert gate_env["ctl"].check("webhook")["outcome"] == "opened"
    assert gate_env["ctl"].check("webhook")["outcome"] == "cooldown"
    gate_env["setup"](AKASH, enabled=False)
    assert gate_env["ctl"].check("webhook")["outcome"] == "disabled"
