import re
import logging

logger = logging.getLogger("geointelgis.comune_resolver")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    logger.addHandler(_handler)
    logger.propagate = False


def _collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _normalize_name(value: str) -> str:
    value = (value or "").upper().replace("_", " ").replace("-", " ")
    return _collapse_ws(value)


def _log_resolver(endpoint: str | None, regione: str, comune_input: str, status: str, matches: list[str] | None):
    matches_count = len(matches or [])
    if status == "not_found":
        logger.info(
            "comune_resolver endpoint=%s regione=%s comune_input=%s status=%s matches_count=%s",
            endpoint or "unknown",
            regione,
            comune_input,
            status,
            matches_count,
        )
    elif status == "ambiguous":
        logger.warning(
            "comune_resolver endpoint=%s regione=%s comune_input=%s status=%s matches_count=%s matches_sample=%s",
            endpoint or "unknown",
            regione,
            comune_input,
            status,
            matches_count,
            (matches or [])[:10],
        )


def resolve_comune_input(conn, parcel_table: str, regione: str, comune_input: str, endpoint: str | None = None) -> dict:
    comune_raw = _collapse_ws(comune_input)
    if not comune_raw:
        _log_resolver(endpoint, regione, comune_input or "", "not_found", [])
        return {"ok": False, "status": "not_found", "matches": []}

    # Rule 1: contains "_" => treat as exact key (case-insensitive)
    if "_" in comune_raw:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT DISTINCT comune
                FROM {parcel_table}
                WHERE regione = %s
                  AND UPPER(comune) = UPPER(%s)
                ORDER BY comune
                LIMIT 2;
                """,
                (regione, comune_raw),
            )
            rows = [r[0] for r in cur.fetchall()]
        if rows:
            return {"ok": True, "comune": rows[0]}
        _log_resolver(endpoint, regione, comune_raw, "not_found", [])
        return {"ok": False, "status": "not_found", "matches": []}

    comune_upper = comune_raw.upper()

    # Rule 2: code-like input
    if re.fullmatch(r"[A-Z]\d{3}", comune_upper) or re.fullmatch(r"\d{3,4}", comune_upper) or re.fullmatch(r"[A-Z0-9]{4}", comune_upper):
        prefix = f"{comune_upper}_%"
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT DISTINCT comune
                FROM {parcel_table}
                WHERE regione = %s
                  AND UPPER(comune) LIKE %s
                ORDER BY comune;
                """,
                (regione, prefix),
            )
            rows = [r[0] for r in cur.fetchall()]
        if len(rows) == 1:
            return {"ok": True, "comune": rows[0]}
        if len(rows) > 1:
            _log_resolver(endpoint, regione, comune_raw, "ambiguous", rows)
            return {"ok": False, "status": "ambiguous", "matches": rows}
        _log_resolver(endpoint, regione, comune_raw, "not_found", [])
        return {"ok": False, "status": "not_found", "matches": []}

    # Rule 3: name-only compare after prefix
    norm_name = _normalize_name(comune_raw)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH c AS (
                SELECT DISTINCT comune
                FROM {parcel_table}
                WHERE regione = %s
            )
            SELECT comune
            FROM c
            WHERE regexp_replace(
                    replace(
                        replace(
                            upper(
                                CASE
                                    WHEN position('_' in comune) > 0 THEN split_part(comune, '_', 2)
                                    ELSE comune
                                END
                            ),
                            '_', ' '
                        ),
                        '-', ' '
                    ),
                    '\\s+', ' ', 'g'
                ) = %s
            ORDER BY comune;
            """,
            (regione, norm_name),
        )
        rows = [r[0] for r in cur.fetchall()]

    if len(rows) == 1:
        return {"ok": True, "comune": rows[0]}
    if len(rows) > 1:
        _log_resolver(endpoint, regione, comune_raw, "ambiguous", rows)
        return {"ok": False, "status": "ambiguous", "matches": rows}
    _log_resolver(endpoint, regione, comune_raw, "not_found", [])
    return {"ok": False, "status": "not_found", "matches": []}
