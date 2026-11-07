# Coeus test plan

Follow the parts in order. Each step says what to do and what you should see. Mark
every step PASS, FAIL or SKIPPED, and use the report template at the end.

* **Part A** runs anywhere: a laptop, a CI box, or an AI agent such as opencode.
* **Part B** needs the owner's home network: the Synology NAS at `192.168.0.111`, the
  cameras, Home Assistant, a YouTube account and an LLM key.

Setup commands are in `AGENTS.md`.

---

## Part A — anywhere, no home network needed

### A1. Automated tests

```bash
pytest -q
```
Expected: every test passes (82 at the time of writing). Nothing is skipped as long
as ffmpeg is installed.

### A2. Start the app with sample recordings

```bash
python scripts/make_sample_recordings.py ./sample-volume
export COEUS_DATA_DIR=./data COEUS_RECORDINGS_DIR=./sample-volume \
       COEUS_SECRET_KEY=dev-secret COEUS_ADMIN_PASSWORD=admin-pass-123
uvicorn coeus.main:create_app --factory --port 8765
```
Expected: the log ends with `Application startup complete`.

```bash
scripts/smoke_test.sh http://localhost:8765
```
Expected: `5 passed, 0 failed`.

### A3. Login and logout

| # | Do | Expect |
|---|---|---|
| 1 | Open http://localhost:8765 | Redirect to the login page |
| 2 | Log in with a wrong password | "Invalid username or password." |
| 3 | Fail 5 more times, then use the right password | "Too many failed attempts…" (the lock lasts 5 minutes) |
| 4 | Restart the app (the lock is cleared) and log in as `admin` / `admin-pass-123` | Dashboard with the sidebar groups Watch / Archive / Setup |
| 5 | Press **Log out** at the bottom of the sidebar | Back on the login page; `/` redirects to login again |
| 6 | Account → add a user with an 8+ character password; log in as that user | Works |

### A4. Recordings folder picker

| # | Do | Expect |
|---|---|---|
| 1 | Recordings | "No camera folders here" and a button to choose the folder (the mount itself holds no cameras) |
| 2 | **Change folder** | "Detected automatically: /surveillance — 3 camera folder(s)" |
| 3 | Browse into `surveillance` → `Main Gate` → **Select this folder** | Message: "Main Gate is a single camera folder, so its parent surveillance is used" |
| 4 | Change folder → browse to the mount root → **Select this folder** | Coeus finds `surveillance` inside it and uses that |
| 5 | Open `/library?path=../..` | Stays inside the mount (shows the mount root) |
| 6 | Select `photo` | Error: "No Surveillance Station camera folders found in photo" |

### A5. YouTube accounts and the account required for each camera

| # | Do | Expect |
|---|---|---|
| 1 | Recordings, with no accounts | Red notice "you need a YouTube account" with an **Add one** link |
| 2 | YouTube accounts → **Add account**, name "Test", paste a client JSON (a fake one is fine here: `{"installed":{"client_id":"x.apps.googleusercontent.com","client_secret":"y","auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token"}}`) → **Next** | The Connect page with an **Open Google sign-in** link. Press **Later** |
| 3 | YouTube accounts | "Test" shows **not connected** |
| 4 | Recordings → `Main Gate` row → choose "Test" → **Add & archive daily** | "Added Main Gate: archived daily to Test (account not connected yet!)" |
| 5 | Cameras → **Add camera**, tick "Merge and upload daily", leave the account empty → Save | Saved, with "archiving stays off: choose which YouTube account to upload to" |
| 6 | Camera form → **Test account** | "✗ YouTube account is not connected" |
| 7 | Camera form | Privacy shows "Private, always"; Playlist says it is created on the first upload |
| 8 | YouTube accounts → **Add account** again | The form offers "Use the OAuth client of an existing account" |
| 9 | Remove the "Test" account | Message lists the cameras whose archiving was turned off |

### A6. Lossless merge, without YouTube

Merges only start for cameras whose account is connected, so check the merge engine
directly:

```bash
python - <<'EOF'
from datetime import datetime, timedelta
from pathlib import Path
from coeus.recordings.synology import scan_segments
from coeus.recordings import merge as mg
root = Path("sample-volume/surveillance")
day = (datetime.now() - timedelta(days=1)).date()
segs = scan_segments(root, "Main Gate", day, settle_seconds=0)
durs = mg.estimate_durations(segs)
parts = mg.plan_parts(segs, durs, max_part_seconds=11.9 * 3600)
out = mg.merge(parts[0].segments, Path("data/work/test-merge.mp4"))
print(len(segs), "segments ->", round(mg.probe_duration(out), 1), "s", out)
EOF
```
Expected: `6 segments -> 30.0 s data/work/test-merge.mp4`, give or take 0.5 s. The
file plays, and its quality matches the input, because nothing is re-encoded.

### A7. Cameras, one live camera, zones

| # | Do | Expect |
|---|---|---|
| 1 | Add camera "Cam A" with RTSP `rtsp://192.0.2.1/x`, tick **Watch this camera live** | Saved |
| 2 | Add camera "Cam B" with live watching ticked | "Live watching moved from Cam A to Cam B (limit: 1)"; Cameras shows only Cam B live |
| 3 | Camera form → **Test main stream** | "✗ cannot open stream…" (this address is unreachable on purpose) |
| 4 | Cam B → zones → click 3 or more points → **Finish zone** → **Save zones** | "Saved 1 zone(s)". The background image is missing without a stream; that is expected |
| 5 | Dashboard | "Live camera" card for Cam B with status connecting, reconnecting or error |

### A8. People and face photos

The first upload downloads the face models (about 40 MB) and needs internet.

| # | Do | Expect |
|---|---|---|
| 1 | People → add "Akash" → upload 3 clear photos of one face | "Added 3 face sample(s)"; face crops appear |
| 2 | Upload a group photo | Rejected: "more than one face…" |
| 3 | Upload a landscape photo | Rejected: "no face found…" |

### A9. Gate

| # | Do | Expect |
|---|---|---|
| 1 | Gate → camera Cam B, tick "Gate opening is on", When to check = webhook, method = on-device → **Save** | "Gate settings saved."; the badge shows On · Cam B |
| 2 | Copy the webhook address; `scripts/smoke_test.sh http://localhost:8765 <token>` | The extra gate-check line returns JSON with `"outcome": "error"` (no stream) |
| 3 | `curl -X POST http://localhost:8765/api/hooks/gate/wrong` | HTTP 404 |
| 4 | Set When to check = "All the time (live)", Save, call the webhook again | HTTP 409 "Webhook checks are switched off" |
| 5 | **Try a photo** with a new photo of Akash (not one already uploaded) | "On-device: Akash (similarity ≥ 0.36)" |
| 6 | Try a photo of someone else | "On-device: not recognised" |
| 7 | Choose an LLM method with no LLM set up → Save | Warning "uses an LLM, but none is set up" |
| 8 | **Make a new address** | The old token returns 404, the new one works |
| 9 | Recent checks table | One row per check, with result, time and duration |

### A10. Rules and settings

| # | Do | Expect |
|---|---|---|
| 1 | Rules → **Add rule** → insert the `webhook` example, point its URL at `https://httpbin.org/post` → Save → **Test** | "OK: HTTP 200 …" |
| 2 | Put broken JSON in the actions box → Save | "Invalid actions JSON" |
| 3 | Settings → enter an LLM API key → Save → reload | The key shows masked (`••••••••1234`), never in full |
| 4 | Change the daily run time to `02:15` → Save → reload | Keeps `02:15` |

### A11. Look and feel

Check the Dashboard, Gate and Recordings pages:
* in light and dark system theme;
* at phone width (about 390 px).

Expected: text is readable in both themes, there is no sideways scrolling on a phone,
and the sidebar becomes a top bar on a phone.

---

## Part B — on the home network

### B1. Docker on the Synology

1. Clone the branch to `/volume1/docker/coeus-src`.
2. `cp .env.example .env`, then set `COEUS_SECRET_KEY` and `COEUS_ADMIN_PASSWORD`.
3. Container Manager → Project → Create from that folder → Build.

Expected: the build succeeds. **This build has never been tested**, so it is the first
real check; keep the build log either way. The container becomes healthy, and
http://192.168.0.111:8765 shows the login page.

### B2. Real recordings

Recordings → change folder.

Expected:
* `/surveillance` is detected with every real camera folder: Main Gate, Main Road Cam,
  Hall 2 Second Floor, Generic_ONVIF-001, and so on.
* `@DetectionEvent`, `node`, `N8NHome`, `SM-S921B` and `xiaomi_camera_videos` are not
  listed as cameras, unless they really contain Synology-named segments.
* Opening a camera lists days with about 48 segments and about 12 GB each.

### B3. YouTube end to end

1. Create a Google Cloud project, enable YouTube Data API v3, set up the OAuth consent
   screen with your account as a test user, and create a **Desktop app** client.
2. YouTube accounts → Add account → paste the JSON → **Open Google sign-in** → allow
   all permissions → copy the `http://localhost:1/?…code=…` address → **Connect and
   test**.
   Expected: "Connected to channel <your channel>; upload permission granted."
3. Recordings → choose the account for **one** camera → open the camera → choose a
   past day → **Merge & upload**.
   Expected:
   * The Upload queue shows the parts going pending → merging → merged → uploading →
     done.
   * On YouTube there is a private video, about 8 h per part, with chapter timestamps
     in the description.
   * There is a private playlist "<camera> — Coeus archive" containing it.
4. Upload the next day.
   Expected: it goes into the **same** playlist.
5. Disk check: after the upload, `/volume1/docker/coeus/work` holds no leftover merged
   file (the default setting deletes it).

### B4. Live camera and motion

1. Cameras → the gate camera → ONVIF section → **Query camera**, or paste the RTSP
   URL.
2. **Test main stream**.
   Expected: ✓ with the resolution and a preview.
3. Tick **Watch this camera live**. Draw a zone over the gate.
4. Walk into the zone.
   Expected: the dashboard shows LIVE, and Events shows a motion event or a
   face_known event with a picture.

### B5. Gate with Home Assistant

1. Settings → set the Home Assistant URL and a long-lived token → **Test Home
   Assistant**.
   Expected: HTTP 200.
2. Gate → set the action to a **test light first**, e.g.
   `{"type":"homeassistant","service":"light.toggle","data":{"entity_id":"light.porch"}}`
   → **Test the gate action**.
   Expected: the light toggles.
3. Webhook mode: stand in front of the camera and run
   `curl -X POST http://192.168.0.111:8765/api/hooks/gate/<token>`.
   Expected: `"outcome":"opened"` with your name within about 15 s, and the light
   toggles.
4. Someone not on the allowed list (or untick yourself): expect `denied` or
   `no_match`, and nothing toggles.
5. Continuous mode: choose "All the time (live)"; the gate camera must be the live
   camera. Walk up.
   Expected: it opens without calling the webhook. It does not open again within the
   cooldown.
6. Only then switch the action to the real gate entity.

### B6. LLM recognition

1. Settings → LLM provider:
   * Anthropic Claude with an API key, or
   * OpenAI-compatible: Ollama on the NAS (`http://192.168.0.111:11434/v1`, model
     `llava`) or Gemini.
2. **Test LLM**.
   Expected: a one-sentence description.
3. Gate → **Try a photo** with "Also ask the LLM" ticked.
   Expected: "LLM: Akash (confidence 0.xx)" with a reason. For a stranger: unknown.
4. Gate method "On-device and LLM must agree" → repeat B5 step 3.
   Expected: it opens only when both agree; the Recent checks row shows source "both"
   and the number of LLM calls.

---

## Report template

```
Coeus test report — <date>, branch ccr-26d76cd2-tnyw4t @ <commit>
Environment: <OS, Python version, ffmpeg version | Synology model, DSM version>

A1 pytest:            PASS/FAIL  (<n> passed)
A2 start + smoke:     PASS/FAIL
A3 login:             PASS/FAIL  <step numbers that failed>
A4 folder picker:     ...
A5 YouTube accounts:  ...
A6 merge:             ...
A7 cameras:           ...
A8 people:            ...
A9 gate:              ...
A10 rules/settings:   ...
A11 look and feel:    ...
B1–B6:                ... / SKIPPED (no home network)

Failures (one block each):
  Step:      A9.5
  Did:       ...
  Expected:  ...
  Got:       ... (paste the error text or the relevant uvicorn log lines)
```
