"""
Parse a CMS hospital standard-charges CSV (v2/v3 "wide" format) into the ClarityBill tables.
Every payer|plan column group becomes rows in `prices`, same structure as the payer MRF rates.

Usage:
    python parse_hospital_sc.py "University Hospital.csv" uw_sc_2026_04 uwhc 1

args: <file> <source_id> <org_key (must exist in db/seed.sql)> <source_no (unique int per file)>
"""
import csv
import sys

from common import ID_BLOCK, TableWriter, norm_code, norm_key, pg_array

METHOD_KIND = {
    "fee schedule": "dollar",
    "case rate": "dollar",
    "percent of total billed charges": "percent_of_charges",
    "per diem": "per_diem",
    "other": "algorithm",
}
# Codes the mediator looks up by; the first one present on a row is the price's primary code.
PRIMARY_TYPES = ["CPT", "HCPCS", "MS-DRG", "APR-DRG", "EAPG", "RC", "LOCAL", "CDM"]

TABLES = {
    "sources": ["source_id", "source_type", "publisher", "file_name", "last_updated_on"],
    "payers": ["payer_key", "name"],
    "plans": ["plan_key", "payer_key", "name"],
    "billing_codes": ["code", "code_type", "description"],
    "hospital_items": ["item_id", "source_id", "org_key", "cdm_code", "description", "setting", "modifiers",
                       "gross_charge", "discounted_cash", "min_negotiated", "max_negotiated",
                       "drug_unit", "drug_unit_type", "generic_notes"],
    "item_codes": ["item_id", "code", "code_type", "raw_code_type"],
    "prices": ["source_id", "code", "code_type", "modifiers", "payer_key", "plan_key", "org_key", "item_id",
               "billing_class", "setting", "rate_kind", "methodology", "negotiated_dollar",
               "negotiated_percentage", "negotiated_algorithm", "median_paid", "p10_paid", "p90_paid",
               "paid_count", "notes"],
}


def num(s):
    s = (s or "").strip().replace(",", "").replace("$", "")
    return s or None


def main(path, source_id, org_key, source_no):
    out = TableWriter(source_id, TABLES)
    f = open(path, newline="", encoding="utf-8-sig")
    r = csv.reader(f)
    meta = dict(zip(next(r), next(r)))
    hdr = next(r)
    low = [h.strip().lower() for h in hdr]
    col = {h: i for i, h in enumerate(low)}

    out.write("sources", [source_id, "hospital_standard_charges", meta.get("hospital_name"),
                          path.split("/")[-1], meta.get("last_updated_on")])

    # payer|plan column groups: standard_charge|<payer>|<plan>|negotiated_dollar etc.
    plans = []
    for h in low:
        parts = h.split("|")
        if parts[0] == "standard_charge" and len(parts) == 4 and parts[3] == "negotiated_dollar":
            payer, plan = hdr[low.index(h)].split("|")[1:3]  # original casing
            plans.append((payer, plan, parts[1], parts[2]))
    payers_seen, plans_seen = set(), set()
    for payer, plan, _, _ in plans:
        pk, plk = norm_key(payer), norm_key(payer) + "|" + norm_key(plan)
        if pk not in payers_seen:
            out.write("payers", [pk, payer.replace("_", " ").strip()]); payers_seen.add(pk)
        if plk not in plans_seen:
            out.write("plans", [plk, pk, plan.replace("_", " ").strip()]); plans_seen.add(plk)

    def g(row, name):
        i = col.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    codes_seen = set()
    for n, row in enumerate(r, 1):
        item_id = source_no * ID_BLOCK + n
        codes, cdm = [], None
        for k in range(1, 5):
            raw_code, raw_type = g(row, f"code|{k}"), g(row, f"code|{k}|type")
            if not raw_code:
                continue
            code, ctype = norm_code(raw_code, raw_type)
            codes.append((code, ctype))
            out.write("item_codes", [item_id, code, ctype, raw_type])
            if ctype == "CDM":
                cdm = code
        desc = g(row, "description")
        setting = g(row, "setting") or None
        mods = [m for m in g(row, "modifiers").split("|") if m]
        out.write("hospital_items", [item_id, source_id, org_key, cdm, desc, setting, pg_array(mods),
                                     num(g(row, "standard_charge|gross")),
                                     num(g(row, "standard_charge|discounted_cash")),
                                     num(g(row, "standard_charge|min")), num(g(row, "standard_charge|max")),
                                     num(g(row, "drug_unit_of_measurement")),
                                     g(row, "drug_type_of_measurement") or None,
                                     g(row, "additional_generic_notes") or None])

        primary = next(((c, t) for pt in PRIMARY_TYPES for c, t in codes if t == pt), None)
        if not primary:
            continue
        for c, t in codes:
            if t in ("MS-DRG", "APR-DRG", "EAPG") and (c, t) not in codes_seen:
                out.write("billing_codes", [c, t, desc]); codes_seen.add((c, t))

        for payer, plan, lp, lpl in plans:
            base = f"standard_charge|{lp}|{lpl}|"
            dollar, pct = num(g(row, base + "negotiated_dollar")), num(g(row, base + "negotiated_percentage"))
            algo = g(row, base + "negotiated_algorithm") or None
            if not (dollar or pct or algo):
                continue
            method = g(row, base + "methodology") or None
            kind = METHOD_KIND.get((method or "").lower(), "algorithm" if algo else "dollar")
            out.write("prices", [
                source_id, primary[0], primary[1], pg_array(mods), norm_key(payer),
                norm_key(payer) + "|" + norm_key(plan), org_key, item_id,
                "institutional",  # hospital standard-charge files are facility charges
                setting, kind, method, dollar, pct, algo,
                num(g(row, f"median_amount|{lp}|{lpl}")), num(g(row, f"10th_percentile|{lp}|{lpl}")),
                num(g(row, f"90th_percentile|{lp}|{lpl}")), g(row, f"count|{lp}|{lpl}") or None,
                g(row, f"additional_payer_notes|{lp}|{lpl}") or None,
            ])
    print(source_id, out.close(), file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]))
