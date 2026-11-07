"""Vision-LLM providers used to describe snapshots and (optionally) identify faces."""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

DESCRIBE_PROMPT = (
    "You are looking at a frame from a home security camera ({camera}, zone {zone}). "
    "Describe in one short sentence who or what is present and what they are doing. "
    "Do not speculate about identity."
)
IDENTIFY_PROMPT = (
    "The first image comes from a home security camera: either a close crop of a face or the "
    "whole camera picture. The following images are reference photos of known people, each "
    "preceded by their name. Decide whether the person in the camera image is one of these "
    "people. Use only the names given. If nobody is visible, the face is unclear, or it does "
    "not match anyone, answer \"unknown\". Confidence is your probability (0 to 1) that the "
    "named person is correct; be conservative, because this decides whether a gate opens. "
    'Answer with JSON only: {"name": "<exact name or unknown>", "confidence": <0..1>, '
    '"reason": "<one short sentence>"}'
)
IDENTIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["name", "confidence", "reason"],
    "additionalProperties": False,
}

# Models that accept server-side refusal fallbacks ("default" routing).
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
# Models that accept output_config.effort.
_EFFORT_MODELS = _FALLBACK_MODELS | {"claude-fable-5", "claude-opus-4-8", "claude-opus-4-7",
                                     "claude-opus-4-6", "claude-sonnet-5", "claude-sonnet-4-6",
                                     "claude-haiku-5-5"}


@dataclass
class Identification:
    name: str | None
    confidence: float
    reason: str = ""


class RateLimited(Exception):
    pass


class HourlyLimiter:
    def __init__(self, per_hour: int):
        self.per_hour = per_hour
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        now = time.monotonic()
        with self._lock:
            while self._calls and now - self._calls[0] > 3600:
                self._calls.popleft()
            if self.per_hour > 0 and len(self._calls) >= self.per_hour:
                raise RateLimited(f"LLM limit of {self.per_hour} calls/hour reached")
            self._calls.append(now)


def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode()


def parse_identification(text: str, known_names: list[str]) -> Identification:
    m = re.search(r"\{.*\}", text, re.S)
    try:
        obj = json.loads(m.group(0) if m else text)
    except (json.JSONDecodeError, AttributeError):
        return Identification(None, 0.0, f"unparseable reply: {text[:200]}")
    name = str(obj.get("name", "")).strip()
    lookup = {n.lower(): n for n in known_names}
    canonical = lookup.get(name.lower())
    try:
        conf = float(obj.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    return Identification(canonical, conf if canonical else 0.0, str(obj.get("reason", "")))


class LLMProvider:
    name = "base"

    def __init__(self, model: str, api_key: str = "", base_url: str = "", per_hour: int = 60):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.limiter = HourlyLimiter(per_hour)

    def describe(self, image_jpeg: bytes, camera: str = "", zone: str = "") -> str:
        raise NotImplementedError

    def identify(self, face_jpeg: bytes, references: list[tuple[str, bytes]]) -> Identification:
        raise NotImplementedError


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    DEFAULT_MODEL = "claude-opus-5-5"

    def __init__(self, model: str = "", api_key: str = "", base_url: str = "", per_hour: int = 60):
        super().__init__(model or self.DEFAULT_MODEL, api_key, base_url, per_hour)
        import anthropic
        kwargs = {"api_key": api_key or None, "max_retries": 2, "timeout": 120.0}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = anthropic.Anthropic(**kwargs)

    def _create(self, content: list[dict], max_tokens: int, schema: dict | None = None):
        self.limiter.acquire()
        params: dict = {"model": self.model, "max_tokens": max_tokens,
                        "messages": [{"role": "user", "content": content}]}
        output_config: dict = {}
        if self.model in _EFFORT_MODELS:
            output_config["effort"] = "low"
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            params["output_config"] = output_config
        if self.model in _FALLBACK_MODELS and not self.base_url:
            resp = self._client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
        else:
            resp = self._client.messages.create(**params)
        if resp.stop_reason == "refusal":
            raise RuntimeError("the model declined this request")
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    @staticmethod
    def _image(data: bytes) -> dict:
        return {"type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64(data)}}

    def describe(self, image_jpeg: bytes, camera: str = "", zone: str = "") -> str:
        return self._create([self._image(image_jpeg),
                             {"type": "text", "text": DESCRIBE_PROMPT.format(camera=camera,
                                                                             zone=zone)}], 1024)

    def identify(self, face_jpeg: bytes, references: list[tuple[str, bytes]]) -> Identification:
        content: list[dict] = [self._image(face_jpeg)]
        for name, img in references:
            content += [{"type": "text", "text": f"Reference: {name}"}, self._image(img)]
        content.append({"type": "text", "text": IDENTIFY_PROMPT})
        text = self._create(content, 1024, IDENTIFY_SCHEMA)
        return parse_identification(text, sorted({n for n, _ in references}))


class OpenAICompatibleProvider(LLMProvider):
    """OpenAI, Google Gemini (OpenAI endpoint), OpenRouter, Ollama, LM Studio, vLLM..."""
    name = "openai"

    def __init__(self, model: str = "", api_key: str = "", base_url: str = "", per_hour: int = 60):
        super().__init__(model or "gpt-4o-mini", api_key,
                         (base_url or "https://api.openai.com/v1").rstrip("/"), per_hour)

    @staticmethod
    def _image(data: bytes) -> dict:
        return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_b64(data)}"}}

    def _chat(self, content: list[dict], max_tokens: int) -> str:
        self.limiter.acquire()
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        resp = httpx.post(f"{self.base_url}/chat/completions", headers=headers, timeout=120,
                          json={"model": self.model, "max_tokens": max_tokens,
                                "messages": [{"role": "user", "content": content}]})
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()

    def describe(self, image_jpeg: bytes, camera: str = "", zone: str = "") -> str:
        return self._chat([{"type": "text", "text": DESCRIBE_PROMPT.format(camera=camera,
                                                                           zone=zone)},
                           self._image(image_jpeg)], 300)

    def identify(self, face_jpeg: bytes, references: list[tuple[str, bytes]]) -> Identification:
        content: list[dict] = [self._image(face_jpeg)]
        for name, img in references:
            content += [{"type": "text", "text": f"Reference: {name}"}, self._image(img)]
        content.append({"type": "text", "text": IDENTIFY_PROMPT})
        return parse_identification(self._chat(content, 300),
                                    sorted({n for n, _ in references}))


PROVIDERS = {"anthropic": AnthropicProvider, "openai": OpenAICompatibleProvider}

_cache: dict[tuple, LLMProvider] = {}
_cache_lock = threading.Lock()


def get_provider(provider: str, model: str, api_key: str, base_url: str,
                 per_hour: int) -> LLMProvider | None:
    if provider not in PROVIDERS:
        return None
    key = (provider, model, api_key, base_url, per_hour)
    with _cache_lock:
        if key not in _cache:
            _cache.clear()
            _cache[key] = PROVIDERS[provider](model, api_key, base_url, per_hour)
        return _cache[key]
