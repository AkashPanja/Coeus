# Coeus — notes for coding agents

Coeus is a self-hosted surveillance assistant for a Synology NAS. It is a Python 3.12
FastAPI app with Jinja templates, SQLite, OpenCV and ffmpeg.

* It merges Surveillance Station recordings each day and uploads them to YouTube as
  private videos, with one private playlist per camera.
* It watches one live camera, detects motion inside zones and recognises faces.
* It opens a gate for allowed people, triggered by a webhook or by continuous
  checking.

Read these first:
* `docs/REQUIREMENTS.md`: what the app must do.
* `docs/TESTING.md`: the test plan to run and report on.

## Branch

Work happens on `ccr-26d76cd2-tnyw4t`:

```bash
git clone -b ccr-26d76cd2-tnyw4t https://github.com/AkashPanja/Coeus.git
cd Coeus
```

## Setup

You need Python 3.12 or newer and **ffmpeg/ffprobe** on PATH.

```bash
python -m venv .venv
. .venv/bin/activate             # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
```

## Commands

| What | Command |
|---|---|
| Unit and integration tests | `pytest -q` (all should pass; the merge tests need ffmpeg) |
| Sample NAS recordings | `python scripts/make_sample_recordings.py ./sample-volume` |
| Run the app locally | see below |
| Smoke-test a running app | `scripts/smoke_test.sh http://localhost:8765 [gate-token]` |
| Docker image | `docker compose build` (needs `.env`, see `.env.example`) |

Run the app locally on Linux or macOS:

```bash
export COEUS_DATA_DIR=./data COEUS_RECORDINGS_DIR=./sample-volume \
       COEUS_SECRET_KEY=dev-secret COEUS_ADMIN_PASSWORD=admin-pass-123
uvicorn coeus.main:create_app --factory --port 8765
```

On Windows PowerShell, set each variable with `$env:COEUS_DATA_DIR="./data"` and so on.
Then log in at http://localhost:8765 as `admin` with `admin-pass-123`.
`COEUS_DISABLE_WORKERS=true` turns off the scheduler and the live camera threads.

## Layout

| Path | What |
|---|---|
| `coeus/main.py` | App factory, startup (admin user, scheduler, live monitor) |
| `coeus/models.py`, `coeus/db.py` | SQLAlchemy models; `db.py` adds missing columns on startup |
| `coeus/settings_store.py` | Settings kept in the DB, with defaults |
| `coeus/recordings/` | Synology folder parsing (`synology.py`), folder picker (`library.py`), lossless merge (`merge.py`) |
| `coeus/youtube/uploader.py` | OAuth paste-back flow, resumable upload, playlists, credential test |
| `coeus/jobs/` | Archive job states, quota per Google project, daily scheduler |
| `coeus/vision/` | Motion detection in zones, YuNet/SFace faces, ONVIF, live monitor thread |
| `coeus/gate.py` | Gate checks (webhook and continuous); on-device and LLM decision |
| `coeus/llm/providers.py` | Claude (Anthropic SDK) and OpenAI-compatible vision providers |
| `coeus/actions/engine.py` | Rules: webhook and Home Assistant actions |
| `coeus/web/` | Routes (`routes.py`, `routes_youtube.py`, `routes_gate.py`), templates, CSS |
| `tests/` | pytest suite; `conftest.py` builds fake Synology folders |

## Rules for changes

* Run `pytest -q` before committing. Add tests next to the feature you change.
* Never write to the recordings mount. It is mounted read-only in production.
* Uploads stay **private**. Do not add a public or unlisted option.
* Only one camera is watched live by default (`live.max_cameras`). Keep it that way.
* Every form POST needs the CSRF token. Only `/api/hooks/*` is exempt, because it is
  protected by its secret token.
* Secrets (Home Assistant token, LLM key, YouTube credentials) must never appear in
  full in pages or logs.
* The face models download on first use into `$COEUS_DATA_DIR/models`. If you are
  offline, face features fail with a clear error, and everything else works.
