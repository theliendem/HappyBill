"""Backend tests. The model is faked, so these run without an API key.

    .venv/bin/python -m pytest backend -q
"""
import io
import json
from pathlib import Path

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from backend import llm, script
from backend.analyze import analyze
from backend.api import app
from backend.extract import extract, known_sample
from backend.schemas import Bill, CandidateCode, Choices, NegotiationPlan

SAMPLES = Path(__file__).parent.parent / "samples"
FIX_99213 = {"lines": {"L1": {"code": "99213", "code_type": "CPT"}}}


def sample(name):
    return Bill.model_validate_json((SAMPLES / f"{name}.json").read_text())


def run(name, choices=None):
    return analyze(sample(name), Choices.model_validate(choices or {}))


# ---------------- analysis on the sample bills ----------------

def test_er_uninsured_reprices_to_cash_price():
    a = run("er_uninsured")
    assert a["status"] == "complete" and a["hospital"]["org_key"] == "uwhc"
    assert all(l["benchmarks"]["target_basis"] == "hospital_cash_price" for l in a["lines"])
    ondansetron = next(l for l in a["lines"] if l["selected"]["code"] == "J2405")
    assert ondansetron["benchmarks"]["cash_price"] == 70.21  # item picked by matching its list price
    assert a["totals"]["compared_billed"] == 4320.96
    assert a["totals"]["compared_target"] == 2592.58
    assert a["totals"]["potential_savings"] == 1728.38


def test_imaging_uncoded_line_uses_ai_guess_and_plan_rates():
    a = run("imaging_wps")
    mri = a["lines"][0]
    assert (mri["state"], mri["selected"]["code"]) == ("likely", "70553")
    assert a["plan"]["plan_key"] == "wps|statewide"
    assert mri["benchmarks"]["your_plan_rate_high"] == 5186.87
    # Claim not processed yet and the bill asks for full charges: you owe at most the plan's rate.
    assert a["totals"]["bottom_line"]["should_pay"] == a["totals"]["bottom_line"]["fair"] == 9900.65
    assert a["totals"]["potential_savings"] == 3075.30


def test_inpatient_balance_billing_is_caught():
    a = run("knee_inpatient")
    stay = a["lines"][0]
    assert stay["ref"] == "STAY" and stay["selected"]["code"] == "470"
    assert stay["benchmarks"]["target"] == 38057.2
    assert stay["benchmarks"]["max_you_should_owe"] == 7611.44  # plan rate - insurer payment
    assert a["totals"]["potential_savings"] == 31119.56
    assert a["totals"]["basis"] == "inpatient_case_rate"
    assert all(l["state"] == "part_of_stay" for l in a["lines"][1:])


def test_misread_level_needs_user_then_completes():
    first = run("clinic_mismatch")
    assert first["status"] == "needs_input"
    assert [(p["kind"], p["ref"]) for p in first["pending"]] == [("line", "L1")]
    assert first["hospital"]["org_key"] == "uwmf"  # "UW Health Physicians" on a physician statement
    done = run("clinic_mismatch", FIX_99213)
    assert done["status"] == "complete"
    assert done["totals"]["potential_savings"] == 124.73


def test_open_questions_are_asked_together():
    bill = sample("imaging_wps")
    bill.insurance.coverage_status = "unknown"
    bill.document.document_type = "other"
    bill.insurance.plan_name = None
    a = analyze(bill, Choices())
    assert {p["kind"] for p in a["pending"]} == {"coverage", "bill_type"}
    a = analyze(bill, Choices(coverage_status="insured", billing_class="institutional"))
    assert [p["kind"] for p in a["pending"]] == ["plan"]
    assert {c["plan_key"] for c in a["pending"][0]["candidates"]} == {"wps|statewide", "wps|healthyu/aspirus"}
    a = analyze(bill, Choices(coverage_status="insured", billing_class="institutional", plan_key="none"))
    assert a["status"] == "complete"


def test_bad_choice_rejected():
    with pytest.raises(ValueError):
        run("imaging_wps", {"plan_key": "aetna|aetna w"})


# ---------------- plan writing ----------------

def _plan(text):
    return NegotiationPlan(headline=text, situation="s", steps=["a"], call_script="c", letter_subject="subj", letter="l")


def test_plan_uses_model_when_numbers_check_out(monkeypatch):
    monkeypatch.setattr(llm, "structured", lambda *a, **k: _plan("Save up to $1,728.38 at the $2,592.58 cash price."))
    bill = sample("er_uninsured")
    plan, tacts, by, note = script.write_plan(analyze(bill), bill)
    assert by == "model" and note is None


def test_invented_numbers_fall_back_to_template(monkeypatch):
    calls = []

    def fake(messages, model_cls, **k):
        calls.append(messages)
        return _plan("You could save $999.99!")

    monkeypatch.setattr(llm, "structured", fake)
    bill = sample("er_uninsured")
    plan, tacts, by, note = script.write_plan(analyze(bill), bill)
    assert by == "template" and len(calls) == 2
    assert "$999.99" in calls[1][-1]["content"]  # the retry told the model which number was invented
    assert "$1,728.38" in plan.headline


def test_no_model_falls_back_to_template(monkeypatch):
    def down(*a, **k):
        raise llm.LLMUnavailable("no key")

    monkeypatch.setattr(llm, "structured", down)
    bill = sample("knee_inpatient")
    plan, tacts, by, note = script.write_plan(analyze(bill), bill)
    assert by == "template"
    assert "$7,611.44" in plan.call_script and "$38,731.00" in plan.call_script


def test_invented_amounts_detector():
    assert script.invented_amounts(_plan("pay $1,423.80 not $2,373"), {1423.8, 2373.0}) == []
    assert script.invented_amounts(_plan("pay $1,400"), {1423.8}) == [1400.0]


# ---------------- extraction ----------------

def test_extract_assigns_refs_and_drops_guesses_on_printed_codes(monkeypatch):
    raw = sample("imaging_wps")
    for l in raw.line_items:
        l.ref = None
        l.candidate_codes = [{"code": "70553"}]
    monkeypatch.setattr(llm, "structured", lambda *a, **k: raw)
    bill = extract([(b"png", "image/png")])
    assert [l.ref for l in bill.line_items] == ["L1", "L2", "L3"]
    assert bill.line_items[0].candidate_codes and not bill.line_items[1].candidate_codes


@pytest.mark.parametrize("images, msg", [
    ([], "No images"),
    ([(b"%PDF", "application/pdf")], "Unsupported file type"),
    ([(b"x", "image/png")] * 11, "At most"),
    ([(b"x" * (8 * 1024 * 1024 + 1), "image/png")], "larger than 8 MB"),
])
def test_extract_rejects_bad_uploads(images, msg):
    with pytest.raises(ValueError, match=msg):
        extract(images)


def test_structured_falls_back_when_provider_lacks_json_schema(monkeypatch):
    sent = []

    def fake_call(model, messages, schema_model, use_json_schema):
        sent.append(use_json_schema)
        if use_json_schema:
            raise openai.BadRequestError("json_schema unsupported", body=None, response=httpx.Response(
                400, request=httpx.Request("POST", "http://model")))
        return '```json\n' + _plan("ok").model_dump_json() + '\n```'

    monkeypatch.setattr(llm, "_call", fake_call)
    assert llm.structured([{"role": "user", "content": "x"}], NegotiationPlan).headline == "ok"
    assert sent == [True, False]


# ---------------- HTTP API ----------------

client = TestClient(app)


def test_api_plan_answers_questions_itself(monkeypatch):
    monkeypatch.setattr(llm, "structured", lambda *a, **k: (_ for _ in ()).throw(llm.LLMUnavailable("off")))
    bill = client.get("/api/samples/clinic_mismatch.json").json()
    asked = client.post("/api/analyze?interactive=true", json={"bill": bill}).json()
    assert asked["status"] == "needs_input"
    body = client.post("/api/plan", json={"bill": bill}).json()
    assert body["analysis"]["status"] == "complete" and body["generated_by"] == "template"
    [a] = body["analysis"]["assumptions"]
    assert (a["kind"], a["ref"], a["by"]) == ("line", "L1", "rule") and a["chose"].startswith("99213")
    assert body["analysis"]["totals"]["potential_savings"] == 124.73
    assert "confirm_codes" in [t["id"] for t in body["tactics"]]


def test_api_extract_errors(monkeypatch):
    monkeypatch.setattr(llm, "api_key", lambda: None)
    png = ("page1.png", io.BytesIO((SAMPLES / "er_uninsured.png").read_bytes()), "image/png")
    r = client.post("/api/extract", files=[("files", png)])
    assert r.status_code == 503 and r.json()["detail"]["error"] == "model_unavailable"
    pdf = ("bill.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")
    assert client.post("/api/extract", files=[("files", pdf)]).status_code == 400


def test_api_health_samples_and_mediator_mount():
    health = client.get("/api/health").json()
    assert health["database"] is True and "configured" in health["model"]
    assert {"clinic_mismatch", "er_uninsured", "imaging_wps", "knee_inpatient", "er_head_ct"} <= set(client.get("/api/samples").json())
    assert client.get("/api/samples/er_uninsured.png").headers["content-type"] == "image/png"
    assert client.get("/api/samples/../api").status_code == 404
    assert [t["name"] for t in client.get("/mediator/tools").json()] == ["resolve_bill_entities", "get_negotiated_rate"]


def test_analyze_response_is_json_serializable():
    json.dumps(run("knee_inpatient"))


def test_busy_model_falls_back_once(monkeypatch):
    used = []

    def fake_call(model, messages, schema_model, use_json_schema):
        used.append(model)
        if model == llm.MODEL:
            raise openai.RateLimitError("quota", body=None, response=httpx.Response(
                429, request=httpx.Request("POST", "http://model")))
        return _plan("ok").model_dump_json()

    monkeypatch.setattr(llm, "_call", fake_call)
    monkeypatch.setattr(llm, "FALLBACK_MODEL", "fallback-model")
    assert llm.structured([{"role": "user", "content": "x"}], NegotiationPlan).headline == "ok"
    assert used == [llm.MODEL, "fallback-model"]


# ---------------- automatic answers ----------------

from backend.autoresolve import auto_analyze  # noqa: E402
from backend.autoresolve import Decisions  # noqa: E402
from backend.present import build_display  # noqa: E402
from backend.tactics import tactics  # noqa: E402


def _fake_decisions(pick):
    def fake(messages, model_cls, **k):
        assert model_cls is Decisions
        questions = json.loads(messages[-1]["content"])["questions"]
        return Decisions(decisions=[{"id": q["id"], "option": pick(q), "reason": "test"} for q in questions])
    return fake


def test_model_picks_the_code(monkeypatch):
    # choose the option whose label starts with 99213
    pick = lambda q: next(o["id"] for o in q["options"] if o["label"].startswith("99213"))
    monkeypatch.setattr(llm, "structured", _fake_decisions(pick))
    a = auto_analyze(sample("clinic_mismatch"))
    assert a["status"] == "complete"
    assert a["lines"][0]["state"] == "assumed" and a["lines"][0]["selected"]["code"] == "99213"
    assert a["assumptions"][0]["by"] == "model"


def test_invented_option_falls_back_to_rule(monkeypatch):
    monkeypatch.setattr(llm, "structured", _fake_decisions(lambda q: "99"))
    a = auto_analyze(sample("clinic_mismatch"))
    assert a["assumptions"][0]["by"] == "rule" and a["lines"][0]["selected"]["code"] == "99213"


def test_everything_unclear_is_decided_by_rules(monkeypatch):
    monkeypatch.setattr(llm, "structured", lambda *a, **k: (_ for _ in ()).throw(llm.LLMUnavailable("off")))
    bill = sample("imaging_wps")
    bill.insurance.coverage_status = "unknown"
    bill.insurance.plan_name = None
    bill.document.document_type = "other"
    a = auto_analyze(bill)
    assert a["status"] == "complete"
    chose = {x["kind"]: x["chose"] for x in a["assumptions"]}
    assert chose["coverage"] == "insured" and chose["bill_type"] == "institutional" and chose["plan"] == "none"


def test_unknown_hospital_is_priced_as_uw_health(monkeypatch):
    """Demo: a provider we can't find is compared with UW Health's prices by code, and says so."""
    monkeypatch.setattr(llm, "structured", lambda *a, **k: (_ for _ in ()).throw(llm.LLMUnavailable("off")))
    bill = sample("er_uninsured")
    bill.provider.billing_provider_name = "SSM Health St. Mary's Hospital Madison"
    bill.provider.billing_npi = None
    a = auto_analyze(bill)
    assert a["status"] == "complete"
    assert a["hospital"]["org_key"] == "uwhc" and a["hospital"]["matched_by"] == "demo_default"
    assert any(l.get("benchmarks") and l["benchmarks"]["target"] is not None for l in a["lines"])
    display = build_display(a, tactics(a, bill), script.template_plan(a, tactics(a, bill), bill), bill)
    assert "UW Health" in display["assumptions"][0]


def test_kept_printed_code_with_level_mismatch_gets_its_own_tactic(monkeypatch):
    pick = lambda q: next(o["id"] for o in q["options"] if o["label"].startswith("99214"))
    monkeypatch.setattr(llm, "structured", _fake_decisions(pick))
    bill = sample("clinic_mismatch")
    a = auto_analyze(bill)
    line = a["lines"][0]
    assert line["state"] == "printed_kept"
    assert line["level_mismatch"] == {"description_level": 3, "billed_code_level": 4, "code_for_description_level": "99213"}
    ids = [t["id"] for t in script.tactics(a, bill)]
    assert "code_level_mismatch" in ids and "confirm_codes" not in ids


def test_billed_amount_equal_to_list_price_decides_the_code(monkeypatch):
    monkeypatch.setattr(llm, "structured", _fake_decisions(lambda q: "none"))  # model would give up
    bill = sample("er_uninsured")
    bill.line_items[0].code_as_printed = None
    bill.line_items[0].description_as_printed = "EMERGENCY DEPT VISIT"
    bill.line_items[0].candidate_codes = [CandidateCode(code=c) for c in ("99285", "99284", "99283")]
    a = auto_analyze(bill)
    line = a["lines"][0]
    assert line["selected"]["code"] == "99284" and line["benchmarks"]["cash_price"] == 1423.8
    assert a["assumptions"][0]["by"] == "rule" and "list price" in a["assumptions"][0]["reason"]



@pytest.mark.parametrize("name", ["er_uninsured", "imaging_wps", "knee_inpatient", "clinic_mismatch"])
def test_display_is_ready_to_render(monkeypatch, name):
    monkeypatch.setattr(llm, "structured", lambda *a, **k: (_ for _ in ()).throw(llm.LLMUnavailable("off")))
    d = client.post("/api/plan", json={"bill": client.get(f"/api/samples/{name}.json").json()}).json()["display"]
    assert d["hero"]["headline"] and d["hero"]["savings"] and d["provider"] and d["insurance"]
    assert d["lines"] and all(c["name"] and not c["name"].startswith("HB-") for c in d["lines"])
    assert len(d["call_script"]) >= 3 and d["letter"]["subject"] and d["letter"]["body"][-1].endswith("[Your name]")
    assert d["sources"] and d["disclaimer"]
    assert "match score" not in json.dumps(d)


def test_insured_unknown_payer_uses_insurer_rates(monkeypatch):
    """Insurer not in our data and no hospital price list: fall back to insurer contracts, then other providers."""
    monkeypatch.setattr(llm, "structured", lambda *a, **k: (_ for _ in ()).throw(llm.LLMUnavailable("off")))
    bill = sample("er_uninsured")
    bill.insurance.coverage_status = "insured"
    bill.insurance.payer_name = "BlueCross BlueShield"
    a = auto_analyze(bill)
    priced = [l for l in a["lines"] if l.get("benchmarks") and l["benchmarks"]["target"] is not None]
    assert priced and a["totals"]["compared_target"] < a["totals"]["compared_billed"]
    display = build_display(a, tactics(a, bill), script.template_plan(a, tactics(a, bill), bill), bill)
    assert display["hero"]["billed"] and display["hero"]["savings"]
    assert all(c["fair_price"] is not None and c["savings"] is not None for c in display["lines"]
               if c["ref"] in {l["ref"] for l in priced} and c["billed"] > c["fair_price"])


@pytest.mark.parametrize("name", sorted(p.stem for p in SAMPLES.glob("*.json")))
def test_every_sample_shows_savings(monkeypatch, name):
    """Demo guarantee: every sample bill finds something to save."""
    monkeypatch.setattr(llm, "structured", lambda *a, **k: (_ for _ in ()).throw(llm.LLMUnavailable("off")))
    bill = sample(name)
    a = auto_analyze(bill)
    t = tactics(a, bill)
    hero = build_display(a, t, script.template_plan(a, t, bill), bill)["hero"]
    assert hero["savings"] and hero["savings"] > 0, hero


@pytest.mark.parametrize("name", sorted(p.stem for p in SAMPLES.glob("*.json")))
def test_uploaded_sample_uses_known_extraction(name):
    truth = sample(name)
    misread = truth.model_copy(deep=True)
    misread.line_items = misread.line_items[:1]  # the model read the image badly...
    misread.provider.billing_provider_name = (truth.provider.billing_provider_name or "").upper()
    assert known_sample(misread) == truth       # ...but the sample is recognized by name + total
    other = truth.model_copy(deep=True)
    other.totals.total_charges = (truth.totals.total_charges or 0) + 1
    assert known_sample(other) is None


def _old_mammogram():
    """The mammogram sample as first generated: same provider and total, $298.80 balance, billed correctly."""
    bill = sample("mammogram_ultrasound")
    for line, paid, adj, owe in zip(bill.line_items, (285.00, 195.20), (285.00, 494.00), (0.00, 298.80)):
        line.insurance_paid, line.adjustment, line.patient_responsibility = paid, adj, owe
    bill.totals.insurance_payments, bill.totals.adjustments, bill.totals.patient_balance_due = 480.20, 779.00, 298.80
    return bill


def test_different_balance_is_not_the_sample():
    assert known_sample(_old_mammogram()) is None


def test_savings_never_exceed_amount_owed(monkeypatch):
    monkeypatch.setattr(llm, "structured", lambda *a, **k: (_ for _ in ()).throw(llm.LLMUnavailable("off")))
    bills = [sample(p.stem) for p in sorted(SAMPLES.glob("*.json"))] + [_old_mammogram()]
    for bill in bills:
        a = auto_analyze(bill)
        t = tactics(a, bill)
        d = build_display(a, t, script.template_plan(a, t, bill), bill)
        owed = bill.totals.patient_balance_due
        assert d["hero"]["savings"] is None or d["hero"]["savings"] <= owed + 0.005, (bill.provider, d["hero"])
        by_ref = {l.ref: l for l in bill.line_items}
        for c in d["lines"]:
            line_owed = by_ref[c["ref"]].patient_responsibility if c["ref"] in by_ref else None
            assert c["savings"] is None or line_owed is None or c["savings"] <= line_owed + 0.005


def test_extract_text_endpoint(monkeypatch):
    truth = sample("er_head_ct")
    monkeypatch.setattr(llm, "structured", lambda *a, **k: truth.model_copy(deep=True))
    r = client.post("/api/extract-text", json={"text": "St. Brigid Memorial Hospital ... TOTAL 6,853.85"})
    assert r.status_code == 200 and r.json()["totals"]["total_charges"] == truth.totals.total_charges
    assert client.post("/api/extract-text", json={"text": "  "}).status_code == 400


def test_site_is_served():
    r = client.get("/analyze.html")
    assert r.status_code == 200 and "analyze.js" in r.text
    assert client.get("/").status_code == 200
    assert client.get("/api/samples").status_code == 200  # API routes still win over the site mount


def _display(bill, monkeypatch):
    monkeypatch.setattr(llm, "structured", lambda *a, **k: (_ for _ in ()).throw(llm.LLMUnavailable("off")))
    a = auto_analyze(bill)
    t = tactics(a, bill)
    return build_display(a, t, script.template_plan(a, t, bill), bill)


def test_bottom_line_pediatric(monkeypatch):
    """Billed -> plan's negotiated rate - insurance paid = what you should pay; savings = asked - that."""
    b = _display(sample("pediatric_well_child"), monkeypatch)["bottom_line"]
    assert (b["billed"], b["fair"], b["insurance_paid"], b["should_pay"], b["asked"], b["savings"]) == \
           (1495.00, 1158.91, 872.00, 286.91, 548.25, 261.34)
    assert b["fair_label"] == "Your plan's negotiated rate" and b["title"] == "You could save $261.34"


@pytest.mark.parametrize("name", sorted(p.stem for p in SAMPLES.glob("*.json")))
def test_bottom_line_adds_up(monkeypatch, name):
    d = _display(sample(name), monkeypatch)
    b = d["bottom_line"]
    assert abs(b["should_pay"] + b["savings"] - b["asked"]) < 0.01
    if b["insurance_paid"] is not None:
        assert abs(b["fair"] - b["insurance_paid"] - b["should_pay"]) < 0.01
    assert b["savings"] == d["hero"]["savings"]  # the script and the screen quote the same savings
