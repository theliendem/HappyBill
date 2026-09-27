"""Step 3: turn the analysis + tactics into a negotiation plan the user can read aloud or send.

The model only writes prose. Every dollar amount it writes is checked against the numbers it was given;
if it invents one (twice), or no model is available, a template plan built from the same tactics is used.
"""
import json
import re

from . import llm
from .schemas import LinePoint, NegotiationPlan
from .present import line_name, money, provider_name
from .tactics import tactics

SYSTEM = """\
You help a patient negotiate a medical bill. You are given the bill analysis (published negotiated \
prices from the hospital's and insurer's own price files) and an ordered list of tactics that apply.

Write the plan using ONLY the dollar amounts in the data, formatted like $1,423.80. Never compute, \
estimate, round or invent a number that isn't in the data. Cover every tactic, in the order given, in \
steps, call_script and letter, including its facts (e.g. which line, which code); don't add legal claims \
the tactics don't make. Refer to the provider by the name in "provider" and the insurer by \
the name in "insurer".

Voice: calm, confident and specific, like a knowledgeable friend. Plain English; avoid insurance jargon \
such as "patient responsibility" or "allowed amount". No threats, no exclamation marks, no filler. Write \
every sentence yourself from the facts; state each number and each ask once.

Never include personal details. In call_script and letter only, use placeholders: [Your name], \
[Account number], [Date of service]; never put placeholders in headline, situation, steps or line_points. \
Refer to services by their "service" name; "ref" is only for line_points.ref, never in text.

- headline: one short sentence (under 15 words) leading with the biggest dollar amount the user can \
save or dispute.
- situation: 2-3 sentences: what was billed, what the published prices say, what that means for the user.
- steps: one short imperative sentence per tactic, in order.
- call_script: what to say to the billing office, ready to read aloud, as 3-5 short paragraphs separated \
by blank lines: (1) greeting with name and account number placeholders and why you're calling; (2) the \
main issue with its key numbers; (3) the specific asks; (4) close by asking for the corrected balance \
in writing and a reference number for the call.
- letter_subject: a specific subject line, e.g. "Request to correct balance on account [Account number]".
- letter: the same asks as a short message: "Hello," then 2-4 short paragraphs separated by blank \
lines, then "Thank you," and "[Your name]" on its own line.
- line_points: one short sentence per line in the data that has a price comparison.

Return JSON only."""

_AMOUNT = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")


def _context(analysis, bill):
    lines = []
    for l in analysis["lines"]:
        b = l.get("benchmarks")
        row = {"ref": l["ref"], "service": line_name(l), "billed": l["billed"]}
        if l.get("part_of_stay"):
            row["note"] = "covered by the inpatient case rate"
        if b:
            row.update({k: v for k, v in b.items() if v is not None and k != "billed"})
        if l.get("selected"):
            row["code"] = l["selected"]["code"]
        lines.append(row)
    return {
        "coverage": analysis["coverage"],
        "provider": provider_name(analysis, bill),
        "insurer": (analysis["payer"] or {}).get("name"),
        "plan": (analysis["plan"] or {}).get("name"),
        "totals": {k: v for k, v in analysis["totals"].items() if v is not None},
        "lines": lines,
    }


def _allowed_amounts(obj, out=None):
    out = set() if out is None else out
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.add(round(float(obj), 2))
    elif isinstance(obj, dict):
        for v in obj.values():
            _allowed_amounts(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _allowed_amounts(v, out)
    elif isinstance(obj, str):
        for m in _AMOUNT.findall(obj):
            out.add(round(float(m.replace(",", "")), 2))
    return out


def invented_amounts(plan, allowed):
    """Dollar amounts in the plan text that don't appear in the data."""
    text = json.dumps(plan.model_dump())
    found = {round(float(m.replace(",", "")), 2) for m in _AMOUNT.findall(text)}
    return sorted(a for a in found if not any(abs(a - x) < 0.005 for x in allowed))


def template_plan(analysis, tacts, bill):
    t = analysis["totals"]
    savings = t.get("potential_savings")
    hospital = provider_name(analysis, bill) or "the hospital"
    if savings:
        headline = f"Published prices suggest you could save up to {money(savings)} on this bill."
    else:
        headline = "Here's how to check this bill against published prices before you pay."
    situation = (f"You were billed {money(t['billed_all_lines'])} by {hospital}. "
                 + (f"Published rates for the services we could match come to {money(t['compared_target'])} "
                    f"against {money(t['compared_billed'])} billed." if t.get("compared_target") else
                    "We couldn't match published prices for these charges yet."))
    says = [x["say"] for x in tacts]
    main, rest = (says[0], says[1:]) if says else ("", [])
    call = "\n\n".join(p for p in (
        "Hi, my name is [Your name], and I'm calling about account [Account number] for services on "
        "[Date of service]. I've compared my bill with your published prices and have a few questions.",
        main, " ".join(rest),
        "Could you send me the corrected balance in writing, and give me a reference number for this call? Thank you.")
        if p)
    letter = "\n\n".join(p for p in (
        "Hello,",
        "I'm writing about account [Account number] for services on [Date of service]. " + main,
        " ".join(rest),
        "Please send me the corrected balance in writing.",
        "Thank you,\n[Your name]") if p)
    points = []
    for l in analysis["lines"]:
        b = l.get("benchmarks")
        if b and b["target"] is not None:
            label = {"your_plan_rate": "your plan's negotiated rate",
                     "hospital_cash_price": "the hospital's cash price",
                     "typical_commercial_insurer_rate": "the typical commercial insurer rate",
                     "insurer_rate_this_provider": "the rate insurers pay this provider",
                     "market_rate": "the typical insurer rate at other providers"}[b["target_basis"]]
            points.append(LinePoint(ref=l["ref"], point=f"{line_name(l)}: billed {money(b['billed'])}; "
                                                        f"{label} is {money(b['target'])}."))
    return NegotiationPlan(headline=headline, situation=situation, steps=[x["step"] for x in tacts],
                           call_script=call, letter_subject="Request to review my bill, account [Account number]",
                           letter=letter, line_points=points)


def write_plan(analysis, bill):
    """Returns (plan, tactics, generated_by, note)."""
    tacts = tactics(analysis, bill)
    context = _context(analysis, bill)
    # Give the model the facts and asks, not our prewritten sentences, so it doesn't paste them verbatim.
    # Tactics without dollar amounts come with wording the model may reuse; the ones with numbers it
    # writes itself, so totals aren't restated in several prewritten sentences.
    context["tactics"] = [{"title": x["title"], "why": x["why"], "ask": x["step"], "facts": x["facts"],
                           "numbers": x["numbers"]} | ({"suggested_wording": x["say"]} if "$" not in x["say"] else {})
                          for x in tacts]
    allowed = _allowed_amounts(context)
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": json.dumps(context, default=str)}]
    try:
        for _ in range(2):
            plan = llm.structured(messages, NegotiationPlan)
            bad = invented_amounts(plan, allowed)
            if not bad:
                return plan, tacts, "model", None
            messages = messages + [
                {"role": "assistant", "content": plan.model_dump_json()},
                {"role": "user", "content": "These amounts are not in the data: "
                                            + ", ".join(money(a) for a in bad)
                                            + ". Rewrite using only amounts from the data. JSON only."}]
        note = "The model used numbers not in the data; showing the template plan instead."
    except (llm.LLMUnavailable, llm.LLMBadOutput) as e:
        note = f"Model unavailable ({type(e).__name__}); showing the template plan."
    return template_plan(analysis, tacts, bill), tacts, "template", note
