import re
from threading import Lock

from fastapi import HTTPException

_CACHE: dict[tuple[str, str, str], dict] = {}
_CACHE_LOCK = Lock()


def _cache_key(parcel_table: str, regione: str, comune: str) -> tuple[str, str, str]:
    return (parcel_table, regione.strip(), comune.strip())


def parse_non_negative_int(value, field_name: str) -> int:
    if value is None:
        raise HTTPException(status_code=422, detail=f"{field_name} is required")
    if isinstance(value, bool):
        raise HTTPException(status_code=422, detail=f"{field_name} must be an integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise HTTPException(status_code=422, detail=f"{field_name} must be an integer")
        parsed = int(value)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise HTTPException(status_code=422, detail=f"{field_name} is required")
        if not re.fullmatch(r"[0-9]+", raw):
            raise HTTPException(status_code=422, detail=f"{field_name} must contain only digits")
        parsed = int(raw)
    else:
        raise HTTPException(status_code=422, detail=f"{field_name} must be an integer")

    if parsed < 0:
        raise HTTPException(status_code=422, detail=f"{field_name} must be >= 0")
    return parsed


def get_scaling_meta(conn, parcel_table: str, regione: str, comune: str) -> dict:
    key = _cache_key(parcel_table, regione, comune)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return cached

    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH stats AS (
                SELECT
                    COUNT(DISTINCT foglio_num)::int AS total_fogli,
                    COUNT(DISTINCT foglio_num) FILTER (WHERE (foglio_num %% 100) = 0)::int AS divisible_by_100_fogli,
                    MIN(foglio_num)::int AS min_foglio,
                    MAX(foglio_num)::int AS max_foglio
                FROM {parcel_table}
                WHERE regione = %s
                  AND comune = %s
                  AND foglio_num IS NOT NULL
            )
            SELECT
                total_fogli,
                divisible_by_100_fogli,
                CASE
                    WHEN total_fogli = 0 THEN 0
                    ELSE ROUND(divisible_by_100_fogli::numeric / total_fogli, 4)
                END AS pct_divisible_by_100,
                min_foglio,
                max_foglio
            FROM stats;
            """,
            (regione, comune),
        )
        total_fogli, divisible_by_100_fogli, pct_divisible_by_100, min_foglio, max_foglio = cur.fetchone()

        cur.execute(
            f"""
            SELECT foglio_num::int
            FROM {parcel_table}
            WHERE regione = %s
              AND comune = %s
              AND foglio_num IS NOT NULL
            GROUP BY foglio_num
            ORDER BY foglio_num
            LIMIT 10;
            """,
            (regione, comune),
        )
        sample_fogli = [str(row[0]) for row in cur.fetchall()]

    pct_div100_f = float(pct_divisible_by_100 or 0.0)
    max_foglio_i = int(max_foglio) if max_foglio is not None else 0
    scaled_flag = bool(max_foglio_i >= 1000 and pct_div100_f >= 0.90)
    if pct_div100_f >= 0.90 and max_foglio_i >= 2000:
        confidence = "high"
    elif pct_div100_f >= 0.60:
        confidence = "medium"
    else:
        confidence = "low"

    meta = {
        "scaled_flag": scaled_flag,
        "pct_divisible_by_100": pct_div100_f,
        "max_foglio": max_foglio,
        "min_foglio": min_foglio,
        "total_fogli": int(total_fogli or 0),
        "divisible_by_100_fogli": int(divisible_by_100_fogli or 0),
        "confidence": confidence,
        "sample_fogli": sample_fogli,
    }
    with _CACHE_LOCK:
        _CACHE[key] = meta
    return meta


def normalized_foglio_display(stored_foglio: int) -> str:
    if stored_foglio % 100 == 0:
        return str(stored_foglio // 100)
    return str(stored_foglio)


def foglio_query_candidates(input_foglio: int, scaled_flag: bool) -> list[int]:
    if not scaled_flag:
        return [input_foglio]
    mapped = input_foglio * 100
    if mapped == input_foglio:
        return [input_foglio]
    return [mapped, input_foglio]
