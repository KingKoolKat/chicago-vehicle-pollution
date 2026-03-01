# chicago-vehicle-pollution

## Snowflake RAG Chatbot (implemented)

The chatbot now supports a Retrieval-Augmented Generation flow:

1. Frontend sends user question to backend `/chat`.
2. Backend queries Snowflake Cortex Search for top matching chunks.
3. Backend calls Snowflake LLM (`AI_COMPLETE`, with fallback to `SNOWFLAKE.CORTEX.COMPLETE`) using retrieved context.
4. Frontend renders answer + source references.

## Files changed

- [app.py](C:\Users\slplm\Desktop\chicago-pollution-modal\chicago-vehicle-pollution\app.py)
- [website/js/main.js](C:\Users\slplm\Desktop\chicago-pollution-modal\chicago-vehicle-pollution\website\js\main.js)
- [snowflake/setup_rag.sql](C:\Users\slplm\Desktop\chicago-pollution-modal\chicago-vehicle-pollution\snowflake\setup_rag.sql)

## 1) Create Snowflake RAG objects

Run:

```sql
-- in Snowsight
-- run the script:
-- snowflake/setup_rag.sql
```

Set your backend env var `SNOWFLAKE_SEARCH_SERVICE` to the fully-qualified service name:

```text
CHI_POLLUTION_DB.RAG.CHI_POLLUTION_SEARCH
```

## 2) Configure backend secrets/env vars

Set these in your Modal deployment environment:

- `SNOWFLAKE_ACCOUNT`
- `SNOWFLAKE_USER`
- `SNOWFLAKE_PASSWORD`
- `SNOWFLAKE_WAREHOUSE` (example: `CHI_POLLUTION_WH`)
- `SNOWFLAKE_DATABASE` (example: `CHI_POLLUTION_DB`)
- `SNOWFLAKE_SCHEMA` (example: `RAG`)
- `SNOWFLAKE_SEARCH_SERVICE` (example above)

Optional:

- `SNOWFLAKE_ROLE`
- `SNOWFLAKE_CHAT_MODEL` (default: `snowflake-arctic`)
- `SNOWFLAKE_RAG_TOP_K` (default: `5`)

## 3) Deploy backend

```bash
modal deploy app.py
```

Use the deployed `chat` endpoint URL in frontend if not same-origin:

```html
<script>
  window.CHAT_API_URL = "https://<your-modal-endpoint>/chat";
</script>
```

If omitted, frontend defaults to `"/chat"`.

## 4) Vision counting endpoints

- `POST /upload_and_count`:
  video tracking mode (counts unique vehicles by track ID)
- `POST /upload_image_and_count`:
  still-image detection mode (counts detected vehicles in one frame)
- `POST /batch_upload_and_count`:
  batch video mode (processes multiple videos concurrently)

Example `curl` for still image:

```bash
curl -X POST "https://<your-modal-endpoint>/upload_image_and_count" \
  -F "file=@/absolute/path/to/image.jpg" \
  -F "lat=41.8781" \
  -F "lng=-87.6298" \
  -F "camera_id=1" \
  -F "speed_mode=fast"
```

Example `curl` for video:

```bash
curl -X POST "https://<your-modal-endpoint>/upload_and_count" \
  -F "file=@/absolute/path/to/video.mp4" \
  -F "lat=41.8781" \
  -F "lng=-87.6298" \
  -F "camera_id=1" \
  -F "speed_mode=fast"
```

Example `curl` for batch videos:

```bash
curl -X POST "https://<your-modal-endpoint>/batch_upload_and_count" \
  -F "files=@/absolute/path/to/video1.mp4" \
  -F "files=@/absolute/path/to/video2.mp4" \
  -F "files=@/absolute/path/to/video3.mp4" \
  -F "camera_id=1" \
  -F "camera_id=2" \
  -F "camera_id=3" \
  -F "speed_mode=fast" \
  -F "max_parallel=4"
```
