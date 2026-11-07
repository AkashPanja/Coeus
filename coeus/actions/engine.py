"""Rule matching and action execution (webhook, Home Assistant, LLM description)."""
from __future__ import annotations

import json
import logging
import string
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

EVENT_TYPES = ("motion", "face_known", "face_unknown")
ACTION_TYPES = ("webhook", "homeassistant", "llm_describe")


@dataclass
class EventContext:
    event_id: int
    event_type: str
    camera_id: int | None
    camera: str
    zone: str
    person_id: int | None
    person: str
    confidence: float
    time: datetime  # local, aware
    snapshot_url: str = ""
    snapshot_path: str = ""
    description: str = ""
    extra: dict = field(default_factory=dict)

    def vars(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "event_type": self.event_type, "camera": self.camera,
                "camera_id": self.camera_id or "", "zone": self.zone, "person": self.person,
                "person_id": self.person_id or "", "confidence": f"{self.confidence:.2f}",
                "time": self.time.strftime("%Y-%m-%d %H:%M:%S"), "iso_time": self.time.isoformat(),
                "snapshot_url": self.snapshot_url, "description": self.description}


@dataclass
class RuleView:
    id: int
    name: str
    enabled: bool
    event_types: list[str]
    camera_ids: list[int]
    zone_names: list[str]
    person_ids: list[int]
    time_start: str
    time_end: str
    days: list[int]
    cooldown_s: int
    actions: list[dict]
    last_fired_at: datetime | None = None


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


_formatter = string.Formatter()


def render(template: str, vars: dict[str, Any]) -> str:
    try:
        return _formatter.vformat(template, (), _SafeDict(vars))
    except (ValueError, IndexError):
        return template


def render_obj(obj: Any, vars: dict[str, Any]) -> Any:
    if isinstance(obj, str):
        return render(obj, vars)
    if isinstance(obj, list):
        return [render_obj(v, vars) for v in obj]
    if isinstance(obj, dict):
        return {k: render_obj(v, vars) for k, v in obj.items()}
    return obj


def _parse_hhmm(s: str) -> tuple[int, int] | None:
    try:
        h, m = s.split(":")
        return int(h), int(m)
    except (ValueError, AttributeError):
        return None


def in_time_window(now: datetime, start: str, end: str) -> bool:
    a, b = _parse_hhmm(start), _parse_hhmm(end)
    if a is None or b is None:
        return True
    cur = now.hour * 60 + now.minute
    s, e = a[0] * 60 + a[1], b[0] * 60 + b[1]
    if s <= e:
        return s <= cur < e
    return cur >= s or cur < e  # window wraps past midnight


def rule_matches(rule: RuleView, ev: EventContext, now_utc: datetime | None = None) -> bool:
    if not rule.enabled:
        return False
    if rule.event_types and ev.event_type not in rule.event_types:
        return False
    if rule.camera_ids and ev.camera_id not in rule.camera_ids:
        return False
    if rule.zone_names and ev.zone not in rule.zone_names:
        return False
    if rule.person_ids and ev.person_id not in rule.person_ids:
        return False
    if rule.days and ev.time.weekday() not in rule.days:
        return False
    if not in_time_window(ev.time, rule.time_start, rule.time_end):
        return False
    if rule.last_fired_at and rule.cooldown_s > 0:
        now_utc = now_utc or datetime.now(timezone.utc)
        last = rule.last_fired_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if now_utc - last < timedelta(seconds=rule.cooldown_s):
            return False
    return True


# --- Action runners -----------------------------------------------------------

@dataclass
class ActionResult:
    ok: bool
    detail: str


def _parse_body(body: Any) -> Any:
    if isinstance(body, str):
        stripped = body.strip()
        if not stripped:
            return None
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return stripped
    return body


def run_webhook(action: dict, vars: dict, client: httpx.Client) -> ActionResult:
    method = (action.get("method") or "POST").upper()
    url = render(action.get("url", ""), vars)
    if not url.startswith(("http://", "https://")):
        return ActionResult(False, f"invalid URL: {url!r}")
    headers = render_obj(action.get("headers") or {}, vars)
    body = render_obj(_parse_body(action.get("body")), vars)
    kwargs: dict[str, Any] = {"headers": headers}
    if body is not None and method not in ("GET", "DELETE"):
        if isinstance(body, (dict, list)):
            kwargs["json"] = body
        else:
            kwargs["content"] = str(body)
    resp = client.request(method, url, **kwargs)
    return ActionResult(resp.status_code < 400, f"HTTP {resp.status_code} {resp.text[:300]}")


def run_homeassistant(action: dict, vars: dict, client: httpx.Client, ha_url: str,
                      ha_token: str) -> ActionResult:
    if not ha_url or not ha_token:
        return ActionResult(False, "Home Assistant URL/token not configured in Settings")
    base = ha_url.rstrip("/")
    data = render_obj(_parse_body(action.get("data")) or {}, vars)
    if action.get("mode", "service") == "event":
        event_type = render(action.get("event_type") or "coeus_event", vars)
        url = f"{base}/api/events/{event_type}"
        payload = data or vars
    else:
        service = render(action.get("service", ""), vars)
        if "." not in service:
            return ActionResult(False, f"service must look like 'domain.service', got {service!r}")
        domain, svc = service.split(".", 1)
        url = f"{base}/api/services/{domain}/{svc}"
        payload = data
    resp = client.post(url, json=payload, headers={"Authorization": f"Bearer {ha_token}"})
    return ActionResult(resp.status_code < 400, f"HTTP {resp.status_code} {resp.text[:300]}")


class ActionEngine:
    """Runs matched rules' actions on a small thread pool."""

    def __init__(self, load_rules: Callable[[], list[RuleView]],
                 on_fired: Callable[[int, datetime], None],
                 log_result: Callable[[int, int, str, ActionResult], None],
                 get_config: Callable[[], dict],
                 describe: Callable[[EventContext], str] | None = None,
                 max_workers: int = 4, http_timeout: float = 10.0):
        self.load_rules = load_rules
        self.on_fired = on_fired
        self.log_result = log_result
        self.get_config = get_config
        self.describe = describe
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="actions")
        self.http_timeout = http_timeout

    def submit(self, ev: EventContext) -> None:
        self.pool.submit(self._handle, ev)

    def matching_rules(self, ev: EventContext) -> list[RuleView]:
        return [r for r in self.load_rules() if rule_matches(r, ev)]

    def _handle(self, ev: EventContext, force_rule: RuleView | None = None) -> list[ActionResult]:
        results = []
        try:
            rules = [force_rule] if force_rule else self.matching_rules(ev)
            for rule in rules:
                self.on_fired(rule.id, datetime.now(timezone.utc))
                results += self.run_rule(rule, ev)
        except Exception:  # noqa: BLE001
            log.exception("action handling failed for event %s", ev.event_id)
        return results

    def run_rule(self, rule: RuleView, ev: EventContext) -> list[ActionResult]:
        cfg = self.get_config()
        results = []
        with httpx.Client(timeout=self.http_timeout, follow_redirects=False) as client:
            for action in rule.actions:
                atype = action.get("type", "")
                result = ActionResult(False, "not run")
                for attempt in range(3):
                    try:
                        if atype == "llm_describe":
                            if self.describe is None:
                                result = ActionResult(False, "no LLM provider configured")
                            elif not ev.description:
                                ev.description = self.describe(ev)
                                result = ActionResult(True, ev.description)
                            else:
                                result = ActionResult(True, ev.description)
                        elif atype == "webhook":
                            result = run_webhook(action, ev.vars(), client)
                        elif atype == "homeassistant":
                            result = run_homeassistant(action, ev.vars(), client,
                                                       cfg.get("ha.url", ""), cfg.get("ha.token", ""))
                        else:
                            result = ActionResult(False, f"unknown action type {atype!r}")
                    except Exception as err:  # noqa: BLE001
                        result = ActionResult(False, f"{type(err).__name__}: {err}")
                    if result.ok or atype not in ("webhook", "homeassistant"):
                        break
                    if "HTTP 4" in result.detail:  # client error: retrying won't help
                        break
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                self.log_result(ev.event_id, rule.id, atype, result)
                results.append(result)
        return results
