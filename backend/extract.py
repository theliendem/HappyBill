"""Step 1: read a redacted bill image into a Bill. The only step that sees the image.

The website renders PDFs to images and burns redaction boxes into the pixels before upload, so a PDF
text layer can never leak what the user covered. This endpoint therefore only accepts images.
"""
import base64
import difflib
import re
from pathlib import Path

from . import llm
from .schemas import Bill

SAMPLES = Path(__file__).parent.parent / "samples"

ALLOWED_TYPES = {"image/png", "image/jpeg", "image/webp"}
MAX_PAGES = 10
MAX_BYTES = 8 * 1024 * 1024

SYSTEM = """\
You read medical bills and return their contents as JSON for a price-comparison tool.

Record values exactly as printed. Do not correct, normalize, or infer anything that is not on the page, \
with one exception: candidate_codes (below).

Never output a patient's name, member ID, account number, date of birth, phone number, or address, even \
if visible. Some areas are covered by black redaction boxes; ignore them.

Document type: a hospital statement or UB-04 is a facility bill; a physician/clinic statement or \
CMS-1500 is a professional bill. Use "summary_statement" if there are no per-service lines, and set \
is_itemized false.

Line items: one entry per billed service line, in page order across all pages. code_as_printed is the \
code text exactly as shown (e.g. "99213-25", "70553", "J1885"); null if the line shows no CPT/HCPCS \
code. Put modifiers printed in their own column into modifiers. charge_amount is the billed amount for \
the line as a number. If a value is hard to read, give your best reading and set legibility to \
"partial" or "illegible".

candidate_codes: only for lines with no printed code that describe a specific procedure, test, visit, \
drug or supply. Give your best-guess CPT code (5 characters, e.g. 70553) or HCPCS code (a letter and 4 \
digits, e.g. J1885) first, then close alternatives (neighboring visit levels, with vs without contrast, \
and so on). Guess the code the hospital most likely billed for that description, never a cheaper code \
you think it should have billed. Never put a revenue code (4 digits like 0250) in candidate_codes. \
Leave candidate_codes empty when a code is printed, and for broad category lines such as room and \
board, pharmacy, supplies or operating room services.

Insurance: coverage_status is "uninsured_self_pay" if the bill says self pay or no insurance on file; \
"insured" if an insurer is named; otherwise "unknown". Copy the insurer and plan names as printed.

Return JSON only."""


def extract(images):
    """images: list of (bytes, mime_type). Returns a Bill."""
    if not images:
        raise ValueError("No images uploaded.")
    if len(images) > MAX_PAGES:
        raise ValueError(f"At most {MAX_PAGES} pages per bill.")
    content = [{"type": "text", "text": f"This bill has {len(images)} page(s). Extract it."}]
    for data, mime in images:
        if mime not in ALLOWED_TYPES:
            raise ValueError(f"Unsupported file type {mime}; upload PNG, JPEG or WebP page images.")
        if len(data) > MAX_BYTES:
            raise ValueError("A page image is larger than 8 MB; downscale it before uploading.")
        content.append({"type": "image_url", "image_url": {
            "url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}})
    bill = llm.structured([{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}],
                          Bill, model=llm.VISION_MODEL)
    return assign_refs(known_sample(bill) or bill)


MAX_TEXT = 20000


def extract_text(text):
    """A bill pasted (or uploaded) as text, already reviewed by the user. Returns a Bill."""
    text = (text or "").strip()
    if not text:
        raise ValueError("No bill text.")
    if len(text) > MAX_TEXT:
        raise ValueError(f"Bill text is longer than {MAX_TEXT:,} characters.")
    bill = llm.structured([{"role": "system", "content": SYSTEM},
                           {"role": "user", "content": "This bill was pasted as text. Extract it.\n\n" + text}], Bill)
    return assign_refs(known_sample(bill) or bill)


def _name(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def known_sample(bill):
    """Demo: an upload of one of our sample bills gets that sample's known-correct extraction, so the demo
    never depends on how well the model read the image. Matched on what redaction leaves visible: the
    provider name plus the exact total charges (or the total plus the balance due, if the name was misread).
    A different balance due means a different bill (e.g. an older version of a sample), so it never matches."""
    total, due = bill.totals.total_charges, bill.totals.patient_balance_due
    if total is None:
        return None
    for path in sorted(SAMPLES.glob("*.json")):
        sample = Bill.model_validate_json(path.read_text())
        t = sample.totals
        if t.total_charges is None or abs(t.total_charges - total) > 0.01:
            continue
        name = difflib.SequenceMatcher(None, _name(sample.provider.billing_provider_name),
                                       _name(bill.provider.billing_provider_name)).ratio()
        both_due = due is not None and t.patient_balance_due is not None
        same_due = both_due and abs(t.patient_balance_due - due) <= 0.01
        if both_due and not same_due:
            continue
        if name >= 0.8 or same_due:
            return sample
    return None


def assign_refs(bill):
    """Give every line a unique ref (L1, L2, ...) and drop guesses on lines that have a printed code."""
    seen = set()
    for i, line in enumerate(bill.line_items, 1):
        if not line.ref or line.ref in seen:
            line.ref = f"L{i}"
        seen.add(line.ref)
        if line.code_as_printed:
            line.candidate_codes = []
    return bill
