from types import SimpleNamespace

import pytest

from coeus.llm.providers import (AnthropicProvider, HourlyLimiter, RateLimited,
                                 parse_identification)


def test_parse_identification():
    r = parse_identification('Sure: {"name": "akash", "confidence": 0.8, "reason": "beard"}',
                             ["Akash", "Riya"])
    assert (r.name, r.confidence) == ("Akash", 0.8)
    assert parse_identification('{"name": "unknown", "confidence": 0.9}', ["Akash"]).name is None
    assert parse_identification("no json", ["Akash"]).name is None


def test_limiter():
    lim = HourlyLimiter(2)
    lim.acquire()
    lim.acquire()
    with pytest.raises(RateLimited):
        lim.acquire()


class FakeMessages:
    def __init__(self, stop="end_turn"):
        self.calls, self.stop = [], stop

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason=self.stop,
                               content=[SimpleNamespace(type="text", text=" A man at the gate. ")])


def provider(stop="end_turn"):
    p = AnthropicProvider(api_key="sk-test")
    fake = FakeMessages(stop)
    p._client = SimpleNamespace(messages=fake, beta=SimpleNamespace(messages=fake))
    return p, fake


def test_anthropic_request_shape():
    p, fake = provider()
    assert p.describe(b"\xff\xd8jpeg", "Main Gate", "gate") == "A man at the gate."
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"] == {"effort": "low"}
    img = call["messages"][0]["content"][0]
    assert img["type"] == "image" and img["source"]["media_type"] == "image/jpeg"


def test_anthropic_identify_uses_schema_and_handles_refusal():
    p, fake = provider()
    fake.create = lambda **kw: (fake.calls.append(kw) or SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text='{"name":"Riya","confidence":0.7,"reason":"x"}')]))
    r = p.identify(b"face", [("Riya", b"ref1"), ("Akash", b"ref2")])
    assert r.name == "Riya"
    assert fake.calls[0]["output_config"]["format"]["type"] == "json_schema"
    p2, _ = provider(stop="refusal")
    with pytest.raises(RuntimeError, match="declined"):
        p2.describe(b"x")
