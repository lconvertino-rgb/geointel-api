from fastapi import FastAPI
import os

app = FastAPI(title="GeoIntel API", version="1.0.0")

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

@app.get("/")
def root():
    return {"service": "geointel-api", "domain": os.getenv("DOMAIN", "")}
