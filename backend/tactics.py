"""Which negotiation tactics apply, decided by rules (not AI) from the analysis numbers.

Each tactic: id, title, why (for the user), step (what the user should do), say (a first-person sentence
for the call/letter), numbers ({label: dollars}). The script step turns these into prose; the template
fallback uses them directly.
"""
from collections import Counter

from .present import line_name, money, provider_name


def _tactic(id, title, why, step, say, numbers=None, facts=None):
    """facts: the specifics (which lines, which codes) the script must mention, without prewritten wording."""
    return {"id": id, "title": title, "why": why, "step": step, "say": say, "numbers": numbers or {},
            "facts": facts or []}


def tactics(analysis, bill):
    out = []
    lines = [l for l in analysis["lines"] if l["kind"] == "line"]
    counted = [l for l in analysis["lines"] if l.get("benchmarks") and l["benchmarks"]["target"] is not None]
    totals = analysis["totals"]
    coverage = analysis["coverage"]
    hospital = analysis["hospital"].get("name") or "the hospital"
    payer = (analysis["payer"] or {}).get("name") or "my insurer"

    if not analysis["is_itemized"] or not lines:
        out.append(_tactic(
            "itemized_bill", "Ask for an itemized bill",
            "This bill doesn't list each service with its billing code, so the charges can't be checked.",
            "Request a fully itemized bill with CPT/HCPCS codes before paying anything.",
            "Before I pay, please send me a fully itemized bill listing each service with its CPT or HCPCS code, units and charge."))

    by_line = {b.ref: b for b in bill.line_items}
    dup_keys = Counter((l["description"].strip().lower(), by_line[l["ref"]].service_date, l["billed"])
                       for l in lines if l["ref"] in by_line)
    dups = [l for l in lines if l["ref"] in by_line and dup_keys[(l["description"].strip().lower(),
                                                                   by_line[l["ref"]].service_date, l["billed"])] > 1]
    if dups:
        seen = {}
        for l in dups:
            seen.setdefault(line_name(l), l["billed"])
        out.append(_tactic(
            "duplicate_charges", "Question possible duplicate charges",
            "The same service appears more than once on the same date for the same amount.",
            "Ask the billing office to confirm each repeated charge or remove it.",
            "Some charges appear twice on the same date for the same amount; please confirm they are separate services or remove the duplicates.",
            {f"{d} (each)": b for d, b in seen.items()},
            [f"'{d}' is billed more than once on the same date" for d in seen]))

    stay = next((l for l in counted if l["kind"] == "stay"), None)
    if coverage == "uninsured_self_pay":
        cash = [l for l in counted if l["kind"] == "line" and l["benchmarks"]["cash_price"] is not None]
        cash_total = round(sum(l["benchmarks"]["cash_price"] for l in cash), 2) if cash and not stay else None
        if cash_total is not None:
            billed_c = round(sum(l["benchmarks"]["billed"] for l in cash), 2)
            if cash_total < billed_c:
                out.append(_tactic(
                    "self_pay_price", "Ask for the hospital's own published cash price",
                    f"{hospital} publishes a discounted cash price for these services in its federally required price list.",
                    "Ask them to reprice your bill to their published discounted cash prices.",
                    f"Your published price list shows a discounted cash price of {money(cash_total)} for these services, "
                    f"but I was billed {money(billed_c)}. Please reprice my bill to your published cash prices.",
                    {"Billed for these services": billed_c, "Published cash price": cash_total}))
        commercial = [l for l in counted if l["benchmarks"]["commercial_median"] is not None]
        if commercial:
            c_billed = round(sum(l["benchmarks"]["billed"] for l in commercial), 2)
            c_median = round(sum(l["benchmarks"]["commercial_median"] for l in commercial), 2)
            # Only worth citing when it asks for less than the hospital's own cash price.
            if c_median < c_billed and (cash_total is None or c_median < cash_total):
                out.append(_tactic(
                    "insurer_benchmark", "Point to what insurers pay",
                    "Commercial insurers pay far less than the list price for the same services at this hospital.",
                    "Ask for your bill to be reduced toward the typical commercial insurer rate.",
                    f"According to your own published rates, the typical commercial insurer pays {money(c_median)} "
                    f"for these services, compared with the {money(c_billed)} I was billed. I'm asking for a similar rate.",
                    {"Billed": c_billed, "Typical commercial insurer rate": c_median}))
        out.append(_tactic(
            "financial_assistance", "Ask about financial assistance",
            "Many hospitals, especially nonprofits, must offer financial assistance; eligibility often reaches well into middle incomes.",
            "Ask for the financial assistance application and for your account to be held from collections while it's reviewed.",
            "Please send me your financial assistance application, and hold my account from collections while it's reviewed."))
        out.append(_tactic(
            "payment_terms", "Negotiate how you pay",
            "Even after a reduction, you can often get a further discount for paying promptly, or an interest-free plan.",
            "Ask for a prompt-pay discount if you can pay in full, or an interest-free payment plan.",
            "If I can pay the adjusted balance promptly, is there an additional prompt-pay discount? Otherwise I'd like an interest-free payment plan."))

    elif coverage == "insured":
        # $0.00 paid (e.g. applied to the deductible) still means the claim was processed.
        processed = bill.totals.insurance_payments is not None or any(
            b.insurance_paid is not None for b in bill.line_items)
        if not processed:
            out.append(_tactic(
                "confirm_claim", "Make sure your insurance was billed first",
                "This bill shows no insurance payment yet. You should only owe your share of the plan's negotiated rate.",
                "Ask them to confirm the claim went to your insurer, and don't pay until you get your Explanation of Benefits.",
                f"This bill doesn't show a payment from {payer}. Please confirm the claim was submitted, "
                "and hold my account until it's processed so I can compare it with my Explanation of Benefits."))
        over = [l for l in counted if l["benchmarks"]["patient_responsibility"] is not None
                and l["benchmarks"]["max_you_should_owe"] is not None
                and l["benchmarks"]["patient_responsibility"] > l["benchmarks"]["max_you_should_owe"] + 0.01]
        if over:
            numbers = {}
            for l in over:
                b = l["benchmarks"]
                numbers[f"{l['description']}: you're asked to pay"] = b["patient_responsibility"]
                numbers[f"{l['description']}: plan's negotiated rate"] = b["your_plan_rate_high"]
                if b["insurance_paid"]:
                    numbers[f"{l['description']}: insurer already paid"] = b["insurance_paid"]
                numbers[f"{l['description']}: most you should owe"] = b["max_you_should_owe"]
            asked = round(sum(l["benchmarks"]["patient_responsibility"] for l in over), 2)
            should = round(sum(l["benchmarks"]["max_you_should_owe"] for l in over), 2)
            out.append(_tactic(
                "over_plan_rate", "You're being asked for more than your plan allows",
                "In-network providers agree to accept the plan's negotiated rate as payment in full. Your share "
                "can't be more than that rate minus what your insurer paid.",
                "Ask for the balance to be corrected; this is a billing error, not a favor.",
                f"I'm being asked to pay {money(asked)}, but under my plan's negotiated rate, minus what my insurer "
                f"already paid, the most I should owe is {money(should)}. Please correct my balance.",
                numbers | {"You're asked to pay (total)": asked, "Most you should owe (total)": should}))
        check = totals.get("balance_check")
        if check and check["overcharge"] > 0.01:
            out.append(_tactic(
                "over_plan_rate", "You're being asked for more than your plan allows",
                "In-network providers agree to accept the plan's negotiated rate as payment in full. Your share "
                "can't be more than that rate minus what your insurer paid.",
                "Ask for the balance to be corrected; this is a billing error, not a favor.",
                f"My balance is {money(check['patient_balance_due'])}, but my plan's negotiated rates total "
                f"{money(check['plan_rate_total'])} and my insurer paid {money(check['insurance_paid'])}, so the most "
                f"I should owe is {money(check['max_you_should_owe'])}. Please correct my balance.",
                {"Balance due": check["patient_balance_due"], "Plan's negotiated rates": check["plan_rate_total"],
                 "Insurer paid": check["insurance_paid"], "Most you should owe": check["max_you_should_owe"]}))
        planned = [l for l in counted if l["benchmarks"]["target_basis"] == "your_plan_rate"]
        overbilled = over or (check and check["overcharge"] > 0.01)
        if planned and not overbilled:  # the overbilling tactic already cites the plan rate
            p_billed = round(sum(l["benchmarks"]["billed"] for l in planned), 2)
            p_rate = round(sum(l["benchmarks"]["target"] for l in planned), 2)
            out.append(_tactic(
                "plan_rate_reference", "Know your plan's negotiated rates",
                "Your plan pays a published negotiated rate; your deductible and coinsurance are based on that amount, not the billed charge.",
                "Check that your Explanation of Benefits uses these allowed amounts.",
                f"My plan's published negotiated rates for these services total {money(p_rate)}, versus {money(p_billed)} billed. "
                "Please confirm my share is calculated from the negotiated rates.",
                {"Billed": p_billed, "Your plan's negotiated rates": p_rate}))
        if bill.encounter.setting == "emergency" and bill.insurance.network_status == "out_of_network":
            out.append(_tactic(
                "no_surprises_act", "Emergency care is protected from surprise bills",
                "Under the federal No Surprises Act, out-of-network emergency care is billed at in-network cost-sharing.",
                "Tell the billing office you expect in-network cost-sharing for this emergency visit.",
                "This was emergency care, so under the No Surprises Act I should only be charged in-network cost-sharing."))

    # Lines priced from insurer contracts (this provider's, or other providers' when it isn't in our data).
    contracted = [l for l in counted if l["kind"] == "line" and l["benchmarks"]["target_basis"]
                  in ("insurer_rate_this_provider", "market_rate")]
    if contracted:
        k_billed = round(sum(l["benchmarks"]["billed"] for l in contracted), 2)
        k_rate = round(sum(l["benchmarks"]["target"] for l in contracted), 2)
        elsewhere = all(l["benchmarks"]["target_basis"] == "market_rate" for l in contracted)
        on_bill = provider_name(analysis, bill) or hospital
        where = "other providers" if elsewhere else on_bill
        if k_rate < k_billed:
            out.append(_tactic(
                "insurer_rates", "Point to what insurers pay",
                (f"{on_bill}'s own price list isn't in our data, but insurers' published rates for the same "
                 "billing codes at other providers are far lower than these charges." if elsewhere else
                 f"Insurers' published contracts with {on_bill} pay far less than these charges for the same services."),
                "Ask the billing office to reduce these charges toward the rates insurers pay.",
                f"Published insurer rates for these services at {where} total about {money(k_rate)}, compared with "
                f"the {money(k_billed)} I was billed. Please review these charges and reduce them toward those rates.",
                {"Billed for these services": k_billed, "Published insurer rates": k_rate},
                [f"'{line_name(l)}' (code {l['selected']['code']}) was billed {money(l['billed'])}; insurers pay "
                 f"{money(l['benchmarks']['target'])}" for l in contracted]))

    mismatched = [l for l in lines if l.get("level_mismatch")]
    if mismatched:
        out.append(_tactic(
            "code_level_mismatch", "Question the visit level billed",
            "The line's description names a lower visit level than the code billed; higher levels are paid more, "
            "so this is a common overbilling error.",
            "Ask them to check the visit level and re-bill at the level the description shows.",
            " ".join(f"The line \"{line_name(l)}\" describes a level {l['level_mismatch']['description_level']} "
                     f"visit but was billed as {l['selected']['code']}, a level {l['level_mismatch']['billed_code_level']} "
                     f"code. Please review it and correct it to {l['level_mismatch']['code_for_description_level']} if "
                     "that matches the visit." for l in mismatched),
            {line_name(l): l["billed"] for l in mismatched},
            [f"The bill describes '{l['description']}' (a level {l['level_mismatch']['description_level']} visit) but was billed "
             f"with code {l['selected']['code']} (a level {l['level_mismatch']['billed_code_level']} visit); the matching "
             f"code would be {l['level_mismatch']['code_for_description_level']}" for l in mismatched]))

    assumed = [l for l in lines if l.get("state") == "assumed"]
    if assumed:
        out.append(_tactic(
            "confirm_codes", "Confirm the billing codes we matched",
            "Some lines had a missing or unclear code, so we matched them from their descriptions.",
            "Ask which code was billed for these lines; if it differs, the comparison changes.",
            "For a few lines, please confirm the CPT or HCPCS code that was billed: "
            + "; ".join(f"{line_name(l)} (we matched {l['selected']['code']})" for l in assumed) + ".",
            {line_name(l): l["billed"] for l in assumed},
            [f"'{line_name(l)}' was matched to code {l['selected']['code']}" for l in assumed]))

    unpriced = [l for l in lines if l.get("state") == "skipped" or l["ref"] in totals["lines_not_priced"]]
    if unpriced and not stay:
        out.append(_tactic(
            "unclear_lines", "Ask about charges we couldn't match",
            "We couldn't find published prices for some lines, often because the code is missing or unclear.",
            "Ask what each of these charges is and which code was billed.",
            "Please explain these charges and tell me the billing code for each.",
            {line_name(l): l["billed"] for l in unpriced}))
    return out
