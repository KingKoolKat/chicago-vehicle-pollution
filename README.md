# "VanData" -- Chicago Vehicle Pollution

AI-powered traffic intelligence for Chicago: vehicle detection from media uploads, pollution-oriented traffic mapping, Snowflake-backed analytics, and a RAG chat assistant.

## What This Project Does

- Detects and counts vehicles (`car`, `bus`, `truck`, `motorcycle`) from uploaded videos and images.
- Persists traffic stats into Snowflake for map visualization and trend analysis.
- Serves a traffic map API with per-camera intensity and historical date selection.
- Provides a Snowflake-grounded RAG chat endpoint for environmental Q&A.
- Includes role-aware auth flows and field reporting workflows (resident + admin).

## Architecture At A Glance

- Backend: `Modal` + `FastAPI` endpoints in `app.py`
- CV model: `Ultralytics YOLO` + OpenCV
- Data layer: `Snowflake` (users, reports, cameras, traffic rows, RAG docs/chunks)
- Frontend: static site under `website/` (Tailwind, Leaflet, Chart.js)

## Repo Structure

```text
.
|-- app.py
|-- requirements.txt
|-- snowflake/
|   |-- sync_camera_data_to_rag.py
|-- website/
|   |-- index.html
|   |-- upload/
|   |-- heat-map/
|   |-- chatbot/
|   |-- login/
|   |-- profile/
|   `-- js/
`-- README.md
```

## Core APIs

All endpoints are defined in `app.py` as Modal FastAPI endpoints.

- `POST /upload_media_and_count`
  - Single file upload (image or video; auto-inferred or `media_type=image|video`)
- `POST /batch_upload_and_count`
  - Multi-video processing with controlled parallelism and optional date overwrite
- `GET /traffic_map`
  - Returns available dates, selected date, camera-level stats, and summary totals
- `POST /chat`
  - RAG Q&A powered by Snowflake Cortex Search + table fallbacks + LLM completion
- `POST /auth`
  - Auth/session/profile/admin operations via `action` payload
- `POST /report_ops`
  - Consolidated actions:
    - `submit_resident_report`
    - `admin_process_video`

## Required Environment Variables

At minimum:

- `SNOWFLAKE_ACCOUNT`
- `SNOWFLAKE_USER`
- `SNOWFLAKE_PASSWORD`

Commonly used (recommended):

- `SNOWFLAKE_ROLE`
- `SNOWFLAKE_WAREHOUSE`
- `SNOWFLAKE_DATABASE`
- `SNOWFLAKE_SCHEMA`
- `SNOWFLAKE_SEARCH_SERVICE`
- `SNOWFLAKE_CHAT_MODEL` (default: `openai-gpt-5`)
- `SNOWFLAKE_RAG_TOP_K` (default: `25`)
- `SNOWFLAKE_USERS_TABLE` (default: `MY_MODAL_DB.PUBLIC.USERS`)
- `SNOWFLAKE_REPORTS_TABLE` (default: `MY_MODAL_DB.PUBLIC.REPORTS`)
- `SNOWFLAKE_CAMERAS_TABLE` (default: `CAMERAS`)
- `SNOWFLAKE_CAMERA_INFO_TABLE` (default: `CAMERA_INFO`)
- `SNOWFLAKE_RAG_TABLE` (default: `RAG_DOCUMENTS`)
- `SNOWFLAKE_RAG_CHUNKS_TABLE` (default: `RAG_DOCUMENT_CHUNKS`)
- `BATCH_MAX_FILES` (default: `20`)
- `BATCH_DEFAULT_PARALLEL` (default: `4`)
- `BATCH_MAX_PARALLEL` (default: `8`)

## Deploy Backend (Modal)

1. Install and authenticate Modal.
2. Configure a Modal secret named `SNOWFLAKE` with the env vars above.
3. Deploy:

```bash
modal deploy app.py
```

## Run Frontend

Serve the `website/` directory using any static server:

```bash
cd website
python -m http.server 8080
```

Then open `http://localhost:8080`.

Frontend API endpoints can be overridden via globals (already used in HTML files), for example:

```html
<script>
  window.CHAT_API_URL = "https://<your-modal-app>-chat.modal.run";
  window.TRAFFIC_MAP_API_URL = "https://<your-modal-app>-traffic-map.modal.run";
  window.AUTH_API_URL = "https://<your-modal-app>-auth.modal.run";
  window.REPORT_OPS_API_URL = "https://<your-modal-app>-report-ops.modal.run";
  window.BATCH_VIDEO_API_URL = "https://<your-modal-app>-batch-upload-and-count.modal.run";
</script>
```

## API Examples

### 1) Upload one image/video

```bash
curl -X POST "https://<your-modal-endpoint>/upload_media_and_count" \
  -F "file=@/absolute/path/to/media.mp4" \
  -F "camera_id=12" \
  -F "lat=41.8781" \
  -F "lng=-87.6298" \
  -F "speed_mode=fast"
```

### 2) Batch upload videos

```bash
curl -X POST "https://<your-modal-endpoint>/batch_upload_and_count" \
  -F "files=@/absolute/path/to/video1.mp4" \
  -F "files=@/absolute/path/to/video2.mp4" \
  -F "camera_id=1" \
  -F "camera_id=2" \
  -F "recorded_date=2026-02-28" \
  -F "recorded_date=2026-02-28" \
  -F "max_parallel=4" \
  -F "overwrite_for_date=true"
```

### 3) Ask the chat assistant

```bash
curl -X POST "https://<your-modal-endpoint>/chat" \
  -H "Content-Type: application/json" \
  -d '{"question":"Which Chicago areas appear highest traffic this week?","top_k":25}'
```

### 4) Fetch traffic map payload

```bash
curl "https://<your-modal-endpoint>/traffic_map?date=2026-02-28"
```

## Frontend Pages

- `website/index.html`: landing page + injected heatmap/chart/chat sections
- `website/heat-map/index.html`: map + trends UI
- `website/chatbot/index.html`: chat-focused standalone page
- `website/upload/index.html`: resident reports + admin processing tools
- `website/login/index.html`: login/signup entry point
- `website/profile/index.html`: user profile/settings

## Notes For Contributors

- Keep endpoint contracts stable; the frontend relies on response key names.
- Prefer adding new operations to consolidated endpoints (`/auth`, `/report_ops`) when it keeps UX simpler.
- When changing Snowflake table shape, update both query logic and frontend assumptions.
