"""
Parse a payer Transparency-in-Coverage in-network rate file (e.g. WPS in-network-rates-020.json)
into the ClarityBill tables. Keeps everything: every code, provider group, and price.

The files are 14GB+, so we never json.load() the whole thing. WPS files are pretty-printed with
2-space indent, so we stream line by line and json.loads() one in_network item at a time
(items are the objects at exactly 4-space indent). Stdlib only.

Usage:
    python parse_wps_mrf.py in-network-rates-020.json wps_mrf_020 2

args: <file> <source_id> <source_no (unique int per file)>
Handles both provider layouts: top-level provider_references (ids) and inline provider_groups.
"""
import json
import sys

from common import ID_BLOCK, TableWriter, norm_code, norm_key, pg_array

TYPE_KIND = {
    "negotiated": "dollar",
    "fee schedule": "dollar",
    "derived": "dollar",
    "percentage": "percent_of_charges",
    "per diem": "per_diem",
}
INLINE_GROUP_OFFSET = ID_BLOCK // 2  # synthesized ids for inline provider_groups

TABLES = {
    "sources": ["source_id", "source_type", "publisher", "file_name", "last_updated_on"],
    "payers": ["payer_key", "name"],
    "plans": ["plan_key", "payer_key", "name"],
    "provider_groups": ["provider_group_id", "source_id", "file_group_id", "network_name", "tin_type", "tin",
                        "business_name"],
    "provider_group_npis": ["provider_group_id", "npi"],
    "billing_codes": ["code", "code_type", "description"],
    "rate_sets": ["rate_set_id", "source_id"],
    "rate_set_providers": ["rate_set_id", "provider_group_id"],
    "prices": ["source_id", "code", "code_type", "modifiers", "payer_key", "plan_key", "rate_set_id",
               "billing_class", "setting", "rate_kind", "methodology", "negotiated_dollar",
               "negotiated_percentage", "service_codes", "expiration_date", "notes"],
}


def iter_items(path):
    """Yield (header_dict, provider_references_list) once, then each in_network item dict."""
    header, prov_lines, item_lines, in_network = {}, None, None, False
    with open(path, "rb") as f:
        for line in f:
            if item_lines is not None:
                item_lines.append(line)
                if line.startswith(b"    }"):
                    yield json.loads(b"".join(item_lines).rstrip().rstrip(b","))
                    item_lines = None
                continue
            if in_network and line == b"    {\n":
                item_lines = [line]
                continue
            if line.startswith(b'  "provider_references"'):
                prov_lines = [line]
                continue
            if line.startswith(b'  "in_network"'):
                refs = []
                if prov_lines:
                    refs = json.loads(b"{" + b"".join(prov_lines).rstrip().rstrip(b",") + b"}")["provider_references"]
                prov_lines, in_network = None, True
                yield header, refs
                continue
            if prov_lines is not None:
                prov_lines.append(line)
            elif line.startswith(b'  "') and line.rstrip().endswith((b'",', b'"')):
                header.update(json.loads(b"{" + line.strip().rstrip(b",") + b"}"))
    if not in_network:
        sys.exit("no pretty-printed in_network array found; this file needs a real streaming JSON parser")


def main(path, source_id, source_no):
    out = TableWriter(source_id, TABLES)
    items = iter_items(path)
    header, refs = next(items)
    payer_name = header.get("reporting_entity_name", "unknown")
    payer_key = norm_key(payer_name)
    out.write("sources", [source_id, "payer_mrf", payer_name, path.split("/")[-1], header.get("last_updated_on")])
    out.write("payers", [payer_key, payer_name])

    plans_seen = set()
    group_plan = {}  # provider_group_id -> plan_key

    def add_group(gid, file_gid, networks, pg):
        network = "|".join(networks or []) or None
        tin = pg.get("tin") or {}
        out.write("provider_groups", [gid, source_id, file_gid, network, tin.get("type"), tin.get("value"),
                                      tin.get("business_name")])
        for npi in pg.get("npi") or []:
            out.write("provider_group_npis", [gid, npi])
        if network:
            plk = payer_key + "|" + norm_key(network)
            if plk not in plans_seen:
                out.write("plans", [plk, payer_key, network]); plans_seen.add(plk)
            group_plan[gid] = plk

    # provider_references: one reference may hold several provider_groups (TINs); each becomes its own
    # provider_groups row, and the reference id maps to all of them.
    ref_map, next_inline = {}, source_no * ID_BLOCK + INLINE_GROUP_OFFSET
    for ref in refs:
        if "provider_groups" not in ref:
            print(f"  skipping external provider reference {ref.get('provider_group_id')}: {ref.get('location')}",
                  file=sys.stderr)
            continue
        ids = []
        for k, pg in enumerate(ref["provider_groups"]):
            gid = source_no * ID_BLOCK + int(ref["provider_group_id"]) if k == 0 else next_inline
            if k:
                next_inline += 1
            add_group(gid, str(ref["provider_group_id"]), ref.get("network_name"), pg)
            ids.append(gid)
        ref_map[ref["provider_group_id"]] = ids

    inline_seen = {}  # dedupe identical inline groups across items
    rate_set_id = source_no * ID_BLOCK
    codes_seen = set()
    for n, item in enumerate(items, 1):
        code, ctype = norm_code(item["billing_code"], item["billing_code_type"])
        if (code, ctype) not in codes_seen:
            out.write("billing_codes", [code, ctype, item.get("description") or item.get("name")])
            codes_seen.add((code, ctype))
        for nr in item.get("negotiated_rates", []):
            rate_set_id += 1
            out.write("rate_sets", [rate_set_id, source_id])
            gids = []
            for ref_id in nr.get("provider_references") or []:
                gids.extend(ref_map.get(ref_id, []))
            for pg in nr.get("provider_groups") or []:
                key = json.dumps(pg, sort_keys=True)
                if key not in inline_seen:
                    inline_seen[key] = next_inline
                    add_group(next_inline, None, None, pg)
                    next_inline += 1
                gids.append(inline_seen[key])
            for gid in gids:
                out.write("rate_set_providers", [rate_set_id, gid])
            plan_keys = {group_plan.get(g) for g in gids}
            plan_key = plan_keys.pop() if len(plan_keys) == 1 else None

            for p in nr["negotiated_prices"]:
                ntype = p["negotiated_type"]
                kind = TYPE_KIND.get(ntype, "algorithm")
                notes = p.get("additional_information")
                if notes and "ASA Base units" in notes:
                    kind = "per_unit"  # anesthesia conversion factor, not a total
                rate = p.get("negotiated_rate")
                dollar = rate if kind != "percent_of_charges" else None
                pct = rate if kind == "percent_of_charges" and rate else None  # 0.0 = % is in notes
                out.write("prices", [
                    source_id, code, ctype, pg_array(p.get("billing_code_modifier")), payer_key, plan_key,
                    rate_set_id, p.get("billing_class"), p.get("setting"), kind, ntype, dollar, pct,
                    pg_array(p.get("service_code")), p.get("expiration_date") or None, notes,
                ])
        if n % 1000 == 0:
            print(f"  {n} codes", file=sys.stderr)
    print(source_id, out.close(), file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]))
