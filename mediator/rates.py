"""
Tool 2: get_negotiated_rate. Exact lookups only, using ids that resolve returned. Unknown ids are
rejected; zero rows means "no published rate" and never falls back to fuzzy matching.

Two sources can answer for the same line:
  hospital price list (e.g. UW Health standard charges): list price, cash price, and every payer/plan's
      rate for the chargemaster item. Facility charges only.
  insurer file (e.g. WPS in-network MRF): the insurer's contract rates for the provider groups that
      include this provider (narrowed by NPI when given).
"""
import re
from statistics import median

from parsers.common import norm_code

from . import db
from .codes import CODE_TYPES

HOSPITAL_FILE, INSURER_FILE = "hospital_price_list", "insurer_file"
ITEM_SAMPLE = 25  # codes like C1713 (implants) cover thousands of chargemaster items
BOILERPLATE_NOTE = "Zero final payments for the item or service in the 15 months prior to posting the file."


class InvalidIds(ValueError):
    def __init__(self, details):
        super().__init__("; ".join(details))
        self.details = details


def _f(x):
    return None if x is None else round(float(x), 2)


def _digits(s):
    d = "".join(ch for ch in str(s or "") if ch.isdigit())
    return d or None


_VALIDATE_SQL = """
SELECT
  (SELECT description FROM billing_codes WHERE code = %(c)s AND code_type = %(t)s) AS description,
  EXISTS (SELECT 1 FROM billing_codes WHERE code = %(c)s AND code_type = %(t)s)
    OR EXISTS (SELECT 1 FROM item_codes WHERE code = %(c)s AND code_type = %(t)s) AS code_ok,
  (%(org)s::text IS NULL OR EXISTS (SELECT 1 FROM organizations WHERE org_key = %(org)s)) AS org_ok,
  (%(tin)s::text IS NULL OR EXISTS (SELECT 1 FROM provider_groups WHERE tin = %(tin)s)) AS tin_ok,
  (%(payer)s::text IS NULL OR EXISTS (SELECT 1 FROM payers WHERE payer_key = %(payer)s)) AS payer_ok,
  (SELECT payer_key FROM plans WHERE plan_key = %(plan)s) AS plan_payer,
  (SELECT hi.org_key FROM hospital_items hi JOIN item_codes ic USING (item_id)
    WHERE hi.item_id = %(item)s AND ic.code = %(c)s AND ic.code_type = %(t)s LIMIT 1) AS item_org
"""

# Start from the few thousand prices for this code, then check providers per rate set (LATERAL);
# letting the planner start from the 60M-row rate_set_providers side is ~500x slower.
_INSURER_SQL = """
WITH p AS MATERIALIZED (
  SELECT * FROM prices
  WHERE code = %(c)s AND code_type = %(t)s AND rate_set_id IS NOT NULL
    AND (%(payer)s::text IS NULL OR payer_key = %(payer)s)
    AND (%(bc)s::text IS NULL OR billing_class = %(bc)s)
    AND CASE WHEN cardinality(%(mods)s::text[]) = 0 THEN modifiers IS NULL
             ELSE modifiers @> %(mods)s::text[] AND modifiers <@ %(mods)s::text[] END
)
SELECT p.payer_key, p.plan_key, p.billing_class, p.setting, p.modifiers, p.rate_kind, p.methodology,
       p.negotiated_dollar, p.negotiated_percentage, p.service_codes, p.expiration_date, p.notes,
       array_agg(DISTINCT pg.file_group_id) AS provider_groups,
       array_agg(DISTINCT pg.business_name) AS provider_names
FROM p
CROSS JOIN LATERAL (SELECT r.provider_group_id FROM rate_set_providers r
                    WHERE r.rate_set_id = p.rate_set_id AND r.provider_group_id = ANY(%(gids)s)
                    OFFSET 0) rsp  -- OFFSET 0 stops Postgres from flattening this into a hash join
JOIN provider_groups pg ON pg.provider_group_id = rsp.provider_group_id
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12
ORDER BY p.negotiated_dollar NULLS LAST
"""


def _line_equivalent(kind, dollar, pct, units, billed, gross):
    """What this rate works out to for the whole bill line, and how that was computed."""
    if kind == "dollar" and dollar is not None:
        return dollar * units, "dollar_rate"
    if kind == "percent_of_charges":
        if pct is not None and billed is not None:
            return pct / 100 * billed, "percent_of_your_billed_amount"
        if dollar is not None:
            return dollar * units, "hospital_computed_from_list_price"
        if pct is not None and gross is not None:
            return pct / 100 * gross * units, "percent_of_hospital_list_price"
        return None, "not_convertible_see_notes"
    if kind == "per_diem":
        return None, "per_day_not_line_total"
    if kind == "per_unit":
        return None, "per_anesthesia_unit_not_line_total"
    return None, "algorithm_see_notes"


def _rate_view(r, source, units, billed, gross):
    dollar, pct = _f(r["negotiated_dollar"]), _f(r["negotiated_percentage"])
    total, basis = _line_equivalent(r["rate_kind"], dollar, pct, units, billed, gross)
    out = {
        "source": source, "payer_key": r["payer_key"], "plan_key": r["plan_key"],
        "billing_class": r["billing_class"], "setting": r["setting"], "rate_kind": r["rate_kind"],
        "methodology": r["methodology"], "negotiated_dollar": dollar, "negotiated_percentage": pct,
        "line_total_equivalent": _f(total), "basis": basis,
    }
    notes = r.get("notes")
    if notes and notes != BOILERPLATE_NOTE:
        out["notes"] = notes
    return out


def _item_view(i, units):
    return {
        "hospital_item_id": i["item_id"], "description": i["description"], "cdm_code": i["cdm_code"],
        "modifiers": i["modifiers"], "setting": i["setting"],
        "list_price": _f(i["gross_charge"]), "cash_price": _f(i["discounted_cash"]),
        "list_price_x_units": _f(float(i["gross_charge"]) * units) if i["gross_charge"] is not None else None,
        "cash_price_x_units": _f(float(i["discounted_cash"]) * units) if i["discounted_cash"] is not None else None,
    }


def _market(item, rows, units):
    """Every payer/plan's rate for one chargemaster item, as dollars for this line."""
    amounts = sorted((float(r["negotiated_dollar"]) * units, r["plan_key"]) for r in rows
                     if r["item_id"] == item["item_id"] and r["negotiated_dollar"] is not None
                     and r["rate_kind"] in ("dollar", "percent_of_charges"))
    if not amounts:
        return None
    vals = [a for a, _ in amounts]
    # Medicare Advantage / Medicaid plans pay near-Medicare rates; commercial plans are the fairer
    # benchmark for a self-pay patient.
    commercial = [a for a, p in amounts if "medicare" not in p and "medicaid" not in p]
    return {
        "hospital_item_id": item["item_id"], "description": item["description"],
        "plans_with_dollar_rates": len(vals),
        "min": _f(vals[0]), "median": _f(median(vals)), "max": _f(vals[-1]),
        "commercial_plans": len(commercial),
        "commercial_median": _f(median(commercial)) if commercial else None,
        "lowest": [{"plan_key": p, "line_total": _f(a)} for a, p in amounts[:3]],
        "highest": {"plan_key": amounts[-1][1], "line_total": _f(amounts[-1][0])},
    }


def get_negotiated_rate(req):
    code, ctype = norm_code((req.get("code") or "").strip(), req.get("code_type") or "")
    mods = sorted({m.strip().upper() for m in req.get("modifiers") or [] if m and m.strip()})
    org_key = req.get("org_key") or None
    tin, npi = _digits(req.get("provider_tin")), _digits(req.get("provider_npi"))
    item_id = req.get("hospital_item_id")
    payer_key, plan_key = req.get("payer_key") or None, req.get("plan_key") or None
    billing_class = req.get("billing_class") or None
    units = float(req.get("units") or 1)
    billed = float(req["billed_amount"]) if req.get("billed_amount") is not None else None
    warnings = []

    # ---- validate: ids only ----
    v = db.one(_VALIDATE_SQL, {"c": code, "t": ctype, "org": org_key, "tin": tin, "payer": payer_key,
                               "plan": plan_key, "item": item_id})
    errors = []
    if ctype not in CODE_TYPES:
        errors.append(f"code_type '{ctype}' is not one of {', '.join(CODE_TYPES)}")
    elif not v["code_ok"]:
        errors.append(f"code {code} ({ctype}) is not in the database")
    if not org_key and not tin:
        errors.append("provide org_key or provider_tin from resolve")
    if not v["org_ok"]:
        errors.append(f"unknown org_key '{org_key}'")
    if not v["tin_ok"]:
        errors.append(f"no provider with tax ID {tin} in any insurer file")
    if not v["payer_ok"]:
        errors.append(f"unknown payer_key '{payer_key}'")
    if plan_key:
        if v["plan_payer"] is None:
            errors.append(f"unknown plan_key '{plan_key}'")
        elif payer_key and v["plan_payer"] != payer_key:
            errors.append(f"plan_key '{plan_key}' belongs to payer '{v['plan_payer']}', not '{payer_key}'")
        else:
            payer_key = v["plan_payer"]
    if item_id is not None and v["item_org"] != org_key:
        errors.append(f"hospital_item_id {item_id} is not a {code} item at '{org_key}'")
    if errors:
        raise InvalidIds(errors)
    if billing_class is None:
        warnings.append("billing_class unknown: facility and professional rates are both shown; they are different charges.")

    plan_sources = {r["source_type"] for r in db.query(
        "SELECT source_type FROM plan_sources WHERE plan_key = %s", (plan_key,))} if plan_key else set()

    # ---- hospital price list ----
    items, hospital_rows, modifier_match = [], [], "exact"
    if org_key and billing_class != "professional":
        items = db.query("""SELECT hi.* FROM item_codes ic JOIN hospital_items hi USING (item_id)
                            WHERE ic.code = %s AND ic.code_type = %s AND hi.org_key = %s ORDER BY hi.item_id""",
                         (code, ctype, org_key))
        if item_id is not None:
            items = [i for i in items if i["item_id"] == item_id]
        exact = [i for i in items if sorted(i["modifiers"] or []) == mods]
        if not exact and mods:
            exact = [i for i in items if not i["modifiers"]]
            if exact:
                modifier_match = "base_rate_fallback"
        items = exact
        if len(items) > 1:
            warnings.append(f"{len(items)} chargemaster items share code {code}; pass hospital_item_id from resolve "
                            "to narrow to the one on the bill.")
        if items:
            sample = items[:ITEM_SAMPLE]
            hospital_rows = db.query("""SELECT * FROM prices WHERE item_id = ANY(%s)""",
                                     ([i["item_id"] for i in sample],))
    elif org_key:
        warnings.append("Hospital price list covers facility charges only; skipped for a professional (physician) bill.")

    gross = {i["item_id"]: (float(i["gross_charge"]) if i["gross_charge"] is not None else None) for i in items}
    single_gross = next(iter(gross.values())) if len(gross) == 1 else None

    # ---- insurer files ----
    groups = db.query("""SELECT pg.provider_group_id, pg.file_group_id, pg.business_name, pg.tin,
                                (SELECT count(*) FROM provider_group_npis n
                                  WHERE n.provider_group_id = pg.provider_group_id) AS npi_count
                         FROM provider_groups pg WHERE pg.org_key = %s OR pg.tin = %s
                         ORDER BY pg.file_group_id""", (org_key, tin))
    npi_applied = False
    if npi and groups:
        hit = {r["provider_group_id"] for r in db.query(
            "SELECT provider_group_id FROM provider_group_npis WHERE npi = %s AND provider_group_id = ANY(%s)",
            (npi, [g["provider_group_id"] for g in groups]))}
        if hit:
            groups, npi_applied = [g for g in groups if g["provider_group_id"] in hit], True
        else:
            warnings.append(f"NPI {npi} is not listed in this provider's insurer contracts; showing all of its contracts.")
    insurer_rows = []
    if groups:
        params = {"c": code, "t": ctype, "gids": [g["provider_group_id"] for g in groups], "payer": payer_key,
                  "bc": billing_class, "mods": mods}
        insurer_rows = db.query(_INSURER_SQL, params)
        if not insurer_rows and mods:
            insurer_rows = db.query(_INSURER_SQL, dict(params, mods=[]))
            if insurer_rows:
                modifier_match = "base_rate_fallback"
    if modifier_match == "base_rate_fallback":
        warnings.append(f"No rate is published specifically for modifier(s) {', '.join(mods)}; showing the base rate.")

    # ---- the requested payer/plan's rates ----
    rates = []
    hosp_plan = plan_key if "hospital_standard_charges" in plan_sources else None
    ins_plan = plan_key if "payer_mrf" in plan_sources else None
    payer_hospital_rows = [r for r in hospital_rows if payer_key and r["payer_key"] == payer_key]
    wanted = [r for r in payer_hospital_rows if hosp_plan is None or r["plan_key"] == hosp_plan]
    if len(items) == 1:
        for r in wanted:
            view = _rate_view(r, HOSPITAL_FILE, units, billed, gross.get(r["item_id"]))
            view["hospital_item_id"] = r["item_id"]
            if r["median_paid"] is not None:
                view["actually_paid"] = {"median": _f(r["median_paid"]), "p10": _f(r["p10_paid"]),
                                         "p90": _f(r["p90_paid"]), "claims": r["paid_count"]}
            rates.append(view)
    else:
        # Several items share the code: one row per plan/contract term, dollar range across items.
        grouped = {}
        for r in wanted:
            k = (r["plan_key"], r["rate_kind"], r["methodology"], _f(r["negotiated_percentage"]))
            grouped.setdefault(k, []).append(r)
        sampled = len(items) > ITEM_SAMPLE
        for rows in grouped.values():
            dollars = sorted({_f(r["negotiated_dollar"]) for r in rows if r["negotiated_dollar"] is not None})
            first = dict(rows[0], negotiated_dollar=dollars[0] if len(dollars) == 1 else None)
            view = _rate_view(first, HOSPITAL_FILE, units, billed, None)
            view["items"] = f"{len(rows)} of {len(items)} (sampled)" if sampled else len(rows)
            if len(dollars) > 1:
                if view["line_total_equivalent"] is None:
                    view["basis"] = "varies_by_item_pass_hospital_item_id"
                if not sampled:
                    view["dollar_range_across_items"] = [dollars[0], dollars[-1]]
            rates.append(view)
    for r in insurer_rows:
        if ins_plan and r["plan_key"] != ins_plan:
            continue
        view = _rate_view(r, INSURER_FILE, units, billed, single_gross)
        view.update(modifiers=r["modifiers"], provider_groups=r["provider_groups"],
                    provider_names=r["provider_names"], service_codes=r["service_codes"],
                    expiration_date=str(r["expiration_date"]) if r["expiration_date"] else None)
        # Cross-check: the same dollar amount in the hospital's own list pins which plan this contract is.
        if view["negotiated_dollar"] is not None:
            view["matches_hospital_list_plans"] = sorted({
                h["plan_key"] for h in payer_hospital_rows if h["negotiated_dollar"] is not None
                and abs(float(h["negotiated_dollar"]) - view["negotiated_dollar"]) < 0.01})
            if hosp_plan and view["matches_hospital_list_plans"]:
                view["consistent_with_requested_plan"] = hosp_plan in view["matches_hospital_list_plans"]
        rates.append(view)
    if plan_key and insurer_rows and not ins_plan:
        networks = sorted({r["plan_key"] for r in insurer_rows})
        warnings.append(f"The insurer's file lists network(s) {', '.join(networks)} for these contracts and does not "
                        f"say which plan each belongs to; matches_hospital_list_plans shows which ones line up with {plan_key}.")
    if plan_key and not plan_sources:
        warnings.append(f"Plan {plan_key} has no rates in any loaded file.")

    # ---- all payers at this hospital (hospital price list) ----
    market = [m for m in (_market(i, hospital_rows, units) for i in items) if m] if len(items) <= 3 else []

    comparison = None
    if billed is not None:
        totals = [r["line_total_equivalent"] for r in rates if r["line_total_equivalent"] is not None
                  and r.get("consistent_with_requested_plan") is not False]
        comparison = {"billed_amount": _f(billed)}
        if totals and payer_key:
            comparison.update(your_plan_lowest=min(totals), your_plan_highest=max(totals),
                              billed_minus_your_plan_lowest=_f(billed - min(totals)))
        if len(items) == 1:
            iv = _item_view(items[0], units)
            if iv["cash_price_x_units"] is not None:
                comparison.update(cash_price=iv["cash_price_x_units"],
                                  billed_minus_cash_price=_f(billed - iv["cash_price_x_units"]))
            if iv["list_price_x_units"] is not None:
                comparison["list_price"] = iv["list_price_x_units"]
        if len(market) == 1:
            comparison.update(all_payers_median=market[0]["median"], all_payers_min=market[0]["min"],
                              billed_minus_all_payers_median=_f(billed - market[0]["median"]))

    found = bool(rates or items or insurer_rows)
    if not found:
        warnings.append("No published rate for this code at this provider. Do not estimate one.")
    return {
        "found": found,
        "code": {"code": code, "code_type": ctype, "description": v["description"]},
        "modifiers": mods,
        "modifier_match": modifier_match,
        "provider": {
            "org_key": org_key, "provider_tin": tin, "npi_filter_applied": npi_applied,
            "insurer_file_groups": [{"group": g["file_group_id"], "name": g["business_name"], "tin": g["tin"],
                                     "npi_count": g["npi_count"]} for g in groups[:10]],
        },
        "request": {"payer_key": payer_key, "plan_key": plan_key, "billing_class": billing_class,
                    "units": units, "billed_amount": billed},
        "hospital_price_list": [_item_view(i, units) for i in items[:5]],
        "hospital_items_matching": len(items),
        "rates": rates,
        "all_payers_at_this_hospital": market,
        "comparison": comparison,
        "warnings": warnings,
    }


# Codes that name a category, not a service: a price for one tells nothing about another.
_CATCH_ALL = re.compile(r"\b(unlisted|unclassified|not otherwise|miscellaneous|noc)\b", re.I)

_MARKET_SQL = """
SELECT count(*) AS n, count(DISTINCT source_id) AS sources,
       percentile_cont(0.1) WITHIN GROUP (ORDER BY negotiated_dollar) AS p10,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY negotiated_dollar) AS median,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY negotiated_dollar) AS p90
FROM prices
WHERE code = %(c)s AND code_type = %(t)s AND negotiated_dollar > 0
  AND rate_kind IN ('dollar', 'percent_of_charges')
  AND (%(bc)s::text IS NULL OR billing_class = %(bc)s)
  AND CASE WHEN cardinality(%(mods)s::text[]) = 0 THEN coalesce(cardinality(modifiers), 0) = 0
           ELSE modifiers @> %(mods)s::text[] AND modifiers <@ %(mods)s::text[] END
  -- Medicare Advantage / Medicaid pay near-Medicare rates; commercial rates are the fairer benchmark.
  AND coalesce(plan_key, '') !~* '(medicare|medicaid)'
"""
MARKET_MIN_RATES = 5


def get_market_rate(req):
    """Typical published rate for a code across every provider in the loaded files. Used only when the
    bill's own provider isn't in our data, so the line can still be compared with what insurers pay."""
    code, ctype = norm_code((req.get("code") or "").strip(), req.get("code_type") or "")
    mods = sorted({m.strip().upper() for m in req.get("modifiers") or [] if m and m.strip()})
    units = float(req.get("units") or 1)
    desc = (db.one("SELECT description FROM billing_codes WHERE code = %s AND code_type = %s", (code, ctype))
            or {}).get("description")
    out = {"found": False, "code": {"code": code, "code_type": ctype, "description": desc}, "units": units}
    if desc and _CATCH_ALL.search(desc):
        out["reason"] = "catch_all_code"
        return out
    params = {"c": code, "t": ctype, "bc": req.get("billing_class") or None, "mods": mods}
    row = db.one(_MARKET_SQL, params)
    if row["n"] < MARKET_MIN_RATES and mods:
        row = db.one(_MARKET_SQL, dict(params, mods=[]))
    if row["n"] < MARKET_MIN_RATES:
        out["reason"] = "too_few_rates"
        return out
    out.update(found=True, rates=row["n"], sources=row["sources"],
               median=_f(row["median"] * units), p10=_f(row["p10"] * units), p90=_f(row["p90"] * units))
    return out
