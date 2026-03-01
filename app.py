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
AUTH_SESSIONS_FILE = os.path.join(AUTH_DIR, "sessions.json")
ADMIN_VIDEO_JOBS_FILE = os.path.join(AUTH_DIR, "admin_video_jobs.json")
AUTH_PBKDF2_ITERATIONS = 120000
AUTH_SALT_BYTES = 16
AUTH_SESSION_TTL_SECONDS = 60 * 60 * 24 * 14
AUTH_USERS_TABLE = os.getenv("SNOWFLAKE_USERS_TABLE", "MY_MODAL_DB.PUBLIC.USERS")
REPORTS_TABLE = os.getenv("SNOWFLAKE_REPORTS_TABLE", "MY_MODAL_DB.PUBLIC.REPORTS")
AUTH_GOOGLE_PASSWORD_SENTINEL = "google_oauth"
_auth_lock = threading.Lock()
DEFAULT_SEARCH_LIMIT = int(os.getenv("SNOWFLAKE_RAG_TOP_K", "25"))
DEFAULT_CHAT_MODEL = os.getenv("SNOWFLAKE_CHAT_MODEL", "openai-gpt-5")
DEFAULT_SEARCH_COLUMNS = ["chunk_text", "doc_id", "file_name", "category", "chunk_id", "doc_content"]
CAMERAS_TABLE = os.getenv("SNOWFLAKE_CAMERAS_TABLE", "CAMERAS")
CAMERA_INFO_TABLE = os.getenv("SNOWFLAKE_CAMERA_INFO_TABLE", "CAMERA_INFO")
RAG_DOCUMENTS_TABLE = os.getenv("SNOWFLAKE_RAG_TABLE", "RAG_DOCUMENTS")
RAG_CHUNKS_TABLE = os.getenv("SNOWFLAKE_RAG_CHUNKS_TABLE", "RAG_DOCUMENT_CHUNKS")
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


def _rows_to_contexts(title: str, rows: List[Dict[str, Any]], max_rows: int = 25) -> List[Dict[str, Any]]:
    contexts: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows[:max_rows], start=1):
        pairs = [f"{k}={v}" for k, v in row.items() if v is not None and str(v) != ""]
        if not pairs:
            continue
        contexts.append(
            {
                "content": f"{title} row {idx}: " + ", ".join(pairs),
                "source_url": f"SNOWFLAKE:{title}",
            }
        )
    return contexts


def _question_keywords(question: str, max_terms: int = 8) -> List[str]:
    stopwords = {
        "the", "and", "for", "with", "that", "this", "from", "into", "about",
        "what", "when", "where", "which", "how", "are", "is", "was", "were",
        "give", "some", "towards", "than", "then", "your", "you", "our",
        "have", "has", "had", "can", "could", "would", "should", "please",
    }
    clean = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in str(question or "").lower())
    terms: List[str] = []
    for token in clean.split():
        if len(token) < 3 or token in stopwords:
            continue
        if token not in terms:
            terms.append(token)
        if len(terms) >= max_terms:
            break
    return terms


def _topic_categories(question: str) -> List[str]:
    q = str(question or "").lower()
    categories: List[str] = []
    if any(t in q for t in ["recommend", "reduce", "mitigate", "solution", "policy"]):
        categories.extend(["recommend", "mitigation", "strategy", "policy", "best practice"])
    if any(t in q for t in ["truck", "diesel", "heavy"]):
        categories.extend(["truck", "diesel", "heavy-duty", "freight"])
    if any(t in q for t in ["health", "asthma", "pm2", "no2", "exposure"]):
        categories.extend(["health", "pm2.5", "no2", "exposure"])
    if any(t in q for t in ["emission", "pollution", "air quality"]):
        categories.extend(["emission", "pollution", "air quality"])
    return categories


def _get_rag_context_from_chunks(conn, question: str, limit: int) -> List[Dict[str, Any]]:
    del question
    rows = _query_rows(
        conn,
        f"""
        SELECT
          chunk_id,
          doc_id,
          category,
          file_name,
          chunk_text
        FROM {RAG_CHUNKS_TABLE}
        LIMIT %s
        """,
        (max(1, min(limit, 100)),),
    )
    return _rows_to_contexts("RAG_CHUNKS", rows, max_rows=limit)


def _get_rag_context_keyword_fallback(conn, question: str, limit: int) -> List[Dict[str, Any]]:
    keywords = _question_keywords(question)
    categories = _topic_categories(question)
    filters: List[str] = []
    params: List[Any] = []

    for term in categories:
        filters.append("LOWER(category) LIKE %s")
        params.append(f"%{term.lower()}%")

    for term in keywords:
        filters.append("LOWER(doc_content) LIKE %s")
        params.append(f"%{term.lower()}%")

    where_clause = " OR ".join(filters) if filters else "1=1"
    params.append(max(1, min(limit, 100)))

    rows = _query_rows(
        conn,
        f"""
        SELECT
          doc_id,
          category,
          file_name,
          doc_content
        FROM {RAG_DOCUMENTS_TABLE}
        WHERE {where_clause}
        LIMIT %s
        """,
        tuple(params),
    )
    return _rows_to_contexts("RAG_DOCUMENTS", rows, max_rows=limit)


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
    contexts.extend(_rows_to_contexts("RECENT_CAMERA_INFO", recent_rows, max_rows=20))

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
    contexts.extend(_rows_to_contexts("POLLUTION_HOTSPOTS", hotspot_rows, max_rows=10))

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


def _write_camera_info_record(
    conn,
    camera_id: Optional[int],
    out: Dict[str, Any],
    recorded_at: Optional[str] = None,
) -> None:
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
              (%s, %s, %s, %s, %s, %s, %s, COALESCE(TRY_TO_TIMESTAMP(%s), CURRENT_TIMESTAMP()))
            """,
            (camera_id, car_count, bus_count, truck_count, motorcycle_count, total_unique, peak, recorded_at),
        )


def _delete_camera_info_for_date(conn, camera_id: int, recorded_date: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            DELETE FROM {CAMERA_INFO_TABLE}
            WHERE camera_id = %s
              AND DATE(recorded_at) = TO_DATE(%s)
            """,
            (camera_id, recorded_date),
        )


def _persist_detector_output(
    out: Dict[str, Any],
    camera_id: Optional[int],
    lat: Optional[str],
    lng: Optional[str],
    recorded_at: Optional[str] = None,
    overwrite_for_date: Optional[str] = None,
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
        clean_overwrite_date = _parse_date(overwrite_for_date)
        if resolved_camera_id is not None and clean_overwrite_date:
            _delete_camera_info_for_date(conn, int(resolved_camera_id), clean_overwrite_date)
        _write_camera_info_record(conn, resolved_camera_id, out, recorded_at=recorded_at)
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


def _recorded_date_timestamp(value: Optional[str]) -> Optional[str]:
    clean_date = _parse_date(value)
    if not clean_date:
        return None
    return f"{clean_date} 12:00:00"


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
    available_dates = [str(row["traffic_date"]) for row in rows if row.get("traffic_date")]
    max_visible_date = _parse_date(os.getenv("TRAFFIC_MAP_DATE_MAX", "2026-02-28"))
    if max_visible_date:
        available_dates = [date for date in available_dates if date <= max_visible_date]
    return available_dates


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


def _get_all_camera_rows(conn) -> List[Dict[str, Any]]:
    return _query_rows(
        conn,
        f"""
        SELECT
          camera_id,
          camera_name,
          latitude,
          longitude
        FROM {CAMERAS_TABLE}
        ORDER BY camera_id ASC
        """,
    )


def _camera_payload_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    camera_id = _to_int(row.get("camera_id"), 0)
    return {
        "camera_id": camera_id,
        "camera_name": str(row.get("camera_name") or f"Camera {camera_id}"),
        "latitude": _to_float(row.get("latitude")),
        "longitude": _to_float(row.get("longitude")),
    }


def _list_camera_directory(conn) -> List[Dict[str, Any]]:
    rows = _get_all_camera_rows(conn)
    return [_camera_payload_from_row(row) for row in rows]


def _next_camera_id(conn) -> int:
    rows = _query_rows(
        conn,
        f"""
        SELECT COALESCE(MAX(camera_id), 0) + 1 AS next_camera_id
        FROM {CAMERAS_TABLE}
        """,
    )
    if not rows:
        return 1
    return max(1, _to_int(rows[0].get("next_camera_id"), 1))


def _create_camera_record(conn, name: str, latitude: float, longitude: float) -> Dict[str, Any]:
    camera_id = _next_camera_id(conn)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {CAMERAS_TABLE} (camera_id, camera_name, latitude, longitude)
            VALUES (%s, %s, %s, %s)
            """,
            (camera_id, str(name).strip(), float(latitude), float(longitude)),
        )
    conn.commit()

    return {
        "camera_id": camera_id,
        "camera_name": str(name).strip(),
        "latitude": float(latitude),
        "longitude": float(longitude),
    }


def _delete_camera_record(conn, camera_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            DELETE FROM {CAMERA_INFO_TABLE}
            WHERE camera_id = %s
            """,
            (camera_id,),
        )
        cur.execute(
            f"""
            DELETE FROM {CAMERAS_TABLE}
            WHERE camera_id = %s
            """,
            (camera_id,),
        )
        removed_camera = (cur.rowcount or 0) > 0
    conn.commit()
    return removed_camera


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
    sessions = _read_json_file(AUTH_SESSIONS_FILE, {})
    if not isinstance(sessions, dict):
        sessions = {}
    return [], sessions


def _auth_save_state(users: List[Dict[str, Any]], sessions: Dict[str, Dict[str, Any]]) -> None:
    del users
    _write_json_file(AUTH_SESSIONS_FILE, sessions)


def _auth_normalize_email(email: Any) -> str:
    return str(email or "").strip().lower()


def _auth_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _auth_make_hash(password: str, salt_hex: str, iterations: int = AUTH_PBKDF2_ITERATIONS) -> str:
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        iterations,
    )
    return derived.hex()


def _auth_new_password_record(password: str) -> str:
    salt_hex = secrets.token_hex(AUTH_SALT_BYTES)
    password_hash = _auth_make_hash(password, salt_hex)
    return f"pbkdf2_sha256${AUTH_PBKDF2_ITERATIONS}${salt_hex}${password_hash}"


def _auth_is_google_provider(password_record: str) -> bool:
    return str(password_record or "").strip().lower() == AUTH_GOOGLE_PASSWORD_SENTINEL


def _auth_verify_password(password: str, password_record: str) -> bool:
    clean_record = str(password_record or "").strip()
    if not clean_record or _auth_is_google_provider(clean_record):
        return False
    parts = clean_record.split("$")
    if len(parts) == 4 and parts[0] == "pbkdf2_sha256":
        try:
            iterations = int(parts[1])
        except Exception:
            return False
        salt_hex = parts[2]
        expected_hash = parts[3]
        provided_hash = _auth_make_hash(password, salt_hex, iterations=iterations)
        return hmac.compare_digest(provided_hash, expected_hash)
    return hmac.compare_digest(password, clean_record)


def _auth_is_admin(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "t", "yes", "y", "admin"}


def _auth_user_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    password_record = str(row.get("password") or "")
    email = _auth_normalize_email(row.get("email"))
    name = str(row.get("username") or "").strip()
    if not name and email:
        name = email.split("@")[0]
    provider = "google" if _auth_is_google_provider(password_record) else "local"
    return {
        "id": str(row.get("user_id") or ""),
        "name": name,
        "email": email,
        "provider": provider,
        "role": "admin" if _auth_is_admin(row.get("is_admin")) else "resident",
        "avatar_url": "",
        "password_record": password_record,
    }


def _auth_ensure_users_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT USER_ID, IS_ADMIN, PASSWORD, EMAIL, USERNAME
            FROM {AUTH_USERS_TABLE}
            LIMIT 1
            """
        )


def _report_key_expression(alias: str = "r") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        "COALESCE("
        f"TO_VARCHAR({prefix}REPORT_ID), "
        "SHA2("
        "CONCAT_WS('|', "
        f"COALESCE(TO_VARCHAR({prefix}CREATED_AT), ''), "
        f"COALESCE(TO_VARCHAR({prefix}USER_ID), ''), "
        f"COALESCE({prefix}DESCRIPTION, ''), "
        f"COALESCE(TO_VARCHAR({prefix}CAR_COUNT), ''), "
        f"COALESCE(TO_VARCHAR({prefix}BUS_COUNT), ''), "
        f"COALESCE(TO_VARCHAR({prefix}TRUCK_COUNT), ''), "
        f"COALESCE(TO_VARCHAR({prefix}MOTORCYCLE_COUNT), ''), "
        f"COALESCE(TO_VARCHAR({prefix}TOTAL_VEHICLES), ''), "
        f"COALESCE(TO_VARCHAR({prefix}LATITUDE), ''), "
        f"COALESCE(TO_VARCHAR({prefix}LONGITUDE), '')"
        "), 256)"
        ")"
    )


def _reports_ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {REPORTS_TABLE} (
                REPORT_ID STRING,
                CREATED_AT TIMESTAMP_NTZ,
                DESCRIPTION STRING,
                USER_ID STRING,
                CAR_COUNT NUMBER(38, 0),
                BUS_COUNT NUMBER(38, 0),
                TRUCK_COUNT NUMBER(38, 0),
                MOTORCYCLE_COUNT NUMBER(38, 0),
                TOTAL_VEHICLES NUMBER(38, 0),
                LATITUDE FLOAT,
                LONGITUDE FLOAT
            )
            """
        )
        cur.execute(f"ALTER TABLE {REPORTS_TABLE} ADD COLUMN IF NOT EXISTS REPORT_ID STRING")
        cur.execute(f"ALTER TABLE {REPORTS_TABLE} ADD COLUMN IF NOT EXISTS CREATED_AT TIMESTAMP_NTZ")


def _report_payload_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    report_id = str(row.get("report_id") or "")
    created_at = str(row.get("created_at") or "")
    user_id = str(row.get("user_id") or "")
    user_name = str(row.get("user_name") or "").strip()
    user_email = str(row.get("user_email") or "").strip()
    description = str(row.get("description") or "").strip()
    car_count = _to_int(row.get("car_count"))
    bus_count = _to_int(row.get("bus_count"))
    truck_count = _to_int(row.get("truck_count"))
    motorcycle_count = _to_int(row.get("motorcycle_count"))
    total_vehicles = _to_int(row.get("total_vehicles"))
    latitude = _to_float(row.get("latitude"))
    longitude = _to_float(row.get("longitude"))

    return {
        "id": report_id,
        "report_id": report_id,
        "created_at": created_at,
        "timestamp": created_at,
        "user_id": user_id,
        "user_name": user_name,
        "user_email": user_email,
        "description": description,
        "notes": description,
        "lat": latitude,
        "lng": longitude,
        "latitude": latitude,
        "longitude": longitude,
        "car_count": car_count,
        "bus_count": bus_count,
        "truck_count": truck_count,
        "motorcycle_count": motorcycle_count,
        "total_vehicles": total_vehicles,
        "stats": {
            "counts_by_class": {
                "car": car_count,
                "bus": bus_count,
                "truck": truck_count,
                "motorcycle": motorcycle_count,
            },
            "total_unique_vehicles": total_vehicles,
            "peak_vehicles_in_frame": total_vehicles,
        },
    }


def _list_resident_reports(conn) -> List[Dict[str, Any]]:
    _reports_ensure_table(conn)
    report_key = _report_key_expression("r")
    rows = _query_rows(
        conn,
        f"""
        SELECT
          {report_key} AS report_id,
          TO_VARCHAR(r.created_at) AS created_at,
          TO_VARCHAR(r.user_id) AS user_id,
          u.username AS user_name,
          u.email AS user_email,
          r.description AS description,
          r.car_count AS car_count,
          r.bus_count AS bus_count,
          r.truck_count AS truck_count,
          r.motorcycle_count AS motorcycle_count,
          r.total_vehicles AS total_vehicles,
          r.latitude AS latitude,
          r.longitude AS longitude
        FROM {REPORTS_TABLE} r
        LEFT JOIN {AUTH_USERS_TABLE} u
          ON TO_VARCHAR(u.user_id) = TO_VARCHAR(r.user_id)
        ORDER BY COALESCE(r.created_at, CURRENT_TIMESTAMP()) DESC
        """,
    )
    return [_report_payload_from_row(row) for row in rows]


def _insert_resident_report(
    conn,
    *,
    report_id: str,
    created_at: str,
    user_id: str,
    description: str,
    car_count: int,
    bus_count: int,
    truck_count: int,
    motorcycle_count: int,
    total_vehicles: int,
    latitude: float,
    longitude: float,
) -> None:
    _reports_ensure_table(conn)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {REPORTS_TABLE}
              (report_id, created_at, description, user_id, car_count, bus_count, truck_count, motorcycle_count, total_vehicles, latitude, longitude)
            VALUES
              (%s, COALESCE(TRY_TO_TIMESTAMP(%s), CURRENT_TIMESTAMP()), %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(report_id),
                str(created_at),
                str(description),
                str(user_id),
                int(car_count),
                int(bus_count),
                int(truck_count),
                int(motorcycle_count),
                int(total_vehicles),
                float(latitude),
                float(longitude),
            ),
        )
    conn.commit()


def _delete_resident_report(conn, report_id: str) -> bool:
    clean_report_id = str(report_id or "").strip()
    if not clean_report_id:
        return False
    _reports_ensure_table(conn)
    report_key = _report_key_expression("")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            DELETE FROM {REPORTS_TABLE}
            WHERE {report_key} = %s
            """,
            (clean_report_id,),
        )
        deleted = (cur.rowcount or 0) > 0
    conn.commit()
    return deleted


def _auth_db_get_user_by_email(conn, email: str) -> Optional[Dict[str, Any]]:
    clean_email = _auth_normalize_email(email)
    if not clean_email:
        return None
    rows = _query_rows(
        conn,
        f"""
        SELECT USER_ID, IS_ADMIN, PASSWORD, EMAIL, USERNAME
        FROM {AUTH_USERS_TABLE}
        WHERE LOWER(EMAIL) = %s
        LIMIT 1
        """,
        (clean_email,),
    )
    if not rows:
        return None
    return _auth_user_from_row(rows[0])


def _auth_db_get_user_by_id(conn, user_id: str) -> Optional[Dict[str, Any]]:
    clean_user_id = str(user_id or "").strip()
    if not clean_user_id:
        return None
    rows = _query_rows(
        conn,
        f"""
        SELECT USER_ID, IS_ADMIN, PASSWORD, EMAIL, USERNAME
        FROM {AUTH_USERS_TABLE}
        WHERE TO_VARCHAR(USER_ID) = %s
        LIMIT 1
        """,
        (clean_user_id,),
    )
    if not rows:
        return None
    return _auth_user_from_row(rows[0])


def _auth_db_insert_user(
    conn,
    *,
    email: str,
    username: str,
    password_record: str,
    is_admin: bool = False,
) -> Optional[Dict[str, Any]]:
    clean_email = _auth_normalize_email(email)
    clean_username = str(username or "").strip()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {AUTH_USERS_TABLE} (IS_ADMIN, PASSWORD, EMAIL, USERNAME)
            VALUES (%s, %s, %s, %s)
            """,
            (bool(is_admin), str(password_record or ""), clean_email, clean_username),
        )
    conn.commit()
    return _auth_db_get_user_by_email(conn, clean_email)


def _auth_db_update_profile(conn, user_id: str, name: str, role: str) -> Optional[Dict[str, Any]]:
    clean_id = str(user_id or "").strip()
    clean_name = str(name or "").strip()
    is_admin = str(role or "").strip().lower() == "admin"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {AUTH_USERS_TABLE}
            SET USERNAME = %s, IS_ADMIN = %s
            WHERE TO_VARCHAR(USER_ID) = %s
            """,
            (clean_name, is_admin, clean_id),
        )
    conn.commit()
    return _auth_db_get_user_by_id(conn, clean_id)


def _auth_db_update_password(conn, user_id: str, password_record: str) -> Optional[Dict[str, Any]]:
    clean_id = str(user_id or "").strip()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {AUTH_USERS_TABLE}
            SET PASSWORD = %s
            WHERE TO_VARCHAR(USER_ID) = %s
            """,
            (str(password_record or ""), clean_id),
        )
    conn.commit()
    return _auth_db_get_user_by_id(conn, clean_id)


def _auth_public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": user.get("id"),
        "name": user.get("name", ""),
        "email": user.get("email", ""),
        "provider": user.get("provider", "local"),
        "role": user.get("role", "resident"),
        "avatarUrl": user.get("avatar_url", ""),
    }


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
    conn=None,
) -> Optional[Dict[str, Any]]:
    del users
    if not token:
        return None
    record = sessions.get(token)
    if not record:
        return None
    user_id = str(record.get("user_id", "")).strip()
    if not user_id:
        return None

    local_conn = conn
    should_close = False
    if local_conn is None:
        local_conn = _snowflake_connect()
        should_close = True
    try:
        _auth_ensure_users_table(local_conn)
        return _auth_db_get_user_by_id(local_conn, user_id)
    finally:
        if should_close and local_conn is not None:
            local_conn.close()


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
        content = str(
            item.get("content")
            or item.get("chunk_text")
            or item.get("doc_content")
            or item.get("CHUNK_TEXT")
            or item.get("DOC_CONTENT")
            or ""
        ).strip()
        if not content:
            # Allow structured rows that may not contain a plain "content" field.
            content = ", ".join(
                f"{k}={v}" for k, v in item.items() if k not in {"source_url"} and v is not None
            ).strip()
        source_url = str(item.get("source_url", "") or item.get("SOURCE_URL", "")).strip()
        file_name = str(item.get("file_name", "") or item.get("FILE_NAME", "")).strip()
        category = str(item.get("category", "") or item.get("CATEGORY", "")).strip()
        if not content:
            continue
        label = (
            source_url
            or file_name
            or f"doc-{item.get('doc_id', item.get('DOC_ID', i))}:chunk-{item.get('chunk_id', item.get('CHUNK_ID', i))}"
        )
        if category:
            label = f"{label} (category={category})"
        context_lines.append(f"[{i}] {content}\nSOURCE: {label}")

    joined_context = "\n\n".join(context_lines) if context_lines else "No context found."

    prompt = (
        "You are an environmental assistant for Chicago vehicle pollution analysis.\n"
        "Answer the user's question directly and in a detailed practical way.\n"
        "Prioritize the provided context for facts and include source citations.\n"
        "If context is partially relevant, provide best-effort recommendations and clearly state assumptions.\n"
        "Only say you do not know when there is truly no relevant evidence in context.\n"
        "Prefer concise sections: Summary, Recommendations, and Evidence.\n"
        "At the end, include a final section titled 'Sources' with 3-8 main source names only "
        "(organization/report names, no URLs, no bracket ranges, no raw chunk IDs). "
        "Examples: EPA, American Lung Association, Illinois EPA, City of Chicago.\n\n"
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


async def _upload_video_and_count_from_form(form: Any, file: Any) -> Dict[str, Any]:
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
        - overwrite_for_date: bool-like (optional; if true, clear existing rows for each camera/date pair before insert)
        - camera_id / lat / lng / timestamp:
          optional metadata; can be repeated to map by file index
        - recorded_date:
          optional YYYY-MM-DD (or repeated `recorded_dates`) to write deterministic dates

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
    overwrite_for_date = str(form.get("overwrite_for_date") or form.get("overwrite") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }

    requested_parallel = _to_int(form.get("max_parallel"), default=BATCH_DEFAULT_PARALLEL)
    max_parallel = max(1, min(requested_parallel, BATCH_MAX_PARALLEL))

    camera_values = _form_values(form, "camera_id") or _form_values(form, "camera_ids")
    lat_values = _form_values(form, "lat") or _form_values(form, "lats")
    lng_values = _form_values(form, "lng") or _form_values(form, "lngs")
    timestamp_values = _form_values(form, "timestamp") or _form_values(form, "timestamps")
    recorded_date_values = (
        _form_values(form, "recorded_date")
        or _form_values(form, "recorded_dates")
        or _form_values(form, "date")
        or _form_values(form, "dates")
    )

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

    per_file_metadata: List[Dict[str, Any]] = []
    overwrite_pairs: set[tuple[int, str]] = set()
    for idx, _saved in enumerate(saved_files):
        camera_id = _parse_int(_pick_index_value(camera_values, idx))
        lat = _pick_index_value(lat_values, idx)
        lng = _pick_index_value(lng_values, idx)
        timestamp = _pick_index_value(timestamp_values, idx)
        recorded_date = _parse_date(_pick_index_value(recorded_date_values, idx))
        recorded_at = _recorded_date_timestamp(recorded_date)

        if overwrite_for_date and camera_id is not None and recorded_date:
            overwrite_pairs.add((int(camera_id), recorded_date))

        per_file_metadata.append(
            {
                "camera_id": camera_id,
                "lat": lat,
                "lng": lng,
                "timestamp": timestamp,
                "recorded_date": recorded_date,
                "recorded_at": recorded_at,
            }
        )

    if overwrite_for_date:
        if not overwrite_pairs:
            return {"error": "overwrite_for_date requires camera_id and recorded_date."}
        conn = None
        try:
            conn = _snowflake_connect()
            for camera_id, recorded_date in sorted(overwrite_pairs):
                _delete_camera_info_for_date(conn, camera_id, recorded_date)
            conn.commit()
        except Exception as exc:
            return {"error": f"Failed to clear existing records for overwrite_for_date: {exc}"}
        finally:
            if conn is not None:
                conn.close()

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

    for idx, (saved, output, metadata) in enumerate(zip(saved_files, raw_outputs, per_file_metadata)):
        camera_id = metadata["camera_id"]
        lat = metadata["lat"]
        lng = metadata["lng"]
        timestamp = metadata["timestamp"]
        recorded_date = metadata["recorded_date"]
        recorded_at = metadata["recorded_at"]

        item_result: Dict[str, Any] = {
            "index": idx,
            "video_id": saved["file_id"],
            "filename": saved["filename"],
            "camera_id": camera_id,
            "lat": lat,
            "lng": lng,
            "timestamp": timestamp,
            "recorded_date": recorded_date,
        }

        if isinstance(output, Exception):
            item_result["error"] = str(output)
            item_result["db_write_status"] = "skipped"
            results.append(item_result)
            continue

        db_write_status = _persist_detector_output(
            output,
            camera_id,
            lat,
            lng,
            recorded_at=recorded_at,
        )
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
        "ok": True,
        "speed_mode": speed_mode,
        "max_parallel": max_parallel,
        "overwrite_for_date": overwrite_for_date,
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


async def _upload_image_and_count_from_form(form: Any, file: Any) -> Dict[str, Any]:
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


@app.function(volumes={VIDEO_DIR: vol}, secrets=[snowflake_secret])
@modal.fastapi_endpoint(method="POST")
async def upload_media_and_count(request: Request) -> Dict[str, Any]:
    """
    Consolidated HTTP endpoint for media uploads.
      POST multipart/form-data with:
        - file: image or video
        - media_type: "image" | "video" (optional; inferred from filename when omitted)
        - lat/lng/timestamp/camera_id/speed_mode: optional
    """
    form = await request.form()
    file = form.get("file")
    if file is None:
        return {"error": "Missing 'file' in form-data."}
    if not hasattr(file, "read"):
        return {"error": "Invalid 'file' in form-data."}

    requested_media_type = str(form.get("media_type") or "").strip().lower()
    filename = str(getattr(file, "filename", "") or "")
    ext = os.path.splitext(filename)[1].lower()

    if requested_media_type not in {"image", "video"}:
        requested_media_type = "image" if ext in IMAGE_EXTENSIONS else "video"

    if requested_media_type == "image":
        return await _upload_image_and_count_from_form(form, file)
    return await _upload_video_and_count_from_form(form, file)


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


@app.function(
    secrets=[snowflake_secret],
    min_containers=4,
    max_containers=10,
    scaledown_window=300,
)
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

    top_k = max(5, min(top_k, 50))

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

        # Try chunk table first (if available) to improve semantic recall on long documents.
        try:
            contexts.extend(_get_rag_context_from_chunks(conn, question, top_k))
        except Exception:
            pass

        # Fallback keyword/category retrieval from raw RAG docs for non-semantic matches.
        try:
            contexts.extend(_get_rag_context_keyword_fallback(conn, question, max(10, top_k)))
        except Exception:
            pass

        contexts.extend(_get_structured_context(conn, question))

        # Deduplicate and cap context size to keep prompts within practical limits.
        deduped: List[Dict[str, Any]] = []
        seen = set()
        for item in contexts:
            key = (
                str(item.get("content") or item.get("chunk_text") or item.get("doc_content") or ""),
                str(item.get("source_url") or item.get("file_name") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
            if len(deduped) >= 120:
                break
        contexts = deduped

        answer = _complete_with_context(conn, question, contexts)

        citations = [
            {
                "id": idx + 1,
                "source_url": item.get("source_url"),
                "doc_id": item.get("doc_id"),
                "chunk_id": item.get("chunk_id"),
                "file_name": item.get("file_name"),
                "category": item.get("category"),
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


@app.function(volumes={AUTH_DIR: auth_vol}, secrets=[snowflake_secret])
@modal.fastapi_endpoint(method="POST")
async def auth(request: Request) -> Dict[str, Any]:
    """
    HTTP endpoint:
      POST application/json with:
        - action: signup|login|google|session|logout|update_profile|update_password|get_user|list_reports|delete_report|list_cameras|create_camera|delete_camera
        - token: string (required for session-bound actions)
        - payload fields for each action
    Persists sessions in a Modal volume and users in Snowflake.
    """
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "message": "Invalid JSON body."}

    action = str((body or {}).get("action", "")).strip().lower()
    if not action:
        return {"ok": False, "message": "Missing 'action'."}

    should_commit = False
    conn = None
    try:
        conn = _snowflake_connect()
        _auth_ensure_users_table(conn)

        with _auth_lock:
            users, sessions = _auth_load_state()
            if _auth_prune_sessions(sessions):
                _auth_save_state(users, sessions)
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

                existing = _auth_db_get_user_by_email(conn, email)
                if existing:
                    return {"ok": False, "message": "An account with this email already exists."}

                password_record = _auth_new_password_record(password)
                user = _auth_db_insert_user(
                    conn,
                    email=email,
                    username=name,
                    password_record=password_record,
                    is_admin=False,
                )
                if not user:
                    return {"ok": False, "message": "Failed to create account."}

                token = _auth_create_session(sessions, str(user["id"]))
                _auth_save_state(users, sessions)
                should_commit = True
                response = {"ok": True, "user": _auth_public_user(user), "token": token}

            elif action == "login":
                email = _auth_normalize_email(body.get("email"))
                password = str(body.get("password", "")).strip()
                user = _auth_db_get_user_by_email(conn, email)
                if not user:
                    return {"ok": False, "message": "Incorrect email or password."}
                if user.get("provider", "local") != "local":
                    return {"ok": False, "message": "Use Google sign-in for this account."}
                if not _auth_verify_password(password, str(user.get("password_record", ""))):
                    return {"ok": False, "message": "Incorrect email or password."}
                stored_record = str(user.get("password_record", ""))
                if stored_record and "$" not in stored_record:
                    upgraded = _auth_db_update_password(conn, str(user.get("id")), _auth_new_password_record(password))
                    if upgraded:
                        user = upgraded

                token = _auth_create_session(sessions, str(user["id"]))
                _auth_save_state(users, sessions)
                should_commit = True
                response = {"ok": True, "user": _auth_public_user(user), "token": token}

            elif action == "google":
                profile = body.get("profile") or {}
                email = _auth_normalize_email(profile.get("email"))
                if not email:
                    return {"ok": False, "message": "Google profile missing email."}

                clean_name = str(profile.get("name") or email.split("@")[0]).strip()
                user = _auth_db_get_user_by_email(conn, email)
                if user and user.get("provider", "local") == "local":
                    return {"ok": False, "message": "Account exists with password. Log in with email/password."}

                if not user:
                    user = _auth_db_insert_user(
                        conn,
                        email=email,
                        username=clean_name,
                        password_record=AUTH_GOOGLE_PASSWORD_SENTINEL,
                        is_admin=False,
                    )
                else:
                    current_role = str(user.get("role", "resident"))
                    updated = _auth_db_update_profile(conn, str(user.get("id")), clean_name, current_role) or user
                    user = _auth_db_update_password(conn, str(updated.get("id")), AUTH_GOOGLE_PASSWORD_SENTINEL) or updated

                if not user:
                    return {"ok": False, "message": "Failed to upsert Google account."}

                token = _auth_create_session(sessions, str(user["id"]))
                _auth_save_state(users, sessions)
                should_commit = True
                response = {"ok": True, "user": _auth_public_user(user), "token": token}

            elif action == "session":
                token = str(body.get("token", "")).strip()
                user = _auth_get_user_from_token(users, sessions, token, conn=conn)
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
                user = _auth_get_user_from_token(users, sessions, token, conn=conn)
                if not user:
                    return {"ok": False, "message": "You need to be logged in."}

                clean_name = str(body.get("name", "")).strip()
                if not clean_name:
                    return {"ok": False, "message": "Name is required."}
                clean_role = "admin" if str(body.get("role", "")).strip().lower() == "admin" else "resident"
                updated_user = _auth_db_update_profile(conn, str(user.get("id")), clean_name, clean_role)
                if not updated_user:
                    return {"ok": False, "message": "User record not found."}
                response = {"ok": True, "user": _auth_public_user(updated_user)}

            elif action == "update_password":
                token = str(body.get("token", "")).strip()
                user = _auth_get_user_from_token(users, sessions, token, conn=conn)
                if not user:
                    return {"ok": False, "message": "You need to be logged in."}
                if user.get("provider", "local") != "local":
                    return {"ok": False, "message": "Password changes are only available for local accounts."}

                current_password = str(body.get("currentPassword", "")).strip()
                new_password = str(body.get("newPassword", "")).strip()
                if len(new_password) < 8:
                    return {"ok": False, "message": "New password must be at least 8 characters."}
                if not _auth_verify_password(current_password, str(user.get("password_record", ""))):
                    return {"ok": False, "message": "Current password is incorrect."}

                next_record = _auth_new_password_record(new_password)
                updated_user = _auth_db_update_password(conn, str(user.get("id")), next_record)
                if not updated_user:
                    return {"ok": False, "message": "User record not found."}
                response = {"ok": True, "user": _auth_public_user(updated_user)}

            elif action == "get_user":
                token = str(body.get("token", "")).strip()
                requester = _auth_get_user_from_token(users, sessions, token, conn=conn)
                if not requester:
                    return {"ok": False, "message": "You need to be logged in."}

                user_id = str(body.get("userId", "")).strip()
                found = _auth_db_get_user_by_id(conn, user_id)
                if not found:
                    return {"ok": False, "message": "User record not found."}
                response = {"ok": True, "user": _auth_public_user(found)}

            elif action == "list_reports":
                token = str(body.get("token", "")).strip()
                requester = _auth_get_user_from_token(users, sessions, token, conn=conn)
                if not requester:
                    return {"ok": False, "message": "You need to be logged in."}
                if str(requester.get("role", "resident")) != "admin":
                    return {"ok": False, "message": "Admin access required."}

                reports = _list_resident_reports(conn)
                response = {"ok": True, "reports": reports}

            elif action == "delete_report":
                token = str(body.get("token", "")).strip()
                requester = _auth_get_user_from_token(users, sessions, token, conn=conn)
                if not requester:
                    return {"ok": False, "message": "You need to be logged in."}
                if str(requester.get("role", "resident")) != "admin":
                    return {"ok": False, "message": "Admin access required."}

                report_id = str(body.get("report_id") or body.get("id") or "").strip()
                if not report_id:
                    return {"ok": False, "message": "report_id is required."}

                deleted = _delete_resident_report(conn, report_id)
                if not deleted:
                    return {"ok": False, "message": f"Report {report_id} was not found."}
                reports = _list_resident_reports(conn)
                response = {"ok": True, "report_id": report_id, "reports": reports}

            elif action == "list_cameras":
                token = str(body.get("token", "")).strip()
                requester = _auth_get_user_from_token(users, sessions, token, conn=conn)
                if not requester:
                    return {"ok": False, "message": "You need to be logged in."}
                if str(requester.get("role", "resident")) != "admin":
                    return {"ok": False, "message": "Admin access required."}

                cameras = _list_camera_directory(conn)
                response = {"ok": True, "cameras": cameras}

            elif action == "create_camera":
                token = str(body.get("token", "")).strip()
                requester = _auth_get_user_from_token(users, sessions, token, conn=conn)
                if not requester:
                    return {"ok": False, "message": "You need to be logged in."}
                if str(requester.get("role", "resident")) != "admin":
                    return {"ok": False, "message": "Admin access required."}

                camera_name = str(body.get("camera_name") or body.get("name") or "").strip()
                latitude = _to_float(body.get("latitude"))
                longitude = _to_float(body.get("longitude"))

                if not camera_name:
                    return {"ok": False, "message": "camera_name is required."}
                if latitude is None or longitude is None:
                    return {"ok": False, "message": "Valid latitude and longitude are required."}
                if latitude < -90 or latitude > 90:
                    return {"ok": False, "message": "Latitude must be between -90 and 90."}
                if longitude < -180 or longitude > 180:
                    return {"ok": False, "message": "Longitude must be between -180 and 180."}

                created = _create_camera_record(conn, camera_name, latitude, longitude)
                cameras = _list_camera_directory(conn)
                response = {"ok": True, "camera": created, "cameras": cameras}

            elif action == "delete_camera":
                token = str(body.get("token", "")).strip()
                requester = _auth_get_user_from_token(users, sessions, token, conn=conn)
                if not requester:
                    return {"ok": False, "message": "You need to be logged in."}
                if str(requester.get("role", "resident")) != "admin":
                    return {"ok": False, "message": "Admin access required."}

                camera_id = _parse_int(body.get("camera_id"))
                if camera_id is None:
                    return {"ok": False, "message": "camera_id is required."}

                removed = _delete_camera_record(conn, camera_id)
                if not removed:
                    return {"ok": False, "message": f"Camera ID {camera_id} was not found."}
                cameras = _list_camera_directory(conn)
                response = {"ok": True, "camera_id": camera_id, "cameras": cameras}

            else:
                return {"ok": False, "message": f"Unsupported action '{action}'."}

        if should_commit:
            await auth_vol.commit.aio()
        return response
    except Exception as exc:
        return {"ok": False, "message": f"Auth endpoint failed: {exc}"}
    finally:
        if conn is not None:
            conn.close()


async def _submit_resident_report_from_form(form: Any) -> Dict[str, Any]:
    token = str(form.get("token") or "").strip()
    description = str(form.get("description") or form.get("notes") or "").strip()
    lat = _to_float(form.get("lat"))
    lng = _to_float(form.get("lng"))
    timestamp = str(form.get("timestamp") or "").strip() or _auth_now_iso()
    file = form.get("file")

    if not token:
        return {"ok": False, "message": "Missing session token."}
    if not description:
        return {"ok": False, "message": "Description is required."}
    if lat is None or lng is None:
        return {"ok": False, "message": "Latitude and longitude are required."}
    if lat < -90 or lat > 90:
        return {"ok": False, "message": "Latitude must be between -90 and 90."}
    if lng < -180 or lng > 180:
        return {"ok": False, "message": "Longitude must be between -180 and 180."}
    if file is None or not hasattr(file, "read"):
        return {"ok": False, "message": "Missing image file."}

    conn = None
    try:
        conn = _snowflake_connect()
        _auth_ensure_users_table(conn)
        _reports_ensure_table(conn)
    except Exception as exc:
        return {"ok": False, "message": f"Auth backend unavailable: {exc}"}

    session_state_changed = False
    with _auth_lock:
        users, sessions = _auth_load_state()
        session_state_changed = _auth_prune_sessions(sessions)
        user = _auth_get_user_from_token(users, sessions, token, conn=conn)
        if session_state_changed:
            _auth_save_state(users, sessions)
    if session_state_changed:
        await auth_vol.commit.aio()
    if not user:
        if conn is not None:
            conn.close()
        return {"ok": False, "message": "You need to be logged in."}

    try:
        suffix = _image_suffix(getattr(file, "filename", ""))
        image_id, save_path = await _save_upload_file(file, suffix)

        svc = CounterService()
        stats = await svc.count_image.remote.aio(save_path, speed_mode="standard")

        counts = stats.get("counts_by_class", {}) or {}
        car_count = int(counts.get("car", 0))
        bus_count = int(counts.get("bus", 0))
        truck_count = int(counts.get("truck", 0))
        motorcycle_count = int(counts.get("motorcycle", 0))
        total_vehicles = int(stats.get("total_unique_vehicles", 0))

        report_id = str(uuid.uuid4())
        created_at = _auth_now_iso()
        _insert_resident_report(
            conn,
            report_id=report_id,
            created_at=created_at,
            user_id=str(user.get("id") or ""),
            description=description,
            car_count=car_count,
            bus_count=bus_count,
            truck_count=truck_count,
            motorcycle_count=motorcycle_count,
            total_vehicles=total_vehicles,
            latitude=float(lat),
            longitude=float(lng),
        )

        report = {
            "id": report_id,
            "report_id": report_id,
            "created_at": created_at,
            "timestamp": timestamp,
            "user_id": user.get("id"),
            "user_name": user.get("name"),
            "user_email": user.get("email"),
            "user_role": user.get("role", "resident"),
            "description": description,
            "notes": description,
            "lat": lat,
            "lng": lng,
            "latitude": lat,
            "longitude": lng,
            "car_count": car_count,
            "bus_count": bus_count,
            "truck_count": truck_count,
            "motorcycle_count": motorcycle_count,
            "total_vehicles": total_vehicles,
            "image_id": image_id,
            "image_filename": str(getattr(file, "filename", "") or ""),
            "stats": stats,
        }
        return {"ok": True, "report": report}
    except Exception as exc:
        return {"ok": False, "message": f"Failed to process report: {exc}"}
    finally:
        if conn is not None:
            conn.close()


async def _admin_process_video_from_form(form: Any) -> Dict[str, Any]:
    token = str(form.get("token") or "").strip()
    file = form.get("file")
    camera_id = _parse_int(form.get("camera_id"))
    recorded_date = _parse_date(str(form.get("recorded_date") or form.get("date") or "").strip())
    recorded_at = _recorded_date_timestamp(recorded_date)
    speed_mode = str(form.get("speed_mode") or form.get("speed") or "standard").strip().lower()

    if not token:
        return {"ok": False, "message": "Missing session token."}
    if file is None or not hasattr(file, "read"):
        return {"ok": False, "message": "Missing video file."}
    if camera_id is None:
        return {"ok": False, "message": "camera_id is required."}
    if not recorded_date or not recorded_at:
        return {"ok": False, "message": "recorded_date is required in YYYY-MM-DD format."}

    conn = None
    try:
        conn = _snowflake_connect()
        _auth_ensure_users_table(conn)
    except Exception as exc:
        return {"ok": False, "message": f"Auth backend unavailable: {exc}"}

    changed = False
    user = None
    with _auth_lock:
        users, sessions = _auth_load_state()
        changed = _auth_prune_sessions(sessions)
        user = _auth_get_user_from_token(users, sessions, token, conn=conn)
        if changed:
            _auth_save_state(users, sessions)
    if conn is not None:
        conn.close()
    if changed:
        await auth_vol.commit.aio()
    if not user:
        return {"ok": False, "message": "You need to be logged in."}
    if str(user.get("role", "resident")) != "admin":
        return {"ok": False, "message": "Admin access required."}

    video_id, save_path = await _save_upload_file(file, ".mp4")

    svc = CounterService()
    out = await svc.count_video.remote.aio(save_path, speed_mode=speed_mode)
    db_write_status = _persist_detector_output(
        out,
        camera_id,
        None,
        None,
        recorded_at=recorded_at,
        overwrite_for_date=recorded_date,
    )

    job = {
        "id": str(uuid.uuid4()),
        "created_at": _auth_now_iso(),
        "admin_user_id": user.get("id"),
        "admin_email": user.get("email"),
        "video_id": video_id,
        "video_filename": str(getattr(file, "filename", "") or ""),
        "camera_id": camera_id,
        "recorded_date": recorded_date,
        "recorded_at": recorded_at,
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


@app.function(volumes={VIDEO_DIR: vol, AUTH_DIR: auth_vol}, secrets=[snowflake_secret])
@modal.fastapi_endpoint(method="POST")
async def report_ops(request: Request) -> Dict[str, Any]:
    """
    Consolidated report/video operations endpoint.
      POST multipart/form-data with:
        - action: "submit_resident_report" | "admin_process_video"
        - remaining fields are the same as the legacy endpoints.
    """
    form = await request.form()
    action = str(form.get("action") or "").strip().lower()

    if action == "submit_resident_report":
        return await _submit_resident_report_from_form(form)
    if action == "admin_process_video":
        return await _admin_process_video_from_form(form)

    return {
        "ok": False,
        "message": "Missing or invalid action. Use 'submit_resident_report' or 'admin_process_video'.",
    }


@app.local_entrypoint()
def main():
    print("Deployed. Use: modal deploy app.py")
