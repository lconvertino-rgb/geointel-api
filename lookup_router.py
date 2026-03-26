import os
import logging
import psycopg2
from fastapi import APIRouter, HTTPException, Query
from comune_resolver import resolve_comune_input
from foglio_scaling import (
    foglio_query_candidates,
    get_scaling_meta,
    normalized_foglio_display,
    parse_non_negative_int,
)

router = APIRouter(prefix="/api/lookup", tags=["lookup"])

DATABASE_URL = os.getenv("DATABASE_URL")
PARCEL_TABLE = os.getenv("PARCEL_TABLE", "public.cadastre_parcel_test")
logger = logging.getLogger("geointelgis.lookup")


def get_conn():
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="DATABASE_URL is not set")
    try:
        return psycopg2.connect(DATABASE_URL, connect_timeout=20)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB connection failed: {e}")


@router.get("/regions")
def list_regions():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT DISTINCT regione
                FROM {PARCEL_TABLE}
                WHERE regione IS NOT NULL
                ORDER BY regione;
            """)
            rows = cur.fetchall()
            return {"regions": [{"value": r[0], "label": r[0]} for r in rows]}
    finally:
        conn.close()


@router.get("/comuni")
def list_comuni(regione: str = Query(...)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT DISTINCT comune
                FROM {PARCEL_TABLE}
                WHERE regione = %s
                  AND comune IS NOT NULL
                ORDER BY comune;
            """, (regione,))
            rows = cur.fetchall()
            return {"comuni": [{"value": r[0], "label": r[0]} for r in rows]}
    finally:
        conn.close()


@router.get("/fogli")
def list_fogli(regione: str = Query(...), comune: str = Query(...)):
    conn = get_conn()
    try:
        resolved = resolve_comune_input(conn, PARCEL_TABLE, regione, comune, endpoint="/api/lookup/fogli")
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
            return {"status": "not_found_db", "fogli": []}
        comune_resolved = resolved["comune"]
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT DISTINCT foglio_num
                FROM {PARCEL_TABLE}
                WHERE regione = %s
                  AND comune = %s
                  AND foglio_num IS NOT NULL
                ORDER BY foglio_num;
            """, (regione, comune_resolved))
            rows = cur.fetchall()
            meta = get_scaling_meta(conn, PARCEL_TABLE, regione, comune_resolved)
            raw_fogli = [int(r[0]) for r in rows]
            if meta["scaled_flag"]:
                display_values = sorted({normalized_foglio_display(v) for v in raw_fogli}, key=int)
            else:
                display_values = [str(v) for v in raw_fogli]
            return {"fogli": [{"value": value, "label": value} for value in display_values]}
    finally:
        conn.close()


@router.get("/particelle")
def list_particelle(
    regione: str = Query(...),
    comune: str = Query(...),
    foglio_num: str = Query(...),
):
    try:
        parsed_foglio = parse_non_negative_int(foglio_num, "foglio_num")
    except HTTPException as exc:
        raise HTTPException(
            status_code=422,
            detail={"status": "invalid_input", "message": exc.detail},
        ) from exc
    conn = get_conn()
    try:
        resolved = resolve_comune_input(conn, PARCEL_TABLE, regione, comune, endpoint="/api/lookup/particelle")
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
            return {"status": "not_found_db", "particelle": []}
        comune_resolved = resolved["comune"]
        meta = get_scaling_meta(conn, PARCEL_TABLE, regione, comune_resolved)
        candidates = foglio_query_candidates(parsed_foglio, meta["scaled_flag"])
        with conn.cursor() as cur:
            rows = []
            for candidate in candidates:
                cur.execute(
                    f"""
                    SELECT DISTINCT particella_num
                    FROM {PARCEL_TABLE}
                    WHERE regione = %s
                      AND comune = %s
                      AND foglio_num = %s
                      AND particella_num IS NOT NULL
                    ORDER BY particella_num;
                    """,
                    (regione, comune_resolved, candidate),
                )
                rows = cur.fetchall()
                if rows:
                    break
            return {"particelle": [{"value": str(r[0]), "label": str(r[0])} for r in rows]}
    finally:
        conn.close()


@router.get("/comune_by_point")
def comune_by_point(
    lng: float = Query(..., ge=-180, le=180),
    lat: float = Query(..., ge=-90, le=90),
):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT regione, comune
                FROM {PARCEL_TABLE}
                WHERE geom IS NOT NULL
                  AND ST_Contains(geom, ST_SetSRID(ST_Point(%s, %s), 4326))
                GROUP BY regione, comune
                LIMIT 2;
                """,
                (lng, lat),
            )
            rows = cur.fetchall()
            if not rows:
                raise HTTPException(
                    status_code=404,
                    detail={"status": "not_found", "message": "No comune found at point."},
                )
            if len(rows) > 1:
                logger.warning("comune_by_point multiple matches for lng=%s lat=%s; using first", lng, lat)
            regione, comune = rows[0]
            return {
                "status": "ok",
                "regione": regione,
                "comune": comune,
                "label": comune,
            }
    finally:
        conn.close()
