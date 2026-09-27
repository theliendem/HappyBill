"""HTTP API for the mediator (for the frontend and for testing).

    .venv/bin/uvicorn mediator.api:app --port 8000
    open http://localhost:8000/docs
"""
from fastapi import FastAPI, HTTPException

from . import db
from .models import RateRequest, ResolveRequest
from .rates import InvalidIds, get_negotiated_rate
from .resolve import reload, resolve
from .tools import TOOLS

app = FastAPI(title="ClarityBill mediator", version="0.1")


@app.get("/health")
def health():
    return {"ok": db.one("SELECT count(*) AS n FROM sources")["n"] > 0}


@app.post("/resolve")
def resolve_endpoint(req: ResolveRequest):
    return resolve(req.model_dump())


@app.post("/rates")
def rates_endpoint(req: RateRequest):
    try:
        return get_negotiated_rate(req.model_dump())
    except InvalidIds as e:
        raise HTTPException(status_code=422, detail={"error": "invalid_ids", "details": e.details})


@app.get("/tools")
def tools():
    """The Claude tool definitions, for the AI side to load."""
    return TOOLS


@app.post("/reload")
def reload_reference_data():
    """Clear cached names/aliases after loading new files or editing db/seed.sql."""
    reload()
    return {"ok": True}
