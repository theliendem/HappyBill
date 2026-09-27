"""Answer the analysis's open questions without asking the user.

Coverage and bill type are inferred by rules. Hospital, insurer, plan and ambiguous line codes are chosen
by the model, but only from the candidates the database returned (or "none"), so every price still comes
from a database lookup. Every automatic decision is returned as an assumption with its reason. Without a
model, conservative rules decide instead.
"""
import json
import re

from pydantic import BaseModel

from mediator.codes import em_level, parse_printed
from mediator.text import bill_level

from . import llm
from .analyze import _item_by_price, analyze
from .schemas import Choices, LineChoice

MAX_ROUNDS = 3


class Decision(BaseModel):
    id: str
    option: str
    reason: str


class Decisions(BaseModel):
    decisions: list[Decision]


SYSTEM = """\
You finish the analysis of a medical bill. For each open question, choose one option by its id, using \
the bill details given. Rules:
- hospital / payer: choose a candidate only if it is clearly the same organization as on the bill \
(allowing for legal vs brand names); otherwise choose "none". A wrong choice would quote another \
organization's prices.
- plan: choose the plan that matches the plan name on the bill; if the bill names no plan or none \
matches, choose "none".
- line: choose the code the hospital most likely billed for that line's description (and printed code, \
if any, allowing for misreads). Choose "none" only if no option fits.
Give a short reason for each, naming codes or organizations, never option ids. Return JSON only."""


# ---------------- rules ----------------

def _coverage_rule(bill):
    ins = bill.insurance
    if ins.payer_name or bill.totals.insurance_payments is not None or any(
            l.insurance_paid is not None for l in bill.line_items):
        return "insured", "An insurer or insurance payment appears on the bill."
    return "uninsured_self_pay", "No insurer or insurance payment appears on the bill."


def _bill_type_rule(bill):
    name = (bill.provider.billing_provider_name or "").lower()
    if bill.encounter.drg_code or any(l.revenue_code for l in bill.line_items) or re.search(r"hospital|medical center", name):
        return "institutional", "The bill has revenue codes, a DRG or a hospital name."
    return "professional", "No revenue codes, DRG or hospital name on the bill."


def _options(p):
    """[(option_id, label, choice_value)] for one pending item; choice_value None = 'none'."""
    out = []
    for i, c in enumerate(p.get("candidates") or [], 1):
        if p["kind"] == "hospital":
            value = {"org_key": c["org_key"]} if c.get("org_key") else {"provider_tin": c.get("provider_tin")}
            label = c.get("name")
        elif p["kind"] == "payer":
            value, label = {"payer_key": c["payer_key"]}, c.get("name")
        elif p["kind"] == "plan":
            value, label = {"plan_key": c["plan_key"]}, c.get("name")
        else:
            value = {"code": c["code"], "code_type": c["code_type"], "hospital_item_id": c.get("hospital_item_id")}
            desc = c.get("official_description") or (c.get("hospital_descriptions") or [""])[0]
            label = f"{c['code']}: {desc}"
        out.append((str(i), label, value))
    return out


def _rule_option(p, options):
    """Conservative pick when no model is available."""
    cands = p.get("candidates") or []
    if p["kind"] in ("hospital", "payer"):
        scores = [c.get("score") or 0 for c in cands]
        if scores and scores[0] >= 0.6 and (len(scores) == 1 or scores[0] - scores[1] >= 0.1):
            return "1", "Closest name match in the price data."
        return "none", "No candidate clearly matches the name on the bill."
    if p["kind"] == "plan":
        return "none", "Plan not identified; using all of this insurer's plans."
    agrees = [i for i, c in enumerate(cands, 1) if c.get("verdict") == "agrees"]
    if agrees:
        return str(agrees[0]), "Best description match."
    return "none", "No code clearly matches the description."


def _apply(p, value, choices):
    kind = p["kind"]
    if kind == "line":
        choices.lines[p["ref"]] = LineChoice(skip=True) if value is None else LineChoice(**value)
    elif value is None:
        setattr(choices, {"hospital": "org_key", "payer": "payer_key", "plan": "plan_key"}[kind], "none")
    else:
        for k, v in value.items():
            setattr(choices, k, v)


def _price_match(p, options, bill, org_key):
    """A line whose billed amount equals exactly one candidate's list price at this hospital is that code."""
    line = next((l for l in bill.line_items if l.ref == p.get("ref")), None)
    if not org_key or not line:
        return None
    hits = []
    for o, label, value in options:
        item = _item_by_price({"code": value["code"], "code_type": value["code_type"], "modifiers": line.modifiers},
                              org_key, line)
        if item:
            hits.append((o, label, dict(value, hospital_item_id=item)))
    return hits[0] if len(hits) == 1 else None


def _bill_context(bill):
    return {
        "provider": bill.provider.model_dump(exclude_none=True),
        "insurance": bill.insurance.model_dump(exclude_none=True),
        "document_type": bill.document.document_type,
        "setting": bill.encounter.setting,
        "lines": [{"ref": l.ref, "code_as_printed": l.code_as_printed, "description": l.description_as_printed,
                   "revenue_code": l.revenue_code, "charge": l.charge_amount} for l in bill.line_items],
    }


def _decide(bill, pending, choices, assumptions, org_key):
    questions, by_id = [], {}
    for p in pending:
        qid = f"line:{p['ref']}" if p["kind"] == "line" else p["kind"]
        options = _options(p)
        if p["kind"] == "line":
            hit = _price_match(p, options, bill, org_key)
            if hit:
                o, label, value = hit
                _apply(p, value, choices)
                assumptions.append({"kind": "line", "ref": p["ref"], "question": p["question"],
                                    "about": p.get("description"), "chose": label, "by": "rule",
                                    "reason": "The billed amount equals this code's list price at this hospital."})
                continue
        by_id[qid] = (p, options)
        questions.append({"id": qid, "question": p["question"],
                          "about": p.get("description") or p.get("found"), "printed_code": p.get("code_as_printed"),
                          "why_open": p.get("reason") or p.get("note"),
                          "options": [{"id": o, "label": label} for o, label, _ in options] + [{"id": "none", "label": "none of these"}]})
    if not by_id:
        return
    decided, by = {}, "model"
    try:
        result = llm.structured([
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps({"bill": _bill_context(bill), "questions": questions}, default=str)}],
            Decisions)
        decided = {d.id: (d.option, d.reason) for d in result.decisions}
    except (llm.LLMUnavailable, llm.LLMBadOutput):
        by = "rule"
    for qid, (p, options) in by_id.items():
        option, reason = decided.get(qid, (None, None))
        valid = {o for o, _, _ in options} | {"none"}
        source = by
        if option not in valid:  # missing or invented answer: fall back to the rule
            option, reason = _rule_option(p, options)
            source = "rule"
        label, value = next(((label, value) for o, label, value in options if o == option), ("none", None))
        _apply(p, value, choices)
        assumptions.append({"kind": p["kind"], "ref": p.get("ref"), "question": p["question"],
                            "about": p.get("description") or p.get("found"), "chose": label,
                            "reason": reason, "by": source})


def auto_analyze(bill, choices=None):
    """analyze() that answers its own open questions. Returns the complete analysis plus `assumptions`."""
    choices = choices.model_copy(deep=True) if choices else Choices()
    assumptions = []
    for _ in range(MAX_ROUNDS):
        result = analyze(bill, choices)
        if result["status"] == "complete":
            break
        pending = result["pending"]
        for p in pending:
            if p["kind"] == "coverage":
                choices.coverage_status, reason = _coverage_rule(bill)
                assumptions.append({"kind": "coverage", "question": p["question"], "chose": choices.coverage_status,
                                    "reason": reason, "by": "rule"})
            elif p["kind"] == "bill_type":
                choices.billing_class, reason = _bill_type_rule(bill)
                assumptions.append({"kind": "bill_type", "question": p["question"], "chose": choices.billing_class,
                                    "reason": reason, "by": "rule"})
        rest = [p for p in pending if p["kind"] not in ("coverage", "bill_type")]
        if rest:
            org_key = result["hospital"].get("org_key") if result["hospital"].get("status") in ("matched", "likely") else None
            _decide(bill, rest, choices, assumptions, org_key)
    else:
        result = analyze(bill, choices)
        if result["status"] != "complete":  # still open after MAX_ROUNDS: leave the rest out
            for p in result["pending"]:
                if p["kind"] in ("hospital", "payer", "plan", "line"):
                    _apply(p, None, choices)
            result = analyze(bill, choices)
    assumed_lines = {a["ref"] for a in assumptions if a["kind"] == "line" and a["chose"] != "none"}
    by_ref = {l.ref: l for l in bill.line_items}
    for line in result.get("lines", []):
        if line["ref"] in assumed_lines and line.get("state") == "user_confirmed":
            line["state"] = "assumed"
            printed = parse_printed(by_ref[line["ref"]].code_as_printed) if line["ref"] in by_ref else None
            code = line["selected"]["code"]
            if printed and printed[0] == code:
                line["state"] = "printed_kept"  # the bill's own code, kept despite the open question
                for a in assumptions:
                    if a["kind"] == "line" and a["ref"] == line["ref"]:
                        a["printed_kept"] = True
                level = bill_level(line["description"])
                if level and em_level(code) and level != em_level(code):
                    line["level_mismatch"] = {"description_level": level, "billed_code_level": em_level(code),
                                              "code_for_description_level": code[:-1] + str(level)}
    result["assumptions"] = assumptions
    return result
