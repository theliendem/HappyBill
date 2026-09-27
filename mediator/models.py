"""Request shapes for the two mediator tools. These models are the single source for both the HTTP
API's validation and the Claude tool input_schemas (see tools.py)."""
from typing import Literal

from pydantic import BaseModel, Field

CodeType = Literal["CPT", "HCPCS", "MS-DRG", "APR-DRG", "EAPG", "RC", "NDC", "LOCAL", "CDM"]
BillingClass = Literal["professional", "institutional"]
DocumentType = Literal["itemized_hospital_statement", "ub04_facility_claim", "physician_statement",
                       "cms1500_professional_claim", "summary_statement", "eob", "other"]


class HospitalIn(BaseModel):
    name: str | None = Field(None, description="Billing provider name as printed, e.g. 'UW Health University Hospital'.")
    facility_name: str | None = Field(None, description="Facility/location name if different from the billing name.")
    npi: str | None = Field(None, description="10-digit billing NPI if printed. Strongest match key.")
    tin: str | None = Field(None, description="Tax ID / EIN if printed, digits only.")


class InsuranceIn(BaseModel):
    coverage_status: Literal["insured", "uninsured_self_pay", "unknown"] = "unknown"
    payer_name: str | None = Field(None, description="Insurer as printed, e.g. 'WPS Health Solutions'.")
    plan_name: str | None = Field(None, description="Plan/product/network as printed, e.g. 'Statewide', 'HealthyU'.")
    plan_type: Literal["commercial", "medicare_advantage", "medicaid", "medicare", "other", "unknown"] | None = None


class CandidateCode(BaseModel):
    code: str = Field(description="A code you believe this line could be, e.g. '70553'.")
    code_type: Literal["CPT", "HCPCS", "MS-DRG", "APR-DRG"] | None = None


class LineIn(BaseModel):
    ref: str | None = Field(None, description="Your identifier for the bill line, echoed back.")
    code_as_printed: str | None = Field(None, description="Code text exactly as printed, e.g. '99213-25', 'CPT 70553', 'DRG 470'.")
    code_type: CodeType | None = Field(None, description="Code type if the bill labels it.")
    modifiers: list[str] = Field(default_factory=list, description="Modifiers if printed separately, e.g. ['26'].")
    description: str | None = Field(None, description="Line description exactly as printed. Used to confirm codes.")
    revenue_code: str | None = Field(None, description="4-digit UB-04 revenue code if printed, e.g. '0611'.")
    candidate_codes: list[CandidateCode] = Field(
        default_factory=list,
        description="Your best guess first, then close alternatives (neighboring levels, with/without contrast). "
                    "Required when no code is printed. Guess the code the hospital billed, never a 'corrected' one.")


class ResolveRequest(BaseModel):
    document_type: DocumentType | None = Field(None, description="Hospital statements/UB-04 -> facility rates; physician statements/CMS-1500 -> professional rates.")
    billing_class: BillingClass | None = Field(None, description="Overrides the billing class implied by document_type.")
    hospital: HospitalIn = Field(default_factory=HospitalIn)
    insurance: InsuranceIn = Field(default_factory=InsuranceIn)
    lines: list[LineIn] = Field(default_factory=list)


class RateRequest(BaseModel):
    code: str = Field(description="Exact code from resolve (selected.code).")
    code_type: CodeType = Field(description="Exact code_type from resolve.")
    modifiers: list[str] = Field(default_factory=list, description="Modifiers from resolve. Empty = base rate.")
    org_key: str | None = Field(None, description="Organization id from resolve (hospital.org_key).")
    provider_tin: str | None = Field(None, description="From resolve when the provider matched only by tax ID (no org_key).")
    provider_npi: str | None = Field(None, description="Billing NPI; narrows payer-file rates to the contracts covering this NPI.")
    hospital_item_id: int | None = Field(None, description="Chargemaster item id from resolve (selected.hospital_item_id), when several items share the code.")
    payer_key: str | None = Field(None, description="Payer id from resolve. Omit for uninsured/self-pay: returns cash price and all-payer range.")
    plan_key: str | None = Field(None, description="Plan id from resolve (plan.plan_key).")
    billing_class: BillingClass | None = Field(None, description="From resolve (billing_class).")
    billed_amount: float | None = Field(None, description="Line total billed on the bill, for the comparison and percent-of-charge math.")
    units: float = Field(1, description="Units billed on the line.")
