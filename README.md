# HappyBill

Upload a medical bill and find out whether you're being overcharged. HappyBill matches each line item
against real published prices (hospital and insurer price-transparency files) and generates a
negotiation script you can use.

## Layout

- `Frontend/` — static web UI (upload, redact, review, results)
- `backend/` — FastAPI app: bill extraction, analysis, negotiation script
- `mediator/` — price lookup and code resolution against the rates database
- `parsers/` — turn hospital/insurer machine-readable price files into CSVs
- `db/` — Postgres schema and loader
- `samples/` — sample bills for testing

## Running locally

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Create a `.env` with `LLM_API_KEY` (or `GEMINI_API_KEY`) and optionally `DATABASE_URL`
(defaults to `postgresql://postgres@localhost:5432/claritybill`). Load parsed price data with
`db/load.sh --fresh parsers/out/<source> ...`, then start the API:

```bash
.venv/bin/uvicorn backend.api:app --port 8000
```

API docs are at http://localhost:8000/docs. Open `Frontend/index.html` for the UI.

The raw source price files (multi-GB JSON/CSV) are not checked in.
