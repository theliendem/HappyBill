"""The website's backend.

    .venv/bin/uvicorn backend.api:app --port 8000      (docs at http://localhost:8000/docs)

Also serves the website (Frontend/) at http://localhost:8000/.

Flow: POST /api/extract (bill images -> Bill), POST /api/extract-text (pasted text -> Bill) or manual
entry -> POST /api/plan (analysis + script in
one call; open questions are answered automatically and listed as `analysis.assumptions`).
POST /api/analyze returns just the analysis (add ?interactive=true to get the open questions instead of
automatic answers). Nothing is stored; bill images and contents are never written to disk or logs.
"""
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mediator import db
from mediator.api import app as mediator_app

from . import extract as extract_step
from . import llm
from .analyze import BadChoice, analyze
from .autoresolve import auto_analyze
from .present import build_display
from .schemas import AnalyzeRequest, Bill
from .script import write_plan

SAMPLES = Path(__file__).parent.parent / "samples"
FRONTEND = Path(__file__).parent.parent / "Frontend"

app = FastAPI(title="ClarityBill API", version="0.1")
app.add_middleware(CORSMiddleware, allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
                   allow_methods=["*"], allow_headers=["*"])
app.mount("/mediator", mediator_app)


@app.get("/api/health")
def health():
    try:
        database = db.one("SELECT count(*) AS n FROM sources")["n"] > 0
    except Exception:
        database = False
    return {"database": database, "model": {"configured": llm.configured(), "model": llm.MODEL,
                                            "vision_model": llm.VISION_MODEL, "base_url": llm.BASE_URL}}


@app.post("/api/extract", response_model=Bill)
async def extract(files: list[UploadFile] = File(..., description="Redacted page images (PNG/JPEG/WebP), in page order.")):
    images = [(await f.read(), f.content_type) for f in files]
    try:
        return extract_step.extract(images)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except llm.LLMUnavailable as e:
        raise HTTPException(503, {"error": "model_unavailable", "detail": str(e),
                                  "hint": "Enter the bill details manually instead."})
    except llm.LLMBadOutput:
        raise HTTPException(502, {"error": "unreadable", "hint": "Try a clearer photo, or enter the details manually."})


class BillText(BaseModel):
    text: str


@app.post("/api/extract-text", response_model=Bill)
def extract_text(req: BillText):
    """A bill as text the user reviewed (pasted, or a .txt/.csv file)."""
    try:
        return extract_step.extract_text(req.text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except llm.LLMUnavailable as e:
        raise HTTPException(503, {"error": "model_unavailable", "detail": str(e),
                                  "hint": "Enter the bill details manually instead."})
    except llm.LLMBadOutput:
        raise HTTPException(502, {"error": "unreadable", "hint": "Enter the details manually."})


def _analyze(req, interactive=False):
    try:
        return analyze(req.bill, req.choices) if interactive else auto_analyze(req.bill, req.choices)
    except BadChoice as e:
        raise HTTPException(422, {"error": "bad_choice", "detail": str(e)})


@app.post("/api/analyze")
def analyze_endpoint(req: AnalyzeRequest, interactive: bool = False):
    return _analyze(req, interactive)


@app.post("/api/plan")
def plan_endpoint(req: AnalyzeRequest):
    """Runs the analysis server-side (so every number comes from the database), answering any open
    questions automatically, then writes the plan."""
    analysis = _analyze(req)
    plan, tacts, generated_by, note = write_plan(analysis, req.bill)
    return {"display": build_display(analysis, tacts, plan, req.bill), "analysis": analysis, "tactics": tacts,
            "plan": plan.model_dump(), "generated_by": generated_by, "note": note}


@app.get("/api/sources")
def sources():
    """Price sources loaded in Postgres and the last Databricks sync (lakehouse/sync.py)."""
    rows = db.query("SELECT source_id, source_type, publisher, loaded_at FROM sources ORDER BY source_id")
    try:
        last = db.one("SELECT synced_at, remote FROM sync_log ORDER BY synced_at DESC LIMIT 1")
    except Exception:
        last = None
    return {"sources": rows, "last_sync": last}


@app.get("/test", include_in_schema=False)
def test_page():
    """Bare-bones page for trying the flow before the real front end exists."""
    return FileResponse(Path(__file__).parent / "test_page.html", media_type="text/html")


@app.get("/api/samples")
def samples():
    """Made-up test bills for development and the demo."""
    return sorted(p.stem for p in SAMPLES.glob("*.png"))


@app.get("/api/samples/{name}.png")
def sample_image(name: str):
    path = SAMPLES / f"{name}.png"
    if not path.is_file() or path.parent != SAMPLES:
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png")


@app.get("/api/samples/{name}.json", response_model=Bill)
def sample_bill(name: str):
    """The correct extraction for a sample: use it to test manual entry and the rest of the flow."""
    path = SAMPLES / f"{name}.json"
    if not path.is_file() or path.parent != SAMPLES:
        raise HTTPException(404)
    return Bill.model_validate_json(path.read_text())


# The website. Mounted last so every /api, /mediator, /docs and /test route above takes precedence.
app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="site")
