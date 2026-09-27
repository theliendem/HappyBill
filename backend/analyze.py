"""
Step 2: bill -> exact ids (mediator resolve) -> published rates per line (mediator get_negotiated_rate).
No AI in this step.

Returns status "needs_input" with every open question at once (coverage, bill type, hospital, insurer,
plan, unmatched lines) so the website can show one confirmation screen; the website sends the answers
back as `choices` and calls again. Once nothing is open, returns status "complete" with per-line
benchmarks and totals.
"""
import re
from statistics import median

from mediator import db, get_market_rate, get_negotiated_rate
from mediator.rates import INSURER_FILE
from mediator.resolve import DOC_CLASS, resolve_hospital, resolve_line, resolve_payer, resolve_plan

from .extract import assign_refs
from .schemas import Bill, BillLine, Choices

READY = ("matched", "likely")
NONE = "none"  # user answer: "mine isn't listed"


class BadChoice(ValueError):
    pass


def _r2(x):
    return None if x is None else round(float(x), 2)


# ---------------- entities ----------------

# Demo: only UW Health's price list is loaded, so a provider we can't find is priced as if it were UW Health
# (the hospital for facility bills, the physician group for doctor bills), matching lines by code alone.
DEMO_ORGS = {"institutional": "uwhc", "professional": "uwmf", None: "uwhc"}


def _hospital(bill, choices, billing_class):
    found = _find_hospital(bill, choices, billing_class)
    if found.get("org_key") and found["status"] in READY:
        return found
    org_key = DEMO_ORGS.get(billing_class, "uwhc")
    r = db.one("SELECT name FROM organizations WHERE org_key = %s", (org_key,))
    return {"status": "matched", "matched_by": "demo_default", "org_key": org_key, "provider_tin": None,
            "name": r["name"] if r else org_key, "provider_npi": None,
            "not_found": bill.provider.billing_provider_name}


def _find_hospital(bill, choices, billing_class):
    if choices.org_key == NONE or choices.provider_tin == NONE:
        return {"status": NONE, "org_key": None, "provider_tin": None, "name": bill.provider.billing_provider_name}
    if choices.org_key:
        r = db.one("SELECT org_key, name FROM organizations WHERE org_key = %s", (choices.org_key,))
        if not r:
            raise BadChoice(f"unknown org_key {choices.org_key}")
        return {"status": "matched", "matched_by": "user", "org_key": r["org_key"], "provider_tin": None,
                "name": r["name"], "provider_npi": bill.provider.billing_npi}
    if choices.provider_tin:
        r = db.one("SELECT tin, business_name FROM provider_groups WHERE tin = %s LIMIT 1", (choices.provider_tin,))
        if not r:
            raise BadChoice(f"unknown provider_tin {choices.provider_tin}")
        return {"status": "matched", "matched_by": "user", "org_key": None, "provider_tin": r["tin"],
                "name": r["business_name"], "provider_npi": bill.provider.billing_npi}
    p = bill.provider
    return resolve_hospital({"name": p.billing_provider_name, "facility_name": p.facility_name,
                             "npi": p.billing_npi, "tin": p.tin}, billing_class)


def _payer(bill, choices):
    if choices.payer_key == NONE:
        return {"status": NONE, "payer_key": None, "name": bill.insurance.payer_name}
    if choices.payer_key:
        r = db.one("SELECT payer_key, name FROM payers WHERE payer_key = %s", (choices.payer_key,))
        if not r:
            raise BadChoice(f"unknown payer_key {choices.payer_key}")
        return {"status": "matched", "matched_by": "user", "payer_key": r["payer_key"], "name": r["name"]}
    ins = bill.insurance
    return resolve_payer({"coverage_status": "insured", "payer_name": ins.payer_name,
                          "plan_type": None if ins.plan_type == "unknown" else ins.plan_type})


def _plan(payer, bill, choices):
    if choices.plan_key == NONE:
        return {"status": NONE, "plan_key": None}
    if choices.plan_key:
        r = db.one("SELECT plan_key, payer_key, name FROM plans WHERE plan_key = %s", (choices.plan_key,))
        if not r or r["payer_key"] != payer["payer_key"]:
            raise BadChoice(f"plan_key {choices.plan_key} is not a plan of {payer['payer_key']}")
        return {"status": "matched", "matched_by": "user", "plan_key": r["plan_key"], "name": r["name"]}
    return resolve_plan(payer, bill.insurance.plan_name)


# ---------------- lines ----------------

def _pricing_lines(bill):
    lines = list(bill.line_items)
    has_drg = any(l.code_type_as_printed in ("MS-DRG", "APR-DRG") or "DRG" in (l.code_as_printed or "").upper()
                  for l in lines)
    if bill.encounter.drg_code and not has_drg:
        billed = bill.totals.total_charges or sum(l.charge_amount for l in lines)
        lines.insert(0, BillLine(ref="STAY", code_as_printed=f"DRG {bill.encounter.drg_code}",
                                 description_as_printed="Whole inpatient stay (case rate)", charge_amount=billed,
                                 insurance_paid=bill.totals.insurance_payments,
                                 patient_responsibility=bill.totals.patient_balance_due))
    return lines


def _item_by_price(selected, org_key, line):
    """Several chargemaster items can share a code (and even a description); the billed amount equal to an
    item's list price x units identifies which one is on the bill."""
    rows = db.query("""SELECT hi.item_id, hi.gross_charge, hi.modifiers FROM item_codes ic
                       JOIN hospital_items hi USING (item_id)
                       WHERE ic.code = %s AND ic.code_type = %s AND hi.org_key = %s""",
                    (selected["code"], selected["code_type"], org_key))
    mods = sorted(selected.get("modifiers") or [])
    hits = [r["item_id"] for r in rows if r["gross_charge"] is not None
            and abs(float(r["gross_charge"]) * line.units - line.charge_amount) < 0.01
            and sorted(r["modifiers"] or []) == mods]
    return hits[0] if len(hits) == 1 else None


def _trim_candidate(c):
    return {k: c.get(k) for k in ("code", "code_type", "official_description", "hospital_descriptions",
                                  "hospital_item_id", "score", "verdict", "source")}


def _benchmarks(line, rate, coverage, payer_key):
    cmp = rate.get("comparison") or {}
    market = rate["all_payers_at_this_hospital"][0] if len(rate["all_payers_at_this_hospital"]) == 1 else {}
    plan_totals = [r["line_total_equivalent"] for r in rate["rates"]
                   if payer_key and r["payer_key"] == payer_key and r["line_total_equivalent"] is not None
                   and r.get("consistent_with_requested_plan") is not False]
    b = {
        "billed": _r2(line.charge_amount),
        "list_price": cmp.get("list_price"),
        "cash_price": cmp.get("cash_price"),
        "your_plan_rate_low": min(plan_totals) if plan_totals else None,
        "your_plan_rate_high": max(plan_totals) if plan_totals else None,
        "all_payers_min": market.get("min"),
        "all_payers_median": market.get("median"),
        "commercial_median": market.get("commercial_median"),
        "all_payers_max": market.get("max"),
        "patient_responsibility": _r2(line.patient_responsibility),
    }
    # Target = the number we ask the hospital to move toward. Conservative on purpose.
    if coverage == "insured" and plan_totals:
        target, basis = max(plan_totals), "your_plan_rate"
    elif coverage == "uninsured_self_pay":
        options = [(v, k) for v, k in ((b["cash_price"], "hospital_cash_price"),
                                       (b["commercial_median"], "typical_commercial_insurer_rate")) if v is not None]
        target, basis = min(options) if options else (None, None)
    else:
        target, basis = b["commercial_median"], ("typical_commercial_insurer_rate" if b["commercial_median"] else None)
    if target is None:
        # No plan rate or cash price (e.g. insurer not identified, or a provider known only from an insurer's
        # file): what insurers have contracted to pay this provider is the next-best published price.
        contracted = [r["line_total_equivalent"] for r in rate["rates"]
                      if r["source"] == INSURER_FILE and r["line_total_equivalent"]
                      and not re.search("medicare|medicaid", r["plan_key"] or "")]
        if contracted:
            target, basis = median(contracted), "insurer_rate_this_provider"
            b["insurer_rate_this_provider"] = _r2(target)
    b["target"], b["target_basis"] = _r2(target), basis
    b["insurance_paid"] = _r2(line.insurance_paid)
    b["max_you_should_owe"] = None

    if target is None:
        b["potential_savings"] = None
    elif coverage == "insured":
        # For insured patients the insurer, not the patient, absorbs billed-minus-allowed. In network,
        # the patient owes at most the plan's rate minus what the insurer already paid.
        owed = line.patient_responsibility
        if basis == "your_plan_rate":
            b["max_you_should_owe"] = _r2(max(0.0, target - (line.insurance_paid or 0.0)))
        cap = b["max_you_should_owe"] if b["max_you_should_owe"] is not None else target
        b["potential_savings"] = _r2(max(0.0, owed - cap)) if owed is not None else None
    else:
        b["potential_savings"] = _r2(max(0.0, line.charge_amount - target))
    return b


def _market_fallback(entry, line, coverage, billing_class):
    """No usable price from this provider: compare with what insurers pay other providers for the code."""
    s = entry["selected"]
    market = get_market_rate({"code": s["code"], "code_type": s["code_type"], "modifiers": s.get("modifiers") or [],
                              "billing_class": billing_class, "units": line.units})
    entry["market_rate"] = market
    if market["found"]:
        entry["benchmarks"] = _market_benchmarks(line, market, coverage)


def _market_benchmarks(line, rate, coverage):
    b = {
        "billed": _r2(line.charge_amount), "list_price": None, "cash_price": None,
        "your_plan_rate_low": None, "your_plan_rate_high": None,
        "all_payers_min": None, "all_payers_median": None, "commercial_median": None, "all_payers_max": None,
        "market_low": rate["p10"], "market_median": rate["median"], "market_high": rate["p90"],
        "market_rates_count": rate["rates"],
        "patient_responsibility": _r2(line.patient_responsibility), "insurance_paid": _r2(line.insurance_paid),
        "max_you_should_owe": None, "target": rate["median"], "target_basis": "market_rate",
    }
    owed = line.patient_responsibility if coverage == "insured" else line.charge_amount
    b["potential_savings"] = _r2(max(0.0, owed - b["target"])) if owed is not None else None
    return b


def analyze(bill: Bill, choices: Choices | None = None):
    choices = choices or Choices()
    bill = assign_refs(bill.model_copy(deep=True))
    billing_class = choices.billing_class or DOC_CLASS.get(bill.document.document_type)
    coverage = choices.coverage_status or bill.insurance.coverage_status
    pending = []

    if coverage == "unknown":
        pending.append({"kind": "coverage", "question": "Did health insurance cover this visit?",
                        "options": ["insured", "uninsured_self_pay"]})
    if billing_class is None:
        pending.append({"kind": "bill_type", "question": "Is this a hospital (facility) bill or a doctor/clinic bill?",
                        "options": ["institutional", "professional"]})

    hospital = _hospital(bill, choices, billing_class)
    if hospital["status"] not in READY + (NONE,):
        pending.append({"kind": "hospital", "question": "Which provider sent this bill?",
                        "found": bill.provider.billing_provider_name, "status": hospital["status"],
                        "note": hospital.get("note"),
                        "candidates": hospital.get("candidates") or hospital.get("system_orgs") or []})

    payer, plan = {"status": "not_needed"}, {"status": "not_needed"}
    if coverage == "insured":
        payer = _payer(bill, choices)
        if payer["status"] not in READY + (NONE,):
            pending.append({"kind": "payer", "question": "Which insurance company?", "found": bill.insurance.payer_name,
                            "status": payer["status"], "candidates": payer.get("candidates", [])})
        elif payer.get("payer_key"):
            plan = _plan(payer, bill, choices)
            if plan["status"] not in READY + (NONE,):
                candidates = [c for c in plan.get("candidates", [])
                              if "hospital_standard_charges" in (c.get("source_types") or [])] or plan.get("candidates", [])
                pending.append({"kind": "plan", "question": "Which plan is on your insurance card?",
                                "found": bill.insurance.plan_name, "status": plan["status"], "candidates": candidates})

    org_key = hospital.get("org_key") if hospital["status"] in READY else None
    tin = hospital.get("provider_tin") if hospital["status"] in READY else None
    facility = billing_class == "institutional" and org_key is not None
    lines = []
    pricing_lines = _pricing_lines(bill)
    has_stay = any(l.ref == "STAY" for l in pricing_lines)
    for line in pricing_lines:
        res = resolve_line({"ref": line.ref, "code_as_printed": line.code_as_printed,
                            "code_type": line.code_type_as_printed, "modifiers": line.modifiers,
                            # the stay line's description is ours, not the bill's: don't check it
                            "description": None if line.ref == "STAY" else line.description_as_printed,
                            "revenue_code": line.revenue_code,
                            "candidate_codes": [c.model_dump() for c in line.candidate_codes]},
                           org_key, facility)
        choice = choices.lines.get(line.ref)
        entry = {"ref": line.ref, "kind": "stay" if line.ref == "STAY" else "line",
                 "description": line.description_as_printed, "code_as_printed": line.code_as_printed,
                 "billed": _r2(line.charge_amount), "units": line.units, "selected": None}
        if choice and choice.skip:
            entry["state"] = "skipped"
        elif choice and choice.code:
            entry["state"] = "user_confirmed"
            entry["selected"] = {"code": choice.code, "code_type": choice.code_type or "CPT",
                                 "modifiers": choice.modifiers if choice.modifiers is not None else res["modifiers"],
                                 "hospital_item_id": choice.hospital_item_id, "source": "user"}
        elif res["status"] in ("confirmed", "likely"):
            entry["state"] = res["status"]
            entry["selected"] = res["selected"]
        elif has_stay and line.ref != "STAY":
            # Inpatient charges (room, pharmacy, supplies...) are paid through the stay's case rate.
            entry["state"] = "part_of_stay"
        else:
            entry["state"] = "needs_confirmation"
            pending.append({"kind": "line", "ref": line.ref, "question": "Which service is this line?",
                            "description": line.description_as_printed, "code_as_printed": line.code_as_printed,
                            "status": res["status"], "reason": res["reason"],
                            "candidates": [_trim_candidate(c) for c in res["candidates"] if c["exists"]][:5]})
        entry["_bill_line"] = line
        lines.append(entry)

    result = {"coverage": coverage, "billing_class": billing_class, "hospital": hospital, "payer": payer,
              "plan": plan, "is_itemized": bill.document.is_itemized}
    if pending:
        for e in lines:
            e.pop("_bill_line")
        return {"status": "needs_input", "pending": pending, **result, "lines": lines}

    payer_key = payer.get("payer_key") if coverage == "insured" else None
    for e in lines:
        line = e.pop("_bill_line")
        if not e["selected"]:
            continue
        s = e["selected"]
        if not (org_key or tin):
            _market_fallback(e, line, coverage, billing_class)
            continue
        if org_key and not s.get("hospital_item_id"):
            s["hospital_item_id"] = _item_by_price(s, org_key, line)
        e["rate"] = get_negotiated_rate({
            "code": s["code"], "code_type": s["code_type"], "modifiers": s.get("modifiers") or [],
            "hospital_item_id": s.get("hospital_item_id"), "org_key": org_key, "provider_tin": tin,
            "provider_npi": bill.provider.billing_npi, "payer_key": payer_key,
            "plan_key": plan.get("plan_key") if payer_key else None, "billing_class": billing_class,
            "billed_amount": line.charge_amount, "units": line.units})
        e["benchmarks"] = _benchmarks(line, e["rate"], coverage, payer_key)
        if e["benchmarks"]["target"] is None:
            _market_fallback(e, line, coverage, billing_class)

    totals = _totals(lines, coverage)
    check = _balance_check(bill, lines, coverage)
    if check:
        totals["balance_check"] = check
        totals["potential_savings"] = check["overcharge"]
    bottom = _bottom_line(bill, totals, coverage)
    if bottom:
        totals["bottom_line"] = bottom
        if bottom["savings"] is not None:
            totals["potential_savings"] = bottom["savings"]
    owed = amount_owed(bill)
    if owed is not None and totals["potential_savings"] is not None:
        # You can't save more than you're asked to pay.
        totals["potential_savings"] = _r2(min(totals["potential_savings"], owed))
    return {"status": "complete", **result, "lines": lines, "totals": totals}


def _bottom_line(bill, totals, coverage):
    """The whole bill in one line: total billed -> fair price for the bill -> what you should pay -> savings.

    Fair price = published price for every charge we could check, plus the billed amount for the ones we
    couldn't. Insured: you should pay the fair price minus what insurance paid. Self-pay: the fair price.
    Never more than you're asked to pay; savings = asked - should pay."""
    if totals["compared_target"] is None or not totals["compared_billed"]:
        return None
    total = bill.totals.total_charges or totals["billed_all_lines"]
    fair = _r2(totals["compared_target"] + max(0.0, total - totals["compared_billed"]))
    paid = bill.totals.insurance_payments
    if paid is None and any(l.insurance_paid is not None for l in bill.line_items):
        paid = _r2(sum(l.insurance_paid or 0 for l in bill.line_items))
    asked = amount_owed(bill)
    if asked is None and coverage == "uninsured_self_pay":
        asked = total
    if asked is None and paid is not None:
        asked = _r2(max(0.0, total - paid - (bill.totals.adjustments or 0)))
    out = {"billed": _r2(total), "fair": fair, "insurance_paid": paid, "asked": _r2(asked) if asked is not None else None,
           "should_pay": None, "savings": None, "above_fair": _r2(max(0.0, total - fair))}
    if asked is None:
        return out  # e.g. insured and the claim isn't processed yet: we can't say what you'll owe
    should = max(0.0, fair - (paid or 0.0)) if coverage == "insured" else fair
    out["should_pay"] = _r2(min(asked, should))
    out["savings"] = _r2(asked - out["should_pay"])
    return out


def amount_owed(bill):
    """What the bill asks the patient to pay: the balance due, else the sum of the lines' 'you owe'."""
    if bill.totals.patient_balance_due is not None:
        return bill.totals.patient_balance_due
    owed = [l.patient_responsibility for l in bill.line_items if l.patient_responsibility is not None]
    return round(sum(owed), 2) if owed else None


def _balance_check(bill, lines, coverage):
    """Insured bill that shows only a total balance (no per-line 'you owe'): once the claim is processed,
    the patient owes at most the plan's rates minus what the insurer paid."""
    t = bill.totals
    if coverage != "insured" or t.patient_balance_due is None or t.insurance_payments is None:
        return None
    if any(l.patient_responsibility is not None for l in bill.line_items):
        return None  # judged line by line instead
    items = [l for l in lines if l["kind"] == "line"]
    if not items or any(l["kind"] == "stay" for l in lines):
        return None
    if any(not l.get("benchmarks") or l["benchmarks"]["target_basis"] != "your_plan_rate" for l in items):
        return None  # need the plan's rate for every line to bound the total
    plan_total = _r2(sum(l["benchmarks"]["target"] for l in items))
    max_owe = _r2(max(0.0, plan_total - t.insurance_payments))
    return {"patient_balance_due": _r2(t.patient_balance_due), "insurance_paid": _r2(t.insurance_payments),
            "plan_rate_total": plan_total, "max_you_should_owe": max_owe,
            "overcharge": _r2(max(0.0, t.patient_balance_due - max_owe))}


def _totals(lines, coverage):
    priced = [l for l in lines if l.get("benchmarks") and l["benchmarks"]["target"] is not None]
    stay = next((l for l in priced if l["kind"] == "stay"), None)
    # An inpatient stay is paid as one case rate: compare it to total charges instead of adding up lines.
    counted = [stay] if stay else priced
    if stay:
        for l in lines:
            if l["kind"] == "line":
                l["part_of_stay"] = True
    billed = sum(l["benchmarks"]["billed"] for l in counted)
    target = sum(l["benchmarks"]["target"] for l in counted)
    savings = [l["benchmarks"]["potential_savings"] for l in counted if l["benchmarks"]["potential_savings"] is not None]
    all_billed = sum(l["billed"] for l in lines if l["kind"] == "line")
    return {
        "billed_all_lines": _r2(all_billed),
        "lines_priced": len([l for l in priced if l["kind"] == "line"]),
        "lines_not_priced": [l["ref"] for l in lines if l["kind"] == "line" and l not in priced
                             and l.get("state") != "part_of_stay"],
        "compared_billed": _r2(billed),
        "compared_target": _r2(target),
        "potential_savings": _r2(sum(savings)) if savings else None,
        "basis": "inpatient_case_rate" if stay else "sum_of_lines",
        "target_bases": sorted({l["benchmarks"]["target_basis"] for l in counted}),
        "coverage": coverage,
    }
