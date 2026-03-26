import os
from typing import Optional

import psycopg2
from fastapi import APIRouter, HTTPException, Query

from comune_resolver import resolve_comune_input

router = APIRouter(prefix="/api/catasto", tags=["catasto"])

DATABASE_URL = os.getenv("DATABASE_URL")
PARCEL_TABLE = os.getenv("PARCEL_TABLE", "public.cadastre_parcel_test")
PARTICELLE_MAX_FEATURES = int(os.getenv("CATASTO_PARTICELLE_MAX", "3000"))
FOGLI_FULL_MAX = int(os.getenv("CATASTO_FOGLI_FULL_MAX", "200"))


def get_conn():
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="DATABASE_URL is not set")
    try:
        return psycopg2.connect(DATABASE_URL, connect_timeout=20)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB connection failed: {e}")


def parse_bbox_or_422(bbox: str):
    try:
        parts = [float(x.strip()) for x in bbox.split(",")]
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={"status": "invalid_input", "message": f"Invalid bbox format: {exc}"},
        ) from exc
    if len(parts) != 4:
        raise HTTPException(
            status_code=422,
            detail={"status": "invalid_input", "message": "bbox must be minx,miny,maxx,maxy"},
        )
    minx, miny, maxx, maxy = parts
    if not (minx < maxx and miny < maxy):
        raise HTTPException(
            status_code=422,
            detail={"status": "invalid_input", "message": "bbox coordinates are invalid"},
        )
    return minx, miny, maxx, maxy


def resolve_comune_or_raise(conn, regione: str, comune: str, endpoint: str):
    resolved = resolve_comune_input(conn, PARCEL_TABLE, regione, comune, endpoint=endpoint)
    if resolved["ok"]:
        return resolved["comune"]
    if resolved["status"] == "ambiguous":
        raise HTTPException(
            status_code=422,
            detail={
                "status": "ambiguous_comune",
                "message": "Comune input matched multiple values.",
                "matches": resolved["matches"],
            },
        )
    raise HTTPException(
        status_code=422,
        detail={"status": "not_found_db", "message": "No comune found for the provided input."},
    )


@router.get("/fogli")
def catasto_fogli(
    regione: Optional[str] = Query(None),
    comune: Optional[str] = Query(None),
    bbox: str = Query(..., description="minx,miny,maxx,maxy"),
    full_foglio: bool = Query(False),
):
    minx, miny, maxx, maxy = parse_bbox_or_422(bbox)
    conn = get_conn()
    try:
        comune_resolved = None
        if comune:
            if regione:
                comune_resolved = resolve_comune_or_raise(conn, regione, comune, endpoint="/api/catasto/fogli")
            else:
                comune_resolved = comune.strip()

        with conn.cursor() as cur:
            bbox_sql = """
                geom IS NOT NULL
                AND foglio_num IS NOT NULL
                AND geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                AND ST_Intersects(geom, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
            """
            bbox_params = [minx, miny, maxx, maxy, minx, miny, maxx, maxy]
            if regione:
                bbox_sql += " AND regione = %s"
                bbox_params.append(regione)
            if comune_resolved:
                bbox_sql += " AND comune = %s"
                bbox_params.append(comune_resolved)

            if full_foglio:
                cur.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM (
                      SELECT DISTINCT comune, foglio_num
                      FROM {PARCEL_TABLE}
                      WHERE {bbox_sql}
                    ) t;
                    """,
                    bbox_params,
                )
                fogli_pairs = int(cur.fetchone()[0] or 0)
                if fogli_pairs > FOGLI_FULL_MAX:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "status": "invalid_input",
                            "message": f"Too many fogli in view ({fogli_pairs}). Zoom in to fetch <= {FOGLI_FULL_MAX}.",
                        },
                    )

                cur.execute(
                    f"""
                    WITH fogli_in_view AS (
                      SELECT DISTINCT comune, foglio_num
                      FROM {PARCEL_TABLE}
                      WHERE {bbox_sql}
                    )
                    SELECT
                      t.regione::text AS regione_name,
                      t.comune::text AS comune_name,
                      t.foglio_num::text,
                      ST_AsGeoJSON(ST_UnaryUnion(ST_Collect(t.geom)))::json AS geom_json,
                      ST_X(ST_Centroid(ST_UnaryUnion(ST_Collect(t.geom)))) AS cx,
                      ST_Y(ST_Centroid(ST_UnaryUnion(ST_Collect(t.geom)))) AS cy
                    FROM {PARCEL_TABLE} t
                    JOIN fogli_in_view fv
                      ON fv.comune = t.comune
                     AND fv.foglio_num = t.foglio_num
                    WHERE t.geom IS NOT NULL
                    GROUP BY t.regione, t.comune, t.foglio_num
                    ORDER BY t.regione, t.comune, t.foglio_num;
                    """,
                    bbox_params,
                )
            else:
                cur.execute(
                    f"""
                    SELECT
                      regione::text AS regione_name,
                      comune::text AS comune_name,
                      foglio_num::text,
                      ST_AsGeoJSON(ST_UnaryUnion(ST_Collect(geom)))::json AS geom_json,
                      ST_X(ST_Centroid(ST_UnaryUnion(ST_Collect(geom)))) AS cx,
                      ST_Y(ST_Centroid(ST_UnaryUnion(ST_Collect(geom)))) AS cy
                    FROM {PARCEL_TABLE}
                    WHERE {bbox_sql}
                    GROUP BY regione, comune, foglio_num
                    ORDER BY regione, comune, foglio_num;
                    """,
                    bbox_params,
                )
            rows = cur.fetchall()

        features = []
        for regione_name, comune_name, foglio_num, geom_json, cx, cy in rows:
            features.append(
                {
                    "type": "Feature",
                    "geometry": geom_json,
                    "properties": {
                        "regione_name": regione_name,
                        "comune_name": comune_name,
                        "foglio_num": foglio_num,
                        "centroid": [cx, cy],
                    },
                }
            )
        return {"status": "ok", "feature_collection": {"type": "FeatureCollection", "features": features}, "count": len(features)}
    finally:
        conn.close()


@router.get("/particelle")
def catasto_particelle(
    regione: Optional[str] = Query(None),
    comune: Optional[str] = Query(None),
    bbox: str = Query(..., description="minx,miny,maxx,maxy"),
):
    minx, miny, maxx, maxy = parse_bbox_or_422(bbox)
    conn = get_conn()
    try:
        comune_resolved = None
        if comune:
            if regione:
                comune_resolved = resolve_comune_or_raise(conn, regione, comune, endpoint="/api/catasto/particelle")
            else:
                comune_resolved = comune.strip()

        where_sql = """
            geom IS NOT NULL
            AND geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
            AND ST_Intersects(geom, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
        """
        where_params = [minx, miny, maxx, maxy, minx, miny, maxx, maxy]
        if regione:
            where_sql += " AND regione = %s"
            where_params.append(regione)
        if comune_resolved:
            where_sql += " AND comune = %s"
            where_params.append(comune_resolved)

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM {PARCEL_TABLE}
                WHERE {where_sql};
                """,
                where_params,
            )
            total = int(cur.fetchone()[0] or 0)
            if total > PARTICELLE_MAX_FEATURES:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "status": "invalid_input",
                        "message": f"Too many particelle in bbox ({total}). Zoom in to fetch <= {PARTICELLE_MAX_FEATURES}.",
                    },
                )

            cur.execute(
                f"""
                SELECT
                  regione::text AS regione_name,
                  comune::text AS comune_name,
                  foglio_num::text,
                  particella_num::text,
                  ST_AsGeoJSON(geom)::json AS geom_json,
                  ST_Area(geom::geography) AS area_m2,
                  ST_X(ST_Centroid(geom)) AS cx,
                  ST_Y(ST_Centroid(geom)) AS cy
                FROM {PARCEL_TABLE}
                WHERE {where_sql}
                  AND foglio_num IS NOT NULL
                  AND particella_num IS NOT NULL
                LIMIT %s;
                """,
                [*where_params, PARTICELLE_MAX_FEATURES],
            )
            rows = cur.fetchall()

        features = []
        for regione_name, comune_name, foglio_num, particella_num, geom_json, area_m2, cx, cy in rows:
            features.append(
                {
                    "type": "Feature",
                    "geometry": geom_json,
                    "properties": {
                        "regione_name": regione_name,
                        "comune_name": comune_name,
                        "foglio_num": foglio_num,
                        "particella_num": particella_num,
                        "area_m2": float(area_m2) if area_m2 is not None else None,
                        "centroid": [cx, cy],
                    },
                }
            )
        return {"status": "ok", "feature_collection": {"type": "FeatureCollection", "features": features}, "count": len(features)}
    finally:
        conn.close()
