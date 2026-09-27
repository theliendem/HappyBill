"""Ready-to-render `display` block for the results screen.

Everything the website shows comes from here, already worded and formatted: the hero stat, one card per
line with a friendly name, badges and price context, findings, the script, the letter, what was assumed,
and where the prices come from. Numbers are copied from the analysis, never computed from scratch beyond
simple totals and percentages.
"""
import re
from collections import Counter
from datetime import datetime

from mediator import db

from .analyze import amount_owed


def money(x):
    return f"${x:,.2f}"

FAIR_LABELS = {
    "hospital_cash_price": "Hospital's own cash price",
    "your_plan_rate": "Your plan's negotiated rate",
    "typical_commercial_insurer_rate": "What insurers typically pay",
    "insurer_rate_this_provider": "What insurers pay this provider",
    "market_rate": "What insurers typically pay other providers",
}

# Short, familiar names for very common codes; everything else uses the official plain-English description.
FRIENDLY_NAMES = {
    "36415": "Blood draw",
    "85025": "Complete blood count (CBC)",
    "85027": "Complete blood count (CBC)",
    "80053": "Comprehensive metabolic panel",
    "80048": "Basic metabolic panel",
    "93005": "EKG (electrocardiogram)",
    "93000": "EKG (electrocardiogram)",
    "96374": "IV push injection",
    "J2405": "Ondansetron (anti-nausea drug)",
    "99283": "Emergency room visit, level 3",
    "99284": "Emergency room visit, level 4",
    "99285": "Emergency room visit, level 5",
    "99212": "Office visit, level 2",
    "99213": "Office visit, level 3",
    "99214": "Office visit, level 4",
    "99215": "Office visit, level 5",
}

_KEEP_UPPER = {"MRI", "CT", "EKG", "ECG", "ED", "ER", "IV", "MG", "ML", "DRG", "MCC", "CC", "CBC", "PET", "ICU",
               "PT", "UW", "WPS", "HMO", "PPO", "POS", "EPO"}


def _sentence_case(text):
    """'MAJOR HIP AND KNEE JOINT' -> 'Major hip and knee joint' (keeps MRI, CT, ...)."""
    if not text or not text.isupper():
        return text
    words = [w if w.strip(".,()/-") in _KEEP_UPPER else w.lower() for w in text.split()]
    out = " ".join(words)
    return out[:1].upper() + out[1:]


def _clean_bill_text(text):
    text = re.sub(r"^(HB|PB)-\s*", "", text or "", flags=re.I)
    return _sentence_case(text.strip())


def _short(text, limit=60):
    text = re.sub(r"\s*\([^)]*\)", "", text or "").strip()
    for sep in (", ", " or ", " without ", " with "):
        if len(text) > limit and sep in text:
            text = text.split(sep)[0]
    return text


def line_name(line):
    sel = line.get("selected") or {}
    code = sel.get("code")
    if line.get("kind") == "stay":
        desc = ((line.get("rate") or {}).get("code") or {}).get("description")
        return f"Inpatient stay: {_short(_sentence_case(desc), 45).lower()}" if desc else "Inpatient stay"
    if code in FRIENDLY_NAMES:
        return FRIENDLY_NAMES[code]
    official = ((line.get("rate") or {}).get("code") or {}).get("description")
    if official:
        return _short(official)
    return _clean_bill_text(line["description"])


def _date(value):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%B %-d, %Y")
        except (TypeError, ValueError):
            continue
    return value


def _sources():
    rows = db.query("SELECT source_type, publisher, last_updated_on FROM sources ORDER BY source_type")
    out = []
    for r in rows:
        kind = {"hospital_standard_charges": "hospital price list", "payer_mrf": "insurer negotiated-rate file"}.get(
            r["source_type"], r["source_type"])
        publisher = {"University of Wisconsin Hospital and Clinics Authority": "UW Health"}.get(r["publisher"], r["publisher"])
        out.append(f"{publisher} {kind} (federally required, updated {_date(r['last_updated_on'])})")
    return out


def _badges(line, duplicates):
    badges = []
    state = line.get("state")
    if line.get("level_mismatch"):
        badges.append({"text": "Visit level questioned", "tone": "warning"})
    elif state in ("confirmed", "printed_kept"):
        badges.append({"text": "Code verified", "tone": "neutral"})
    elif state in ("likely", "assumed", "user_confirmed"):
        badges.append({"text": "Matched from description", "tone": "neutral"})
    if line["ref"] in duplicates:
        badges.append({"text": "Possible duplicate", "tone": "warning"})
    b = line.get("benchmarks") or {}
    if b.get("potential_savings"):
        badges.append({"text": "Overcharged", "tone": "alert"})
    elif not b.get("target") and state not in ("part_of_stay",):
        badges.append({"text": "No published price", "tone": "muted"})
    return badges


def _price_range(b):
    if b.get("target_basis") == "market_rate":
        lo, hi = b.get("market_low"), b.get("market_high")
        if lo is None or hi is None or lo == hi:
            return None
        return {"min": lo, "max": hi, "text": f"Most insurer rates at other providers fall between {money(lo)} and {money(hi)}"}
    lo, hi = b.get("all_payers_min"), b.get("all_payers_max")
    # A minimum under 2% of the maximum is a per-unit rate (e.g. per mg of a drug), not a price for the line.
    if lo is None or hi is None or lo == hi or lo < 0.02 * hi:
        return None
    return {"min": lo, "max": hi, "text": f"Insurers pay {money(lo)} – {money(hi)} for this at this hospital"}


def _line_savings(line, b, fair):
    savings = b.get("potential_savings")
    if savings is None and fair is not None and b.get("target_basis") != "your_plan_rate" and line["billed"] > fair:
        savings = round(line["billed"] - fair, 2)
    if savings is not None and b.get("patient_responsibility") is not None:
        savings = min(savings, b["patient_responsibility"])  # can't save more than you owe for the line
    return savings or None


def _line_card(line, duplicates):
    b = line.get("benchmarks") or {}
    fair = b.get("target")
    card = {
        "ref": line["ref"],
        "name": line_name(line),
        "billed_as": _clean_bill_text(line["description"]) if line["kind"] != "stay" else None,
        "code": (line.get("selected") or {}).get("code"),
        "billed": line["billed"],
        "fair_price": fair,
        "fair_label": FAIR_LABELS.get(b.get("target_basis")),
        "savings": _line_savings(line, b, fair),
        "badges": _badges(line, duplicates),
        "price_range": _price_range(b),
        "note": None,
    }
    if line.get("level_mismatch"):
        m = line["level_mismatch"]
        card["note"] = (f"Described as a level {m['description_level']} visit but billed as a level "
                        f"{m['billed_code_level']} code.")
    elif b.get("max_you_should_owe") is not None and b.get("patient_responsibility") is not None \
            and b["patient_responsibility"] > b["max_you_should_owe"]:
        card["note"] = (f"You're asked to pay {money(b['patient_responsibility'])}; the most you should owe is "
                        f"{money(b['max_you_should_owe'])}.")
    return card


def _hero(analysis, headline, bill):
    t = analysis["totals"]
    billed, fair, savings = t.get("compared_billed"), t.get("compared_target"), t.get("potential_savings")
    if not billed:  # nothing could be priced: still show what was billed
        billed, fair = t.get("billed_all_lines"), None
    bases = t.get("target_bases") or []
    basis = bases[0] if len(bases) == 1 else None
    hero = {"headline": headline, "savings": savings, "savings_label": "Potential savings",
            "billed": billed, "fair": fair, "fair_label": FAIR_LABELS.get(basis) or ("Published prices" if fair else None),
            "comparison": None}
    check = t.get("balance_check")
    stay = next((l for l in analysis["lines"] if l["kind"] == "stay" and l.get("benchmarks")), None)
    if check:
        hero["comparison"] = (f"You're asked to pay {money(check['patient_balance_due'])}; under your plan the most "
                              f"you should owe is {money(check['max_you_should_owe'])}.")
    elif stay and stay["benchmarks"].get("max_you_should_owe") is not None \
            and stay["benchmarks"].get("patient_responsibility") is not None:
        b = stay["benchmarks"]
        hero["comparison"] = (f"You're asked to pay {money(b['patient_responsibility'])}; under your plan the most "
                              f"you should owe is {money(b['max_you_should_owe'])}.")
    elif billed and fair and billed > fair:
        pct = round((billed / fair - 1) * 100)
        label = hero["fair_label"].lower().replace("hospital's own", "the hospital's own")
        hero["comparison"] = f"You were billed {pct}% more than {label}."
    if savings is None and billed and fair and billed > fair:
        # e.g. insured, claim not processed yet: show the gap the insurer should remove instead
        hero["savings"] = round(billed - fair, 2)
        if basis == "your_plan_rate":
            hero["savings_label"] = "Above your plan's negotiated rates"
    owed = amount_owed(bill)
    if owed is not None and hero["savings"] is not None and hero["savings"] > owed:
        hero["savings"] = round(owed, 2)  # can't save more than you're asked to pay
    return hero


def _bottom_line(analysis, hero):
    """The results screen's main numbers (computed in the analysis, so they always add up), plus wording."""
    t = analysis["totals"]
    b = t.get("bottom_line")
    if not b:
        return {"title": "We couldn't find published prices for this bill", "billed": t.get("billed_all_lines"),
                "fair": None, "fair_label": None, "insurance_paid": None, "should_pay": None, "asked": None,
                "savings": None, "note": None}
    label = hero["fair_label"] or "Published prices"
    if b["savings"]:
        title = f"You could save {money(b['savings'])}"
    elif b["savings"] is None and b["above_fair"]:
        title = f"You were billed {money(b['above_fair'])} more than {label.lower()}"
    else:
        title = "Your bill looks right"
    paid, should = b["insurance_paid"], b["should_pay"]
    # Show "fair price - insurance paid = you should pay" only when it adds up exactly.
    shows_math = None not in (paid, should) and paid > 0 and abs(b["fair"] - paid - should) < 0.01
    unchecked = len(t.get("lines_not_priced") or [])
    return {
        "title": title,
        "billed": b["billed"],
        "fair": b["fair"],
        "fair_label": label,
        "insurance_paid": paid if shows_math else None,
        "should_pay": should,
        "asked": b["asked"],
        "savings": b["savings"] if b["savings"] is not None else None,
        "note": (f"{unchecked} charge{'s' if unchecked != 1 else ''} we couldn't check "
                 f"{'are' if unchecked != 1 else 'is'} counted at the billed price.") if unchecked else None,
    }


def _assumption_text(a):
    """Plain-English line for each automatic decision. Uses our own wording, not the model's free text."""
    about = f"“{_clean_bill_text(a['about'])}”" if a.get("about") else None
    chose = str(a.get("chose") or "")
    if a["kind"] == "coverage":
        return "Treated this bill as " + ("insured." if chose == "insured" else "self-pay (no insurance).")
    if a["kind"] == "bill_type":
        return "Treated this as a " + ("hospital bill." if chose == "institutional" else "doctor/clinic bill.")
    if chose == "none":
        return {"hospital": f"Couldn't find {about or 'this provider'} in our price data.",
                "payer": f"Couldn't find {about or 'this insurer'} in our price data.",
                "plan": "Plan not identified, so we compared against all of this insurer's plans.",
                "line": f"Left out {about or 'a line'}: no matching code."}.get(a["kind"], "")
    if a["kind"] == "line":
        code = chose.split(":")[0]
        if a.get("printed_kept"):
            return f"{about}: used the code printed on the bill ({code})."
        if a["by"] == "rule" and a.get("reason"):
            return f"{about}: matched to code {code} ({a['reason'][:1].lower() + a['reason'][1:].rstrip('.')})."
        return f"{about}: matched to code {code} from its description."
    what = {"hospital": "Identified the provider as", "payer": "Identified the insurer as",
            "plan": "Identified the plan as"}.get(a["kind"], "Chose")
    return f"{what} {chose}."


def _paragraphs(text):
    parts = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    return parts or [text]


def provider_name(analysis, bill):
    """The name on the bill ('UW Health University Hospital'), not the legal entity name."""
    return bill.provider.billing_provider_name or _sentence_case(analysis["hospital"].get("name") or "") or None


def build_display(analysis, tactics, plan, bill):
    lines = [l for l in analysis["lines"] if l["kind"] in ("line", "stay")]
    keys = Counter((l["description"].strip().lower(), l["billed"]) for l in lines if l["kind"] == "line")
    duplicates = {l["ref"] for l in lines if l["kind"] == "line" and keys[(l["description"].strip().lower(), l["billed"])] > 1}
    shown = [l for l in lines if l.get("state") != "part_of_stay"]
    in_stay = [l for l in lines if l.get("state") == "part_of_stay"]

    provider = provider_name(analysis, bill)
    if analysis["coverage"] == "uninsured_self_pay":
        insurance = "Self-pay (no insurance)"
    else:
        payer = bill.insurance.payer_name or (analysis.get("payer") or {}).get("name")
        plan_name = bill.insurance.plan_name or (analysis.get("plan") or {}).get("name")
        insurance = " · ".join(x for x in (payer, f"{plan_name} plan" if plan_name else None) if x) or None

    hero = _hero(analysis, plan.headline, bill)
    return {
        "bottom_line": _bottom_line(analysis, hero),
        "hero": hero,
        "summary": plan.situation,
        "provider": provider,
        "insurance": insurance,
        "lines": [_line_card(l, duplicates) for l in shown],
        "included_in_stay": ({"title": "Included in the stay's single case rate",
                              "items": [{"name": _clean_bill_text(l["description"]), "billed": l["billed"]} for l in in_stay]}
                             if in_stay else None),
        "findings": [{"title": t["title"], "detail": t["why"], "action": t["step"]} for t in tactics],
        "steps": plan.steps,
        "call_script": _paragraphs(plan.call_script),
        "letter": {"subject": plan.letter_subject, "body": _paragraphs(plan.letter)},
        "assumptions": ([f"{analysis['hospital'].get('not_found') or 'This provider'} isn't in our price data yet, "
                         "so we compared each billing code with UW Health's published prices."]
                        if analysis["hospital"].get("matched_by") == "demo_default" else [])
                       + [_assumption_text(a) for a in analysis.get("assumptions", [])],
        "sources": _sources(),
        "disclaimer": "Prices come from the hospital's and insurer's own federally required price files. "
                      "ClarityBill is not legal or financial advice.",
    }
