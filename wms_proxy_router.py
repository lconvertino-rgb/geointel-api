import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

router = APIRouter(prefix="/api", tags=["wms-proxy"])

# Fixed upstream allowlist for this pass to avoid open-proxy behavior.
ALLOWED_UPSTREAMS = {
    "puglia_aree_protette": "http://webapps.sit.puglia.it/arcgis/services/Operationals/AreeProtetteReteNatura2000/MapServer/WMSServer"
}

DEFAULT_UPSTREAM = "puglia_aree_protette"
FORWARD_TIMEOUT = httpx.Timeout(timeout=30.0, connect=10.0)


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

    try:
        async with httpx.AsyncClient(timeout=FORWARD_TIMEOUT, follow_redirects=True) as client:
            upstream_request = client.build_request("GET", base_url, params=params)
            upstream_response = await client.send(upstream_request, stream=True)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail=f"WMS upstream timeout: {exc}") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"WMS upstream request failed: {exc}") from exc

    content_type = upstream_response.headers.get("content-type", "application/octet-stream")
    passthrough_headers = {}
    for header_name in ("cache-control", "expires", "content-encoding", "vary"):
        value = upstream_response.headers.get(header_name)
        if value:
            passthrough_headers[header_name] = value

    return StreamingResponse(
        upstream_response.aiter_raw(),
        status_code=upstream_response.status_code,
        media_type=content_type,
        headers=passthrough_headers,
        background=BackgroundTask(upstream_response.aclose),
    )
