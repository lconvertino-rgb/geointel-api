import os

import psycopg2
from fastapi import APIRouter, HTTPException, Query

from foglio_scaling import get_scaling_meta

router = APIRouter(prefix="/api/debug", tags=["debug"])

DATABASE_URL = os.getenv("DATABASE_URL")
PARCEL_TABLE = os.getenv("PARCEL_TABLE", "public.cadastre_parcel_test")


def get_conn():
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="DATABASE_URL is not set")
    try:
        return psycopg2.connect(DATABASE_URL, connect_timeout=20)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB connection failed: {e}")


@router.get("/foglio-scaling")
def debug_foglio_scaling(regione: str = Query(...), comune: str = Query(...)):
    conn = get_conn()
    try:
        meta = get_scaling_meta(conn, PARCEL_TABLE, regione, comune)
        return {
            "regione": regione,
            "comune": comune,
            "scaled_flag": meta["scaled_flag"],
            "pct_divisible_by_100": meta["pct_divisible_by_100"],
            "max_foglio": meta["max_foglio"],
            "sample_fogli": meta["sample_fogli"],
            "confidence": meta["confidence"],
        }
    finally:
        conn.close()
