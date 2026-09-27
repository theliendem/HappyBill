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

## Databricks lakehouse sync

Databricks is the system of record for price data; Postgres is the fast serving copy the app queries.

```
parsers/out/<source>/*.csv.gz ──push──► Databricks Volume + Delta tables (workspace.happybill.*)
                                               │
                                             sync (new sources only)
                                               ▼
                                      Postgres ──► API ──► site footer shows last sync
```

Add `DATABRICKS_HOST`, `DATABRICKS_TOKEN` and `DATABRICKS_WAREHOUSE_ID` to `.env`, then:

```bash
.venv/bin/python -m lakehouse.push     # upload parsed sources, build Delta tables
.venv/bin/python -m lakehouse.sync     # load any new Databricks sources into Postgres
```

`GET /api/sources` lists loaded sources and the last sync. The LLM can also run on Databricks Model
Serving (OpenAI-compatible): set `LLM_BASE_URL=https://<workspace>/serving-endpoints`,
`LLM_API_KEY=<token>` and `LLM_MODEL=<endpoint name>`.
