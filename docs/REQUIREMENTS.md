# Coeus — Requirements

Coeus is a self-hosted surveillance assistant that runs as a Docker container
on a Synology NAS. It does two jobs:

1. **Archive.** Every day it merges each selected camera's Surveillance Station
   recordings into long videos and uploads them to YouTube at the original
   quality.
2. **Watch.** It reads live ONVIF/RTSP streams, detects motion inside zones you
   draw, finds faces, matches them against a list of known people, and then
   runs actions such as webhooks, Home Assistant service calls or phone calls.

This document turns the initial brief into concrete requirements. Items marked
**(P1)** are in the first release. **(P2)** and **(P3)** come later.

---

## 0. The initial brief

> Build an application that runs on a Synology NAS in a Docker container. Select
> the camera folder; the system merges the recordings and posts them to YouTube
> in high quality automatically every day. It should also monitor ONVIF/RTSP
> URLs, detect motion in a particular area, detect faces in it, identify the
> person from a preloaded list of faces, get the name, and call an action
> (call an API, the Home Assistant API, etc.). It needs login/logout, the option
> to use various LLMs, or face recognition on the system itself.

Assumption: "detect factory in it" in the brief means **detect faces in it**.

---

## 1. What we know about the environment (from the NAS screenshots)

| Fact | Consequence |
|---|---|
| Synology DSM with Surveillance Station; the share is `/volume1/surveillance` (shown as `surveillance` in File Station). | Mount it **read-only** into the container. Coeus must never change Surveillance Station's files. |
| There is one folder per camera: `Main Gate`, `Main Road Cam`, `Hall 2 Second Floor`, `Generic_ONVIF-001`, and others. | A "camera library" is one folder under the share. The user picks which folders to archive. |
| Some folders are not cameras: `@DetectionEvent`, `@SSRECMETA`, `node`, `N8NHome`, `SM-S921B`, `xiaomi_camera_videos`. | Skip names that start with `@`. Treat a folder as a camera only if it holds `YYYYMMDDAM`/`PM` subfolders or segment files that match the pattern below. |
| Inside a camera there is one folder per half day: `20250516AM`, `20250516PM`, plus a `cam_settings.conf` file. | Find a day's segments in its two half-day folders. |
| Segments are named `Main Gate-20250516-122734-1747378654295-7.mp4`, which is `<camera>-<YYYYMMDD>-<HHMMSS>-<epoch ms>-<n>.mp4`. | Parse the name to get the local start time. The epoch in milliseconds is the exact instant; in the sample it shows the NAS runs at UTC+05:30. |
| Each segment is about 30 minutes and 200–300 MB. | One camera produces about 48 segments and 11–14 GB per day. |
| Home Assistant (`hassbackup`) and n8n (`N8NHome`) already run on the NAS. | Home Assistant and generic webhooks (n8n) are the first action targets. |

## 2. Users and roles

* **Admin**: configures cameras, zones, people, rules, integrations and users.
* **Viewer** (P2): sees dashboards, events and recordings, and changes nothing.

## 3. Functional requirements

### 3.1 Authentication and access (P1)

* FR-A1: Log in with a username and password, and log out. Sessions live in a
  signed, HTTP-only cookie with `SameSite=Lax`, and expire after a configurable
  time (default 7 days).
* FR-A2: On first start, create an admin account from `COEUS_ADMIN_USER` /
  `COEUS_ADMIN_PASSWORD`. If no password is set, generate a random one and print
  it once in the container log.
* FR-A3: Hash passwords with bcrypt. Lock an account for 5 minutes after 5
  failed attempts from the same IP address.
* FR-A4: Every form that changes state carries a CSRF token.
* FR-A5: Admins can add users, remove users and change passwords. A user can
  change their own password.
* FR-A6 (P2): Personal API tokens for machine clients such as Home Assistant or
  n8n that call back into Coeus.
* FR-A7 (P3): OIDC single sign-on (Authentik, Synology SSO Server).

### 3.2 Recording library (P1)

* FR-R1: Scan the mounted recordings root and list the camera folders, the days
  available for each, and the segment count, total size and time span per day.
* FR-R2: Parse Synology's segment filenames (§1). Ignore files that do not
  match. Order segments by their start time, not by filename.
* FR-R3: Skip segments that are still being written. A file counts as finished
  when it was last modified more than *N* minutes ago (default 5) or when a
  newer segment exists.
* FR-R4: The user can enable or disable archiving for each camera folder and
  give each one a YouTube title template, a privacy setting and a playlist.
* FR-R5: **Choose the library folder in the UI.** docker-compose exposes one host
  path to the container (for example `/volume1/surveillance`, or all of `/volume1`).
  The UI browses that path, suggests folders that contain camera folders, and lets
  the user select the main folder. Coeus then works out the rest:
  * Selecting the share uses it.
  * Selecting a single camera folder uses its parent.
  * Selecting a folder above the share (such as `/volume1`) uses the one folder
    inside it that holds cameras, or asks which one if there are several.
  * Paths outside the mount are refused.
* FR-R6: **Every camera added for archiving must say which YouTube account it
  uploads to.** If no account exists yet, Coeus sends the user to create one, then
  returns them to the camera they were adding. The Recordings page offers an
  "Add & archive daily" control per folder that asks for the account.

### 3.3 Daily merge (P1)

* FR-M1: Join a day's segments **without re-encoding** (ffmpeg concat demuxer,
  `-c copy`). The result has exactly the camera's original quality and costs
  almost no CPU, which matters on the NAS's Celeron or Ryzen processor.
* FR-M2: **YouTube limits a video to 12 hours** (and 256 GB), so a full day
  cannot be one video. Split each day into *parts*, each no longer than a set
  maximum (default 11 h 55 m), on segment boundaries. Part 1 is usually the
  morning and part 2 the afternoon and evening.
* FR-M3: Measure segment durations with `ffprobe`. If that fails, use the gap
  to the next segment's start time.
* FR-M4: Detect gaps in the recording (default: more than 2 minutes). List them
  in the video description, and optionally start a new part at each gap.
* FR-M5: If one segment is corrupt or has a different codec, set it aside, log
  it, and keep going.
* FR-M6: Write output to a work volume (`/data/work`) and check that enough free
  space exists before starting. After a successful upload, delete the merged
  file (configurable: delete, keep for *N* days, or keep).
* FR-M7 (P2): Optional time-lapse mode that re-encodes at 10–60× speed, so a
  whole day fits in one short video.
* FR-M8 (P3): Burn in a timestamp overlay. This needs a re-encode; offer it
  only with hardware encoding (VAAPI/QSV through `/dev/dri`).

### 3.4 YouTube upload (P1)

* FR-Y0: **Multiple YouTube accounts.** Any number of accounts (channels) can be
  added, each with its own name, OAuth client and daily upload limit. A new
  account can reuse an existing account's OAuth client, which means the same
  Google project and therefore a shared quota. Each account has a **Test** button
  that checks the token, the channel and the upload permission, and shows today's
  quota use. The camera form has a Test button for the selected account. Removing
  an account turns off archiving for the cameras that used it.
* FR-Y1: Connect a YouTube channel with Google OAuth 2.0 using a
  "Desktop app" client. Google does not accept LAN addresses such as
  `https://192.168.0.111` as redirect URIs. Instead, Coeus shows the consent
  link, the user approves it, and then pastes back the `http://localhost/...`
  address the browser lands on. The refresh token is stored in Coeus.
* FR-Y2: Upload in resumable chunks and retry with exponential backoff on
  errors 5xx and 429 and on network drops. Resume interrupted uploads after a
  container restart.
* FR-Y3: Fill in titles and descriptions from templates, with variables such as
  `{camera}`, `{date}`, `{part}`, `{parts}`, `{start}` and `{end}`. The
  description lists **chapters**, one timestamp per segment, so viewers can
  jump to a time of day.
* FR-Y4: Uploads are **private by default**. YouTube also locks uploads from
  unverified API projects to private, so the README explains how to apply for
  verification if the user wants unlisted or public videos.
* FR-Y5: Handle the API quota, tracked **per Google Cloud project** (OAuth client
  ID). One exhausted project never blocks cameras that upload through another. The default quota is 10,000 units a day and each
  upload costs about 1,600, so only about 6 uploads fit in a day. Coeus keeps a
  priority queue, stops when it gets `quotaExceeded`, and continues after the
  quota resets at midnight Pacific time. The dashboard shows the backlog.
* FR-Y6: Optionally add each video to a playlist per camera (P2: create the
  playlist if it is missing).
* FR-Y7: Save every job with its status (pending, merging, uploading, done,
  failed), log, YouTube video ID and link. Jobs are idempotent: one row per
  camera, day and part. A part that is already done is never uploaded again.
* FR-Y8: The user can re-run or cancel a job, and can start a merge or upload
  by hand for any camera and day.

### 3.5 Scheduling (P1)

* FR-S1: A daily run at a configurable local time (default 01:30) queues the
  previous day for every camera with archiving on.
* FR-S2: All times use the configured `TZ` (default `Asia/Kolkata`, as the
  screenshots suggest).
* FR-S3: At most one merge and one upload run at a time (configurable), so the
  NAS's disks and network are not saturated.

### 3.6 Cameras and live streams (P1)

* FR-C1: Add a camera with a direct RTSP URL, or with ONVIF host, port,
  username and password. Coeus then asks the camera for its profiles and
  stream URIs (`GetProfiles` and `GetStreamUri` with WS-Security
  UsernameToken).
* FR-C2: Each camera has two streams: the **main stream** (for snapshots) and an
  optional **sub-stream** for detection. Running detection on the sub-stream
  keeps CPU use low.
* FR-C3: Each camera can be linked to its recordings folder, so archiving and
  live watching share one camera list.
* FR-C3a: **Test stream** buttons for the main and detection URLs. Each one grabs
  a frame and shows the resolution, frame rate and a preview.
* FR-C4: Show the latest snapshot and the connection status (online,
  reconnecting, or an error) for each camera. Reconnect automatically with
  backoff.
* FR-C5 (P2): Discover ONVIF cameras on the LAN with WS-Discovery.
* FR-C6 (P2): Import cameras from the Synology Surveillance Station Web API.
* FR-C7 (P3): Live view in the browser (MJPEG or WebRTC through go2rtc).

### 3.7 Zones and motion detection (P1)

* FR-Z1: Draw one or more polygon **zones** per camera on a snapshot. Points are
  stored as fractions of the image (0–1), so a zone works at any resolution.
* FR-Z2: Detect motion with background subtraction (MOG2), blur and erosion,
  restricted to the zone mask. Motion is reported when the moving area is larger
  than the zone's threshold (default 1% of the zone) for *K* frames in a row.
* FR-Z3: Settings for each camera: detection frame rate (default 5), minimum
  motion area, and a cooldown between events.
* FR-Z4: Zones can be *include* zones (only watch here) or *exclude* zones (for
  example trees or a road).
* FR-Z5 (P2): Person and vehicle detection (YOLO ONNX), so leaves and shadows
  do not cause false alarms.

### 3.8 Face detection and recognition (P1)

* FR-F1: **People library.** Add a person with a name and optional notes, and
  upload several face photos. Coeus finds the face in each photo, rejects photos
  with no face or with several faces, and stores the face embedding.
* FR-F2: **On-device backend (default):** OpenCV YuNet to detect faces and
  SFace for 128-dimensional embeddings, compared by cosine similarity (default
  threshold 0.363). It runs on the CPU in tens of milliseconds per face, sends
  no data off the NAS, and the model files are under 40 MB in total.
* FR-F3: When motion happens in a zone, detect faces in that zone's area. Match
  each face against the library and return the best match with a confidence, or
  "unknown".
* FR-F4: Save a snapshot and a face crop with every event.
* FR-F5: When the same person stays in view, merge the repeat sightings into
  one event within a time window (default 60 s).
* FR-F6 (P2): Unknown faces are grouped, and the user can name a group with one
  click, which adds it to the library.
* FR-F7 (P2): InsightFace (ArcFace) backend for better accuracy on machines
  that can afford it.

### 3.9 LLM providers (P1)

* FR-L1: Choose an LLM provider for visual tasks:
  * **Anthropic Claude** (default model `claude-opus-5-5`)
  * **OpenAI-compatible**: OpenAI, Google Gemini through its OpenAI-compatible
    endpoint, OpenRouter, and local **Ollama** or LM Studio (for example
    `llava` or `qwen2.5-vl`).
  * **None** (only on-device processing).
* FR-L2: Things an LLM can do:
  * *Describe* an event snapshot in one sentence for notifications, for example
    "A delivery rider in a red jacket is standing at the gate".
  * *Identify* a face by comparing the crop with reference photos of the known
    people. This is an alternative or a second opinion when the on-device
    match is below the threshold.
* FR-L3: The face-recognition mode is set for each camera: `local`, `llm`, or
  `local+llm-fallback`.
* FR-L4: Show a privacy warning when a cloud provider is chosen. Snapshots are
  sent only when a rule asks for it.
* FR-L5: Limit LLM calls per hour, so a busy scene cannot run up a bill.

### 3.10 Rules and actions (P1)

* FR-E1: A **rule** matches on event type (`motion`, `face_known`,
  `face_unknown`), camera, zone, people, a time-of-day window and days of the
  week. It has a cooldown.
* FR-E2: Action types:
  * **Webhook / HTTP API**: method, URL, headers and a JSON body template. This
    also covers n8n, Node-RED, Twilio and Telegram.
  * **Home Assistant**: call a service (`POST /api/services/<domain>/<service>`)
    or fire an event (`POST /api/events/coeus_event`), using a long-lived token.
  * **LLM describe**: adds a description to the event, which later actions can
    use as `{description}`.
  * (P2) MQTT publish, with Home Assistant MQTT discovery for sensors such as
    "last person at Main Gate".
  * (P2) Phone call or SMS through a provider (Twilio, or an Asterisk/FreePBX
    webhook).
* FR-E3: Templates can use `{event_type}`, `{camera}`, `{zone}`, `{person}`,
  `{confidence}`, `{time}`, `{description}`, `{snapshot_url}` and `{event_id}`.
* FR-E4: Every action run is saved with its status code and any error. Failed
  actions are retried up to 3 times.
* FR-E5: The user can test a rule from the UI by firing a fake event.

### 3.11 Events (P1)

* FR-V1: The event list shows the snapshot thumbnail, camera, zone, person and
  confidence, and can be filtered by camera, person, type and date.
* FR-V2: Delete events older than a set number of days (default 30), including
  their images.

### 3.12 Settings and operations (P1)

* FR-O1: All configuration is in the web UI and stored in SQLite under
  `/data`. Environment variables are only for bootstrap settings: secret key,
  admin account, time zone and paths.
* FR-O2: `/healthz` for the Docker health check, plus a dashboard showing CPU
  and disk use, workers, the job queue and the YouTube quota used.
* FR-O3: Structured logs go to stdout, which the Synology Container Manager
  log viewer shows.
* FR-O4 (P2): Back up and restore the configuration (people, zones, rules) as a
  zip file.

## 4. Non-functional requirements

| ID | Requirement |
|---|---|
| NF-1 | Runs on x86_64 Synology models with Container Manager (DSM 7.2+). The image also builds for arm64. |
| NF-2 | Image under about 600 MB. Idle RAM under 300 MB. Each camera watched on a 640×360 sub-stream at 5 fps uses at most about 10% of one Celeron J4125 core. |
| NF-3 | Merging is stream copy only and limited by disk I/O. A 12-hour part (about 6 GB) merges in a few minutes. |
| NF-4 | The recordings share is mounted read-only. Everything Coeus writes goes under `/data`. |
| NF-5 | Secrets (OAuth refresh token, Home Assistant token, LLM keys) stay in `/data` and are never shown in full in the UI or logs (P2: encrypted at rest). |
| NF-6 | Restarting the container loses no jobs. Interrupted merges start again and interrupted uploads resume. |
| NF-7 | The app sits behind DSM's reverse proxy for HTTPS. The `Not secure` warning in the screenshots goes away when a certificate is set up in DSM. |
| NF-8 | Face data is personal data. Uploaded photos and embeddings stay on the NAS, and the user can delete a person together with all their samples and events. |

## 5. Architecture

```
+--------------------- Docker container "coeus" ----------------------+
|  FastAPI web UI and REST API  <-- login session                     |
|        |                                                            |
|  SQLite (/data/coeus.db)  <-- users, cameras, zones, people,        |
|        |                      face samples, rules, events, jobs     |
|  +-----+----------------+   +-----------------------------------+   |
|  | Scheduler (APScheduler)|  | Camera monitor threads (1 each)  |   |
|  |  daily: plan jobs     |   |  RTSP -> motion(zone) -> faces   |   |
|  |  worker: merge->upload|   |   -> match -> event -> rules     |   |
|  +--+-------------+------+   +------+-----------------+---------+   |
|     | ffmpeg -c copy | YouTube API   | Face backend   | Actions     |
+-----+----------------+---------------+----------------+-------------+
      |                                 |                |
 /recordings (ro) = /volume1/surveillance   local / LLM   webhook / HA
```

* **Language:** Python 3.12. This gives first-class OpenCV, ffmpeg and Google
  API libraries.
* **Web:** FastAPI with server-rendered Jinja2 pages and a little vanilla JS
  (zone editor). No separate front-end build is needed.
* **Storage:** SQLite through SQLAlchemy 2.
* **Video:** the system ffmpeg and ffprobe.
* **Vision:** OpenCV (MOG2, YuNet, SFace).

## 6. Deployment on Synology

1. Create a `docker/coeus` folder on the NAS.
2. In Container Manager, choose **Project**, then **Create**, and paste
   `docker-compose.yml`.
3. Volumes:
   * `/volume1/surveillance:/recordings:ro`
   * `/volume1/docker/coeus:/data`
4. Set the environment variables `TZ`, `COEUS_SECRET_KEY`,
   `COEUS_ADMIN_PASSWORD`.
5. Open `http://<nas>:8765`, log in, connect YouTube, choose the cameras, add
   people and rules.

## 7. Release plan

| Release | Scope |
|---|---|
| **P1 (this work)** | Auth, recording library scanner, merge planner and executor, YouTube OAuth and upload with chapters and quota handling, daily scheduler, cameras (RTSP/ONVIF), zone editor, motion detection, local face recognition, people library, LLM providers (Claude and OpenAI-compatible), rules with webhook and Home Assistant actions, events list, Docker packaging. |
| P2 | Viewer role, API tokens, MQTT, phone/SMS actions, person/vehicle detection, grouping of unknown faces, time-lapse, ONVIF discovery, Surveillance Station API import, config backup, secrets encrypted at rest. |
| P3 | OIDC, burned-in timestamps with hardware encoding, live view through go2rtc, InsightFace backend, mobile push notifications. |

## 8. Open questions for the owner

1. Which cameras should be archived to YouTube? Should all of them be, given
   about 6 uploads a day under the default quota?
2. Should the videos be private (default) or unlisted? Unlisted needs a
   verified Google API project.
3. Which actions matter first: a Home Assistant notification, opening a gate
   relay for known people, or a phone call for unknown people at night?
4. Which Synology model is it (CPU architecture, RAM)? This decides how many
   live cameras can be analysed at once.
5. Should the original Surveillance Station recordings ever be deleted after
   upload? The default is no: Coeus never writes to that share.
