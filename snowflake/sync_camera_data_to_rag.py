import os
from typing import Any, Dict, List, Tuple

import modal

APP_NAME = "snowflake-rag-sync"
SNOWFLAKE_SECRET_NAME = "SNOWFLAKE"

image = modal.Image.debian_slim(python_version="3.11").pip_install(
    "snowflake-connector-python==3.12.2"
)
app = modal.App(APP_NAME, image=image)
snowflake_secret = modal.Secret.from_name(SNOWFLAKE_SECRET_NAME)


REQUIRED_RAG_COLUMNS = {"DOC_ID", "CATEGORY", "DOC_CONTENT"}


def _connect():
    import snowflake.connector

    required = ["SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing Snowflake env vars in Modal secret: {', '.join(missing)}")

    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        role=os.getenv("SNOWFLAKE_ROLE"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
        database=os.getenv("SNOWFLAKE_DATABASE"),
        schema=os.getenv("SNOWFLAKE_SCHEMA"),
    )


def _assert_rag_schema(conn, rag_table: str) -> None:
    db = os.getenv("SNOWFLAKE_DATABASE")
    schema = os.getenv("SNOWFLAKE_SCHEMA")
    if not db or not schema:
        raise RuntimeError("SNOWFLAKE_DATABASE and SNOWFLAKE_SCHEMA must be set")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_CATALOG = %s
              AND TABLE_SCHEMA = %s
              AND TABLE_NAME = %s
            """,
            (db.upper(), schema.upper(), rag_table.upper()),
        )
        cols = {row[0].upper() for row in cur.fetchall()}

    missing = sorted(REQUIRED_RAG_COLUMNS - cols)
    if missing:
        raise RuntimeError(
            f"RAG table {rag_table} is missing required columns: {', '.join(missing)}"
        )


def _fetch_rows(conn, cameras_table: str, camera_info_table: str):
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT camera_id, camera_name, longitude, latitude
            FROM {cameras_table}
            ORDER BY camera_id
            """
        )
        cameras = cur.fetchall()

        cur.execute(
            f"""
            SELECT
              ci.camera_id,
              c.camera_name,
              c.longitude,
              c.latitude,
              ci.car_count,
              ci.bus_count,
              ci.truck_count,
              ci.motorcycle_count,
              ci.total_unique_vehicles,
              ci.peak_vehicles_per_frame,
              ci.recorded_at
            FROM {camera_info_table} ci
            LEFT JOIN {cameras_table} c ON c.camera_id = ci.camera_id
            ORDER BY ci.recorded_at DESC
            """
        )
        camera_info = cur.fetchall()

    return cameras, camera_info


def _build_documents(
    cameras: List[Tuple[Any, ...]],
    camera_info: List[Tuple[Any, ...]],
) -> List[Tuple[str, str, str]]:
    docs: List[Tuple[str, str, str]] = []

    for row in cameras:
        camera_id, camera_name, longitude, latitude = row
        doc_id = f"camera_meta:{camera_id}"
        content = (
            f"Camera metadata. camera_id={camera_id}; "
            f"camera_name={camera_name}; longitude={longitude}; latitude={latitude}."
        )
        docs.append((doc_id, "camera_metadata", content))

    for row in camera_info:
        (
            camera_id,
            camera_name,
            longitude,
            latitude,
            car_count,
            bus_count,
            truck_count,
            motorcycle_count,
            total_unique_vehicles,
            peak_vehicles_per_frame,
            recorded_at,
        ) = row
        ts = str(recorded_at)
        doc_id = f"camera_info:{camera_id}:{ts}"
        content = (
            f"Camera traffic observation. camera_id={camera_id}; camera_name={camera_name}; "
            f"longitude={longitude}; latitude={latitude}; recorded_at={ts}; "
            f"car_count={car_count}; bus_count={bus_count}; truck_count={truck_count}; "
            f"motorcycle_count={motorcycle_count}; total_unique_vehicles={total_unique_vehicles}; "
            f"peak_vehicles_per_frame={peak_vehicles_per_frame}."
        )
        docs.append((doc_id, "camera_observation", content))

    return docs


def _upsert_docs(conn, rag_table: str, docs: List[Tuple[str, str, str]]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TEMPORARY TABLE TMP_RAG_SYNC (
              doc_id STRING,
              category STRING,
              doc_content STRING
            )
            """
        )

        cur.executemany(
            """
            INSERT INTO TMP_RAG_SYNC (doc_id, category, doc_content)
            VALUES (%s, %s, %s)
            """,
            docs,
        )

        cur.execute(
            f"""
            MERGE INTO {rag_table} t
            USING TMP_RAG_SYNC s
            ON t.doc_id = s.doc_id
            WHEN MATCHED THEN UPDATE SET
              category = s.category,
              doc_content = s.doc_content
            WHEN NOT MATCHED THEN INSERT (doc_id, category, doc_content)
            VALUES (s.doc_id, s.category, s.doc_content)
            """
        )


@app.function(secrets=[snowflake_secret], timeout=1800)
def sync_camera_data_to_rag() -> Dict[str, Any]:
    cameras_table = os.getenv("SNOWFLAKE_CAMERAS_TABLE", "CAMERAS")
    camera_info_table = os.getenv("SNOWFLAKE_CAMERA_INFO_TABLE", "CAMERA_INFO")
    rag_table = os.getenv("SNOWFLAKE_RAG_TABLE", "RAG_DOCUMENTS")

    conn = _connect()
    try:
        _assert_rag_schema(conn, rag_table)
        cameras, camera_info = _fetch_rows(conn, cameras_table, camera_info_table)
        docs = _build_documents(cameras, camera_info)
        _upsert_docs(conn, rag_table, docs)

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM {rag_table}
                WHERE category IN ('camera_metadata', 'camera_observation')
                """
            )
            camera_rag_docs = int(cur.fetchone()[0])

            cur.execute(f"SELECT COUNT(*) FROM {rag_table}")
            total_rag_docs = int(cur.fetchone()[0])

        conn.commit()
    finally:
        conn.close()

    return {
        "cameras_table": cameras_table,
        "camera_info_table": camera_info_table,
        "rag_table": rag_table,
        "source_camera_rows": len(cameras),
        "source_camera_info_rows": len(camera_info),
        "upserted_camera_rag_docs": len(docs),
        "final_camera_related_rag_docs": camera_rag_docs,
        "final_total_rag_docs": total_rag_docs,
    }


@app.local_entrypoint()
def main():
    result = sync_camera_data_to_rag.remote()
    print(result)
