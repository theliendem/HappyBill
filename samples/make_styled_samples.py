"""Generate realistic-looking made-up bills (PNG) plus their correct extraction (JSON).

    .venv/bin/python samples/make_styled_samples.py

Each bill mixes a header style, logo, table style and summary style so the set looks varied, like bills
from different billing systems. Providers, patients, member IDs and account numbers are fictitious.
Charges are mostly UW Health list prices so lines match the database. Every bill shows total charges,
what insurance paid and what the patient owes. Every insured bill contains a common billing error (the
insurer's contract discount not applied), so the analysis always finds savings.
"""
import json
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent
W, M = 1700, 100
SUP = "/System/Library/Fonts/Supplemental/"
FONTS = {
    "helvetica": ("/System/Library/Fonts/Helvetica.ttc", 0, 1),
    "avenir": ("/System/Library/Fonts/Avenir Next.ttc", 0, 2),
    "georgia": (SUP + "Georgia.ttf", SUP + "Georgia Bold.ttf"),
    "verdana": (SUP + "Verdana.ttf", SUP + "Verdana Bold.ttf"),
    "trebuchet": (SUP + "Trebuchet MS.ttf", SUP + "Trebuchet MS Bold.ttf"),
    "arial": (SUP + "Arial.ttf", SUP + "Arial Bold.ttf"),
    "tahoma": (SUP + "Tahoma.ttf", SUP + "Tahoma Bold.ttf"),
    "times": (SUP + "Times New Roman.ttf", SUP + "Times New Roman Bold.ttf"),
    "courier": (SUP + "Courier New.ttf", SUP + "Courier New Bold.ttf"),
}
GRAY, LIGHT, INK = (110, 110, 110), (225, 225, 225), (25, 25, 25)


def font(family, size, bold=False):
    spec = FONTS[family]
    if isinstance(spec[1], int):  # .ttc collection: (path, regular index, bold index)
        return ImageFont.truetype(spec[0], size, index=spec[2] if bold else spec[1])
    return ImageFont.truetype(spec[1] if bold else spec[0], size)


def money(x):
    return f"${x:,.2f}"


def minus(x):
    return f"-{money(x)}" if x else money(x)


def tint(rgb, amount):
    return tuple(round(c + (255 - c) * amount) for c in rgb)


# ---------------- bills ----------------
# lines: (service_date, revenue_code, code, description, units, charge)

BILLS = [
    dict(key="urgent_care_strep", provider="Northside Urgent Care", kind="physician", setting="outpatient",
         address="2210 N Sherman Ave, Madison WI 53704", phone="(608) 555-0142", npi="1487265530",
         payer="Quartz Health Solutions", plan="Quartz One", member="QZ8841207", group="40221",
         patient=("MARIA L. TESTER", "4412 Birch Ln, Madison WI 53711", "03/14/1987"), account="NUC-2231087",
         statement="09/08/2026", due="10/03/2026",
         lines=[("08/21/2026", None, "99214", "Office visit, established, moderate", 1, 285.00),
                ("08/21/2026", None, "87880", "Strep A rapid antigen test", 1, 168.00),
                ("08/21/2026", None, "87804", "Influenza rapid antigen test", 1, 95.00),
                ("08/21/2026", None, "36415", "Venipuncture", 1, 38.95)],
         ratio=0.58, deductible=0, coins=0.2,
         style=dict(font="avenir", color=(0, 128, 128), header="sidebar", logo="cross", table="zebra",
                    summary="box", extras=["message"])),
    dict(key="colonoscopy_outpatient", provider="Lakeview Regional Medical Center", kind="hospital",
         setting="outpatient", address="1800 Lakeview Pkwy, Middleton WI 53562", phone="(608) 555-0190",
         npi="1639204471", payer="Anthem Blue Cross Blue Shield", plan="Blue Access PPO", member="XWB902114587",
         group="177530", patient=("ROBERT J. EXAMPLE", "88 Orchard Ct, Verona WI 53593", "11/02/1962"),
         account="LRMC-00482213", statement="08/30/2026", due="09/24/2026",
         lines=[("08/12/2026", "0750", "45380", "HB-COLONOSCOPY W/ BIOPSY", 1, 4740.00),
                ("08/12/2026", "0370", "00811", "ANESTHESIA LOWER GI ENDOSCOPY", 1, 1265.00),
                ("08/12/2026", "0310", "88305", "HB-GROSS AND MICRO LEVEL IV", 1, 501.00),
                ("08/12/2026", "0636", "J2250", "MIDAZOLAM HCL 2 MG/2ML INJ SOLN", 1, 165.48),
                ("08/12/2026", "0710", None, "RECOVERY ROOM PER HOUR", 2, 1180.00)],
         ratio=0.47, deductible=1500, coins=0.2,
         style=dict(font="helvetica", color=(22, 52, 110), header="band", logo="shield", table="grid",
                    summary="stacked", extras=["stub"])),
    dict(key="knee_mri", provider="Cedar Ridge Imaging Center", kind="hospital", setting="outpatient",
         address="455 Ridge Point Dr, Fitchburg WI 53719", phone="(608) 555-0117", npi=None,
         payer="UnitedHealthcare", plan="Choice Plus", member="991045623", group="701882",
         patient=("DANIEL P. PLACEHOLDER", "2019 Monroe St, Madison WI 53711", "06/30/1979"),
         account="CRI-7730915", statement="09/02/2026", due="09/30/2026",
         lines=[("08/19/2026", "0610", "73721", "MRI LOWER EXTREMITY JOINT W/O CONTRAST - RIGHT KNEE", 1, 4367.00)],
         ratio=0.36, deductible=2000, coins=0.3,
         style=dict(font="georgia", color=(92, 64, 51), header="centered", logo="mountain", table="lines",
                    summary="panel", extras=["message"])),
    dict(key="physical_therapy", provider="Riverbend Physical Therapy", kind="hospital", setting="outpatient",
         address="120 E Wilson St, Madison WI 53703", phone="(608) 555-0166", npi="1295837746",
         payer="WPS Health Solutions", plan="Statewide", member="WPS55812309", group="STW-0091",
         patient=("KEVIN A. SAMPLEMAN", "731 Willow Way, Sun Prairie WI 53590", "09/09/1991"),
         account="RPT-1182044", statement="09/15/2026", due="10/10/2026",
         lines=[("08/04/2026", "0424", "97161", "PT EVAL LOW COMPLEXITY", 1, 340.00),
                ("08/04/2026", "0420", "97110", "THERAPEUTIC EXERCISE EA 15 MIN", 2, 392.00),
                ("08/11/2026", "0420", "97110", "THERAPEUTIC EXERCISE EA 15 MIN", 2, 392.00),
                ("08/11/2026", "0420", "97140", "MANUAL THERAPY EA 15 MIN", 1, 196.00),
                ("08/18/2026", "0420", "97110", "THERAPEUTIC EXERCISE EA 15 MIN", 2, 392.00),
                ("08/18/2026", "0420", "97140", "MANUAL THERAPY EA 15 MIN", 1, 196.00),
                ("08/25/2026", "0420", "97110", "THERAPEUTIC EXERCISE EA 15 MIN", 2, 392.00)],
         ratio=0.52, deductible=0, coins=0.2, per_line=True,
         style=dict(font="trebuchet", color=(46, 125, 50), header="left_logo", logo="leaf", table="lines",
                    summary="stacked", extras=[])),
    dict(key="annual_physical_labs", provider="Maple Grove Family Medicine", kind="physician", setting="outpatient",
         address="6701 Mineral Point Rd, Madison WI 53705", phone="(608) 555-0133", npi="1750483362",
         payer="Aetna", plan="Open Access Managed Choice", member="W284739102", group="086512",
         patient=("SUSAN M. DOE-TEST", "15 Cardinal Ct, Waunakee WI 53597", "02/17/1974"),
         account="MGFM-338120", statement="08/28/2026", due="09/22/2026",
         lines=[("08/06/2026", None, "99396", "Preventive visit, est. patient, age 40-64", 1, 310.00),
                ("08/06/2026", None, "80053", "Comprehensive metabolic panel", 1, 379.00),
                ("08/06/2026", None, "85025", "CBC with automated differential", 1, 135.00),
                ("08/06/2026", None, "80061", "Lipid panel", 1, 286.00),
                ("08/06/2026", None, "83036", "Hemoglobin A1c", 1, 108.00),
                ("08/06/2026", None, "84443", "Thyroid stimulating hormone (TSH)", 1, 214.00),
                ("08/06/2026", None, "36415", "Routine venipuncture", 1, 38.95)],
         ratio=0.12, deductible=400, coins=0.1, per_line=True, preventive={"99396"},
         style=dict(font="helvetica", color=(94, 53, 177), header="left_logo", logo="circle", table="zebra",
                    summary="callout", extras=["message"])),
    dict(key="er_head_ct", provider="St. Brigid Memorial Hospital", kind="hospital", setting="emergency",
         address="900 Cathedral Sq, Janesville WI 53545", phone="(608) 555-0101", npi="1043378215",
         payer="Blue Cross Blue Shield", plan="BluePreferred PPO", member="BCW772019334", group="12004",
         patient=("THOMAS R. FICTION", "310 Oak St, Janesville WI 53548", "12/05/1968"),
         account="SBMH-9920417", statement="09/10/2026", due="10/05/2026",
         lines=[("08/27/2026", "0450", "99285", "HB-ED LEVEL 5 VISIT", 1, 3548.00),
                ("08/27/2026", "0351", "70450", "HB-CT HEAD WO IV CONTRAST", 1, 2213.00),
                ("08/27/2026", "0260", "96374", "HB-PRIMARY TPD INJ IV PUSH", 1, 331.00),
                ("08/27/2026", "0636", "J1885", "KETOROLAC TROMETHAMINE 30 MG/ML INJ", 1, 130.84),
                ("08/27/2026", "0636", "J2405", "ONDANSETRON HCL 4 MG/2ML INJ SOLN", 1, 117.01),
                ("08/27/2026", "0300", "85025", "HB-HEM. SURV. W/AUTO DIFF&PLT", 1, 135.00),
                ("08/27/2026", "0300", "80053", "HB-COMPREHENSIVE METAB PANEL", 1, 379.00)],
         ratio=0.41, deductible=1000, coins=0.25,
         style=dict(font="arial", color=(128, 24, 40), header="band", logo="cross", table="lines",
                    summary="box", extras=["message", "stub"])),
    dict(key="mammogram_ultrasound", provider="Harbor Women's Health Center", kind="hospital", setting="outpatient",
         address="75 Harbor View Rd, Monona WI 53716", phone="(608) 555-0158", npi="1861502294",
         payer="Dean Health Plan", plan="Dean Prime", member="DHP0048217", group="G55910",
         patient=("ANGELA K. MOCKWELL", "402 Lake Edge Blvd, Monona WI 53716", "05/21/1970"),
         account="HWHC-561209", statement="09/05/2026", due="10/01/2026",
         lines=[("08/15/2026", "0403", "77067", "SCREENING MAMMOGRAPHY BILATERAL W/CAD", 1, 570.00),
                ("08/15/2026", "0402", "76641", "ULTRASOUND BREAST UNILATERAL COMPLETE", 1, 988.00)],
         ratio=0.5, deductible=250, coins=0.2, per_line=True, preventive={"77067"},
         style=dict(font="avenir", color=(173, 20, 87), header="centered", logo="heart", table="grid",
                    summary="panel", extras=[])),
    dict(key="dermatology_biopsy", provider="Clearwater Dermatology", kind="physician", setting="outpatient",
         address="3030 University Ave, Madison WI 53705", phone="(608) 555-0124", npi="1326078841",
         payer="Cigna", plan="Open Access Plus", member="U58823014", group="3338812",
         patient=("PRIYA N. SAMPLE", "118 S Few St, Madison WI 53703", "10/12/1985"),
         account="CWD-44109", statement="09/12/2026", due="10/07/2026",
         lines=[("08/29/2026", None, "99203", "New patient office visit, low complexity", 1, 245.00),
                ("08/29/2026", None, "11102", "Tangential biopsy of skin, single lesion", 1, 409.00),
                ("08/29/2026", None, "88305", "Surgical pathology, gross & micro, level IV", 1, 180.00)],
         ratio=0.55, deductible=250, coins=0.2,
         style=dict(font="verdana", color=(2, 119, 189), header="sidebar", logo="hex", table="lines",
                    summary="stacked", extras=["stub"])),
    dict(key="echo_cardiology", provider="Heartland Cardiology Associates", kind="physician", setting="outpatient",
         address="1 S Park St, Madison WI 53715", phone="(608) 555-0187", npi="1578390026",
         payer="WPS Health Solutions", plan="Statewide", member="WPS60318842", group="STW-0142",
         patient=("GEORGE H. NOTREAL", "92 Hillcrest Dr, Stoughton WI 53589", "07/04/1958"),
         account="HCA-2093381", statement="09/03/2026", due="09/28/2026",
         lines=[("08/20/2026", None, "99204", "New patient visit, moderate complexity", 1, 395.00),
                ("08/20/2026", None, "93306", "Echocardiogram, complete, with Doppler", 1, 3113.00),
                ("08/20/2026", None, "93000", "Electrocardiogram, 12-lead, with report", 1, 85.00)],
         ratio=0.39, deductible=750, coins=0.2,
         error="small_adjustment",
         style=dict(font="tahoma", color=(198, 40, 40), header="left_logo", logo="heart", table="zebra",
                    summary="box", extras=["message"])),
    dict(key="sleep_study", provider="Pinecrest Sleep Center", kind="hospital", setting="outpatient",
         address="2600 Pinecrest Rd, Madison WI 53719", phone="(608) 555-0171", npi="1932664105",
         payer="Anthem Blue Cross Blue Shield", plan="Pathway X HMO", member="XWB731055281", group="220917",
         patient=("LINDA F. EXAMPLEE", "1504 Regent St, Madison WI 53726", "08/08/1966"),
         account="PSC-771204", statement="09/18/2026", due="10/13/2026",
         lines=[("09/01/2026", "0740", "95810", "POLYSOMNOGRAPHY, 4+ PARAMETERS, ATTENDED", 1, 5950.00),
                ("09/01/2026", "0920", None, "SLEEP TECHNOLOGIST MONITORING", 1, 420.00)],
         ratio=0.33, deductible=1200, coins=0.3, per_line=True,
         style=dict(font="helvetica", color=(27, 94, 32), header="band", logo="leaf", table="grid",
                    summary="callout", extras=["stub"])),
    dict(key="wrist_fracture_selfpay", provider="Summit Orthopedics & Sports Medicine", kind="physician",
         setting="outpatient", address="7800 Summit Ridge Rd, Madison WI 53719", phone="(608) 555-0149",
         npi="1669021837", payer=None, plan=None,
         patient=("JORDAN T. PLACEHOLD", "2800 Fish Hatchery Rd, Fitchburg WI 53713", "04/25/1999"),
         account="SOSM-310552", statement="09/09/2026", due="10/09/2026",
         lines=[("08/31/2026", None, "99203", "New patient office visit", 1, 245.00),
                ("08/31/2026", None, "73110", "X-ray wrist, 3+ views", 1, 502.00),
                ("08/31/2026", None, "29125", "Application of short arm splint, static", 1, 559.00),
                ("08/31/2026", None, "L3908", "Wrist hand orthosis, prefabricated", 1, 145.00)],
         ratio=0.75, deductible=0, coins=1.0, self_pay_discount=True,
         style=dict(font="arial", color=(55, 71, 79), header="left_logo", logo="mountain", table="lines",
                    summary="box", extras=["message", "stub"])),
    dict(key="pediatric_well_child", provider="Little Oaks Pediatrics", kind="physician", setting="outpatient",
         address="410 Acorn Dr, Sun Prairie WI 53590", phone="(608) 555-0136", npi="1205948830",
         payer="Quartz Health Solutions", plan="Quartz Choice", member="QZ7730215", group="40377",
         patient=("MILO B. TESTCHILD", "65 Prairie Ln, Sun Prairie WI 53590", "03/02/2025"),
         account="LOP-88213", statement="09/11/2026", due="10/06/2026",
         lines=[("09/02/2026", None, "99392", "Well child visit, age 1-4, established", 1, 410.00),
                ("09/02/2026", None, "90707", "MMR vaccine, live, subcutaneous", 1, 385.00),
                ("09/02/2026", None, "90700", "DTaP vaccine, under 7 years", 1, 245.00),
                ("09/02/2026", None, "90460", "Immunization admin w/ counseling, 1st component", 2, 360.00),
                ("09/02/2026", None, "96110", "Developmental screening, standardized", 1, 95.00)],
         ratio=0.6, deductible=0, coins=0.0, copay=25.00,
         error="small_adjustment",
         style=dict(font="trebuchet", color=(239, 108, 0), header="centered", logo="circle", table="zebra",
                    summary="panel", extras=["message"])),
    dict(key="knee_injection", provider="Lakeshore Sports Medicine", kind="physician", setting="outpatient",
         address="1410 Lakeshore Dr, Madison WI 53715", phone="(608) 555-0163", npi="1417730958",
         payer="UnitedHealthcare", plan="Navigate HMO", member="883021974", group="902211",
         patient=("ERIC W. MOCKUP", "22 Spaight St, Madison WI 53703", "01/19/1971"),
         account="LSM-5520981", statement="09/14/2026", due="10/09/2026",
         lines=[("09/03/2026", None, "99213", "Office visit, established, low complexity", 1, 210.00),
                ("09/03/2026", None, "20610", "Arthrocentesis/injection, major joint", 1, 1050.00),
                ("09/03/2026", None, "J3301", "Triamcinolone acetonide, per 10 mg", 4, 607.92),
                ("09/03/2026", None, "76942", "Ultrasonic guidance for needle placement", 1, 390.00)],
         ratio=0.42, deductible=600, coins=0.2,
         style=dict(font="georgia", color=(0, 77, 64), header="sidebar", logo="square", table="grid",
                    summary="stacked", extras=[])),
    dict(key="er_abdominal_selfpay", provider="Prairie View Community Hospital", kind="hospital", setting="emergency",
         address="500 Prairie View Rd, Portage WI 53901", phone="(608) 555-0112", npi="1780526613",
         payer=None, plan=None,
         patient=("ALEX C. NOBODY", "1717 Canal St, Portage WI 53901", "09/30/1994"),
         account="PVCH-6620395", statement="09/16/2026", due="10/16/2026",
         lines=[("09/04/2026", "0450", "99283", "HB-ED LEVEL 3 VISIT", 1, 1698.00),
                ("09/04/2026", "0402", "76700", "HB-ULTRASOUND-ABDOMEN COMPLETE", 1, 1838.00),
                ("09/04/2026", "0300", "81001", "HB-URINALYSIS AUTO W MICRO", 1, 66.15),
                ("09/04/2026", "0300", "84703", "HB-HCG QL SERUM", 1, 82.75),
                ("09/04/2026", "0300", "85025", "HB-HEM. SURV. W/AUTO DIFF&PLT", 1, 135.00),
                ("09/04/2026", "0300", "36415", "HB-VENIPUNCTURE", 1, 38.95)],
         ratio=1.0, deductible=0, coins=1.0,
         style=dict(font="verdana", color=(69, 90, 100), header="band", logo="shield", table="zebra",
                    summary="callout", extras=["message", "stub"])),
    dict(key="allergy_testing", provider="Bluestem Allergy & Asthma Clinic", kind="physician", setting="outpatient",
         address="3100 Bluestem Way, Middleton WI 53562", phone="(608) 555-0155", npi="1548862207",
         payer="Aetna", plan="Choice POS II", member="W310928457", group="086701",
         patient=("NINA R. SAMPLEFORD", "908 Century Ave, Middleton WI 53562", "12/12/1990"),
         account="BAAC-19302", statement="09/19/2026", due="10/14/2026",
         lines=[("09/08/2026", None, "99204", "New patient visit, moderate complexity", 1, 395.00),
                ("09/08/2026", None, "95004", "Percutaneous allergy tests, each", 40, 4240.00),
                ("09/08/2026", None, "94010", "Spirometry", 1, 165.00)],
         ratio=0.35, deductible=1500, coins=0.2, per_line=True,
         error="small_adjustment",
         style=dict(font="avenir", color=(21, 101, 192), header="sidebar", logo="hex", table="lines",
                    summary="callout", extras=[])),
    dict(key="laceration_walk_in", provider="QuickCare Walk-In Clinic", kind="physician", setting="outpatient",
         address="5 Junction Rd, Madison WI 53717", phone="(608) 555-0109", npi=None,
         payer="WPS Health Solutions", plan="Statewide", member="WPS41277590", group="STW-0033",
         patient=("SAM J. TESTPERSON", "440 Junction Ct, Madison WI 53717", "06/11/1983"),
         account="QC-2201776", statement="09/06/2026", due="10/01/2026",
         lines=[("08/26/2026", None, "99213", "Office visit, established patient", 1, 210.00),
                ("08/26/2026", None, "12002", "Simple repair of wound, 2.6-7.5 cm", 1, 879.00),
                ("08/26/2026", None, "90715", "Tdap vaccine, 7 years and older", 1, 130.33),
                ("08/26/2026", None, "90471", "Immunization administration, 1st", 1, 98.50)],
         ratio=0.5, deductible=0, coins=0.2, copay=40.00,
         style=dict(font="courier", color=(66, 66, 66), header="left_logo", logo="square", table="plain",
                    summary="box", extras=["stub"])),
]

LABEL_SETS = [
    ("Total Charges", "Insurance Payments", "Insurance Adjustments", "Patient Balance Due"),
    ("Total Billed", "Paid by Your Insurance", "Discounts & Adjustments", "Amount You Owe"),
    ("Charges", "Insurance Paid", "Contractual Adjustment", "Your Responsibility"),
    ("Total Charges", "Insurance Paid", "Plan Discount", "Balance Due"),
]


# ---------------- money ----------------

def compute(b):
    """Per-line allowed amount, insurance paid, adjustment and patient share; totals are their sums."""
    out, ded_left, copay_left = [], b.get("deductible", 0), b.get("copay", 0)
    insured = b["payer"] is not None
    for line in b["lines"]:
        charge = line[5]
        if not insured:
            allowed = round(charge * b["ratio"], 2) if b.get("self_pay_discount") else charge
            out.append({"allowed": allowed, "paid": 0.0, "adj": round(charge - allowed, 2), "pt": allowed})
            continue
        allowed = round(charge * b["ratio"], 2)
        if line[2] in b.get("preventive", ()):
            pt = 0.0  # preventive care covered in full
        else:
            ded = min(allowed, ded_left)
            ded_left -= ded
            copay = min(allowed - ded, copay_left)
            copay_left -= copay
            pt = round(ded + copay + (allowed - ded - copay) * b["coins"], 2)
        paid = round(allowed - pt, 2)
        # The billing error each insured sample contains: the plan's contract discount wasn't applied (or only a
        # small courtesy discount was), so the patient is billed the full charge minus what insurance paid.
        adj = round(charge * 0.05, 2) if b.get("error") == "small_adjustment" else 0.0
        out.append({"allowed": allowed, "paid": paid, "adj": adj, "pt": round(charge - paid - adj, 2)})
    total = round(sum(l[5] for l in b["lines"]), 2)
    totals = {"total_charges": total, "insurance_payments": round(sum(x["paid"] for x in out), 2),
              "adjustments": round(sum(x["adj"] for x in out), 2), "patient_balance_due": round(sum(x["pt"] for x in out), 2)}
    return out, totals


def ground_truth(b, per_line, totals):
    shown = b.get("per_line")
    lines = []
    for i, ((date, rc, code, desc, units, charge), x) in enumerate(zip(b["lines"], per_line)):
        lines.append({"ref": f"L{i + 1}", "service_date": date, "revenue_code": rc, "code_as_printed": code,
                      "code_type_as_printed": None, "modifiers": [], "description_as_printed": desc,
                      "units": units, "charge_amount": charge,
                      "insurance_paid": x["paid"] if shown else None, "adjustment": x["adj"] if shown else None,
                      "patient_responsibility": x["pt"] if shown else None,
                      "legibility": "clear", "candidate_codes": []})
    m, d, y = b["statement"].split("/")
    return {
        "document": {"document_type": "itemized_hospital_statement" if b["kind"] == "hospital" else "physician_statement",
                     "is_itemized": True, "statement_date": f"{y}-{m}-{d}"},
        "provider": {"billing_provider_name": b["provider"], "billing_npi": b.get("npi")},
        "insurance": {"coverage_status": "insured" if b["payer"] else "uninsured_self_pay",
                      "payer_name": b["payer"], "plan_name": b["plan"]},
        "encounter": {"setting": b["setting"]},
        "line_items": lines,
        "totals": totals,
    }


# ---------------- drawing ----------------

def initials(name):
    words = [w for w in name.replace("&", "").split() if w[0].isupper() and w.lower() not in ("of",)]
    return "".join(w[0] for w in words[:2])


def draw_logo(d, kind, x, y, s, color, fg, name, family):
    """A simple mark inside the box (x, y, x+s, y+s)."""
    cx, cy = x + s / 2, y + s / 2
    if kind == "circle":
        d.ellipse((x, y, x + s, y + s), fill=color)
        d.text((cx, cy), initials(name), font=font(family, int(s * 0.38), True), fill=fg, anchor="mm")
    elif kind == "cross":
        t = s * 0.3
        d.rounded_rectangle((cx - t / 2, y, cx + t / 2, y + s), radius=6, fill=color)
        d.rounded_rectangle((x, cy - t / 2, x + s, cy + t / 2), radius=6, fill=color)
    elif kind == "shield":
        d.polygon([(x, y), (x + s, y), (x + s, y + s * 0.55), (cx, y + s), (x, y + s * 0.55)], fill=color)
        d.text((cx, cy - s * 0.06), initials(name)[:1], font=font(family, int(s * 0.45), True), fill=fg, anchor="mm")
    elif kind == "leaf":
        # a lens between the bottom-left and top-right corners, with a vein
        steps = [i / 20 for i in range(21)]
        upper = [(x + s * t, y + s * (1 - t) - s * 0.35 * math.sin(math.pi * t)) for t in steps]
        lower = [(x + s * t, y + s * (1 - t) + s * 0.35 * math.sin(math.pi * t)) for t in reversed(steps)]
        d.polygon(upper + lower, fill=color)
        d.line((x + s * 0.12, y + s * 0.88, x + s * 0.85, y + s * 0.15), fill=tint(color, 0.6), width=max(2, int(s * 0.04)))
    elif kind == "mountain":
        d.polygon([(x, y + s), (x + s * 0.4, y + s * 0.2), (x + s * 0.7, y + s)], fill=color)
        d.polygon([(x + s * 0.35, y + s), (x + s * 0.7, y + s * 0.45), (x + s, y + s)], fill=tint(color, 0.35))
    elif kind == "heart":
        r = s * 0.28
        d.ellipse((x + s * 0.05, y + s * 0.1, x + s * 0.05 + 2 * r, y + s * 0.1 + 2 * r), fill=color)
        d.ellipse((x + s * 0.95 - 2 * r, y + s * 0.1, x + s * 0.95, y + s * 0.1 + 2 * r), fill=color)
        d.polygon([(x + s * 0.07, y + s * 0.45), (x + s * 0.93, y + s * 0.45), (cx, y + s * 0.95)], fill=color)
    elif kind == "hex":
        pts = [(cx + s / 2 * c, cy + s / 2 * v) for c, v in
               ((1, 0), (0.5, 0.87), (-0.5, 0.87), (-1, 0), (-0.5, -0.87), (0.5, -0.87))]
        d.polygon(pts, fill=color)
        d.text((cx, cy), initials(name), font=font(family, int(s * 0.32), True), fill=fg, anchor="mm")
    elif kind == "square":
        d.rounded_rectangle((x, y, x + s, y + s), radius=s * 0.2, fill=color)
        d.text((cx, cy), initials(name)[:1], font=font(family, int(s * 0.55), True), fill=fg, anchor="mm")


def fit(d, text, f, width):
    while d.textlength(text, font=f) > width and len(text) > 4:
        text = text[:-2].rstrip() + "…" if not text.endswith("…") else text[:-2] + "…"
    return text


def dashed(d, x1, y, x2, color=GRAY, dash=14, gap=10):
    x = x1
    while x < x2:
        d.line((x, y, min(x + dash, x2), y), fill=color, width=2)
        x += dash + gap


def header(d, b, st, labels):
    fam, color = st["font"], st["color"]
    title = "STATEMENT" if b["kind"] == "physician" else "HOSPITAL STATEMENT"
    kind = st["header"]
    if kind == "band":
        d.rectangle((0, 0, W, 230), fill=color)
        draw_logo(d, st["logo"], M, 55, 110, (255, 255, 255), color, b["provider"], fam)
        d.text((M + 140, 70), b["provider"], font=font(fam, 44, True), fill="white")
        d.text((M + 140, 130), f"{b['address']}  ·  {b['phone']}", font=font(fam, 24), fill=tint(color, 0.75))
        d.text((W - M, 176), title, font=font(fam, 28, True), fill="white", anchor="ra")
        return 290
    if kind == "centered":
        draw_logo(d, st["logo"], W / 2 - 50, 60, 100, color, (255, 255, 255), b["provider"], fam)
        d.text((W / 2, 200), b["provider"], font=font(fam, 46, True), fill=INK, anchor="ma")
        d.text((W / 2, 262), f"{b['address']}  |  {b['phone']}", font=font(fam, 24), fill=GRAY, anchor="ma")
        d.line((M, 315, W - M, 315), fill=color, width=4)
        d.line((M, 323, W - M, 323), fill=color, width=1)
        d.text((W / 2, 345), title, font=font(fam, 28, True), fill=color, anchor="ma")
        return 410
    if kind == "sidebar":
        d.rectangle((0, 0, 36, 99999), fill=color)
        draw_logo(d, st["logo"], M, 70, 90, color, (255, 255, 255), b["provider"], fam)
        d.text((M + 115, 72), b["provider"], font=font(fam, 42, True), fill=color)
        d.text((M + 115, 128), b["address"], font=font(fam, 24), fill=GRAY)
        d.text((W - M, 80), title, font=font(fam, 30, True), fill=INK, anchor="ra")
        d.text((W - M, 124), b["phone"], font=font(fam, 24), fill=GRAY, anchor="ra")
        return 230
    # left_logo
    draw_logo(d, st["logo"], M, 60, 100, color, (255, 255, 255), b["provider"], fam)
    d.text((M + 125, 62), b["provider"], font=font(fam, 42, True), fill=INK)
    d.text((M + 125, 116), b["address"], font=font(fam, 24), fill=GRAY)
    d.text((M + 125, 148), b["phone"], font=font(fam, 24), fill=GRAY)
    d.text((W - M, 70), title, font=font(fam, 32, True), fill=color, anchor="ra")
    d.line((M, 205, W - M, 205), fill=color, width=3)
    return 240


def info(d, b, st, y, totals, labels):
    fam, color = st["font"], st["color"]
    small, bold = font(fam, 23), font(fam, 23, True)
    # left: patient / insurance
    rows = [("Patient", b["patient"][0]), ("Address", b["patient"][1]), ("Date of birth", b["patient"][2])]
    if b["payer"]:
        rows += [("Insurance", b["payer"]), ("Plan", b["plan"]), ("Member ID", b["member"]), ("Group #", b["group"])]
    else:
        rows += [("Insurance", "None on file - Self pay")]
    if b.get("npi"):
        rows.append(("Provider NPI", b["npi"]))
    yy = y
    for k, v in rows:
        d.text((M, yy), k, font=small, fill=GRAY)
        d.text((M + 200, yy), v, font=bold if k in ("Patient", "Insurance") else small, fill=INK)
        yy += 36
    # right: account box
    bx1, bx2 = W - M - 560, W - M
    meta = [("Statement date", b["statement"]), ("Account number", b["account"]), ("Payment due", b["due"])]
    by2 = y + len(meta) * 42 + 110
    if st["summary"] in ("box", "callout"):
        d.rounded_rectangle((bx1, y - 10, bx2, by2), radius=14, fill=tint(color, 0.9))
    else:
        d.rectangle((bx1, y - 10, bx2, by2), outline=color, width=2)
    yy2 = y + 8
    for k, v in meta:
        d.text((bx1 + 25, yy2), k, font=small, fill=GRAY)
        d.text((bx2 - 25, yy2), v, font=small, fill=INK, anchor="ra")
        yy2 += 42
    d.line((bx1 + 25, yy2 + 4, bx2 - 25, yy2 + 4), fill=tint(color, 0.5), width=2)
    d.text((bx1 + 25, yy2 + 30), labels[3], font=font(fam, 26, True), fill=INK)
    d.text((bx2 - 25, yy2 + 26), money(totals["patient_balance_due"]), font=font(fam, 34, True), fill=color, anchor="ra")
    return max(yy, by2) + 50


def table(d, b, st, y, per):
    fam, color, style = st["font"], st["color"], st["table"]
    f, fb = font(fam, 22), font(fam, 22, True)
    show_rev, show_ins = b["kind"] == "hospital", b.get("per_line")
    cols = [("Date", 150, "l"), ("Code", 105, "l")]
    if show_rev:
        cols.insert(1, ("Rev", 80, "l"))
    right = [("Qty", 60, "r"), ("Charges", 150, "r")]
    if show_ins:
        right += [("Ins. Paid", 145, "r"), ("Adjust.", 140, "r"), ("You Owe", 135, "r")]
    desc_w = (W - 2 * M) - sum(c[1] for c in cols + right) - 4 * (len(cols) + len(right))
    cols = cols + [("Description", desc_w, "l")] + right
    xs, x = [], M
    for _, w, _ in cols:
        xs.append(x)
        x += w + 4
    row_h = 46

    def cell_x(i, align):
        return xs[i] + 8 if align == "l" else xs[i] + cols[i][1] - 6

    # header row
    if style in ("zebra",):
        d.rectangle((M, y, W - M, y + row_h), fill=color)
        hdr_fill = "white"
    elif style == "grid":
        d.rectangle((M, y, W - M, y + row_h), fill=tint(color, 0.85))
        hdr_fill = INK
    else:
        hdr_fill = color if style == "lines" else INK
    for i, (name, _, align) in enumerate(cols):
        d.text((cell_x(i, align), y + row_h / 2), name.upper() if style != "plain" else name, font=fb,
               fill=hdr_fill, anchor=("lm" if align == "l" else "rm"))
    top = y
    y += row_h
    if style in ("lines", "plain"):
        d.line((M, y, W - M, y), fill=color if style == "lines" else INK, width=2)
    for n, ((date, rc, code, desc, units, charge), x) in enumerate(zip(b["lines"], per)):
        if style == "zebra" and n % 2:
            d.rectangle((M, y, W - M, y + row_h), fill=tint(color, 0.92))
        vals = [date, code or ""]
        if show_rev:
            vals.insert(1, rc or "")
        vals += [desc, f"{units:g}", f"{charge:,.2f}"]
        if show_ins:
            vals += [f"{x['paid']:,.2f}", f"-{x['adj']:,.2f}" if x["adj"] else "0.00", f"{x['pt']:,.2f}"]
        for i, (v, (_, w, align)) in enumerate(zip(vals, cols)):
            d.text((cell_x(i, align), y + row_h / 2), fit(d, v, f, w - 12), font=f, fill=INK,
                   anchor=("lm" if align == "l" else "rm"))
        y += row_h
        if style == "lines":
            d.line((M, y, W - M, y), fill=LIGHT, width=1)
    if style == "grid":
        for yy in range(top, y + 1, row_h):
            d.line((M, yy, W - M, yy), fill=(190, 190, 190), width=1)
        for xx in xs[1:] + [W - M]:
            d.line((xx - 2, top, xx - 2, y), fill=(190, 190, 190), width=1)
        d.line((M, top, M, y), fill=(190, 190, 190), width=1)
    if style in ("zebra", "plain"):
        d.line((M, y, W - M, y), fill=color if style == "zebra" else INK, width=2)
    return y + 40


def summary(d, b, st, y, totals, labels):
    fam, color, kind = st["font"], st["color"], st["summary"]
    rows = [(labels[0], money(totals["total_charges"])), (labels[1], minus(totals["insurance_payments"])),
            (labels[2], minus(totals["adjustments"]))]
    due = totals["patient_balance_due"]
    f, fb, big = font(fam, 26), font(fam, 28, True), font(fam, 44, True)
    if kind == "panel":
        d.rounded_rectangle((M, y, W - M, y + 170), radius=18, fill=tint(color, 0.9))
        items = rows + [(labels[3], money(due))]
        w = (W - 2 * M) / 4
        for i, (label, v) in enumerate(items):
            cx = M + w * i + w / 2
            last = i == 3
            if last:
                d.rounded_rectangle((M + w * 3 + 10, y + 10, W - M - 10, y + 160), radius=14, fill=color)
            d.text((cx, y + 40), label, font=font(fam, 22, True), fill="white" if last else GRAY, anchor="mm")
            d.text((cx, y + 100), v, font=font(fam, 36, True), fill="white" if last else INK,
                   anchor="mm")
        return y + 220
    x1, x2 = W - M - 700, W - M
    if kind == "callout":
        d.rounded_rectangle((M, y, M + 560, y + 230), radius=20, fill=color)
        d.text((M + 280, y + 60), labels[3].upper(), font=font(fam, 26, True), fill=tint(color, 0.8), anchor="mm")
        d.text((M + 280, y + 125), money(due), font=font(fam, 62, True), fill="white", anchor="mm")
        d.text((M + 280, y + 190), f"Please pay by {b['due']}", font=font(fam, 22), fill=tint(color, 0.8), anchor="mm")
    if kind == "box":
        d.rectangle((x1, y, x2, y + 250), outline=color, width=3)
    yy = y + 25
    for label, v in rows:
        d.text((x1 + 30, yy), label, font=f, fill=INK)
        d.text((x2 - 30, yy), v, font=f, fill=INK, anchor="ra")
        yy += 48
    if kind == "box":
        d.rectangle((x1, yy + 5, x2, y + 250), fill=tint(color, 0.85))
    else:
        d.line((x1 + 30, yy + 5, x2 - 30, yy + 5), fill=INK, width=3)
    d.text((x1 + 30, yy + 30), labels[3], font=fb, fill=INK)
    d.text((x2 - 30, yy + 22), money(due), font=font(fam, 38, True), fill=color, anchor="ra")
    return y + 290


def message(d, b, st, y):
    fam, color = st["font"], st["color"]
    lines = [f"Questions about your bill? Call Patient Financial Services at {b['phone']}, Mon-Fri 8am-5pm.",
             "Pay online at the patient portal, by phone, or mail a check using the payment slip.",
             "Financial assistance is available. Ask us for an application."]
    if not b["payer"]:
        lines[1] = "No insurance on file. If you have coverage, please call us so we can bill your plan."
    d.rounded_rectangle((M, y, W - M, y + 60 + 38 * len(lines)), radius=12, outline=tint(color, 0.4), width=2)
    d.text((M + 30, y + 22), "IMPORTANT MESSAGES", font=font(fam, 22, True), fill=color)
    for i, t in enumerate(lines):
        d.text((M + 30, y + 62 + 38 * i), "•  " + t, font=font(fam, 22), fill=INK)
    return y + 100 + 38 * len(lines)


def stub(d, b, st, y, due, rng):
    fam, color = st["font"], st["color"]
    dashed(d, M - 40, y, W - M + 40)
    d.text((W / 2, y + 16), "Please detach and return this portion with your payment", font=font(fam, 20),
           fill=GRAY, anchor="ma")
    y += 70
    draw_logo(d, st["logo"], M, y, 60, color, (255, 255, 255), b["provider"], fam)
    d.text((M + 80, y + 4), b["provider"], font=font(fam, 26, True), fill=INK)
    d.text((M + 80, y + 40), b["address"], font=font(fam, 20), fill=GRAY)
    y2 = y + 110
    cells = [("Account number", b["account"]), ("Due date", b["due"]), ("Amount due", money(due)),
             ("Amount enclosed", "$")]
    w = (W - 2 * M) / 4
    for i, (k, v) in enumerate(cells):
        x = M + w * i
        d.rectangle((x, y2, x + w - 10, y2 + 90), outline=(160, 160, 160), width=2)
        d.text((x + 15, y2 + 12), k.upper(), font=font(fam, 18, True), fill=GRAY)
        d.text((x + 15, y2 + 44), v, font=font(fam, 28, True), fill=INK)
    x = M
    y3 = y2 + 120
    while x < M + 620:  # barcode
        wbar = rng.choice((2, 3, 5, 7))
        d.rectangle((x, y3, x + wbar, y3 + 60), fill=INK)
        x += wbar + rng.choice((3, 4, 6))
    d.text((W - M, y3 + 10), "Make checks payable to:", font=font(fam, 20), fill=GRAY, anchor="ra")
    d.text((W - M, y3 + 40), b["provider"], font=font(fam, 22, True), fill=INK, anchor="ra")
    return y3 + 110


def render(b, per, totals, n):
    st, rng = b["style"], random.Random(b["key"])
    labels = LABEL_SETS[n % len(LABEL_SETS)]
    img = Image.new("RGB", (W, 4000), "white")
    d = ImageDraw.Draw(img)
    y = header(d, b, st, labels)
    y = info(d, b, st, y, totals, labels)
    y = table(d, b, st, y, per)
    y = summary(d, b, st, y, totals, labels)
    if "message" in st["extras"]:
        y = message(d, b, st, y + 10)
    if "stub" in st["extras"]:
        y = stub(d, b, st, y + 40, totals["patient_balance_due"], rng)
    y = max(y + 40, 2100)
    d.text((M, y), "Sample bill for testing. Fictitious patient and account details.", font=font(st["font"], 18),
           fill=(160, 160, 160))
    d.text((W - M, y), "Page 1 of 1", font=font(st["font"], 18), fill=(160, 160, 160), anchor="ra")
    return img.crop((0, 0, W, max(y + 60, 2200)))


if __name__ == "__main__":
    for n, b in enumerate(BILLS):
        per, totals = compute(b)
        render(b, per, totals, n).save(OUT / f"{b['key']}.png", optimize=True)
        (OUT / f"{b['key']}.json").write_text(json.dumps(ground_truth(b, per, totals), indent=2) + "\n")
        print(f"wrote {b['key']:<24} total {money(totals['total_charges']):>11}  "
              f"insurance {money(totals['insurance_payments']):>10}  owes {money(totals['patient_balance_due']):>10}")
