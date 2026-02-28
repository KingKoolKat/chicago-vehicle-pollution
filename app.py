import os
import json
import uuid
from typing import Dict, Any, List, Optional

import modal
from fastapi import Request

APP_NAME = "trucksense-inference"

# Build a container image with deps.
# Notes:
# - ultralytics pulls in torch and friends; for hackathon MVP this is okay.
# - opencv-python-headless avoids GUI deps.
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")  # helps OpenCV decode more video codecs
    .pip_install(
        "ultralytics==8.3.0",
        "opencv-python-headless==4.10.0.84",
        "numpy==1.26.4",
        "fastapi[standard]==0.116.1",
        "snowflake-connector-python==3.12.2",
    )
)

app = modal.App(APP_NAME, image=image)
snowflake_secret = modal.Secret.from_name("SNOWFLAKE")

# Persistent storage for uploaded videos (optional but handy).
vol = modal.Volume.from_name("trucksense-videos", create_if_missing=True)
VIDEO_DIR = "/data"
DEFAULT_SEARCH_LIMIT = int(os.getenv("SNOWFLAKE_RAG_TOP_K", "5"))
DEFAULT_CHAT_MODEL = os.getenv("SNOWFLAKE_CHAT_MODEL", "snowflake-arctic")
DEFAULT_SEARCH_COLUMNS = ["content", "source_url", "doc_id", "chunk_id"]
CAMERAS_TABLE = os.getenv("SNOWFLAKE_CAMERAS_TABLE", "CAMERAS")
CAMERA_INFO_TABLE = os.getenv("SNOWFLAKE_CAMERA_INFO_TABLE", "CAMERA_INFO")
RAG_DOCUMENTS_TABLE = os.getenv("SNOWFLAKE_RAG_TABLE", "RAG_DOCUMENTS")



def _snowflake_connect():
    import snowflake.connector

    required = ["SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing Snowflake env vars: {', '.join(missing)}")

    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        role=os.getenv("SNOWFLAKE_ROLE"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
        database=os.getenv("SNOWFLAKE_DATABASE"),
        schema=os.getenv("SNOWFLAKE_SCHEMA"),
    )


def _run_cortex_search(conn, question: str, limit: int) -> List[Dict[str, Any]]:
    search_service = os.getenv("SNOWFLAKE_SEARCH_SERVICE")
    if not search_service:
        raise RuntimeError("Missing env var: SNOWFLAKE_SEARCH_SERVICE")

    request_body = {
        "query": question,
        "columns": DEFAULT_SEARCH_COLUMNS,
        "limit": limit,
    }

    with conn.cursor() as cur:
        cur.execute(
            "SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW(%s, PARSE_JSON(%s))",
            (search_service, json.dumps(request_body)),
        )
        payload = cur.fetchone()[0]

    if isinstance(payload, str):
        payload = json.loads(payload)

    return payload.get("results", []) if isinstance(payload, dict) else []


def _query_rows(conn, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(query, params)
        cols = [desc[0].lower() for desc in cur.description]
        rows = cur.fetchall()
    return [dict(zip(cols, row)) for row in rows]


def _format_rows(title: str, rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not rows:
        return None

    lines = [title]
    for row in rows:
        pairs = [f"{k}={v}" for k, v in row.items() if v is not None]
        if pairs:
            lines.append(f"- {', '.join(pairs)}")

    return {"content": "\n".join(lines), "source_url": f"SNOWFLAKE:{title}"}


def _get_structured_context(conn, question: str) -> List[Dict[str, Any]]:
    del question  # reserved for future question-specific routing
    contexts: List[Dict[str, Any]] = []

    recent_rows = _query_rows(
        conn,
        f"""
        SELECT
          ci.camera_id,
          c.camera_name,
          c.latitude,
          c.longitude,
          ci.car_count,
          ci.bus_count,
          ci.truck_count,
          ci.motorcycle_count,
          ci.total_unique_vehicles,
          ci.peak_vehicles_per_frame,
          ci.recorded_at
        FROM {CAMERA_INFO_TABLE} ci
        LEFT JOIN {CAMERAS_TABLE} c ON c.camera_id = ci.camera_id
        ORDER BY ci.recorded_at DESC
        LIMIT 15
        """,
    )
    recent_context = _format_rows("RECENT_CAMERA_INFO", recent_rows)
    if recent_context:
        contexts.append(recent_context)

    hotspot_rows = _query_rows(
        conn,
        f"""
        SELECT
          c.camera_id,
          c.camera_name,
          AVG(ci.car_count) AS avg_car_count,
          AVG(ci.bus_count) AS avg_bus_count,
          AVG(ci.truck_count) AS avg_truck_count,
          AVG(ci.motorcycle_count) AS avg_motorcycle_count,
          AVG(ci.total_unique_vehicles) AS avg_total_unique_vehicles,
          AVG(
            (ci.car_count * 1.0) +
            (ci.bus_count * 4.0) +
            (ci.truck_count * 8.0) +
            (ci.motorcycle_count * 1.5)
          ) AS pollution_proxy_score
        FROM {CAMERAS_TABLE} c
        JOIN {CAMERA_INFO_TABLE} ci ON ci.camera_id = c.camera_id
        GROUP BY c.camera_id, c.camera_name
        ORDER BY pollution_proxy_score DESC
        LIMIT 5
        """,
    )
    hotspot_context = _format_rows("POLLUTION_HOTSPOTS", hotspot_rows)
    if hotspot_context:
        contexts.append(hotspot_context)

    rag_rows = _query_rows(
        conn,
        f"""
        SELECT
          doc_id,
          category,
          doc_content
        FROM {RAG_DOCUMENTS_TABLE}
        ORDER BY doc_id DESC
        LIMIT 15
        """,
    )
    rag_context = _format_rows("RAG_DOCUMENTS", rag_rows)
    if rag_context:
        contexts.append(rag_context)

    return contexts


def _parse_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _find_nearest_camera_id(conn, lat: Optional[float], lng: Optional[float]) -> Optional[int]:
    if lat is None or lng is None:
        return None

    rows = _query_rows(
        conn,
        f"""
        SELECT camera_id
        FROM {CAMERAS_TABLE}
        ORDER BY POWER(latitude - %s, 2) + POWER(longitude - %s, 2)
        LIMIT 1
        """,
        (lat, lng),
    )
    if not rows:
        return None
    return _parse_int(rows[0].get("camera_id"))


def _write_camera_info_record(conn, camera_id: Optional[int], out: Dict[str, Any]) -> None:
    if camera_id is None:
        return

    counts = out.get("counts_by_class", {}) or {}
    car_count = int(counts.get("car", 0))
    bus_count = int(counts.get("bus", 0))
    truck_count = int(counts.get("truck", 0))
    motorcycle_count = int(counts.get("motorcycle", 0))
    total_unique = int(out.get("total_unique_vehicles", 0))
    peak = int(out.get("peak_vehicles_in_frame", 0))

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {CAMERA_INFO_TABLE}
              (camera_id, car_count, bus_count, truck_count, motorcycle_count, total_unique_vehicles, peak_vehicles_per_frame, recorded_at)
            VALUES
              (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP())
            """,
            (camera_id, car_count, bus_count, truck_count, motorcycle_count, total_unique, peak),
        )


def _complete_with_context(conn, question: str, contexts: List[Dict[str, Any]]) -> str:
    context_lines = []
    for i, item in enumerate(contexts, start=1):
        content = str(item.get("content", "")).strip()
        if not content:
            # Allow structured rows that may not contain a plain "content" field.
            content = ", ".join(
                f"{k}={v}" for k, v in item.items() if k not in {"source_url"} and v is not None
            ).strip()
        source_url = str(item.get("source_url", "")).strip()
        if not content:
            continue
        label = source_url or f"doc-{item.get('doc_id', i)}:chunk-{item.get('chunk_id', i)}"
        context_lines.append(f"[{i}] {content}\nSOURCE: {label}")

    joined_context = "\n\n".join(context_lines) if context_lines else "No context found."

    prompt = (
        "You are an environmental assistant for Chicago vehicle pollution analysis.\n"
        "Use ONLY the context below. If the answer is not present in context, say you do not know.\n"
        "Keep the answer concise and include source bracket ids such as [1], [2] when relevant.\n\n"
        f"QUESTION:\n{question}\n\n"
        f"CONTEXT:\n{joined_context}"
    )

    model = os.getenv("SNOWFLAKE_CHAT_MODEL", DEFAULT_CHAT_MODEL)

    with conn.cursor() as cur:
        try:
            cur.execute("SELECT AI_COMPLETE(model => %s, prompt => %s)", (model, prompt))
        except Exception:
            # Compatibility fallback for accounts that have not enabled AI_COMPLETE yet.
            cur.execute("SELECT SNOWFLAKE.CORTEX.COMPLETE(%s, %s)", (model, prompt))

        result = cur.fetchone()[0]

    if isinstance(result, dict):
        if "content" in result:
            return str(result.get("content", ""))
        if "choices" in result and result["choices"]:
            first = result["choices"][0]
            return str(first.get("messages", "") or first.get("message", "") or "")
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                if "content" in parsed:
                    return str(parsed.get("content", ""))
                if "choices" in parsed and parsed["choices"]:
                    first = parsed["choices"][0]
                    return str(first.get("messages", "") or first.get("message", "") or result)
        except Exception:
            pass
        return result
    return str(result)


@app.cls(
    gpu="L4",  # good inference GPU to start; change to "A10" / "L40S" if needed
    volumes={VIDEO_DIR: vol},
    scaledown_window=300,  # keep warm briefly for faster demos
)
class CounterService:
    def _load_model(self):
        # Load model once per container (fast subsequent calls).
        from ultralytics import YOLO

        # You can swap to yolov8s.pt if accuracy matters and speed is still OK.
        self.model = YOLO("yolov8n.pt")

        # COCO class names
        self.names = self.model.names

        # Only count traffic-related classes for your pollution proxy.
        self.vehicle_classes = {"car", "truck", "bus", "motorcycle"}

    @modal.enter()
    def load_model(self):
        self._load_model()

    @modal.method()
    def count_video(self, video_path: str) -> Dict[str, Any]:
        """
        Runs YOLOv8 + ByteTrack on the video and returns:
          - unique counts per class (unique track IDs)
          - total unique vehicles
          - peak vehicles per frame (congestion proxy)
        """
        if not hasattr(self, "model"):
            self._load_model()

        # Ensure this container sees latest files committed by the uploader.
        vol.reload()

        from collections import defaultdict, Counter

        # Run tracking. Ultralytics supports ByteTrack via tracker="bytetrack.yaml". :contentReference[oaicite:2]{index=2}
        results = self.model.track(
            source=video_path,
            tracker="bytetrack.yaml",
            persist=True,
            verbose=False,
            conf=0.25,
            iou=0.5,
        )

        # Track-level aggregation
        # We'll:
        # - collect all (track_id -> list of class_names) to do majority vote
        # - track peak number of visible vehicle tracks per frame
        track_to_classes = defaultdict(list)
        peak_vehicles = 0

        for r in results:
            if r.boxes is None or r.boxes.id is None:
                continue

            ids = r.boxes.id.cpu().numpy().astype(int)
            cls = r.boxes.cls.cpu().numpy().astype(int)

            # vehicles visible this frame (unique IDs in this frame, filtered to vehicle classes)
            frame_vehicle_ids = set()

            for tid, c in zip(ids, cls):
                name = self.names[int(c)]
                if name in self.vehicle_classes:
                    track_to_classes[tid].append(name)
                    frame_vehicle_ids.add(tid)

            if len(frame_vehicle_ids) > peak_vehicles:
                peak_vehicles = len(frame_vehicle_ids)

        # Majority vote per track to reduce class flip noise
        counts_by_class = Counter()
        for tid, class_list in track_to_classes.items():
            if not class_list:
                continue
            final_class = Counter(class_list).most_common(1)[0][0]
            counts_by_class[final_class] += 1

        total_unique = sum(counts_by_class.values())

        return {
            "counts_by_class": dict(counts_by_class),
            "total_unique_vehicles": int(total_unique),
            "peak_vehicles_in_frame": int(peak_vehicles),
        }


@app.function(volumes={VIDEO_DIR: vol}, secrets=[snowflake_secret])
@modal.fastapi_endpoint(method="POST")
async def upload_and_count(request: Request) -> Dict[str, Any]:
    """
    HTTP endpoint:
      POST multipart/form-data with:
        - file: video
        - lat: float (optional)
        - lng: float (optional)
        - timestamp: string (optional)
    Returns JSON counts + echoes metadata.
    """
    form = await request.form()
    file = form.get("file")
    if file is None:
        return {"error": "Missing 'file' in form-data."}
    if not hasattr(file, "read"):
        return {"error": "Invalid 'file' in form-data."}

    lat = form.get("lat")
    lng = form.get("lng")
    timestamp = form.get("timestamp")
    camera_id = _parse_int(form.get("camera_id"))

    # Save to volume so the GPU method can read it
    vid_id = str(uuid.uuid4())
    save_path = os.path.join(VIDEO_DIR, f"{vid_id}.mp4")

    # Stream upload to disk
    with open(save_path, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)

    # Make sure volume persists
    await vol.commit.aio()

    # Run GPU inference
    svc = CounterService()
    out = await svc.count_video.remote.aio(save_path)

    # Persist detector output into Snowflake CAMERA_INFO when possible.
    # Password is expected via Modal secret; other Snowflake config can come from env.
    db_write_status = "skipped"
    try:
        conn = _snowflake_connect()
        parsed_lat = float(lat) if lat is not None and lat != "" else None
        parsed_lng = float(lng) if lng is not None and lng != "" else None
        resolved_camera_id = camera_id or _find_nearest_camera_id(conn, parsed_lat, parsed_lng)
        _write_camera_info_record(conn, resolved_camera_id, out)
        db_write_status = "ok" if resolved_camera_id is not None else "no_camera_id"
        conn.close()
    except Exception as exc:
        db_write_status = f"error: {exc}"

    return {
        "video_id": vid_id,
        "camera_id": camera_id,
        "lat": lat,
        "lng": lng,
        "timestamp": timestamp,
        "db_write_status": db_write_status,
        **out,
    }

@app.function(secrets=[snowflake_secret])
@modal.fastapi_endpoint(method="POST")
async def chat(request: Request) -> Dict[str, Any]:
    """
    HTTP endpoint:
      POST application/json with:
        - question: string
        - top_k: int (optional)
    Returns:
      - answer: LLM answer grounded in Snowflake Cortex Search results
      - citations: source metadata for retrieved chunks
    """
    body = await request.json()
    question = str(body.get("question", "")).strip()
    if not question:
        return {"error": "Missing 'question' in request body."}

    try:
        top_k = int(body.get("top_k", DEFAULT_SEARCH_LIMIT))
    except (TypeError, ValueError):
        top_k = DEFAULT_SEARCH_LIMIT

    top_k = max(1, min(top_k, 20))

    conn = None
    try:
        conn = _snowflake_connect()
        contexts: List[Dict[str, Any]] = []

        # Optional Cortex Search path (for text-based service). If unavailable, continue with structured context.
        search_service = os.getenv("SNOWFLAKE_SEARCH_SERVICE")
        if search_service:
            try:
                contexts.extend(_run_cortex_search(conn, question, top_k))
            except Exception:
                pass

        contexts.extend(_get_structured_context(conn, question))
        answer = _complete_with_context(conn, question, contexts)

        citations = [
            {
                "id": idx + 1,
                "source_url": item.get("source_url"),
                "doc_id": item.get("doc_id"),
                "chunk_id": item.get("chunk_id"),
            }
            for idx, item in enumerate(contexts)
        ]

        return {
            "question": question,
            "answer": answer,
            "citations": citations,
            "retrieved_chunks": len(contexts),
        }
    except Exception as exc:
        return {"error": f"Chat endpoint failed: {exc}"}
    finally:
        if conn is not None:
            conn.close()


@app.local_entrypoint()
def main():
    print("Deployed. Use: modal deploy app.py")
