# ClarityBill front end: build spec

Give this file to Claude Code to build the website. The backend is done and running at
`http://localhost:8000` (interactive API docs at `/docs`). Build the front end as a separate app in
`frontend/` (suggested: React + Vite + TypeScript, dev server on `http://localhost:5173`; the backend
already allows CORS from any localhost port). Read the API base URL from `VITE_API_URL`, default
`http://localhost:8000`.

## What the site does

A patient uploads a medical bill (or types it in), we match each line to real published prices
(the hospital's and insurer's own federally required price files), and we show how much they may be
overcharged plus a ready-to-use negotiation script.

## Flow

```
Start ──► Upload ──► Redact ──► (extract) ──► Review ──► (plan) ──► Results
   └──────► Manual entry ────────────────────────┘
```

### 1. Start
- Two buttons: **Upload a bill** and **Enter it manually**.
- A small "Try a sample bill" link: list names from `GET /api/samples`; picking one loads
  `GET /api/samples/{name}.png` into the Upload/Redact flow as if the user uploaded it. For manual-entry
  testing, `GET /api/samples/{name}.json` returns the correct extraction to prefill the Review screen.

### 2. Upload
- Accept PDF, PNG, JPEG, WebP; multiple files; phone camera (`<input accept="image/*,application/pdf" capture>`).
- **PDF:** render every page to a `<canvas>` in the browser with pdf.js (target ~1700px wide).
  **Never upload the PDF itself.**
- **Photos:** draw to a canvas, downscale so the longest side is at most 2000px.
- Max 10 pages per bill.

### 3. Redact (privacy-critical, see rules below)
- Show each page canvas. The user drags to draw black rectangles; undo, clear, and page navigation.
- Checklist beside the page:
  - **Cover:** patient name, address, date of birth, account/MRN/member/guarantor numbers, phone.
  - **Leave visible:** hospital/clinic name and NPI, insurer and plan name, billing codes, descriptions,
    units, amounts, totals, whether insurance paid.
- Required checkbox: "I've covered my personal details" before continuing.
- On continue: paint the rectangles into the canvas pixels (`fillRect`, solid black), then export with
  `canvas.toBlob(cb, 'image/png')`. Upload only these blobs.

### 4. Extract
- `POST /api/extract`, `multipart/form-data`, one `files` field per page, in page order.
- Takes ~3-10 s (longer if the free model is busy and falls back): show progress ("Reading your bill...").
- Response: a `Bill` (below). Go to Review.
- Errors: `503` (model unavailable) or `502` (unreadable): say so and offer **Enter manually** (keep any
  pages the user redacted so they can read from them). `400`: show `detail`.

### 5. Review (also the manual-entry screen)
- Editable form of the `Bill`:
  - Bill type (hospital bill vs doctor/clinic bill -> `document.document_type` =
    `itemized_hospital_statement` / `physician_statement`), itemized yes/no.
  - Provider name, NPI (optional), tax ID (optional).
  - Coverage: insured / no insurance (self-pay) / not sure; insurer name; plan name (e.g. "Statewide").
  - Setting (inpatient/outpatient/emergency), DRG code (inpatient only, optional).
  - Lines table: code (`code_as_printed`), description, units, charge, insurance paid, you owe
    (`patient_responsibility`). Add/delete rows. Highlight rows where `legibility` is not `clear`.
  - Totals: total charges, insurance payments, adjustments, balance due.
- Keep fields the form doesn't show (e.g. `candidate_codes`, `ref`) when sending the bill back.
- Manual entry starts from an empty `Bill` (every section is optional except each line's
  `description_as_printed` and `charge_amount`).

### 6. No questions to the user

The backend never asks the user anything. Unclear items (coverage, bill type, hospital, insurer, plan,
unclear codes) are decided automatically: rules first (e.g. a billed amount equal to a code's list price),
then the model choosing only among database candidates. Each decision comes back in
`analysis.assumptions` as `{kind, ref, question, about, chose, reason, by: "rule" | "model"}`; show them
on the Results screen under "What we assumed".

### 7. Results

`POST /api/plan` with `{ "bill": Bill }` returns `{ display, analysis, tactics, plan, generated_by, note }`.
**Render the Results screen from `display` only**: everything is already worded, cleaned up and formatted
for the demo. Don't recompute or reword numbers. `analysis`/`plan` are there for debugging.

| `display` field | Render as |
|---|---|
| `hero.headline` | Big headline |
| `hero.savings` + `hero.savings_label` | The hero number (e.g. "$1,728.38 Potential savings") |
| `hero.billed`, `hero.fair`, `hero.fair_label` | "Billed $4,320.96" vs "Hospital's own cash price $2,592.58": two bars or two big numbers |
| `hero.comparison` | Sub-line, e.g. "You were billed 67% more than the hospital's own cash price." |
| `provider`, `insurance` | Small context line under the hero |
| `summary` | Short paragraph |
| `findings[]` (`title`, `detail`, `action`) | "What we found" cards |
| `lines[]` | "Your bill, line by line": `name` (bold), `billed_as` + `code` (small, "On your bill: ..."), `billed`, `fair_price` with `fair_label`, `savings`; `badges[]` as pills (`tone`: `alert` red, `warning` amber, `neutral` gray, `muted` light gray); `note` in italics; `price_range.text` as a small caption (a range bar from `min` to `max` with the billed amount marked looks great) |
| `included_in_stay` (may be null) | Collapsible list under the stay line: `title`, `items[]` (`name`, `billed`) |
| `call_script[]` | "What to say when you call": one paragraph per item, with a Copy button |
| `letter.subject`, `letter.body[]` | "Or send this message": subject line + paragraphs (body items may contain `\n`), Copy button |
| `steps[]` | Numbered checklist |
| `assumptions[]` (may be empty) | Small "What we assumed" list; hide the section when empty |
| `sources[]`, `disclaimer` | Footer |

Format money as `$1,423.80`. Hide any field that is null.

## Privacy rules (must follow)

1. No original file ever leaves the browser: only the re-encoded, redacted PNG page images. PDFs are
   rasterized first (a PDF's text layer would keep text hidden under a drawn box).
2. Redaction is burned into pixels before export; re-encoding via canvas also strips photo metadata
   (EXIF/GPS).
3. Don't persist bills: no localStorage/IndexedDB/cookies for bill data or images; keep them in memory
   only. Reloading the page may lose the session; that's intended.
4. No analytics or third-party scripts on these screens.
5. The model provider may retain uploads (the free Gemini tier does and may use them to improve its
   products), so the redaction step is required, not optional.

## Data shapes

The authoritative schemas are at `http://localhost:8000/docs` (and `ai/bill_extraction.schema.json`
for `Bill`). Summary:

```ts
type Bill = {
  document:  { document_type: "itemized_hospital_statement" | "physician_statement" | "ub04_facility_claim" |
               "cms1500_professional_claim" | "summary_statement" | "eob" | "other"; is_itemized: boolean; statement_date?: string | null };
  provider:  { billing_provider_name?: string | null; facility_name?: string | null; billing_npi?: string | null; tin?: string | null };
  insurance: { coverage_status: "insured" | "uninsured_self_pay" | "unknown"; payer_name?: string | null; plan_name?: string | null;
               plan_type: "commercial" | "medicare_advantage" | "medicaid" | "medicare" | "other" | "unknown";
               network_status: "in_network" | "out_of_network" | "unknown" };
  encounter: { setting: "inpatient" | "outpatient" | "emergency" | "unknown"; admission_date?: string | null; discharge_date?: string | null; drg_code?: string | null };
  line_items: BillLine[];
  totals: { total_charges?: number | null; insurance_payments?: number | null; adjustments?: number | null; patient_balance_due?: number | null };
};
type BillLine = {
  ref?: string | null; service_date?: string | null; revenue_code?: string | null; code_as_printed?: string | null;
  code_type_as_printed?: "CPT" | "HCPCS" | "MS-DRG" | "APR-DRG" | "NDC" | "CDM" | null; modifiers: string[];
  description_as_printed: string; units: number; charge_amount: number;
  insurance_paid?: number | null; adjustment?: number | null; patient_responsibility?: number | null;
  legibility: "clear" | "partial" | "illegible"; candidate_codes: { code: string; code_type?: string | null }[];
};
// Optional and not needed by the website: /api/plan accepts {bill, choices} to override automatic decisions.
type Choices = {
  coverage_status?: "insured" | "uninsured_self_pay"; billing_class?: "institutional" | "professional";
  org_key?: string; provider_tin?: string; payer_key?: string; plan_key?: string;   // "none" = not listed / not sure
  lines?: Record<string, { skip?: boolean; code?: string; code_type?: string; modifiers?: string[]; hospital_item_id?: number | null }>;
};
```

## Testing

- Backend: `.venv/bin/uvicorn backend.api:app --port 8000` from the repo root (Postgres must be running).
- The four sample bills cover the main cases: `er_uninsured` (self-pay, duplicate lab charge),
  `imaging_wps` (insured, a line with no printed code), `knee_inpatient` (inpatient stay billed far
  above the plan rate), `clinic_mismatch` (doctor's bill; visit described as level 3 but billed as level 4).
- Without a model key, `/api/extract` returns 503: test the upload path with a key set, and everything
  else via "Try a sample bill" + manual entry.
