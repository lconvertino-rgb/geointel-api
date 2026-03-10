import os

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.responses import PlainTextResponse

from catasto_router import router as catasto_router
from debug_router import router as debug_router
from lookup_router import router as lookup_router
from parcels_router import router as parcels_router
from wms_proxy_router import router as wms_proxy_router

app = FastAPI(title="GeoIntelGIS API", version="1.0")

app.include_router(lookup_router)
app.include_router(parcels_router)
app.include_router(debug_router)
app.include_router(catasto_router)
app.include_router(wms_proxy_router)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/api/parcels/") or request.url.path.startswith("/api/lookup/"):
        errors = []
        for err in exc.errors():
            loc = [str(p) for p in err.get("loc", []) if p != "body"]
            field = ".".join(loc) if loc else "payload"
            errors.append({"field": field, "message": err.get("msg", "Invalid value")})
        return JSONResponse(
            status_code=422,
            content={
                "status": "invalid_input",
                "message": "Invalid request payload or query parameters.",
                "errors": errors,
            },
        )
    return JSONResponse(status_code=422, content={"status": "invalid_input", "errors": exc.errors()})


@app.get("/api/healthz", response_class=PlainTextResponse)
def healthz():
    return "OK"


@app.get("/api/health/contract")
def health_contract():
    image_ref = os.getenv("PROD_API_IMAGE", "")
    build_tag = os.getenv("BUILD_TAG")
    if not build_tag and image_ref and ":" in image_ref:
        build_tag = image_ref.rsplit(":", 1)[-1]

    contract_statuses = [
        "ok",
        "not_found_db",
        "ambiguous_comune",
        "invalid_input",
        "internal_error",
    ]
    return {
        "status": "ok",
        "service": os.getenv("SERVICE_NAME", "geointelgis-api"),
        "version": app.version,
        "build_tag": build_tag or "unknown",
        "parcel_table": os.getenv("PARCEL_TABLE", "public.cadastre_parcel_test"),
        "feature_flags": {
            "comune_input_resolver": True,
            "foglio_scaling": True,
            "batch_search": True,
            "csv_upload_report": True,
            "foglio_extent": True,
            "catasto_layers": True,
            "bbox_fetch": True,
            "labels": True,
        },
        "env_presence": {
            "DATABASE_URL": bool(os.getenv("DATABASE_URL")),
            "PARCEL_TABLE": bool(os.getenv("PARCEL_TABLE")),
            "BUILD_TAG": bool(os.getenv("BUILD_TAG")),
            "SERVICE_NAME": bool(os.getenv("SERVICE_NAME")),
            "SUPABASE_DB_HOST": bool(os.getenv("SUPABASE_DB_HOST")),
            "SUPABASE_DB_USER": bool(os.getenv("SUPABASE_DB_USER")),
            "SUPABASE_DB_PASSWORD": bool(os.getenv("SUPABASE_DB_PASSWORD")),
            "SUPABASE_DB_NAME": bool(os.getenv("SUPABASE_DB_NAME")),
        },
        "contract_statuses": contract_statuses,
    }
