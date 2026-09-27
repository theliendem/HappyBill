"""
Tool 1: resolve. Turns what is printed on a bill into exact database ids. Returns no prices.

Matching order, strongest first:
  hospital: NPI -> tax ID -> known alias -> fuzzy name (organizations, then payer-file provider names)
  payer:    alias -> exact key -> payer name contained in the bill text -> fuzzy name
  plan:     fuzzy name within the payer's plans
  code:     printed code (checked against its description) -> the AI's candidate codes, each scored
            against the hospital's chargemaster text and the official description -> fuzzy search
Fuzzy results never get picked when two candidates are close; those come back 'ambiguous' so the
user confirms.
"""
from functools import lru_cache

from . import db
from .codes import em_level, em_sibling, infer_code, parse_printed
from .text import bill_level, name_key, norm_key, set_similarity, similarity, trigrams, words

AGREE = 0.45    # description similarity at/above which a code's description agrees with the bill text
WEAK = 0.25     # below this the description contradicts the bill text
MARGIN = 0.10   # lead the best candidate needs over the runner-up to be picked without the user
NAME_LIKELY, NAME_MIN = 0.5, 0.3

DOC_CLASS = {
    "itemized_hospital_statement": "institutional",
    "ub04_facility_claim": "institutional",
    "physician_statement": "professional",
    "cms1500_professional_claim": "professional",
}
EXACT_MATCHES = ("npi", "tin", "npi_in_payer_file", "alias", "exact", "only_plan")
VARIANT_WORDS = {"medicare_advantage": {"medicare", "advantage"}, "medicaid": {"medicaid"}}


# ---------- cached reference data (small tables) ----------

@lru_cache(maxsize=1)
def _orgs():
    orgs = {r["org_key"]: r for r in db.query("SELECT * FROM organizations")}
    labels = [(r["alias"], r["org_key"]) for r in db.query("SELECT alias, org_key FROM org_aliases")]
    labels += [(o["name"].lower(), k) for k, o in orgs.items()]
    return orgs, [(a, k, trigrams(name_key(a))) for a, k in labels]


@lru_cache(maxsize=1)
def _provider_names():
    rows = db.query("SELECT DISTINCT business_name, tin FROM provider_groups "
                    "WHERE business_name IS NOT NULL AND org_key IS NULL")
    return [(r, trigrams(name_key(r["business_name"]))) for r in rows]


@lru_cache(maxsize=1)
def _payers():
    payers = {r["payer_key"]: r["name"] for r in db.query("SELECT payer_key, name FROM payers")}
    aliases = {r["alias"]: r for r in db.query("SELECT alias, payer_key, plan_key FROM payer_aliases")}
    return payers, aliases


@lru_cache(maxsize=1)
def _plans():
    rows = db.query("""
        SELECT pl.plan_key, pl.payer_key, pl.name,
               array_remove(array_agg(DISTINCT ps.source_type), NULL) AS source_types
        FROM plans pl LEFT JOIN plan_sources ps USING (plan_key)
        GROUP BY 1, 2, 3 ORDER BY 1""")
    by_payer = {}
    for r in rows:
        by_payer.setdefault(r["payer_key"], []).append(r)
    return by_payer


def reload():
    """Drop cached reference data (call after loading new files or editing aliases)."""
    for f in (_orgs, _provider_names, _payers, _plans):
        f.cache_clear()


def _digits(s):
    d = "".join(ch for ch in str(s or "") if ch.isdigit())
    return d or None


def _pick(scored):
    """scored: [(score, item)]. Returns (status, best_item_or_None)."""
    scored = sorted(scored, key=lambda x: -x[0])
    if not scored or scored[0][0] < NAME_MIN:
        return "not_found", None
    if scored[0][0] >= NAME_LIKELY and (len(scored) == 1 or scored[0][0] - scored[1][0] >= MARGIN):
        return "likely", scored[0][1]
    return "ambiguous", None


# ---------- hospital ----------

def _org_match(org, matched_by, npi=None, billing_class=None, score=None):
    orgs, _ = _orgs()
    out = {
        "status": "matched" if matched_by in EXACT_MATCHES else "likely",
        "org_key": org["org_key"], "provider_tin": None, "name": org["name"], "org_type": org["org_type"],
        "system_name": org["system_name"], "matched_by": matched_by, "provider_npi": npi,
    }
    if score is not None:
        out["score"] = round(score, 2)
    # A brand name ("UW Health") covers several billing entities; the bill type decides which one billed.
    if matched_by in ("alias", "fuzzy_name") and org["system_name"]:
        siblings = [o for o in orgs.values() if o["system_name"] == org["system_name"]]
        if len(siblings) > 1:
            want = {"professional": "physician_group", "institutional": "hospital"}.get(billing_class)
            pick = next((o for o in siblings if o["org_type"] == want), None)
            if pick and pick["org_key"] != org["org_key"]:
                out.update(org_key=pick["org_key"], name=pick["name"], org_type=pick["org_type"],
                           matched_by=matched_by + "+billing_class")
            if not want:
                out["status"] = "ambiguous"
                out["note"] = (f"'{org['system_name']}' covers several billing entities; the document type "
                               "(hospital vs physician bill) decides which one billed.")
            out["system_orgs"] = [{"org_key": o["org_key"], "name": o["name"], "org_type": o["org_type"]}
                                  for o in siblings]
    return out


def _provider_match(row, matched_by, npi=None, score=None):
    out = {
        "status": "matched" if matched_by in EXACT_MATCHES else "likely",
        "org_key": None, "provider_tin": row["tin"], "name": row["business_name"], "matched_by": matched_by,
        "provider_npi": npi,
        "note": "Known only from insurer files: no hospital price list (list price, cash price) is loaded for this provider.",
    }
    if score is not None:
        out["score"] = round(score, 2)
    return out


def resolve_hospital(h, billing_class):
    orgs, labels = _orgs()
    npi, tin = _digits(h.get("npi")), _digits(h.get("tin"))
    names = [n for n in (h.get("name"), h.get("facility_name")) if n and n.strip()]

    if npi:
        r = db.one("SELECT org_key FROM org_npis WHERE npi = %s", (npi,))
        if r:
            return _org_match(orgs[r["org_key"]], "npi", npi=npi)
        rows = db.query("""SELECT DISTINCT pg.org_key, pg.tin, pg.business_name
                           FROM provider_group_npis n JOIN provider_groups pg USING (provider_group_id)
                           WHERE n.npi = %s""", (npi,))
        org_keys = {r["org_key"] for r in rows if r["org_key"]}
        if len(org_keys) == 1:
            return _org_match(orgs[org_keys.pop()], "npi_in_payer_file", npi=npi)
        if len({r["tin"] for r in rows}) == 1:
            return _provider_match(rows[0], "npi_in_payer_file", npi)
        if rows:
            return {"status": "ambiguous", "matched_by": "npi_in_payer_file", "provider_npi": npi,
                    "candidates": [{"org_key": r["org_key"], "provider_tin": r["tin"], "name": r["business_name"]}
                                   for r in rows[:5]]}

    if tin:
        org = next((o for o in orgs.values() if o["tin"] == tin), None)
        if org:
            return _org_match(org, "tin", npi=npi)
        r = db.one("SELECT business_name, tin FROM provider_groups WHERE tin = %s LIMIT 1", (tin,))
        if r:
            return _provider_match(r, "tin", npi)

    if not names:
        return {"status": "not_given" if not (npi or tin) else "not_found", "provider_npi": npi}

    for n in names:
        key = n.strip().lower()
        for alias, org_key, _ in labels:
            if alias == key:
                return _org_match(orgs[org_key], "alias", npi=npi, billing_class=billing_class)

    best = {}  # candidate key -> (score, kind, row)
    for n in names:
        tn = trigrams(name_key(n))
        for alias, org_key, ta in labels:
            s = set_similarity(tn, ta)
            if s > best.get(("org", org_key), (0,))[0]:
                best[("org", org_key)] = (s, "org", orgs[org_key])
        for row, tr in _provider_names():
            s = set_similarity(tn, tr)
            if s > best.get(("tin", row["tin"]), (0,))[0]:
                best[("tin", row["tin"])] = (s, "tin", row)
    ranked = sorted(best.values(), key=lambda x: -x[0])
    status, top = _pick([(s, (kind, row, s)) for s, kind, row in ranked])
    if top:
        kind, row, s = top
        if kind == "org":
            return _org_match(row, "fuzzy_name", npi=npi, billing_class=billing_class, score=s)
        return _provider_match(row, "fuzzy_name", npi, score=s)
    return {"status": status, "provider_npi": npi, "candidates": [
        {"org_key": row["org_key"] if kind == "org" else None,
         "provider_tin": row.get("tin") if kind == "tin" else None,
         "name": row.get("name") or row.get("business_name"), "score": round(s, 2)}
        for s, kind, row in ranked[:5]]}


# ---------- payer & plan ----------

def _apply_variant(base_key, plan_type, payers):
    """Aetna + medicare_advantage -> 'aetna medicare adv' when that payer exists."""
    variant = VARIANT_WORDS.get(plan_type)
    if not variant or variant <= words(base_key):
        return base_key
    want = words(base_key) | variant
    matches = [k for k in payers if want <= words(k)]
    return min(matches, key=lambda k: len(words(k))) if matches else base_key


def _base_payer(name, payers, aliases):
    """(payer_key, matched_by, score, alias_row) for the insurer named on the bill, or (None, status, candidates)."""
    a = aliases.get(name.lower())
    if a:
        return a["payer_key"], "alias", None, a
    if norm_key(name) in payers:
        return norm_key(name), "exact", None, None
    # Payer names / aliases fully contained in the bill text ("Aetna Medicare Advantage HMO" contains
    # "aetna medicare adv"; "UnitedHealthcare Community Plan" contains alias "unitedhealthcare").
    # The most specific one wins.
    bill_w = words(name)
    labels = [(k, k) for k in payers] + [(alias, r["payer_key"]) for alias, r in aliases.items()]
    contained = [(len(words(label)), k) for label, k in labels if words(label) and words(label) <= bill_w]
    if contained:
        most = max(n for n, _ in contained)
        top = {k for n, k in contained if n == most}
        if len(top) == 1:
            return top.pop(), "name_contains", None, None
    best = {}
    for label, k in labels:
        best[k] = max(similarity(name, label), best.get(k, 0))
    status, top = _pick([(sc, k) for k, sc in best.items()])
    if top:
        return top, "fuzzy_name", best[top], None
    return None, status, sorted(best.items(), key=lambda x: -x[1])[:5], None


def resolve_payer(ins):
    if ins.get("coverage_status") == "uninsured_self_pay":
        return {"status": "uninsured"}
    name = (ins.get("payer_name") or "").strip()
    if not name:
        return {"status": "not_given"}
    payers, aliases = _payers()
    key, how, extra, alias = _base_payer(name, payers, aliases)
    if key is None:
        return {"status": how, "candidates": [{"payer_key": k, "name": payers[k], "score": round(sc, 2)}
                                              for k, sc in extra]}
    variant_key = _apply_variant(key, ins.get("plan_type"), payers)
    out = {"status": "matched" if how in EXACT_MATCHES else "likely", "payer_key": variant_key,
           "name": payers[variant_key], "matched_by": how + ("+plan_type" if variant_key != key else ""),
           "plan_hint": alias["plan_key"] if alias and variant_key == key else None}
    if extra is not None:
        out["score"] = round(extra, 2)
    return out


def resolve_plan(payer, plan_name):
    payer_key = payer.get("payer_key")
    if not payer_key:
        return {"status": "no_payer"}
    plans = _plans().get(payer_key, [])
    cands = [{"plan_key": p["plan_key"], "name": p["name"], "source_types": p["source_types"]} for p in plans]
    if not plans:
        return {"status": "not_found", "candidates": []}
    hospital_plans = [p for p in plans if "hospital_standard_charges" in p["source_types"]]

    if not plan_name and payer.get("plan_hint"):
        hint = next((p for p in plans if p["plan_key"] == payer["plan_hint"]), None)
        if hint:
            return {"status": "matched", "plan_key": hint["plan_key"], "name": hint["name"],
                    "matched_by": "alias", "candidates": cands}
    if plan_name:
        scored = []
        for p in plans:
            pw = words(p["name"])
            cover = len(pw & words(plan_name)) / len(pw) if pw else 0
            scored.append((max(similarity(plan_name, p["name"]), cover), p))
        status, top = _pick(scored)
        for c, (s, _) in zip(cands, scored):
            c["score"] = round(s, 2)
        if top:
            return {"status": status, "plan_key": top["plan_key"], "name": top["name"],
                    "matched_by": "fuzzy_name", "candidates": cands}
        return {"status": status, "candidates": cands}
    if len(hospital_plans) == 1:
        p = hospital_plans[0]
        return {"status": "matched", "plan_key": p["plan_key"], "name": p["name"], "matched_by": "only_plan",
                "candidates": cands}
    return {"status": "not_given", "candidates": cands,
            "note": "Plan not stated; rates will be returned for every plan of this payer. Ask the user which plan is on their insurance card."}


# ---------- codes ----------

_EVAL_SQL = """
SELECT c.code, c.code_type, bc.description AS official_description,
       EXISTS (SELECT 1 FROM prices p WHERE p.code = c.code AND p.code_type = c.code_type) AS has_prices,
       (SELECT json_agg(x) FROM (
           SELECT hi.item_id, hi.description, hi.modifiers,
                  (SELECT array_agg(r.code) FROM item_codes r
                    WHERE r.item_id = hi.item_id AND r.code_type = 'RC') AS revenue_codes
             FROM item_codes ic JOIN hospital_items hi USING (item_id)
            WHERE ic.code = c.code AND ic.code_type = c.code_type
              AND (%(org)s::text IS NULL OR hi.org_key = %(org)s)
            ORDER BY word_similarity(%(q)s, hi.description) DESC
            LIMIT 50) x) AS hospital_items
FROM unnest(%(codes)s::text[], %(types)s::text[]) WITH ORDINALITY AS c(code, code_type, ord)
LEFT JOIN billing_codes bc ON bc.code = c.code AND bc.code_type = c.code_type
ORDER BY c.ord
"""

_FUZZY_ITEMS_SQL = """
SELECT ic.code, ic.code_type, hi.description
FROM hospital_items hi JOIN item_codes ic USING (item_id)
WHERE ic.code_type IN ('CPT', 'HCPCS', 'MS-DRG', 'APR-DRG')
  AND (%(org)s::text IS NULL OR hi.org_key = %(org)s)
  AND (%(rc)s::text IS NULL OR EXISTS (SELECT 1 FROM item_codes r WHERE r.item_id = hi.item_id
                                       AND r.code_type = 'RC' AND r.code = %(rc)s))
ORDER BY word_similarity(%(q)s, hi.description) DESC
LIMIT 40
"""

_FUZZY_OFFICIAL_SQL = """
SELECT code, code_type, description FROM billing_codes
WHERE code_type IN ('CPT', 'HCPCS')
ORDER BY word_similarity(%(q)s, description) DESC
LIMIT 20
"""


def _fuzzy_codes(desc, org_key, rc, limit=5):
    rows = db.query(_FUZZY_ITEMS_SQL, {"org": org_key, "rc": rc, "q": desc})
    rows += db.query(_FUZZY_OFFICIAL_SQL, {"q": desc})
    best = {}
    for r in rows:
        k = (r["code"], r["code_type"])
        best[k] = max(similarity(desc, r["description"] or ""), best.get(k, 0))
    return [k for k, _ in sorted(best.items(), key=lambda x: -x[1])[:limit]]


def _same_mods(item_mods, mods):
    return sorted(item_mods or []) == sorted(mods)


def _evaluate(keys, sources, desc, org_key, rc, mods):
    if not keys:
        return {}
    rows = db.query(_EVAL_SQL, {"org": org_key, "q": desc or "", "codes": [k[0] for k in keys],
                                "types": [k[1] for k in keys]})
    lvl_bill = bill_level(desc) if desc else None
    out = {}
    for r in rows:
        key = (r["code"], r["code_type"])
        items = r["hospital_items"] or []
        rc_items = [i for i in items if rc and rc in (i["revenue_codes"] or [])]
        scored_items = [(similarity(desc, i["description"]) if desc else 0.0, i) for i in (rc_items or items)]
        official = similarity(desc, r["official_description"]) if desc and r["official_description"] else 0.0
        score = max([official] + [s for s, _ in scored_items]) if desc else None

        # Visit levels only rule codes out; text similarity can't tell "level 4" from "level 5".
        lvl_code = em_level(r["code"])
        level_check = None
        if lvl_bill and lvl_code:
            level_check = "match" if lvl_bill == lvl_code else "differs"
        if not desc:
            verdict = "no_description"
        elif level_check == "differs":
            verdict = "mismatch"
        elif score >= AGREE:
            verdict = "agrees"
        elif not items:
            # Official descriptions use different words than bills ("Insertion of needle into vein" vs
            # "VENIPUNCTURE"), so a low score against them alone is not evidence of a misread.
            verdict = "unverified"
        else:
            verdict = "weak" if score >= WEAK else "mismatch"

        # Which chargemaster item this line most likely is (several items can share one code).
        pool = [(s, i) for s, i in scored_items if _same_mods(i["modifiers"], mods)] or \
               [(s, i) for s, i in scored_items if not i["modifiers"]]
        item_id = None
        if org_key is None:
            pass  # items came from other hospitals' price lists: good for checking wording, not for pricing
        elif len(pool) == 1:
            item_id = pool[0][1]["item_id"]
        elif pool and desc:
            pool.sort(key=lambda x: -x[0])
            if pool[0][0] - pool[1][0] >= MARGIN:
                item_id = pool[0][1]["item_id"]

        out[key] = {
            "code": r["code"], "code_type": r["code_type"], "source": sources[key],
            "exists": bool(r["official_description"] or items), "has_prices": r["has_prices"],
            "in_hospital_price_list": bool(items),
            "official_description": r["official_description"],
            "hospital_descriptions": [i["description"] for _, i in sorted(scored_items, key=lambda x: -x[0])][:4],
            "hospital_item_id": item_id,
            "revenue_code_match": (bool(rc_items) if rc and items else None),
            "score": round(score, 2) if score is not None else None,
            "level_check": level_check, "verdict": verdict,
        }
    return out


def resolve_line(line, org_key, facility=False):
    desc = (line.get("description") or "").strip()
    rc = (line.get("revenue_code") or "").strip() or None
    if rc and rc.isdigit():
        rc = rc.zfill(4)
    mods = [m.strip().upper() for m in line.get("modifiers") or [] if m and m.strip()]
    order, sources = [], {}

    def add(key, source):
        if key and key not in sources:
            sources[key] = source
            order.append(key)

    printed = None
    if line.get("code_as_printed"):
        p = parse_printed(line["code_as_printed"], line.get("code_type"))
        if p:
            printed = (p[0], p[1])
            mods = mods or p[2]
            add(printed, "printed")
    for g in line.get("candidate_codes") or []:
        add(infer_code(g.get("code"), g.get("code_type")), "ai_guess")
    if desc and not printed:
        for k in _fuzzy_codes(desc, org_key, rc):
            add(k, "fuzzy")

    cands = _evaluate(order, sources, desc, org_key, rc, mods)
    # A printed code that doesn't exist or contradicts the description may be misread: add alternatives,
    # starting with the same visit code at the level the description states.
    if printed and desc and (not cands[printed]["exists"] or cands[printed]["verdict"] == "mismatch"):
        before = set(sources)
        add(em_sibling(printed[0], bill_level(desc)), "level_sibling")
        for k in _fuzzy_codes(desc, org_key, rc):
            add(k, "fuzzy")
        extra = [k for k in order if k not in before]
        cands.update(_evaluate(extra, sources, desc, org_key, rc, mods))

    status, reason, selected = _line_status(printed, cands, desc, facility)
    ranked = sorted(cands.values(), key=lambda c: (c["source"] != "printed", -(c["score"] or 0)))
    return {
        "ref": line.get("ref"),
        "status": status,
        "needs_user_confirmation": status not in ("confirmed", "likely"),
        "reason": reason,
        "selected": ({"code": selected["code"], "code_type": selected["code_type"], "modifiers": mods,
                      "source": selected["source"], "hospital_item_id": selected["hospital_item_id"]}
                     if selected else None),
        "modifiers": mods,
        "candidates": ranked[:6],
    }


def _line_status(printed, cands, desc, facility):
    if printed:
        c = cands[printed]
        if not c["exists"]:
            return "not_found", "Printed code is not in the database; it may be misread.", None
        if c["verdict"] == "mismatch":
            why = ("the visit level on the bill differs from this code's level" if c["level_check"] == "differs"
                   else "its description does not match the bill text")
            return "needs_confirmation", f"Printed code exists but {why}; it may be misread.", None
        note = {"no_description": "", "agrees": "; description agrees",
                "unverified": "; description could not be checked against the hospital's own wording",
                "weak": "; description only loosely matches"}[c["verdict"]]
        return "confirmed", f"Printed on the bill{note}.", c

    existing = [c for c in cands.values() if c["exists"]]
    # On a hospital bill the line must be an item in that hospital's own price list, when we have one.
    if facility and any(c["in_hospital_price_list"] for c in existing):
        existing = [c for c in existing if c["in_hospital_price_list"]]
    if not existing:
        return "not_found", "No candidate code exists in the database.", None
    if not desc:
        return "needs_confirmation", "No printed code and no description to check a guess against.", None
    agrees = sorted([c for c in existing if c["verdict"] == "agrees"], key=lambda c: -c["score"])
    if len(agrees) == 1 or (len(agrees) > 1 and agrees[0]["score"] - agrees[1]["score"] >= MARGIN):
        return "likely", f"Only {agrees[0]['code']} clearly matches the bill text.", agrees[0]
    if len(agrees) > 1:
        return "ambiguous", "Several codes match the bill text about equally; ask the user.", None
    if any(c["verdict"] in ("weak", "unverified") for c in existing):
        return "needs_confirmation", "Candidates only weakly match the bill text.", None
    return "not_found", "No candidate's description matches the bill text.", None


# ---------- entry point ----------

def resolve(req):
    billing_class = req.get("billing_class") or DOC_CLASS.get(req.get("document_type"))
    hospital = resolve_hospital(req.get("hospital") or {}, billing_class)
    ins = req.get("insurance") or {}
    payer = resolve_payer(ins)
    plan = resolve_plan(payer, ins.get("plan_name"))
    facility = billing_class == "institutional" and hospital.get("org_key") is not None
    lines = [resolve_line(l, hospital.get("org_key"), facility) for l in req.get("lines") or []]
    return {
        "billing_class": billing_class,
        "hospital": hospital,
        "payer": payer,
        "plan": plan,
        "lines": lines,
        "next_step": ("For each line with status confirmed or likely, call get_negotiated_rate with "
                      "selected.code/code_type/modifiers/hospital_item_id, hospital.org_key (or provider_tin) and "
                      "provider_npi, payer.payer_key, plan.plan_key and billing_class. For other lines, ask the "
                      "user to choose among the candidates first; do not pick yourself."),
    }
