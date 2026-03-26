import csv
import io
import os
import re
import tempfile
import uuid
from pathlib import Path

import psycopg2
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from comune_resolver import resolve_comune_input
from pydantic import BaseModel, field_validator
from foglio_scaling import foglio_query_candidates, get_scaling_meta, parse_non_negative_int

router = APIRouter(prefix="/api/parcels", tags=["parcels"])

DATABASE_URL = os.getenv("DATABASE_URL")

# Which table the API queries
# Default is your test table that you loaded with 5000 rows:
# public.cadastre_parcel_test
PARCEL_TABLE = os.getenv("PARCEL_TABLE", "public.cadastre_parcel_test")
REPORT_DIR = Path(tempfile.gettempdir()) / "geointelgis_reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)


class ParcelSearch(BaseModel):
    regione: str
    comune: str
    foglio_num: int | str
    particella_num: int | str

    @field_validator("regione", "comune")
    @classmethod
    def validate_text_key(cls, value: str, info):
        text = str(value).strip() if value is not None else ""
        if not text:
            raise ValueError(f"{info.field_name} is required")
        return text

    @field_validator("foglio_num", "particella_num", mode="before")
    @classmethod
    def parse_numeric_key(cls, value, info):
        field_name = info.field_name

        if value is None:
            raise ValueError(f"{field_name} is required")

        if isinstance(value, bool):
            raise ValueError(f"{field_name} must be an integer")

        if isinstance(value, int):
            parsed = value
        elif isinstance(value, float):
            if not value.is_integer():
                raise ValueError(f"{field_name} must be an integer")
            parsed = int(value)
        elif isinstance(value, str):
            raw = value.strip()
            if not raw:
                raise ValueError(f"{field_name} is required")
            if not raw.isdigit():
                raise ValueError(f"{field_name} must contain only digits")
            parsed = int(raw)
        else:
            raise ValueError(f"{field_name} must be an integer")

        if parsed < 0:
            raise ValueError(f"{field_name} must be >= 0")

        return parsed


class ParcelBatchItem(ParcelSearch):
    ref_id: str | int | None = None


class ParcelBatchRequest(BaseModel):
    items: list[ParcelBatchItem]

    @field_validator("items")
    @classmethod
    def validate_items(cls, value):
        if not value:
            raise ValueError("items is required")
        return value


class PointSearchQuery(BaseModel):
    lng: float
    lat: float


class XYSearchQuery(BaseModel):
    x: float
    y: float
    srid: int = 4326


class BufferSearchItem(BaseModel):
    regione: str
    comune: str
    foglio_num: int | str
    particella_num: int | str

    @field_validator("regione", "comune")
    @classmethod
    def validate_text_key(cls, value: str, info):
        text = str(value).strip() if value is not None else ""
        if not text:
            raise ValueError(f"{info.field_name} is required")
        return text

    @field_validator("foglio_num", "particella_num", mode="before")
    @classmethod
    def parse_numeric_key(cls, value, info):
        return ParcelSearch.parse_numeric_key(value, info)


class BufferSearchRequest(BaseModel):
    items: list[BufferSearchItem]
    distance_m: float
    limit: int = 2000

    @field_validator("items")
    @classmethod
    def validate_items(cls, value):
        if not value:
            raise ValueError("items is required")
        return value

    @field_validator("distance_m")
    @classmethod
    def validate_distance(cls, value):
        if value <= 0:
            raise ValueError("distance_m must be > 0")
        if value > 5000:
            raise ValueError("distance_m must be <= 5000")
        return float(value)

    @field_validator("limit")
    @classmethod
    def validate_limit(cls, value):
        return max(1, min(int(value), 5000))


def get_conn():
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="DATABASE_URL is not set")
    try:
        return psycopg2.connect(DATABASE_URL, connect_timeout=20)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB connection failed: {e}")


@router.get("/debug/table")
def debug_table():
    return {"parcel_table": PARCEL_TABLE}


def _find_parcel_rows(conn, payload: ParcelSearch, comune_resolved: str):
    meta = get_scaling_meta(conn, PARCEL_TABLE, payload.regione, comune_resolved)
    foglio_candidates = foglio_query_candidates(payload.foglio_num, meta["scaled_flag"])
    with conn.cursor() as cur:
        sql = f"""
            SELECT
                ogc_fid,
                regione,
                comune,
                foglio_num,
                particella_num,
                label,
                nationalcadastralreference,
                ST_Area(geom::geography) AS area_m2,
                ST_AsGeoJSON(geom)::json AS geom_geojson
            FROM {PARCEL_TABLE}
            WHERE regione = %s
              AND comune = %s
              AND foglio_num = %s
              AND particella_num = %s
            LIMIT 50;
        """
        for foglio_candidate in foglio_candidates:
            cur.execute(
                sql,
                (payload.regione, comune_resolved, foglio_candidate, payload.particella_num),
            )
            rows = cur.fetchall()
            if rows:
                return rows, foglio_candidate
    return [], None


def _keys_for_result(payload: ParcelSearch, foglio_stored: int | None, comune_resolved: str | None):
    return {
        "regione": payload.regione,
        "comune": comune_resolved or payload.comune,
        "foglio_display": str(payload.foglio_num),
        "foglio_stored": str(foglio_stored) if foglio_stored is not None else None,
        "particella_display": str(payload.particella_num),
        "particella_stored": str(payload.particella_num),
    }


def _build_search_feature_collection(payload: ParcelSearch, rows, foglio_stored: int, comune_resolved: str):
    features = []
    keys = _keys_for_result(payload, foglio_stored, comune_resolved)
    for row in rows:
        ogc_fid, regione, comune, foglio_num, particella_num, label, ncr, area_m2, geom_geojson = row
        features.append(
            {
                "type": "Feature",
                "geometry": geom_geojson,
                "properties": {
                    "ogc_fid": ogc_fid,
                    "regione": regione,
                    "comune": comune,
                    "foglio_num": foglio_num,
                    "particella_num": particella_num,
                    "label": label,
                    "nationalcadastralreference": ncr,
                    "foglio_display": keys["foglio_display"],
                    "foglio_stored": keys["foglio_stored"],
                    "particella_display": keys["particella_display"],
                    "particella_stored": keys["particella_stored"],
                    "area_m2": float(area_m2) if area_m2 is not None else None,
                },
            }
        )
    return {"type": "FeatureCollection", "features": features, "keys": keys}


def _build_feature_collection(rows):
    features = []
    for row in rows:
        ogc_fid, regione, comune, foglio_num, particella_num, label, ncr, area_m2, geom_geojson = row
        features.append(
            {
                "type": "Feature",
                "geometry": geom_geojson,
                "properties": {
                    "ogc_fid": ogc_fid,
                    "regione": regione,
                    "comune": comune,
                    "foglio_num": foglio_num,
                    "particella_num": particella_num,
                    "label": label,
                    "nationalcadastralreference": ncr,
                    "foglio_display": str(foglio_num),
                    "foglio_stored": str(foglio_num),
                    "particella_display": str(particella_num),
                    "particella_stored": str(particella_num),
                    "area_m2": float(area_m2) if area_m2 is not None else None,
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _query_parcels_by_point(conn, lng: float, lat: float, srid: int = 4326, limit: int = 100):
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                ogc_fid,
                regione,
                comune,
                foglio_num,
                particella_num,
                label,
                nationalcadastralreference,
                ST_Area(geom::geography) AS area_m2,
                ST_AsGeoJSON(geom)::json AS geom_geojson
            FROM {PARCEL_TABLE}
            WHERE geom IS NOT NULL
              AND ST_Intersects(
                    geom,
                    ST_Transform(ST_SetSRID(ST_Point(%s, %s), %s), 4326)
                  )
            LIMIT %s;
            """,
            (lng, lat, srid, limit),
        )
        return cur.fetchall()


def _run_batch(conn, items: list[ParcelBatchItem]):
    results = []
    found_count = 0
    not_found_count = 0
    error_count = 0

    for item in items:
        try:
            resolved = resolve_comune_input(
                conn, PARCEL_TABLE, item.regione, item.comune, endpoint="/api/parcels/search_batch"
            )
            if not resolved["ok"]:
                if resolved["status"] == "ambiguous":
                    error_count += 1
                    results.append(
                        {
                            "ref_id": item.ref_id,
                            "status": "ambiguous_comune",
                            "found": False,
                            "keys": _keys_for_result(item, None, None),
                            "error": "Comune input matched multiple values.",
                            "matches": resolved["matches"],
                        }
                    )
                    continue
                not_found_count += 1
                results.append(
                    {
                        "ref_id": item.ref_id,
                        "status": "not_found_db",
                        "found": False,
                        "keys": _keys_for_result(item, None, None),
                        "error": "No comune found for the provided input.",
                    }
                )
                continue

            comune_resolved = resolved["comune"]
            rows, foglio_stored = _find_parcel_rows(conn, item, comune_resolved)
            keys = _keys_for_result(item, foglio_stored, comune_resolved)
            if rows:
                found_count += 1
                first_geom = rows[0][8]
                first_area_m2 = rows[0][7]
                results.append(
                    {
                        "ref_id": item.ref_id,
                        "status": "ok",
                        "found": True,
                        "keys": keys,
                        "geometry": first_geom,
                        "area_m2": float(first_area_m2) if first_area_m2 is not None else None,
                    }
                )
            else:
                not_found_count += 1
                results.append(
                    {
                        "ref_id": item.ref_id,
                        "status": "not_found_db",
                        "found": False,
                        "keys": keys,
                        "error": "No parcel found for the selected region/comune/foglio/particella.",
                    }
                )
        except Exception as exc:
            error_count += 1
            results.append(
                {
                    "ref_id": item.ref_id,
                    "status": "internal_error",
                    "found": False,
                    "keys": _keys_for_result(item, None, None),
                    "error": str(exc),
                }
            )

    counts = {
        "total": len(items),
        "found": found_count,
        "not_found": not_found_count,
        "errors": error_count,
    }
    return {"results": results, "counts": counts}


@router.post("/search")
def search_parcel(payload: ParcelSearch):
    conn = get_conn()
    try:
        resolved = resolve_comune_input(conn, PARCEL_TABLE, payload.regione, payload.comune, endpoint="/api/parcels/search")
        if not resolved["ok"]:
            if resolved["status"] == "ambiguous":
                raise HTTPException(
                    status_code=422,
                    detail={
                        "status": "ambiguous_comune",
                        "message": "Comune input matched multiple values.",
                        "matches": resolved["matches"],
                    },
                )
            return {
                "status": "not_found_db",
                "message": "No comune found for the provided input.",
                "parcel_table": PARCEL_TABLE,
                "keys": _keys_for_result(payload, None, None),
            }
        comune_resolved = resolved["comune"]
        rows, foglio_stored = _find_parcel_rows(conn, payload, comune_resolved)
        if not rows:
            return {
                "status": "not_found_db",
                "message": "No parcel found for the selected region/comune/foglio/particella.",
                "parcel_table": PARCEL_TABLE,
                "keys": _keys_for_result(payload, None, comune_resolved),
            }
        return _build_search_feature_collection(payload, rows, foglio_stored, comune_resolved)
    finally:
        conn.close()


@router.post("/search_batch")
def search_batch(payload: ParcelBatchRequest):
    conn = get_conn()
    try:
        return _run_batch(conn, payload.items)
    finally:
        conn.close()


@router.get("/by_point")
def parcels_by_point(
    lng: float = Query(..., ge=-180, le=180),
    lat: float = Query(..., ge=-90, le=90),
    limit: int = Query(100, ge=1, le=1000),
):
    conn = get_conn()
    try:
        rows = _query_parcels_by_point(conn, lng=lng, lat=lat, srid=4326, limit=limit)
        return {
            "status": "ok",
            "feature_collection": _build_feature_collection(rows),
            "count": len(rows),
            "point": {"lng": lng, "lat": lat, "srid": 4326},
        }
    finally:
        conn.close()


@router.get("/by_xy")
def parcels_by_xy(
    x: float = Query(...),
    y: float = Query(...),
    srid: int = Query(3857, ge=2000, le=1000000),
    limit: int = Query(100, ge=1, le=1000),
):
    conn = get_conn()
    try:
        rows = _query_parcels_by_point(conn, lng=x, lat=y, srid=srid, limit=limit)
        return {
            "status": "ok",
            "feature_collection": _build_feature_collection(rows),
            "count": len(rows),
            "point": {"x": x, "y": y, "srid": srid},
        }
    finally:
        conn.close()


@router.post("/search_buffer")
def search_buffer(payload: BufferSearchRequest):
    conn = get_conn()
    try:
        selected_ids = []
        for item in payload.items:
            resolved = resolve_comune_input(
                conn,
                PARCEL_TABLE,
                item.regione,
                item.comune,
                endpoint="/api/parcels/search_buffer",
            )
            if not resolved["ok"]:
                continue
            comune_resolved = resolved["comune"]
            search_payload = ParcelSearch(
                regione=item.regione,
                comune=comune_resolved,
                foglio_num=item.foglio_num,
                particella_num=item.particella_num,
            )
            rows, _ = _find_parcel_rows(conn, search_payload, comune_resolved)
            selected_ids.extend([r[0] for r in rows])

        selected_ids = sorted(set(selected_ids))
        if not selected_ids:
            return {
                "status": "not_found_db",
                "message": "No valid selected parcels found for buffer input.",
                "count": 0,
                "feature_collection": {"type": "FeatureCollection", "features": []},
                "buffer_geometry": None,
            }

        with conn.cursor() as cur:
            cur.execute(
                f"""
                WITH selected AS (
                  SELECT geom
                  FROM {PARCEL_TABLE}
                  WHERE ogc_fid = ANY(%s)
                    AND geom IS NOT NULL
                ),
                buffered AS (
                  SELECT ST_Buffer(ST_UnaryUnion(ST_Collect(geom))::geography, %s)::geometry AS geom
                  FROM selected
                )
                SELECT
                    t.ogc_fid,
                    t.regione,
                    t.comune,
                    t.foglio_num,
                    t.particella_num,
                    t.label,
                    t.nationalcadastralreference,
                    ST_Area(t.geom::geography) AS area_m2,
                    ST_AsGeoJSON(t.geom)::json AS geom_geojson
                FROM {PARCEL_TABLE} t
                CROSS JOIN buffered b
                WHERE t.geom IS NOT NULL
                  AND b.geom IS NOT NULL
                  AND ST_Intersects(t.geom, b.geom)
                LIMIT %s;
                """,
                (selected_ids, payload.distance_m, payload.limit),
            )
            rows = cur.fetchall()

            cur.execute(
                f"""
                WITH selected AS (
                  SELECT geom
                  FROM {PARCEL_TABLE}
                  WHERE ogc_fid = ANY(%s)
                    AND geom IS NOT NULL
                )
                SELECT ST_AsGeoJSON(ST_Buffer(ST_UnaryUnion(ST_Collect(geom))::geography, %s)::geometry)::json
                FROM selected;
                """,
                (selected_ids, payload.distance_m),
            )
            buffer_row = cur.fetchone()
            buffer_geom = buffer_row[0] if buffer_row else None

        return {
            "status": "ok",
            "count": len(rows),
            "feature_collection": _build_feature_collection(rows),
            "buffer_geometry": buffer_geom,
            "distance_m": payload.distance_m,
            "selected_source_count": len(selected_ids),
        }
    finally:
        conn.close()


@router.get("/foglio_extent")
def foglio_extent(regione: str, comune: str, foglio_num: str):
    try:
        parsed_foglio = parse_non_negative_int(foglio_num, "foglio_num")
    except HTTPException as exc:
        raise HTTPException(
            status_code=422,
            detail={"status": "invalid_input", "message": exc.detail},
        ) from exc

    conn = get_conn()
    try:
        resolved = resolve_comune_input(
            conn, PARCEL_TABLE, regione, comune, endpoint="/api/parcels/foglio_extent"
        )
        if not resolved["ok"]:
            if resolved["status"] == "ambiguous":
                raise HTTPException(
                    status_code=422,
                    detail={
                        "status": "ambiguous_comune",
                        "message": "Comune input matched multiple values.",
                        "matches": resolved["matches"],
                    },
                )
            return {
                "status": "not_found_db",
                "message": "No comune found for the provided input.",
            }

        comune_resolved = resolved["comune"]
        meta = get_scaling_meta(conn, PARCEL_TABLE, regione, comune_resolved)
        foglio_candidates = foglio_query_candidates(parsed_foglio, meta["scaled_flag"])

        with conn.cursor() as cur:
            for candidate in foglio_candidates:
                cur.execute(
                    f"""
                    SELECT ST_AsGeoJSON(ST_Envelope(ST_Collect(geom)))::json
                    FROM {PARCEL_TABLE}
                    WHERE regione = %s
                      AND comune = %s
                      AND foglio_num = %s
                      AND geom IS NOT NULL;
                    """,
                    (regione, comune_resolved, candidate),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return {
                        "status": "ok",
                        "geometry": row[0],
                        "keys": {
                            "regione": regione,
                            "comune": comune_resolved,
                            "foglio_display": str(parsed_foglio),
                            "foglio_stored": str(candidate),
                        },
                    }

        return {
            "status": "not_found_db",
            "message": "No foglio geometry found for the selected keys.",
            "keys": {
                "regione": regione,
                "comune": comune_resolved,
                "foglio_display": str(parsed_foglio),
                "foglio_stored": None,
            },
        }
    finally:
        conn.close()


def _resolve_column(row: dict, keys: list[str]):
    for key in keys:
        if key in row and row[key] is not None:
            value = str(row[key]).strip()
            if value:
                return value
    return None


@router.post("/upload")
async def upload_parcels_csv(file: UploadFile = File(...)):
    filename = file.filename or ""
    if not filename.lower().endswith(".csv"):
        raise HTTPException(
            status_code=422,
            detail={"status": "invalid_input", "message": "Only CSV uploads are supported."},
        )

    payload = await file.read()
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail={"status": "invalid_input", "message": f"CSV decode failed: {exc}"},
        ) from exc

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(
            status_code=422,
            detail={"status": "invalid_input", "message": "CSV is missing a header row."},
        )

    normalized_map = {str(name).strip().lower(): name for name in reader.fieldnames}
    required_cols = {
        "regione": ["regione"],
        "comune": ["comune"],
        "foglio_num": ["foglio", "foglio_num"],
        "particella_num": ["particella", "particella_num"],
        "ref_id": ["ref_id", "id", "reference"],
    }

    resolved = {}
    for field, candidates in required_cols.items():
        for candidate in candidates:
            original = normalized_map.get(candidate)
            if original:
                resolved[field] = original
                break

    for required in ("regione", "comune", "foglio_num", "particella_num"):
        if required not in resolved:
            raise HTTPException(
                status_code=422,
                detail={"status": "invalid_input", "message": f"Missing required CSV column: {required}"},
            )

    ordered_entries = []
    valid_items = []
    for line_no, row in enumerate(reader, start=2):
        try:
            data = {
                "regione": _resolve_column(row, [resolved["regione"]]),
                "comune": _resolve_column(row, [resolved["comune"]]),
                "foglio_num": _resolve_column(row, [resolved["foglio_num"]]),
                "particella_num": _resolve_column(row, [resolved["particella_num"]]),
                "ref_id": _resolve_column(row, [resolved["ref_id"]]) if "ref_id" in resolved else str(line_no),
            }
            item = ParcelBatchItem(**data)
            valid_items.append(item)
            ordered_entries.append({"kind": "valid"})
        except Exception as exc:
            ordered_entries.append(
                {
                    "kind": "error",
                    "result": {
                        "ref_id": _resolve_column(row, [resolved["ref_id"]]) if "ref_id" in resolved else str(line_no),
                        "status": "invalid_input",
                        "found": False,
                        "keys": {
                            "regione": _resolve_column(row, [resolved["regione"]]),
                            "comune": _resolve_column(row, [resolved["comune"]]),
                            "foglio_display": _resolve_column(row, [resolved["foglio_num"]]),
                            "foglio_stored": None,
                            "particella_display": _resolve_column(row, [resolved["particella_num"]]),
                            "particella_stored": None,
                        },
                        "error": f"Invalid CSV row {line_no}: {exc}",
                    },
                }
            )

    batch = {"results": [], "counts": {"total": len(ordered_entries), "found": 0, "not_found": 0, "errors": 0}}
    if valid_items:
        conn = get_conn()
        try:
            batch = _run_batch(conn, valid_items)
        finally:
            conn.close()

    merged_results = []
    valid_index = 0
    for entry in ordered_entries:
        if entry["kind"] == "valid":
            merged_results.append(batch["results"][valid_index])
            valid_index += 1
        else:
            merged_results.append(entry["result"])

    counts = {"total": len(merged_results), "found": 0, "not_found": 0, "errors": 0}
    for result in merged_results:
        if result["status"] == "ok":
            counts["found"] += 1
        elif result["status"] == "not_found_db":
            counts["not_found"] += 1
        else:
            counts["errors"] += 1

    report_id = str(uuid.uuid4())
    report_path = REPORT_DIR / f"{report_id}.csv"
    with report_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "ref_id",
                "status",
                "found",
                "regione",
                "comune",
                "foglio_display",
                "foglio_stored",
                "particella_display",
                "particella_stored",
                "possible_matches",
                "error",
            ]
        )
        for result in merged_results:
            keys = result.get("keys", {})
            possible_matches = ""
            if result.get("matches"):
                possible_matches = "|".join(result["matches"])
            writer.writerow(
                [
                    result.get("ref_id"),
                    result.get("status"),
                    result.get("found"),
                    keys.get("regione"),
                    keys.get("comune"),
                    keys.get("foglio_display"),
                    keys.get("foglio_stored"),
                    keys.get("particella_display"),
                    keys.get("particella_stored"),
                    possible_matches,
                    result.get("error", ""),
                ]
            )

    return {
        "results": merged_results,
        "counts": counts,
        "report_id": report_id,
        "report_url": f"/api/parcels/report/{report_id}",
    }


@router.get("/report/{report_id}")
def download_report(report_id: str):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", report_id):
        raise HTTPException(status_code=400, detail="Invalid report id.")
    report_path = REPORT_DIR / f"{report_id}.csv"
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="Report not found.")
    return FileResponse(
        path=str(report_path),
        media_type="text/csv",
        filename=f"parcel_report_{report_id}.csv",
    )
