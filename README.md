# Coeus

Self-hosted surveillance assistant for a Synology NAS:

* **Daily YouTube archive.** Merges each camera's Surveillance Station recordings
  **losslessly** (ffmpeg stream copy, original quality) into parts no longer than
  YouTube's 12-hour limit, then uploads them with clickable per-segment chapters.
* **Live watching.** Reads RTSP/ONVIF streams, detects motion inside zones you draw,
  finds faces and recognises people from your own photo library (on-device by default,
  or with an LLM), then triggers webhooks or Home Assistant actions.
* Login/logout, multiple users, and a choice of LLM provider: Claude, OpenAI-compatible
  (OpenAI, Gemini, OpenRouter, local Ollama) or none.

See **[docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)** for the full requirements, the
architecture and the release plan, and **[docs/TESTING.md](docs/TESTING.md)** for the
step-by-step test plan. Coding agents (opencode and others) should start with
**[AGENTS.md](AGENTS.md)**.

## Install on Synology (Container Manager)

1. Copy this repository to the NAS, for example `/volume1/docker/coeus-src`.
2. Create the data folder `/volume1/docker/coeus`.
3. `cp .env.example .env` and set `COEUS_SECRET_KEY` (for example the output of
   `openssl rand -hex 32`) and `COEUS_ADMIN_PASSWORD`.
4. In **Container Manager → Project → Create**, choose the folder, use `docker-compose.yml`,
   and build.
5. Open `http://<nas-ip>:8765` and log in as `admin`.

The compose file mounts `/volume1/surveillance` **read-only** at `/recordings`. You can
mount a higher folder (even `/volume1`) instead and pick the recordings folder in the UI. Coeus
never changes Surveillance Station files. Everything it writes (database, face photos,
event snapshots, merged videos) goes to `/data`. Merging needs roughly as much free
space as one part's segments, about 6 GB per 12-hour part.

## First steps in the UI

1. **Recordings → change folder.** Browse the mounted path and select the folder that
   holds your camera folders. Coeus suggests the right one, and if you pick a single
   camera folder or a parent folder, it works out the library from that.
2. **YouTube → Add account.** Repeat for each channel. Create a Google Cloud project,
   enable *YouTube Data API v3*, and create an OAuth client of type **Desktop app**.
   Paste its JSON, or reuse another account's client. Open the sign-in link, then paste
   back the `http://localhost:1/?code=…` address your browser lands on. (Google does not
   accept LAN addresses such as `192.168.x.x` as redirect targets.) Press **Test** to
   check the channel and the upload permission.
3. **Recordings.** For each camera folder, choose the YouTube account and press
   **Add & archive daily**. If there is no account yet, you are taken to create one
   first. You can also click **Merge & upload** on any past day.
4. **Cameras.** Add the RTSP URL, or query ONVIF to fill it in, and press **Test main
   stream**. Use the sub-stream for detection, turn on **Monitor this camera live**,
   then **edit zones** on the snapshot.
5. **People.** Add each person with 3–10 clear face photos.
6. **Gate.** Choose the gate camera, who may open it, the recognition method
   (on-device, LLM with confidence, or both) and the Home Assistant action that opens
   the gate. Call the webhook address shown on the page from a doorbell or an
   automation, or let the live camera check all the time.
7. **Rules.** Choose event types, cameras, zones, people, a time window and actions.
   Use **Test** to send a fake event.

### Gate webhook from Home Assistant

```yaml
rest_command:
  coeus_gate_check:
    url: "http://192.168.0.111:8765/api/hooks/gate/<token from the Gate page>"
    method: post
    timeout: 30
```

The response is JSON, for example
`{"outcome": "opened", "person": "Akash", "confidence": 0.82, "source": "on-device"}`.

### Action examples

```json
[
  {"type": "llm_describe"},
  {"type": "homeassistant", "service": "notify.mobile_app_sm_s921b",
   "data": {"title": "{camera}", "message": "{person} at {zone}: {description}"}},
  {"type": "homeassistant", "service": "switch.turn_on",
   "data": {"entity_id": "switch.gate_relay"}},
  {"type": "webhook", "url": "http://192.168.0.111:5678/webhook/coeus",
   "body": {"person": "{person}", "camera": "{camera}", "snapshot": "{snapshot_url}"}}
]
```

`{snapshot_url}` needs `COEUS_PUBLIC_URL` to be set, and the URL requires a login.

## YouTube limits to know

* **12 hours per video.** A day is split into evenly sized parts (default maximum
  11.9 h). With 30-minute segments that is usually 3 parts of 8 hours.
* **Quota.** The default API quota (10,000 units a day per Google Cloud project) allows
  about 6 uploads. Quota is tracked per project, so accounts on different projects
  upload independently. Coeus keeps a queue, holds back a project when its quota runs
  out, and continues after the reset at midnight Pacific time.
* **Privacy and playlists.** Archives are always uploaded as *private*, and each
  camera gets its own private playlist on its YouTube account, created on the first
  upload.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                      # unit and integration tests (needs ffmpeg for the merge tests)
COEUS_DATA_DIR=./data COEUS_RECORDINGS_DIR=/path/to/surveillance COEUS_SECRET_KEY=dev \
  uvicorn coeus.main:create_app --factory --reload --port 8765
```

Code map:

| Path | What |
|---|---|
| `coeus/recordings/synology.py` | Parses Surveillance Station folders and segment names |
| `coeus/recordings/library.py` | Library folder picker: safe browsing and auto-detection |
| `coeus/recordings/merge.py` | Plans parts, gaps and chapters; lossless ffmpeg concat |
| `coeus/youtube/uploader.py` | OAuth paste-back flow, resumable upload, quota detection |
| `coeus/jobs/` | Archive job state machine and the daily scheduler |
| `coeus/vision/` | Zone masks and MOG2 motion, YuNet/SFace faces, ONVIF, per-camera monitor threads |
| `coeus/llm/providers.py` | Claude and OpenAI-compatible vision providers |
| `coeus/actions/engine.py` | Rule matching, webhook and Home Assistant actions |
| `coeus/gate.py` | Gate opener: webhook/continuous checks, on-device + LLM decision |
| `coeus/web/` | FastAPI routes (`routes_youtube.py`: accounts and library picker) and Jinja templates |

The face models (YuNet and SFace from the OpenCV model zoo, about 38 MB) are downloaded
during the Docker build. If that fails, they are downloaded on first use. You can also
place them in `/data/models` yourself.
