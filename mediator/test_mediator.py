"""Golden cases against the loaded database (UW Health standard charges + WPS in-network file).

    .venv/bin/python -m pytest mediator -q
"""
import json

import pytest

from mediator import InvalidIds, TOOLS, get_negotiated_rate, resolve, run_tool

UW_NPI = "1922043744"


def line(r, i=0):
    return r["lines"][i]


def hospital_bill(*lines, **insurance):
    return resolve({
        "document_type": "itemized_hospital_statement",
        "hospital": {"name": "UW Health University Hospital", "npi": UW_NPI},
        "insurance": insurance or {"coverage_status": "insured", "payer_name": "WPS Health Solutions",
                                   "plan_name": "Statewide"},
        "lines": list(lines),
    })


# ---------- resolve: hospital / payer / plan ----------

def test_hospital_by_npi_payer_alias_plan_fuzzy():
    r = hospital_bill()
    assert r["billing_class"] == "institutional"
    assert (r["hospital"]["status"], r["hospital"]["org_key"], r["hospital"]["matched_by"]) == ("matched", "uwhc", "npi")
    assert (r["payer"]["payer_key"], r["payer"]["matched_by"]) == ("wps", "alias")
    assert (r["plan"]["status"], r["plan"]["plan_key"]) == ("likely", "wps|statewide")


def test_brand_name_uses_bill_type_to_pick_billing_entity():
    doc = resolve({"document_type": "physician_statement", "hospital": {"name": "UW Health"}})
    hosp = resolve({"document_type": "itemized_hospital_statement", "hospital": {"name": "UW Health"}})
    unknown = resolve({"hospital": {"name": "UW Health"}})
    assert doc["hospital"]["org_key"] == "uwmf"
    assert hosp["hospital"]["org_key"] == "uwhc"
    assert unknown["hospital"]["status"] == "ambiguous"


def test_hospital_known_only_from_insurer_file():
    r = resolve({"hospital": {"name": "Froedtert Memorial Lutheran Hospital"}})
    h = r["hospital"]
    assert h["status"] in ("likely", "ambiguous")
    top = h if h["status"] == "likely" else h["candidates"][0]
    assert top["org_key"] is None and top["provider_tin"]


def test_payer_variants():
    ma = resolve({"insurance": {"payer_name": "Aetna Medicare Advantage HMO"}})["payer"]
    medicaid = resolve({"insurance": {"payer_name": "UnitedHealthcare Community Plan", "plan_type": "medicaid"}})["payer"]
    uninsured = resolve({"insurance": {"coverage_status": "uninsured_self_pay"}})["payer"]
    assert ma["payer_key"] == "aetna medicare adv"
    assert medicaid["payer_key"] == "united healthcare medicaid"
    assert uninsured["status"] == "uninsured"


# ---------- resolve: codes ----------

def test_printed_code_with_modifier_is_confirmed():
    l = line(hospital_bill({"code_as_printed": "99213-25", "description": "OFFICE/OUTPATIENT VISIT EST LVL 3"}))
    assert l["status"] == "confirmed"
    assert l["selected"]["code"] == "99213" and l["selected"]["modifiers"] == ["25"]


def test_ai_guess_with_contrast_wording():
    l = line(hospital_bill({"description": "MRI BRAIN W/WO CONTRAST",
                            "candidate_codes": [{"code": "70553"}, {"code": "70552"}, {"code": "70551"}]}))
    assert l["status"] == "likely" and l["selected"]["code"] == "70553"
    assert l["selected"]["hospital_item_id"] is not None


def test_visit_level_picks_the_code():
    l = line(hospital_bill({"description": "ED VISIT LEVEL 4",
                            "candidate_codes": [{"code": "99284"}, {"code": "99285"}, {"code": "99283"}]}))
    assert l["status"] == "likely" and l["selected"]["code"] == "99284"


def test_missing_level_is_ambiguous_not_guessed():
    l = line(hospital_bill({"description": "ED VISIT",
                            "candidate_codes": [{"code": "99283"}, {"code": "99284"}, {"code": "99285"}]}))
    assert l["status"] == "ambiguous" and l["selected"] is None and l["needs_user_confirmation"]


def test_misread_printed_level_is_flagged_with_sibling():
    l = line(hospital_bill({"code_as_printed": "99214", "description": "OFFICE VISIT EST LVL 3"}))
    assert l["status"] == "needs_confirmation" and l["selected"] is None
    assert any(c["code"] == "99213" and c["source"] == "level_sibling" for c in l["candidates"])


def test_unknown_printed_code():
    l = line(hospital_bill({"code_as_printed": "99999", "description": "MYSTERY"}))
    assert l["status"] == "not_found"


def test_drg_printed():
    l = line(hospital_bill({"code_as_printed": "DRG 470"}))
    assert l["status"] == "confirmed" and (l["selected"]["code"], l["selected"]["code_type"]) == ("470", "MS-DRG")


# ---------- get_negotiated_rate ----------

def test_hospital_bill_mri_statewide():
    r = get_negotiated_rate(dict(code="70553", code_type="CPT", org_key="uwhc", provider_npi=UW_NPI,
                                 payer_key="wps", plan_key="wps|statewide", billing_class="institutional",
                                 billed_amount=6798))
    assert r["found"]
    assert r["hospital_price_list"][0]["list_price"] == 6798.0
    assert r["hospital_price_list"][0]["cash_price"] == 4078.8
    plan_rate = [x for x in r["rates"] if x["source"] == "hospital_price_list"]
    assert [(x["plan_key"], x["negotiated_percentage"], x["line_total_equivalent"]) for x in plan_rate] == \
        [("wps|statewide", 76.3, 5186.87)]
    assert r["all_payers_at_this_hospital"][0]["min"] == 356.43
    assert r["comparison"]["billed_minus_cash_price"] == 2719.2


def test_drg_cross_check_between_hospital_list_and_insurer_file():
    r = get_negotiated_rate(dict(code="470", code_type="MS-DRG", org_key="uwhc", payer_key="wps",
                                 plan_key="wps|statewide", billing_class="institutional", billed_amount=69176.76))
    hosp = [x for x in r["rates"] if x["source"] == "hospital_price_list"]
    insurer = [x for x in r["rates"] if x["source"] == "insurer_file"]
    assert hosp[0]["negotiated_dollar"] == 38057.2
    assert insurer[0]["negotiated_dollar"] == 21194.75
    assert insurer[0]["matches_hospital_list_plans"] == ["wps|healthyu/aspirus"]
    assert insurer[0]["consistent_with_requested_plan"] is False
    assert r["comparison"]["your_plan_lowest"] == 38057.2  # other plan's rate is excluded


def test_physician_bill_modifier_falls_back_to_base_rate():
    r = get_negotiated_rate(dict(code="99213", code_type="CPT", modifiers=["25"], org_key="uwmf",
                                 payer_key="wps", billing_class="professional"))
    assert r["modifier_match"] == "base_rate_fallback"
    assert r["hospital_price_list"] == []
    main = [x for x in r["rates"] if x.get("provider_groups") == ["47"]]
    assert main[0]["negotiated_dollar"] == 193.27


def test_npi_narrows_insurer_contracts():
    r = get_negotiated_rate(dict(code="99213", code_type="CPT", org_key="uwhc", provider_npi=UW_NPI,
                                 payer_key="wps", billing_class="professional"))
    assert r["provider"]["npi_filter_applied"]
    assert {g for x in r["rates"] for g in x["provider_groups"]} <= {"49", "1661", "1662"}


def test_uninsured_gets_cash_price_and_all_payer_range():
    r = get_negotiated_rate(dict(code="99284", code_type="CPT", org_key="uwhc", hospital_item_id=100000014004,
                                 billing_class="institutional", billed_amount=2373))
    assert r["comparison"]["cash_price"] == 1423.8
    assert "your_plan_lowest" not in r["comparison"]
    assert r["all_payers_at_this_hospital"][0]["plans_with_dollar_rates"] > 50


def test_percent_of_charges_uses_billed_amount():
    r = get_negotiated_rate(dict(code="C1713", code_type="HCPCS", org_key="uwhc", payer_key="wps",
                                 plan_key="wps|healthyu/aspirus", billing_class="institutional", billed_amount=500))
    hosp = [x for x in r["rates"] if x["source"] == "hospital_price_list"][0]
    assert (hosp["negotiated_percentage"], hosp["line_total_equivalent"], hosp["basis"]) == \
        (69.3, 346.5, "percent_of_your_billed_amount")


def test_several_items_share_a_code():
    r = get_negotiated_rate(dict(code="99285", code_type="CPT", org_key="uwhc", payer_key="aetna",
                                 billing_class="institutional"))
    assert r["hospital_items_matching"] == 5
    assert all(x["basis"] == "varies_by_item_pass_hospital_item_id" for x in r["rates"])


@pytest.mark.parametrize("req, needle", [
    (dict(code="70553", code_type="CPT", org_key="uw health", payer_key="wps"), "unknown org_key"),
    (dict(code="70553", code_type="CPT", org_key="uwhc", payer_key="wps", plan_key="aetna|aetna w"), "belongs to payer"),
    (dict(code="MRI brain", code_type="CPT", org_key="uwhc"), "not in the database"),
    (dict(code="70553", code_type="CPT"), "provide org_key or provider_tin"),
    (dict(code="70553", code_type="CPT", org_key="uwhc", hospital_item_id=100000014004), "is not a 70553 item"),
])
def test_rejects_anything_but_resolved_ids(req, needle):
    with pytest.raises(InvalidIds) as e:
        get_negotiated_rate(req)
    assert any(needle in d for d in e.value.details)


# ---------- Claude tool plumbing ----------

def test_tool_definitions_are_self_contained():
    assert [t["name"] for t in TOOLS] == ["resolve_bill_entities", "get_negotiated_rate"]
    for t in TOOLS:
        dumped = json.dumps(t["input_schema"])
        assert "$ref" not in dumped and '"title"' not in dumped
        assert t["input_schema"]["type"] == "object"


def test_run_tool_round_trip_and_errors():
    out, err = run_tool("get_negotiated_rate", {"code": "70553", "code_type": "CPT", "org_key": "uwhc",
                                                "payer_key": "wps"})
    assert not err and json.loads(out)["found"]
    out, err = run_tool("get_negotiated_rate", {"code": "70553", "code_type": "CPT", "org_key": "nope"})
    assert err and json.loads(out)["error"] == "invalid_ids"
    out, err = run_tool("get_negotiated_rate", {"code_type": "CPT"})
    assert err and json.loads(out)["error"] == "invalid_input"


def test_unknown_hospital_line_gets_no_hospital_item():
    r = resolve({"document_type": "itemized_hospital_statement", "hospital": {"name": "Meriter Hospital"},
                 "lines": [{"code_as_printed": "99284", "description": "ED VISIT LEVEL 4"}]})
    assert r["hospital"]["org_key"] is None
    assert line(r)["selected"]["hospital_item_id"] is None


def test_generic_words_do_not_match_the_wrong_hospital():
    h = resolve({"document_type": "itemized_hospital_statement",
                 "hospital": {"name": "SSM Health St. Mary's Hospital Madison"}})["hospital"]
    assert h.get("org_key") != "uwhc" and h["status"] != "likely"
    assert resolve({"hospital": {"name": "Meriter Hospital"}})["hospital"]["name"] == "MERITER HOSPITAL INC"
