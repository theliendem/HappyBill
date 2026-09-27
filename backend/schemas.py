"""Data shapes shared by the vision step, the manual-entry form, and the analysis/script steps.

Bill is the single source for the bill format: the vision model must return it, the website's manual
entry form produces it, and ai/bill_extraction.schema.json is generated from it
(python -m backend.schemas).
"""
from typing import Literal

from pydantic import BaseModel, Field

# ---------------- the bill (vision output == manual entry) ----------------

DocumentType = Literal["itemized_hospital_statement", "ub04_facility_claim", "physician_statement",
                       "cms1500_professional_claim", "summary_statement", "eob", "other"]
Coverage = Literal["insured", "uninsured_self_pay", "unknown"]


class BillDocument(BaseModel):
    document_type: DocumentType = Field(
        "other", description="Hospital statement / UB-04 = facility bill; physician statement / CMS-1500 = professional bill.")
    is_itemized: bool = Field(True, description="False if the bill shows only totals or departments, no per-service lines.")
    statement_date: str | None = Field(None, description="YYYY-MM-DD")


class BillProvider(BaseModel):
    billing_provider_name: str | None = Field(None, description="As printed, e.g. 'UW Health University Hospital'.")
    facility_name: str | None = Field(None, description="Location if different from the billing name.")
    billing_npi: str | None = Field(None, description="10-digit billing/facility NPI if printed.")
    tin: str | None = Field(None, description="Federal tax ID / EIN if printed, digits only.")


class BillInsurance(BaseModel):
    coverage_status: Coverage = Field("unknown", description="'Self pay' / 'no insurance on file' -> uninsured_self_pay.")
    payer_name: str | None = Field(None, description="Insurer as printed, e.g. 'WPS Health Solutions'.")
    plan_name: str | None = Field(None, description="Plan/product/network as printed, e.g. 'Statewide', 'HealthyU'.")
    plan_type: Literal["commercial", "medicare_advantage", "medicaid", "medicare", "other", "unknown"] = "unknown"
    network_status: Literal["in_network", "out_of_network", "unknown"] = "unknown"


class BillEncounter(BaseModel):
    setting: Literal["inpatient", "outpatient", "emergency", "unknown"] = "unknown"
    admission_date: str | None = None
    discharge_date: str | None = None
    drg_code: str | None = Field(None, description="DRG if printed (inpatient bills), e.g. '470'.")


class CandidateCode(BaseModel):
    code: str
    code_type: Literal["CPT", "HCPCS", "MS-DRG", "APR-DRG"] | None = None


class BillLine(BaseModel):
    ref: str | None = Field(None, description="Line id; assigned by the server if missing (L1, L2, ...).")
    service_date: str | None = None
    revenue_code: str | None = Field(None, description="4-digit UB-04 revenue code if printed, e.g. '0611'.")
    code_as_printed: str | None = Field(None, description="Exact code text as printed, e.g. '99213-25'. Null if no code is printed.")
    code_type_as_printed: Literal["CPT", "HCPCS", "MS-DRG", "APR-DRG", "NDC", "CDM"] | None = None
    modifiers: list[str] = Field(default_factory=list, description="Modifiers if printed separately.")
    description_as_printed: str = Field(description="Line description, verbatim.")
    units: float = 1
    charge_amount: float = Field(description="Billed amount for the line.")
    insurance_paid: float | None = None
    adjustment: float | None = None
    patient_responsibility: float | None = None
    legibility: Literal["clear", "partial", "illegible"] = "clear"
    candidate_codes: list[CandidateCode] = Field(
        default_factory=list,
        description="ONLY when no code is printed: your best-guess billing code first, then close alternatives "
                    "(neighboring visit levels, with/without contrast). Guess the code the hospital billed.")


class BillTotals(BaseModel):
    total_charges: float | None = None
    insurance_payments: float | None = None
    adjustments: float | None = None
    patient_balance_due: float | None = None


class Bill(BaseModel):
    """Everything on a (user-redacted) bill needed to pin each line to a negotiated rate.
    Never contains patient name, member ID, account number, date of birth or address."""
    document: BillDocument = Field(default_factory=BillDocument)
    provider: BillProvider = Field(default_factory=BillProvider)
    insurance: BillInsurance = Field(default_factory=BillInsurance)
    encounter: BillEncounter = Field(default_factory=BillEncounter)
    line_items: list[BillLine] = Field(default_factory=list)
    totals: BillTotals = Field(default_factory=BillTotals)


# ---------------- user choices after the confirmation screen ----------------

class LineChoice(BaseModel):
    skip: bool = Field(False, description="User says this line can't be matched; leave it out.")
    code: str | None = None
    code_type: str | None = None
    modifiers: list[str] | None = None
    hospital_item_id: int | None = None


class Choices(BaseModel):
    """Answers from the confirmation screen. For hospital/payer/plan, "none" means 'mine isn't listed'."""
    coverage_status: Coverage | None = None
    billing_class: Literal["institutional", "professional"] | None = None
    org_key: str | None = None
    provider_tin: str | None = None
    payer_key: str | None = None
    plan_key: str | None = None
    lines: dict[str, LineChoice] = Field(default_factory=dict, description="Keyed by line ref.")


class AnalyzeRequest(BaseModel):
    bill: Bill
    choices: Choices = Field(default_factory=Choices)


# ---------------- the negotiation plan (script model output) ----------------

class LinePoint(BaseModel):
    ref: str
    point: str = Field(description="One or two sentences on this line, citing its numbers.")


class NegotiationPlan(BaseModel):
    headline: str = Field(description="One sentence: what the user can likely save and the main lever.")
    situation: str = Field(description="2-4 plain-English sentences explaining where the bill stands.")
    steps: list[str] = Field(description="Ordered actions for the user, one sentence each.")
    call_script: str = Field(description="What to say on the phone to the billing office, first person, ready to read "
                                         "aloud, 3-5 short paragraphs separated by blank lines.")
    letter_subject: str = Field(description="Subject line for the written request.")
    letter: str = Field(description="A short written request (email/portal message) making the same asks, in "
                                    "paragraphs separated by blank lines, with a greeting and sign-off.")
    line_points: list[LinePoint] = Field(default_factory=list)


if __name__ == "__main__":
    import json
    from pathlib import Path

    from mediator.tools import inline_schema

    out = Path(__file__).parent.parent / "ai" / "bill_extraction.schema.json"
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "Bill", **inline_schema(Bill)}
    out.write_text(json.dumps(schema, indent=2) + "\n")
    print("wrote", out)
