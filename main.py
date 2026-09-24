from fastapi import FastAPI

from app.api import build_router
from app.storage import JsonStore

store = JsonStore()
app = FastAPI(title="Health Data Consent Gateway")
app.include_router(build_router(store))


@app.get("/")
def read_root():
    return {"service": "health-data-consent-gateway", "status": "ok"}
