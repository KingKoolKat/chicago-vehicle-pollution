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
