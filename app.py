import os
import json
import uuid
import datetime
import asyncio
import hashlib
import hmac
import secrets
import threading
from typing import Dict, Any, List, Optional, Tuple

import modal
from fastapi import Request

APP_NAME = "ecotrack-inference"

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

# Persistent storage for uploaded media files (videos + images).
vol = modal.Volume.from_name("ecotrack-videos", create_if_missing=True)
VIDEO_DIR = "/data"
auth_vol = modal.Volume.from_name("ecotrack-auth", create_if_missing=True)
AUTH_DIR = "/authdata"
AUTH_USERS_FILE = os.path.join(AUTH_DIR, "users.json")
AUTH_SESSIONS_FILE = os.path.join(AUTH_DIR, "sessions.json")
REPORTS_FILE = os.path.join(AUTH_DIR, "reports.json")
ADMIN_VIDEO_JOBS_FILE = os.path.join(AUTH_DIR, "admin_video_jobs.json")
AUTH_PBKDF2_ITERATIONS = 120000
AUTH_SALT_BYTES = 16
AUTH_SESSION_TTL_SECONDS = 60 * 60 * 24 * 14
_auth_lock = threading.Lock()
DEFAULT_SEARCH_LIMIT = int(os.getenv("SNOWFLAKE_RAG_TOP_K", "5"))
DEFAULT_CHAT_MODEL = os.getenv("SNOWFLAKE_CHAT_MODEL", "mistral-large2")
DEFAULT_SEARCH_COLUMNS = ["content", "source_url", "doc_id", "chunk_id"]
CAMERAS_TABLE = os.getenv("SNOWFLAKE_CAMERAS_TABLE", "CAMERAS")
CAMERA_INFO_TABLE = os.getenv("SNOWFLAKE_CAMERA_INFO_TABLE", "CAMERA_INFO")
RAG_DOCUMENTS_TABLE = os.getenv("SNOWFLAKE_RAG_TABLE", "RAG_DOCUMENTS")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
BATCH_MAX_FILES = max(1, int(os.getenv("BATCH_MAX_FILES", "20")))
BATCH_DEFAULT_PARALLEL = max(1, int(os.getenv("BATCH_DEFAULT_PARALLEL", "4")))
BATCH_MAX_PARALLEL = max(1, int(os.getenv("BATCH_MAX_PARALLEL", "8")))



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


def _persist_detector_output(
    out: Dict[str, Any],
    camera_id: Optional[int],
    lat: Optional[str],
    lng: Optional[str],
) -> str:
    db_write_status = "skipped"
    conn = None
    try:
        conn = _snowflake_connect()
        parsed_lat = float(lat) if lat is not None and lat != "" else None
        parsed_lng = float(lng) if lng is not None and lng != "" else None
        resolved_camera_id = (
            camera_id
            if camera_id is not None
            else _find_nearest_camera_id(conn, parsed_lat, parsed_lng)
        )
        _write_camera_info_record(conn, resolved_camera_id, out)
        db_write_status = "ok" if resolved_camera_id is not None else "no_camera_id"
    except Exception as exc:
        db_write_status = f"error: {exc}"
    finally:
        if conn is not None:
            conn.close()
    return db_write_status


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _traffic_level(intensity: float) -> str:
    if intensity >= 0.67:
        return "heavy"
    if intensity >= 0.34:
        return "moderate"
    return "light"


def _get_available_traffic_dates(conn) -> List[str]:
    rows = _query_rows(
        conn,
        f"""
        SELECT TO_CHAR(DATE(recorded_at), 'YYYY-MM-DD') AS traffic_date
        FROM {CAMERA_INFO_TABLE}
        GROUP BY 1
        ORDER BY 1
        """,
    )
    return [str(row["traffic_date"]) for row in rows if row.get("traffic_date")]


def _get_traffic_camera_rows(conn, selected_date: str) -> List[Dict[str, Any]]:
    return _query_rows(
        conn,
        f"""
        SELECT
          c.camera_id,
          c.camera_name,
          c.latitude,
          c.longitude,
          COALESCE(SUM(ci.car_count), 0) AS car_count,
          COALESCE(SUM(ci.bus_count), 0) AS bus_count,
          COALESCE(SUM(ci.truck_count), 0) AS truck_count,
          COALESCE(SUM(ci.motorcycle_count), 0) AS motorcycle_count,
          COALESCE(SUM(ci.total_unique_vehicles), 0) AS total_unique_vehicles,
          COALESCE(MAX(ci.peak_vehicles_per_frame), 0) AS peak_vehicles_per_frame
        FROM {CAMERAS_TABLE} c
        LEFT JOIN {CAMERA_INFO_TABLE} ci
          ON ci.camera_id = c.camera_id
         AND DATE(ci.recorded_at) = TO_DATE(%s)
        WHERE c.latitude IS NOT NULL
          AND c.longitude IS NOT NULL
        GROUP BY c.camera_id, c.camera_name, c.latitude, c.longitude
        ORDER BY total_unique_vehicles DESC, c.camera_id ASC
        """,
        (selected_date,),
    )


def _build_traffic_payload(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    cameras: List[Dict[str, Any]] = []
    max_total = 0
    totals = {
        "car_count": 0,
        "bus_count": 0,
        "truck_count": 0,
        "motorcycle_count": 0,
        "total_unique_vehicles": 0,
    }

    for row in rows:
        car_count = _to_int(row.get("car_count"))
        bus_count = _to_int(row.get("bus_count"))
        truck_count = _to_int(row.get("truck_count"))
        motorcycle_count = _to_int(row.get("motorcycle_count"))
        summed_by_class = car_count + bus_count + truck_count + motorcycle_count
        total_unique = max(_to_int(row.get("total_unique_vehicles")), summed_by_class)
        max_total = max(max_total, total_unique)

        totals["car_count"] += car_count
        totals["bus_count"] += bus_count
        totals["truck_count"] += truck_count
        totals["motorcycle_count"] += motorcycle_count
        totals["total_unique_vehicles"] += total_unique

        cameras.append(
            {
                "camera_id": _to_int(row.get("camera_id")),
                "camera_name": str(row.get("camera_name") or f"Camera {_to_int(row.get('camera_id'))}"),
                "latitude": _to_float(row.get("latitude")),
                "longitude": _to_float(row.get("longitude")),
                "car_count": car_count,
                "bus_count": bus_count,
                "truck_count": truck_count,
                "motorcycle_count": motorcycle_count,
                "total_unique_vehicles": total_unique,
                "peak_vehicles_per_frame": _to_int(row.get("peak_vehicles_per_frame")),
            }
        )

    heavy_count = 0
    moderate_count = 0
    light_count = 0
    scale_denom = max_total if max_total > 0 else 1

    for camera in cameras:
        intensity = camera["total_unique_vehicles"] / scale_denom
        level = _traffic_level(intensity)
        camera["intensity"] = round(float(intensity), 4)
        camera["traffic_level"] = level
        if level == "heavy":
            heavy_count += 1
        elif level == "moderate":
            moderate_count += 1
        else:
            light_count += 1

    return {
        "cameras": cameras,
        "summary": {
            "camera_count": len(cameras),
            "heavy_count": heavy_count,
            "moderate_count": moderate_count,
            "light_count": light_count,
            "max_total_unique_vehicles": max_total,
            **totals,
        },
    }


def _image_suffix(filename: Optional[str]) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in IMAGE_EXTENSIONS:
        return ext
    return ".jpg"


def _read_json_file(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception:
        return default


def _write_json_file(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=True, indent=2)


def _auth_load_state() -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    users = _read_json_file(AUTH_USERS_FILE, [])
    sessions = _read_json_file(AUTH_SESSIONS_FILE, {})
    if not isinstance(users, list):
        users = []
    if not isinstance(sessions, dict):
        sessions = {}
    return users, sessions


def _auth_save_state(users: List[Dict[str, Any]], sessions: Dict[str, Dict[str, Any]]) -> None:
    _write_json_file(AUTH_USERS_FILE, users)
    _write_json_file(AUTH_SESSIONS_FILE, sessions)


def _auth_normalize_email(email: Any) -> str:
    return str(email or "").strip().lower()


def _auth_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _auth_make_hash(password: str, salt_hex: str) -> str:
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        AUTH_PBKDF2_ITERATIONS,
    )
    return derived.hex()


def _auth_new_password_record(password: str) -> Tuple[str, str]:
    salt_hex = secrets.token_hex(AUTH_SALT_BYTES)
    return salt_hex, _auth_make_hash(password, salt_hex)


def _auth_public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": user.get("id"),
        "name": user.get("name", ""),
        "email": user.get("email", ""),
        "provider": user.get("provider", "local"),
        "role": user.get("role", "resident"),
        "avatarUrl": user.get("avatar_url", ""),
    }


def _auth_find_user_by_email(users: List[Dict[str, Any]], email: str) -> Tuple[int, Optional[Dict[str, Any]]]:
    clean = _auth_normalize_email(email)
    for idx, user in enumerate(users):
        if _auth_normalize_email(user.get("email")) == clean:
            return idx, user
    return -1, None


def _auth_find_user_by_id(users: List[Dict[str, Any]], user_id: str) -> Tuple[int, Optional[Dict[str, Any]]]:
    for idx, user in enumerate(users):
        if str(user.get("id")) == str(user_id):
            return idx, user
    return -1, None


def _auth_prune_sessions(sessions: Dict[str, Dict[str, Any]]) -> bool:
    now = datetime.datetime.now(datetime.timezone.utc)
    changed = False
    for token in list(sessions.keys()):
        raw_exp = sessions[token].get("expires_at")
        try:
            expires_at = datetime.datetime.fromisoformat(str(raw_exp).replace("Z", "+00:00"))
        except Exception:
            expires_at = None
        if not expires_at or expires_at <= now:
            sessions.pop(token, None)
            changed = True
    return changed


def _auth_create_session(sessions: Dict[str, Dict[str, Any]], user_id: str) -> str:
    token = secrets.token_urlsafe(48)
    expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=AUTH_SESSION_TTL_SECONDS)
    sessions[token] = {
        "user_id": user_id,
        "expires_at": expires.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    return token


def _auth_get_user_from_token(
    users: List[Dict[str, Any]],
    sessions: Dict[str, Dict[str, Any]],
    token: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    record = sessions.get(token)
    if not record:
        return None
    _, user = _auth_find_user_by_id(users, str(record.get("user_id", "")))
    return user


def _form_values(form: Any, key: str) -> List[str]:
    values: List[str] = []
    if hasattr(form, "getlist"):
        for value in form.getlist(key):
            if value is None or value == "":
                continue
            values.append(str(value))
    if values:
        return values
    value = form.get(key)
    if value is None or value == "":
        return []
    return [str(value)]


def _pick_index_value(values: List[str], index: int) -> Optional[str]:
    if not values:
        return None
    if index < len(values):
        return values[index]
    return values[-1]


async def _save_upload_files(files: List[Any], suffix: str) -> List[Dict[str, str]]:
    saved: List[Dict[str, str]] = []
    for file in files:
        file_id = str(uuid.uuid4())
        save_path = os.path.join(VIDEO_DIR, f"{file_id}{suffix}")
        filename = str(getattr(file, "filename", "") or "")

        with open(save_path, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)

        saved.append(
            {
                "file_id": file_id,
                "save_path": save_path,
                "filename": filename,
            }
        )

    await vol.commit.aio()
    return saved


async def _save_upload_file(file: Any, suffix: str) -> Tuple[str, str]:
    saved = await _save_upload_files([file], suffix)
    file_id = saved[0]["file_id"]
    save_path = saved[0]["save_path"]
    return file_id, save_path


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
        # COCO class IDs for vehicle classes: car, motorcycle, bus, truck.
        self.vehicle_class_ids = [2, 3, 5, 7]

    @modal.enter()
    def load_model(self):
        self._load_model()

    @modal.method()
    def count_video(self, video_path: str, speed_mode: str = "standard") -> Dict[str, Any]:
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

        requested_mode = str(speed_mode or "standard").strip().lower()
        if requested_mode in {"fast", "faster"}:
            # Faster mode prioritizes throughput for long videos.
            mode = "fast"
            track_conf = 0.30
            track_iou = 0.45
            track_imgsz = 416
            track_vid_stride = 3
        else:
            mode = "standard"
            track_conf = 0.25
            track_iou = 0.5
            track_imgsz = 512
            track_vid_stride = 1

        # Run tracking. Ultralytics supports ByteTrack via tracker="bytetrack.yaml".
        results = self.model.track(
            source=video_path,
            tracker="bytetrack.yaml",
            persist=True,
            stream=True,
            verbose=False,
            classes=self.vehicle_class_ids,
            imgsz=track_imgsz,
            vid_stride=track_vid_stride,
            conf=track_conf,
            iou=track_iou,
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
            "speed_mode": mode,
            "counts_by_class": dict(counts_by_class),
            "total_unique_vehicles": int(total_unique),
            "peak_vehicles_in_frame": int(peak_vehicles),
        }

    @modal.method()
    def count_image(self, image_path: str, speed_mode: str = "standard") -> Dict[str, Any]:
        """
        Runs YOLOv8 detection on a single image and returns:
          - detected counts per class
          - total detected vehicles
        """
        if not hasattr(self, "model"):
            self._load_model()

        # Ensure this container sees latest files committed by the uploader.
        vol.reload()

        from collections import Counter

        requested_mode = str(speed_mode or "standard").strip().lower()
        if requested_mode in {"fast", "faster"}:
            mode = "fast"
            detect_conf = 0.30
            detect_iou = 0.45
            detect_imgsz = 416
        else:
            mode = "standard"
            detect_conf = 0.25
            detect_iou = 0.5
            detect_imgsz = 512

        results = self.model.predict(
            source=image_path,
            verbose=False,
            classes=self.vehicle_class_ids,
            imgsz=detect_imgsz,
            conf=detect_conf,
            iou=detect_iou,
        )

        counts_by_class = Counter()
        for r in results:
            if r.boxes is None:
                continue
            classes = r.boxes.cls.cpu().numpy().astype(int)
            for c in classes:
                name = self.names[int(c)]
                if name in self.vehicle_classes:
                    counts_by_class[name] += 1

        total_detected = sum(counts_by_class.values())
        return {
            "speed_mode": mode,
            "counts_by_class": dict(counts_by_class),
            "total_unique_vehicles": int(total_detected),
            "peak_vehicles_in_frame": int(total_detected),
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
        - speed_mode: "standard" | "fast" (optional, default "standard")
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
    speed_mode = str(form.get("speed_mode") or form.get("speed") or "standard").strip().lower()

    # Save to volume so the GPU method can read it
    vid_id, save_path = await _save_upload_file(file, ".mp4")

    # Run GPU inference
    svc = CounterService()
    out = await svc.count_video.remote.aio(save_path, speed_mode=speed_mode)

    # Persist detector output into Snowflake CAMERA_INFO when possible.
    db_write_status = _persist_detector_output(out, camera_id, lat, lng)

    return {
        "video_id": vid_id,
        "camera_id": camera_id,
        "lat": lat,
        "lng": lng,
        "timestamp": timestamp,
        "db_write_status": db_write_status,
        **out,
    }


@app.function(volumes={VIDEO_DIR: vol}, secrets=[snowflake_secret])
@modal.fastapi_endpoint(method="POST")
async def batch_upload_and_count(request: Request) -> Dict[str, Any]:
    """
    HTTP endpoint:
      POST multipart/form-data with:
        - files: one or more video files (repeat this key for multiple files)
        - speed_mode: "standard" | "fast" (optional, default "standard")
        - max_parallel: int (optional, default env or 4)
        - camera_id / lat / lng / timestamp:
          optional metadata; can be repeated to map by file index

    Returns one result per video and aggregate totals.
    """
    form = await request.form()

    files: List[Any] = []
    if hasattr(form, "getlist"):
        files = [f for f in form.getlist("files") if f is not None]
    if not files:
        single = form.get("file")
        if single is not None:
            files = [single]

    if not files:
        return {"error": "Missing 'files' (or 'file') in form-data."}
    if len(files) > BATCH_MAX_FILES:
        return {"error": f"Too many files. Max allowed per request is {BATCH_MAX_FILES}."}

    invalid_count = sum(0 if hasattr(file, "read") else 1 for file in files)
    if invalid_count:
        return {"error": "Invalid file payload in form-data. Ensure all entries are files."}

    requested_mode = str(form.get("speed_mode") or form.get("speed") or "standard").strip().lower()
    speed_mode = "fast" if requested_mode in {"fast", "faster"} else "standard"

    requested_parallel = _to_int(form.get("max_parallel"), default=BATCH_DEFAULT_PARALLEL)
    max_parallel = max(1, min(requested_parallel, BATCH_MAX_PARALLEL))

    camera_values = _form_values(form, "camera_id") or _form_values(form, "camera_ids")
    lat_values = _form_values(form, "lat") or _form_values(form, "lats")
    lng_values = _form_values(form, "lng") or _form_values(form, "lngs")
    timestamp_values = _form_values(form, "timestamp") or _form_values(form, "timestamps")

    # Save all files first, then commit once to reduce volume overhead.
    saved_files = await _save_upload_files(files, ".mp4")

    svc = CounterService()
    semaphore = asyncio.Semaphore(max_parallel)

    async def _run_single(save_path: str) -> Dict[str, Any]:
        async with semaphore:
            return await svc.count_video.remote.aio(save_path, speed_mode=speed_mode)

    raw_outputs = await asyncio.gather(
        *[_run_single(saved["save_path"]) for saved in saved_files],
        return_exceptions=True,
    )

    aggregate_counts = {
        "car": 0,
        "bus": 0,
        "truck": 0,
        "motorcycle": 0,
    }
    aggregate_total = 0
    aggregate_peak = 0
    success_count = 0
    results: List[Dict[str, Any]] = []

    for idx, (saved, output) in enumerate(zip(saved_files, raw_outputs)):
        camera_id = _parse_int(_pick_index_value(camera_values, idx))
        lat = _pick_index_value(lat_values, idx)
        lng = _pick_index_value(lng_values, idx)
        timestamp = _pick_index_value(timestamp_values, idx)

        item_result: Dict[str, Any] = {
            "index": idx,
            "video_id": saved["file_id"],
            "filename": saved["filename"],
            "camera_id": camera_id,
            "lat": lat,
            "lng": lng,
            "timestamp": timestamp,
        }

        if isinstance(output, Exception):
            item_result["error"] = str(output)
            item_result["db_write_status"] = "skipped"
            results.append(item_result)
            continue

        db_write_status = _persist_detector_output(output, camera_id, lat, lng)
        counts = output.get("counts_by_class", {}) or {}

        for vehicle_type in aggregate_counts:
            aggregate_counts[vehicle_type] += int(counts.get(vehicle_type, 0))

        total_unique = int(output.get("total_unique_vehicles", 0))
        peak_vehicles = int(output.get("peak_vehicles_in_frame", 0))
        aggregate_total += total_unique
        aggregate_peak = max(aggregate_peak, peak_vehicles)
        success_count += 1

        item_result["db_write_status"] = db_write_status
        item_result.update(output)
        results.append(item_result)

    error_count = len(results) - success_count
    return {
        "speed_mode": speed_mode,
        "max_parallel": max_parallel,
        "requested_file_count": len(files),
        "processed_file_count": len(results),
        "summary": {
            "success_count": success_count,
            "error_count": error_count,
            "counts_by_class": aggregate_counts,
            "total_unique_vehicles": aggregate_total,
            "peak_vehicles_in_frame": aggregate_peak,
        },
        "results": results,
    }


@app.function(volumes={VIDEO_DIR: vol}, secrets=[snowflake_secret])
@modal.fastapi_endpoint(method="POST")
async def upload_image_and_count(request: Request) -> Dict[str, Any]:
    """
    HTTP endpoint:
      POST multipart/form-data with:
        - file: image
        - lat: float (optional)
        - lng: float (optional)
        - timestamp: string (optional)
        - camera_id: int (optional)
        - speed_mode: "standard" | "fast" (optional, default "standard")
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
    speed_mode = str(form.get("speed_mode") or form.get("speed") or "standard").strip().lower()
    suffix = _image_suffix(getattr(file, "filename", ""))

    # Save to volume so the GPU method can read it
    image_id, save_path = await _save_upload_file(file, suffix)

    # Run GPU inference
    svc = CounterService()
    out = await svc.count_image.remote.aio(save_path, speed_mode=speed_mode)

    # Persist detector output into Snowflake CAMERA_INFO when possible.
    db_write_status = _persist_detector_output(out, camera_id, lat, lng)

    return {
        "image_id": image_id,
        "camera_id": camera_id,
        "lat": lat,
        "lng": lng,
        "timestamp": timestamp,
        "db_write_status": db_write_status,
        **out,
    }


@app.function(secrets=[snowflake_secret])
@modal.fastapi_endpoint(method="GET")
async def traffic_map(request: Request) -> Dict[str, Any]:
    """
    HTTP endpoint:
      GET with optional query params:
        - date: YYYY-MM-DD (optional)
    Returns:
      - selected date, available dates
      - per-camera traffic counts and normalized intensity
      - summary totals
    """
    requested_date = _parse_date(request.query_params.get("date"))
    conn = None
    try:
        conn = _snowflake_connect()
        available_dates = _get_available_traffic_dates(conn)
        if not available_dates:
            return {
                "selected_date": None,
                "available_dates": [],
                "cameras": [],
                "summary": {
                    "camera_count": 0,
                    "heavy_count": 0,
                    "moderate_count": 0,
                    "light_count": 0,
                    "max_total_unique_vehicles": 0,
                    "car_count": 0,
                    "bus_count": 0,
                    "truck_count": 0,
                    "motorcycle_count": 0,
                    "total_unique_vehicles": 0,
                },
            }

        selected_date = requested_date if requested_date in available_dates else available_dates[-1]
        rows = _get_traffic_camera_rows(conn, selected_date)
        payload = _build_traffic_payload(rows)
        payload["selected_date"] = selected_date
        payload["available_dates"] = available_dates
        payload["requested_date"] = requested_date
        return payload
    except Exception as exc:
        return {"error": f"Traffic map endpoint failed: {exc}"}
    finally:
        if conn is not None:
            conn.close()


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


@app.function(volumes={AUTH_DIR: auth_vol})
@modal.fastapi_endpoint(method="POST")
async def auth(request: Request) -> Dict[str, Any]:
    """
    HTTP endpoint:
      POST application/json with:
        - action: signup|login|google|session|logout|update_profile|update_password|get_user
        - token: string (required for session-bound actions)
        - payload fields for each action
    Persists users/sessions in files on a Modal volume.
    """
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "message": "Invalid JSON body."}

    action = str((body or {}).get("action", "")).strip().lower()
    if not action:
        return {"ok": False, "message": "Missing 'action'."}

    should_commit = False

    with _auth_lock:
        users, sessions = _auth_load_state()
        if _auth_prune_sessions(sessions):
            should_commit = True

        if action == "signup":
            name = str(body.get("name", "")).strip()
            email = _auth_normalize_email(body.get("email"))
            password = str(body.get("password", "")).strip()

            if not name:
                return {"ok": False, "message": "Name is required."}
            if not email or "@" not in email:
                return {"ok": False, "message": "Valid email is required."}
            if len(password) < 8:
                return {"ok": False, "message": "Password must be at least 8 characters."}

            _, existing = _auth_find_user_by_email(users, email)
            if existing:
                return {"ok": False, "message": "An account with this email already exists."}

            salt_hex, password_hash = _auth_new_password_record(password)
            user = {
                "id": str(uuid.uuid4()),
                "name": name,
                "email": email,
                "salt": salt_hex,
                "password_hash": password_hash,
                "role": "resident",
                "avatar_url": "",
                "provider": "local",
                "created_at": _auth_now_iso(),
            }
            users.append(user)
            token = _auth_create_session(sessions, user["id"])
            _auth_save_state(users, sessions)
            should_commit = True
            response = {"ok": True, "user": _auth_public_user(user), "token": token}

        elif action == "login":
            email = _auth_normalize_email(body.get("email"))
            password = str(body.get("password", "")).strip()
            _, user = _auth_find_user_by_email(users, email)
            if not user:
                return {"ok": False, "message": "Incorrect email or password."}

            if user.get("provider", "local") != "local":
                return {"ok": False, "message": "Use Google sign-in for this account."}

            salt_hex = str(user.get("salt", ""))
            expected_hash = str(user.get("password_hash", ""))
            provided_hash = _auth_make_hash(password, salt_hex) if salt_hex and expected_hash else ""
            if not expected_hash or not hmac.compare_digest(provided_hash, expected_hash):
                return {"ok": False, "message": "Incorrect email or password."}

            token = _auth_create_session(sessions, str(user["id"]))
            _auth_save_state(users, sessions)
            should_commit = True
            response = {"ok": True, "user": _auth_public_user(user), "token": token}

        elif action == "google":
            profile = body.get("profile") or {}
            email = _auth_normalize_email(profile.get("email"))
            if not email:
                return {"ok": False, "message": "Google profile missing email."}

            idx, user = _auth_find_user_by_email(users, email)
            if user and user.get("provider") == "local":
                return {"ok": False, "message": "Account exists with password. Log in with email/password."}

            if not user:
                user = {
                    "id": str(profile.get("sub") or uuid.uuid4()),
                    "name": str(profile.get("name") or email.split("@")[0]),
                    "email": email,
                    "salt": "",
                    "password_hash": "",
                    "role": "resident",
                    "avatar_url": str(profile.get("picture") or ""),
                    "provider": "google",
                    "created_at": _auth_now_iso(),
                }
                users.append(user)
            else:
                users[idx]["provider"] = "google"
                users[idx]["salt"] = ""
                users[idx]["password_hash"] = ""
                users[idx]["name"] = str(profile.get("name") or users[idx].get("name") or "")
                users[idx]["avatar_url"] = str(profile.get("picture") or users[idx].get("avatar_url") or "")
                user = users[idx]

            token = _auth_create_session(sessions, str(user["id"]))
            _auth_save_state(users, sessions)
            should_commit = True
            response = {"ok": True, "user": _auth_public_user(user), "token": token}

        elif action == "session":
            token = str(body.get("token", "")).strip()
            user = _auth_get_user_from_token(users, sessions, token)
            if not user:
                return {"ok": False, "message": "Invalid session."}
            response = {"ok": True, "user": _auth_public_user(user)}

        elif action == "logout":
            token = str(body.get("token", "")).strip()
            if token and token in sessions:
                sessions.pop(token, None)
                _auth_save_state(users, sessions)
                should_commit = True
            response = {"ok": True}

        elif action == "update_profile":
            token = str(body.get("token", "")).strip()
            user = _auth_get_user_from_token(users, sessions, token)
            if not user:
                return {"ok": False, "message": "You need to be logged in."}

            clean_name = str(body.get("name", "")).strip()
            if not clean_name:
                return {"ok": False, "message": "Name is required."}
            clean_role = "admin" if str(body.get("role", "")).strip().lower() == "admin" else "resident"
            clean_avatar = str(body.get("avatarUrl", "")).strip()

            idx, _ = _auth_find_user_by_id(users, str(user.get("id")))
            if idx < 0:
                return {"ok": False, "message": "User record not found."}

            users[idx]["name"] = clean_name
            users[idx]["role"] = clean_role
            users[idx]["avatar_url"] = clean_avatar
            _auth_save_state(users, sessions)
            should_commit = True
            response = {"ok": True, "user": _auth_public_user(users[idx])}

        elif action == "update_password":
            token = str(body.get("token", "")).strip()
            user = _auth_get_user_from_token(users, sessions, token)
            if not user:
                return {"ok": False, "message": "You need to be logged in."}
            if user.get("provider", "local") != "local":
                return {"ok": False, "message": "Password changes are only available for local accounts."}

            current_password = str(body.get("currentPassword", "")).strip()
            new_password = str(body.get("newPassword", "")).strip()
            if len(new_password) < 8:
                return {"ok": False, "message": "New password must be at least 8 characters."}

            salt_hex = str(user.get("salt", ""))
            expected_hash = str(user.get("password_hash", ""))
            provided_hash = _auth_make_hash(current_password, salt_hex) if salt_hex and expected_hash else ""
            if not expected_hash or not hmac.compare_digest(provided_hash, expected_hash):
                return {"ok": False, "message": "Current password is incorrect."}

            next_salt, next_hash = _auth_new_password_record(new_password)
            idx, _ = _auth_find_user_by_id(users, str(user.get("id")))
            if idx < 0:
                return {"ok": False, "message": "User record not found."}

            users[idx]["salt"] = next_salt
            users[idx]["password_hash"] = next_hash
            _auth_save_state(users, sessions)
            should_commit = True
            response = {"ok": True, "user": _auth_public_user(users[idx])}

        elif action == "get_user":
            token = str(body.get("token", "")).strip()
            requester = _auth_get_user_from_token(users, sessions, token)
            if not requester:
                return {"ok": False, "message": "You need to be logged in."}

            user_id = str(body.get("userId", "")).strip()
            _, found = _auth_find_user_by_id(users, user_id)
            if not found:
                return {"ok": False, "message": "User record not found."}
            response = {"ok": True, "user": _auth_public_user(found)}

        else:
            return {"ok": False, "message": f"Unsupported action '{action}'."}

    if should_commit:
        await auth_vol.commit.aio()
    return response


@app.function(volumes={VIDEO_DIR: vol, AUTH_DIR: auth_vol})
@modal.fastapi_endpoint(method="POST")
async def submit_resident_report(request: Request) -> Dict[str, Any]:
    """
    HTTP endpoint:
      POST multipart/form-data with:
        - token: auth session token
        - file: image
        - notes: optional text
        - lat/lng: optional
        - timestamp: optional
    Runs image model, stores report metadata + stats in server file.
    """
    form = await request.form()
    token = str(form.get("token") or "").strip()
    notes = str(form.get("notes") or "").strip()
    lat = form.get("lat")
    lng = form.get("lng")
    timestamp = str(form.get("timestamp") or "").strip() or _auth_now_iso()
    file = form.get("file")

    if not token:
        return {"ok": False, "message": "Missing session token."}
    if file is None or not hasattr(file, "read"):
        return {"ok": False, "message": "Missing image file."}

    session_state_changed = False
    with _auth_lock:
        users, sessions = _auth_load_state()
        session_state_changed = _auth_prune_sessions(sessions)
        user = _auth_get_user_from_token(users, sessions, token)
        if session_state_changed:
            _auth_save_state(users, sessions)
    if session_state_changed:
        await auth_vol.commit.aio()
    if not user:
        return {"ok": False, "message": "You need to be logged in."}

    suffix = _image_suffix(getattr(file, "filename", ""))
    image_id, save_path = await _save_upload_file(file, suffix)

    svc = CounterService()
    stats = await svc.count_image.remote.aio(save_path, speed_mode="standard")

    report = {
        "id": str(uuid.uuid4()),
        "created_at": _auth_now_iso(),
        "timestamp": timestamp,
        "user_id": user.get("id"),
        "user_name": user.get("name"),
        "user_email": user.get("email"),
        "user_role": user.get("role", "resident"),
        "notes": notes,
        "lat": lat,
        "lng": lng,
        "image_id": image_id,
        "image_filename": str(getattr(file, "filename", "") or ""),
        "stats": stats,
    }

    with _auth_lock:
        reports = _read_json_file(REPORTS_FILE, [])
        if not isinstance(reports, list):
            reports = []
        reports.append(report)
        _write_json_file(REPORTS_FILE, reports)
    await auth_vol.commit.aio()

    return {"ok": True, "report": report}


@app.function(volumes={AUTH_DIR: auth_vol})
@modal.fastapi_endpoint(method="POST")
async def list_reports(request: Request) -> Dict[str, Any]:
    """
    HTTP endpoint:
      POST application/json with:
        - token: auth session token
    Returns all resident reports for admin users.
    """
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "message": "Invalid JSON body."}

    token = str((body or {}).get("token") or "").strip()
    if not token:
        return {"ok": False, "message": "Missing session token."}

    changed = False
    user = None
    reports_sorted: List[Dict[str, Any]] = []
    with _auth_lock:
        users, sessions = _auth_load_state()
        changed = _auth_prune_sessions(sessions)
        user = _auth_get_user_from_token(users, sessions, token)
        if user and str(user.get("role", "resident")) == "admin":
            reports = _read_json_file(REPORTS_FILE, [])
            if not isinstance(reports, list):
                reports = []
            reports_sorted = sorted(
                reports,
                key=lambda r: str(r.get("created_at") or r.get("timestamp") or ""),
                reverse=True,
            )
        if changed:
            _auth_save_state(users, sessions)
    if changed:
        await auth_vol.commit.aio()
    if not user:
        return {"ok": False, "message": "You need to be logged in."}
    if str(user.get("role", "resident")) != "admin":
        return {"ok": False, "message": "Admin access required."}

    return {"ok": True, "reports": reports_sorted}


@app.function(volumes={VIDEO_DIR: vol, AUTH_DIR: auth_vol}, secrets=[snowflake_secret])
@modal.fastapi_endpoint(method="POST")
async def admin_process_video(request: Request) -> Dict[str, Any]:
    """
    HTTP endpoint:
      POST multipart/form-data with:
        - token: auth session token (admin)
        - file: video
        - camera_id: int
        - start_time/end_time: optional ISO timestamps
        - lat/lng: optional
    Runs video model and writes counts into dataset.
    """
    form = await request.form()
    token = str(form.get("token") or "").strip()
    file = form.get("file")
    camera_id = _parse_int(form.get("camera_id"))
    lat = form.get("lat")
    lng = form.get("lng")
    start_time = str(form.get("start_time") or "").strip()
    end_time = str(form.get("end_time") or "").strip()
    timestamp = end_time or str(form.get("timestamp") or "").strip() or _auth_now_iso()
    speed_mode = str(form.get("speed_mode") or form.get("speed") or "standard").strip().lower()

    if not token:
        return {"ok": False, "message": "Missing session token."}
    if file is None or not hasattr(file, "read"):
        return {"ok": False, "message": "Missing video file."}
    if camera_id is None:
        return {"ok": False, "message": "camera_id is required."}

    changed = False
    user = None
    with _auth_lock:
        users, sessions = _auth_load_state()
        changed = _auth_prune_sessions(sessions)
        user = _auth_get_user_from_token(users, sessions, token)
        if changed:
            _auth_save_state(users, sessions)
    if changed:
        await auth_vol.commit.aio()
    if not user:
        return {"ok": False, "message": "You need to be logged in."}
    if str(user.get("role", "resident")) != "admin":
        return {"ok": False, "message": "Admin access required."}

    video_id, save_path = await _save_upload_file(file, ".mp4")

    svc = CounterService()
    out = await svc.count_video.remote.aio(save_path, speed_mode=speed_mode)
    db_write_status = _persist_detector_output(out, camera_id, lat, lng)

    job = {
        "id": str(uuid.uuid4()),
        "created_at": _auth_now_iso(),
        "admin_user_id": user.get("id"),
        "admin_email": user.get("email"),
        "video_id": video_id,
        "video_filename": str(getattr(file, "filename", "") or ""),
        "camera_id": camera_id,
        "lat": lat,
        "lng": lng,
        "start_time": start_time or None,
        "end_time": end_time or None,
        "timestamp": timestamp,
        "db_write_status": db_write_status,
        "stats": out,
    }

    with _auth_lock:
        jobs = _read_json_file(ADMIN_VIDEO_JOBS_FILE, [])
        if not isinstance(jobs, list):
            jobs = []
        jobs.append(job)
        _write_json_file(ADMIN_VIDEO_JOBS_FILE, jobs)
    await auth_vol.commit.aio()

    return {"ok": True, "job": job, **out}


@app.local_entrypoint()
def main():
    print("Deployed. Use: modal deploy app.py")

@app.function(secrets=[snowflake_secret])
def test_snowflake_query():
    conn = _snowflake_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT CURRENT_ACCOUNT(), CURRENT_DATABASE(), CURRENT_SCHEMA(), CURRENT_WAREHOUSE()
            """)
            print(cur.fetchone())
    finally:
        conn.close()
