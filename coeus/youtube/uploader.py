"""YouTube OAuth (desktop-app 'paste the redirect URL' flow) and resumable uploads."""
from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube"]
REDIRECT_URI = "http://localhost:1"
CHUNK_SIZE = 64 * 1024 * 1024
RETRIABLE_STATUS = {500, 502, 503, 504, 429}
TITLE_MAX = 100
DESCRIPTION_MAX = 5000


class QuotaExceeded(Exception):
    pass


class NotConnected(Exception):
    pass


# --- OAuth --------------------------------------------------------------------

def _flow(client_config: dict, code_verifier: str | None = None):
    from google_auth_oauthlib.flow import Flow
    kwargs: dict[str, Any] = {"redirect_uri": REDIRECT_URI}
    if code_verifier:
        kwargs["code_verifier"] = code_verifier
    else:
        kwargs["autogenerate_code_verifier"] = True
    return Flow.from_client_config(client_config, scopes=SCOPES, **kwargs)


def normalise_client_config(raw: str | dict) -> dict:
    cfg = json.loads(raw) if isinstance(raw, str) else raw
    if "installed" not in cfg and "web" not in cfg:
        raise ValueError("Expected a Google OAuth client JSON with an 'installed' section "
                         "(create an OAuth client of type 'Desktop app').")
    return cfg


def start_authorization(client_config: dict) -> tuple[str, str, str]:
    """Return (auth_url, state, code_verifier)."""
    flow = _flow(client_config)
    url, state = flow.authorization_url(access_type="offline", prompt="consent",
                                        include_granted_scopes="true")
    return url, state, flow.code_verifier


def extract_code(pasted: str) -> str:
    pasted = pasted.strip()
    if pasted.startswith("http"):
        qs = parse_qs(urlparse(pasted).query)
        if "error" in qs:
            raise ValueError(f"Google returned an error: {qs['error'][0]}")
        if "code" not in qs:
            raise ValueError("No 'code' parameter in the pasted URL")
        return qs["code"][0]
    return pasted


def finish_authorization(client_config: dict, code_verifier: str, pasted: str) -> dict:
    flow = _flow(client_config, code_verifier)
    flow.fetch_token(code=extract_code(pasted))
    return json.loads(flow.credentials.to_json())


def build_service(credentials_info: dict | None, on_refresh: Callable[[dict], None] | None = None):
    if not credentials_info:
        raise NotConnected("YouTube account is not connected")
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_info(credentials_info, SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        if on_refresh:
            on_refresh(json.loads(creds.to_json()))
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def channel_title(service) -> str:
    resp = service.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items", [])
    return items[0]["snippet"]["title"] if items else "(no channel)"


# --- Metadata -----------------------------------------------------------------

def render_title(template: str, **vars: Any) -> str:
    try:
        title = template.format(**vars)
    except (KeyError, IndexError, ValueError):
        title = f"{vars.get('camera')} {vars.get('date')} part {vars.get('part')}"
    title = title.replace("<", "").replace(">", "").strip()
    return title[:TITLE_MAX]


def build_description(camera: str, day: str, part: int, parts: int,
                      chapter_list: list[tuple[float, str]], gaps: list,
                      fmt_ts: Callable[[float], str]) -> str:
    lines = [f"{camera} — recordings for {day} (part {part} of {parts}).",
             "Archived automatically by Coeus.", ""]
    # YouTube chapters need: first at 0:00, >= 3 entries, each >= 10 s.
    usable = [c for i, c in enumerate(chapter_list)
              if i == 0 or c[0] - chapter_list[i - 1][0] >= 10]
    if len(usable) >= 3:
        lines.append("Chapters (wall-clock time):")
        lines += [f"{fmt_ts(off)} {label}" for off, label in usable]
        lines.append("")
    if gaps:
        lines.append("Recording gaps:")
        lines += [f"- after {after}: {int(sec // 60)} min missing" for after, sec in gaps]
    desc = "\n".join(lines).replace("<", "").replace(">", "")
    return desc[:DESCRIPTION_MAX]


# --- Upload -------------------------------------------------------------------

def _is_quota_error(err) -> bool:
    try:
        reasons = [e.get("reason") for e in json.loads(err.content)["error"]["errors"]]
    except Exception:  # noqa: BLE001
        reasons = []
    return any(r in ("quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded",
                     "dailyLimitExceeded") for r in reasons)


def upload_video(service, path: Path, title: str, description: str, privacy: str = "private",
                 category_id: str = "22", resume_uri: str = "",
                 on_session: Callable[[str], None] | None = None,
                 on_progress: Callable[[float], None] | None = None,
                 max_retries: int = 10) -> str:
    """Upload with resumable chunks; returns the video id."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {"title": title, "description": description, "categoryId": category_id},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(path), chunksize=CHUNK_SIZE, resumable=True,
                            mimetype="video/mp4")
    request = service.videos().insert(part="snippet,status", body=body, media_body=media,
                                      notifySubscribers=False)
    if resume_uri:
        # Ask the server how much it already has before sending more.
        request.resumable_uri = resume_uri
        request._in_error_state = True  # noqa: SLF001 - forces a status query in next_chunk

    response, retries, announced = None, 0, bool(resume_uri)
    while response is None:
        try:
            status, response = request.next_chunk()
            retries = 0
            if not announced and request.resumable_uri and on_session:
                on_session(request.resumable_uri)
                announced = True
            if status and on_progress:
                on_progress(status.progress())
        except HttpError as err:
            if err.resp.status in (403, 400) and _is_quota_error(err):
                raise QuotaExceeded(str(err)) from err
            if err.resp.status == 404 and resume_uri:
                # Session expired (they live about a week); start over.
                log.warning("resumable session expired, restarting upload")
                return upload_video(service, path, title, description, privacy, category_id,
                                    "", on_session, on_progress, max_retries)
            if err.resp.status not in RETRIABLE_STATUS:
                raise
            retries += 1
        except (OSError, ConnectionError) as err:
            log.warning("network error during upload: %s", err)
            retries += 1
        if retries:
            if retries > max_retries:
                raise RuntimeError("upload failed after too many retries")
            sleep = min(2 ** retries, 300) * (0.5 + random.random())
            log.info("retrying upload chunk in %.0fs", sleep)
            time.sleep(sleep)
    return response["id"]


def add_to_playlist(service, playlist_id: str, video_id: str) -> None:
    service.playlistItems().insert(part="snippet", body={"snippet": {
        "playlistId": playlist_id,
        "resourceId": {"kind": "youtube#video", "videoId": video_id}}}).execute()
