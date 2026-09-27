"""Generate made-up test bills (PNG) plus their correct extraction (JSON).

    .venv/bin/python samples/make_samples.py

Charges are real UW Health list prices so the bills flow through the database like real ones. The
patient details are fictitious and exist so the website's redaction step has something to cover.
"""
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent
FONT = "/System/Library/Fonts/Menlo.ttc"
PATIENT = {"name": "JANE Q. SAMPLE", "account": "SAMPLE-004417", "dob": "01/01/1990",
           "address": "123 EXAMPLE ST, MADISON WI 53700"}

# (revenue_code, code_as_printed, description, units, charge)
BILLS = {
    "er_uninsured": {
        "title": "UW Health University Hospital", "subtitle": "600 Highland Ave, Madison WI 53705",
        "npi": "1922043744", "kind": "ITEMIZED HOSPITAL STATEMENT", "date": "08/14/2026",
        "insurance": "SELF PAY - NO INSURANCE ON FILE", "setting": "emergency",
        "lines": [("0450", "99284", "HB-ED LEVEL 4 VISIT", 1, 2373.00),
                  ("0730", "93005", "HB-EKG ROUTINE TRACING ONLY >=12 LEADS", 1, 323.00),
                  ("0300", "85025", "HB-HEM. SURV. W/AUTO DIFF&PLT", 1, 135.00),
                  ("0300", "85025", "HB-HEM. SURV. W/AUTO DIFF&PLT", 1, 135.00),
                  ("0300", "80053", "HB-COMPREHENSIVE METAB PANEL", 1, 379.00),
                  ("0300", "36415", "HB-VENIPUNCTURE", 1, 38.95),
                  ("0324", "71046", "HB-X-RAY EXAM CHEST 2 VIEWS", 1, 489.00),
                  ("0260", "96374", "HB-PRIMARY TPD INJ IV PUSH", 1, 331.00),
                  ("0636", "J2405", "ONDANSETRON HCL 4 MG/2ML INJ SOLN", 1, 117.01)],
        "insurance_paid": None, "adjustments": None,
    },
    "imaging_wps": {
        "title": "UW Health University Hospital", "subtitle": "600 Highland Ave, Madison WI 53705",
        "npi": "1922043744", "kind": "ITEMIZED HOSPITAL STATEMENT", "date": "07/22/2026",
        "insurance": "WPS HEALTH SOLUTIONS - STATEWIDE   CLAIM STATUS: PENDING", "setting": "outpatient",
        "payer": "WPS Health Solutions", "plan": "Statewide",
        "lines": [("0611", None, "MRI BRAIN W/WO CONTRAST", 1, 6798.00),
                  ("0352", "74177", "HB-CT ABD & PELVIS W/CONTRAST", 1, 6139.00),
                  ("0300", "36415", "HB-VENIPUNCTURE", 1, 38.95)],
        "candidates": {0: ["70553", "70552", "70551"]},
        "insurance_paid": None, "adjustments": None,
    },
    "knee_inpatient": {
        "title": "UW Health University Hospital", "subtitle": "600 Highland Ave, Madison WI 53705",
        "npi": "1922043744", "kind": "ITEMIZED HOSPITAL STATEMENT - INPATIENT", "date": "07/06/2026 - 07/08/2026",
        "insurance": "WPS HEALTH SOLUTIONS - STATEWIDE   MS-DRG 470", "setting": "inpatient", "drg": "470",
        "payer": "WPS Health Solutions", "plan": "Statewide",
        "lines": [("0120", None, "ROOM & BOARD SEMI-PRIVATE", 2, 9840.00),
                  ("0250", None, "PHARMACY", 1, 4122.35),
                  ("0272", None, "STERILE SUPPLIES", 1, 3877.41),
                  ("0278", None, "IMPLANTS", 1, 21450.00),
                  ("0360", None, "OPERATING ROOM SERVICES", 1, 18940.00),
                  ("0370", None, "ANESTHESIA", 1, 4318.00),
                  ("0420", None, "PHYSICAL THERAPY", 1, 2129.00),
                  ("0710", None, "RECOVERY ROOM", 1, 4500.00)],
        "insurance_paid": 30445.76, "adjustments": 0.00,
    },
    "clinic_mismatch": {
        "title": "UW Health Physicians", "subtitle": "UW Medical Foundation - Madison WI",
        "npi": None, "kind": "PHYSICIAN STATEMENT", "date": "09/02/2026",
        "insurance": "WPS HEALTH SOLUTIONS - STATEWIDE   APPLIED TO DEDUCTIBLE", "setting": "outpatient",
        "payer": "WPS Health Solutions", "plan": "Statewide",
        "lines": [(None, "99214", "OFFICE/OUTPATIENT VISIT EST LVL 3", 1, 312.00),
                  (None, "36415", "VENIPUNCTURE", 1, 25.00)],
        "insurance_paid": 0.00, "adjustments": 0.00,
    },
}


def ground_truth(key, b):
    total = round(sum(l[4] for l in b["lines"]), 2)
    paid, adj = b.get("insurance_paid"), b.get("adjustments")
    lines = []
    for i, (rc, code, desc, units, amount) in enumerate(b["lines"]):
        lines.append({
            "ref": f"L{i + 1}", "revenue_code": rc, "code_as_printed": code,
            "code_type_as_printed": None, "modifiers": [], "description_as_printed": desc,
            "units": units, "charge_amount": amount,
            "candidate_codes": [{"code": c, "code_type": "CPT"} for c in b.get("candidates", {}).get(i, [])],
        })
    physician = b["kind"].startswith("PHYSICIAN")
    return {
        "document": {"document_type": "physician_statement" if physician else "itemized_hospital_statement",
                     "is_itemized": True},
        "provider": {"billing_provider_name": b["title"], "billing_npi": b["npi"]},
        "insurance": {"coverage_status": "insured" if b.get("payer") else "uninsured_self_pay",
                      "payer_name": b.get("payer"), "plan_name": b.get("plan")},
        "encounter": {"setting": b["setting"], "drg_code": b.get("drg")},
        "line_items": lines,
        "totals": {"total_charges": total, "insurance_payments": paid, "adjustments": adj,
                   "patient_balance_due": round(total - (paid or 0) - (adj or 0), 2)},
    }


def render(key, b, truth):
    W, H = 1700, 2200
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    big, mid, small = (ImageFont.truetype(FONT, s) for s in (46, 30, 26))
    y = 60
    d.text((80, y), "SAMPLE BILL - FOR TESTING ONLY - NOT A REAL PATIENT", fill=(200, 0, 0), font=small); y += 60
    d.text((80, y), b["title"], fill="black", font=big); y += 60
    d.text((80, y), b["subtitle"], fill="black", font=small); y += 40
    if b["npi"]:
        d.text((80, y), f"Billing NPI: {b['npi']}", fill="black", font=small); y += 40
    d.text((80, y), b["kind"], fill="black", font=mid); y += 70
    for label, value in (("Patient", PATIENT["name"]), ("Account #", PATIENT["account"]),
                         ("Date of birth", PATIENT["dob"]), ("Address", PATIENT["address"]),
                         ("Service date(s)", b["date"]), ("Insurance", b["insurance"])):
        d.text((80, y), f"{label + ':':<17}{value}", fill="black", font=small); y += 40
    y += 30
    d.line((80, y, W - 80, y), fill="black", width=2); y += 20
    cols = (80, 330, 470, 650, 1340, 1400)
    for x, h in zip(cols, ("DATE", "REV", "CODE", "DESCRIPTION", "QTY", "   CHARGES")):
        d.text((x, y), h, fill="black", font=small)
    y += 45
    date = b["date"].split(" ")[0]
    for rc, code, desc, units, amount in b["lines"]:
        for x, v in zip(cols, (date, rc or "", code or "", desc[:36], str(units), f"{amount:>11,.2f}")):
            d.text((x, y), v, fill="black", font=small)
        y += 42
    y += 20
    d.line((80, y, W - 80, y), fill="black", width=2); y += 30
    t = truth["totals"]
    for label, v in (("TOTAL CHARGES", t["total_charges"]), ("INSURANCE PAYMENTS", t["insurance_payments"]),
                     ("ADJUSTMENTS", t["adjustments"]), ("PATIENT BALANCE DUE", t["patient_balance_due"])):
        if v is not None:
            d.text((900, y), f"{label:<22}{v:>12,.2f}", fill="black", font=small); y += 42
    img.save(OUT / f"{key}.png", optimize=True)


if __name__ == "__main__":
    for key, b in BILLS.items():
        truth = ground_truth(key, b)
        render(key, b, truth)
        (OUT / f"{key}.json").write_text(json.dumps(truth, indent=2) + "\n")
        print("wrote", key)
