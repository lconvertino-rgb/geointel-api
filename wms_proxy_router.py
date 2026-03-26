import asyncio

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from starlette.responses import Response

router = APIRouter(prefix="/api", tags=["wms-proxy"])

# Fixed upstream allowlist for this pass to avoid open-proxy behavior.
ALLOWED_UPSTREAMS = {
    "puglia_ctr-2008": "http://webapps.sit.puglia.it/arcgis/services/Background/CTR2008/MapServer/WMSServer",
    "puglia_confini-comunali": "http://webapps.sit.puglia.it/arcgis/services/Background/TNOInquadramento/MapServer/WMSServer",
    "puglia_ortofoto-2006": "http://webapps.sit.puglia.it/arcgis/services/BaseMaps/Ortofoto2006/ImageServer/WMSServer",
    "puglia_ortofoto-2010": "http://webapps.sit.puglia.it/arcgis/services/BaseMaps/Ortofoto2010/ImageServer/WMSServer",
    "puglia_ortofoto-2011": "http://webapps.sit.puglia.it/arcgis/services/BaseMaps/Ortofoto2011/ImageServer/WMSServer",
    "puglia_ortofoto-2013": "http://webapps.sit.puglia.it/arcgis/services/BaseMaps/Ortofoto2013/ImageServer/WMSServer",
    "puglia_ortofoto-2015": "http://webapps.sit.puglia.it/arcgis/services/BaseMaps/Ortofoto2015/ImageServer/WMSServer",
    "puglia_ortofoto-2016": "http://webapps.sit.puglia.it/arcgis/services/BaseMaps/Ortofoto2016/ImageServer/WMSServer",
    "puglia_ortofoto-2019": "http://webapps.sit.puglia.it/arcgis/services/BaseMaps/Ortofoto2019/ImageServer/WMSServer",
    "puglia_dtm-colori": "http://webapps.sit.puglia.it/arcgis/services/BaseMaps/DTMColori/ImageServer/WMSServer",
    "puglia_carta-ombreggiature": "http://webapps.sit.puglia.it/arcgis/services/BaseMaps/CartaOmbreggiature/ImageServer/WMSServer",
    "puglia_carta-pendenze": "http://webapps.sit.puglia.it/arcgis/services/BaseMaps/CartaPendenze/ImageServer/WMSServer",
    "puglia_carta-esposizioni": "http://webapps.sit.puglia.it/arcgis/services/BaseMaps/CartaEsposizioni/ImageServer/WMSServer",
    "puglia_putt-geomorfologico": "http://webapps.sit.puglia.it/arcgis/services/BaseMaps/PuttGeomorfologico/ImageServer/WMSServer",
    "puglia_putt-ate": "http://webapps.sit.puglia.it/arcgis/services/BaseMaps/PuttAte/ImageServer/WMSServer",
    "puglia_dbti-copertura-topologica": "http://webapps.sit.puglia.it/arcgis/services/Operationals/DBTICoperturaTopologica/MapServer/WMSServer",
    "puglia_dbti-viabilit": "http://webapps.sit.puglia.it/arcgis/services/Operationals/DBTIViabilita/MapServer/WMSServer",
    "puglia_uso-del-suolo-2006": "http://webapps.sit.puglia.it/arcgis/services/ServicesArcIMS/UDS2006/MapServer/WMSServer",
    "puglia_uso-del-suolo-2011": "http://webapps.sit.puglia.it/arcgis/services/ServicesArcIMS/UDS2011/MapServer/WMSServer",
    "puglia_idrogeomorfologia": "http://webapps.sit.puglia.it/arcgis/services/ServicesArcIMS/Idrogeomorfologia/MapServer/WMSServer",
    "puglia_batimetria": "http://webapps.sit.puglia.it/arcgis/services/ServicesArcIMS/batimetria/MapServer/WMSServer",
    "puglia_fer-aree-non-idonee": "http://webapps.sit.puglia.it/arcgis/services/Operationals/FERAreeNonIdonee/MapServer/WMSServer",
    "puglia_vincoli-delegati": "http://webapps.sit.puglia.it/arcgis/services/Operationals/VincoliDelegati/MapServer/WMSServer",
    "puglia_pptr-approvato": "http://webapps.sit.puglia.it/arcgis/services/Operationals/PPTR_APPROVATO/MapServer/WMSServer",
    "puglia_pptr-adottato": "http://webapps.sit.puglia.it/arcgis/services/Operationals/PPTR_ADOTTATO/MapServer/WMSServer",
    "puglia_aree_protette": "http://webapps.sit.puglia.it/arcgis/services/Operationals/AreeProtetteReteNatura2000/MapServer/WMSServer",
    "puglia_ulivi-monumentali": "http://webapps.sit.puglia.it/arcgis/services/Operationals/UliviMonumentali/MapServer/WMSServer",
    "puglia_catasto-grotte": "http://webapps.sit.puglia.it/arcgis/services/Operationals/CatastoGrotte/MapServer/WMSServer",
    "puglia_geositi": "http://webapps.sit.puglia.it/arcgis/services/Operationals/Geositi/MapServer/WMSServer",
    "puglia_sentieri-web": "http://webapps.sit.puglia.it/arcgis/services/Operationals/SentieriWEB/MapServer/WMSServer",
    "puglia_biomap": "http://webapps.sit.puglia.it/arcgis/services/Operationals/Biomap/MapServer/WMSServer",
    "puglia_catasto-manufatti": "http://webapps.sit.puglia.it/arcgis/services/Operationals/CatastoManufatti/MapServer/WMSServer",
    "puglia_area-vasta-sud-salento": "http://webapps.sit.puglia.it/arcgis/services/Operationals/AreaVastaSudSalento/MapServer/WMSServer",
    "puglia_pta-2019-vincoli": "http://webapps.sit.puglia.it/arcgis/services/Operationals2/PTA2019_Vincoli/MapServer/WMSServer",
    "puglia_distretti-irrigui": "http://webapps.sit.puglia.it/arcgis/services/Operationals2/DistrettiIrrigui/MapServer/WMSServer",
    "puglia_pai-frane": "http://wms.distrettoappenninomeridionale.it/geoserver/PAI_VIGENTE_PERICOLOSITA_FRANE/wms",
    "puglia_pai-idraulico": "http://wms.distrettoappenninomeridionale.it/geoserver/PAI_VIGENTE_IDRAULICO/wms",
    "puglia_pai-reticolo": "http://wms.distrettoappenninomeridionale.it/geoserver/RETICOLO/wms",
}

DEFAULT_UPSTREAM = "puglia_aree_protette"
FORWARD_TIMEOUT = httpx.Timeout(timeout=30.0, connect=10.0)
FORWARD_LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=80)
MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 0.25

HTTP_CLIENT = httpx.AsyncClient(
    timeout=FORWARD_TIMEOUT,
    follow_redirects=True,
    limits=FORWARD_LIMITS,
    # Many upstreams are HTTP/1.1-only and protocol downgrades can be flaky.
    http2=False,
)


@router.get("/wms-proxy")
async def wms_proxy(
    request: Request,
    upstream: str = Query(default=DEFAULT_UPSTREAM, description="Allowed upstream key"),
):
    base_url = ALLOWED_UPSTREAMS.get(upstream)
    if not base_url:
        raise HTTPException(status_code=400, detail="Invalid upstream key")

    params = []
    for key, value in request.query_params.multi_items():
        if key == "upstream":
            continue
        params.append((key, value))

    if not any(key.lower() == "request" for key, _ in params):
        raise HTTPException(status_code=422, detail="Missing WMS request parameter")

    query = request.query_params
    request_type = query.get("request", "").strip().upper()
    requested_format = query.get("format", "").strip().lower()
    is_getmap = request_type == "GETMAP"
    is_png_map = is_getmap and "image/png" in requested_format

    req_headers = {
        "Accept": "image/png,*/*;q=0.8" if is_png_map else "*/*",
        # Ask upstream for uncompressed payloads to avoid browser/proxy content-encoding edge cases.
        "Accept-Encoding": "identity",
        "Connection": "close",
    }

    upstream_response: httpx.Response | None = None
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            upstream_response = await HTTP_CLIENT.get(base_url, params=params, headers=req_headers)
            break
        except httpx.TimeoutException as exc:
            last_error = exc
            if attempt >= MAX_RETRIES:
                raise HTTPException(status_code=504, detail=f"WMS upstream timeout: {exc}") from exc
        except httpx.RequestError as exc:
            last_error = exc
            if attempt >= MAX_RETRIES:
                raise HTTPException(status_code=502, detail=f"WMS upstream request failed: {exc}") from exc

        await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))

    if upstream_response is None:
        raise HTTPException(status_code=502, detail=f"WMS upstream request failed: {last_error}")

    body = upstream_response.content
    content_type = upstream_response.headers.get("content-type", "application/octet-stream")
    passthrough_headers: dict[str, str] = {
        "content-length": str(len(body)),
        "x-content-type-options": "nosniff",
    }
    for header_name in ("cache-control", "expires", "vary", "etag", "last-modified"):
        value = upstream_response.headers.get(header_name)
        if value:
            passthrough_headers[header_name] = value

    return Response(
        content=body,
        status_code=upstream_response.status_code,
        media_type=content_type,
        headers=passthrough_headers,
    )
