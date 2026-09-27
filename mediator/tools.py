"""Claude tool definitions for the mediator, plus a dispatcher for tool_use blocks.

    from mediator.tools import TOOLS, run_tool
    client.messages.create(model=..., tools=TOOLS, messages=...)
    for block in response.content:
        if block.type == "tool_use":
            content, is_error = run_tool(block.name, block.input)
            tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                 "content": content, "is_error": is_error})
"""
import json

from pydantic import ValidationError

from .models import RateRequest, ResolveRequest
from .rates import InvalidIds, get_negotiated_rate
from .resolve import resolve

RESOLVE_DESCRIPTION = """\
Turn what is printed on a medical bill into exact database ids. Call this first, once per bill, with \
every line. Returns no prices.

It matches the hospital (NPI, then tax ID, then known name, then fuzzy name), the insurer and plan, \
and each line's billing code. Set document_type: hospital statements get facility rates, physician \
statements get professional rates.

For a line with no printed code, put your best-guess code first in candidate_codes followed by the \
close alternatives (neighboring visit levels, with/without contrast). The tool checks each guess \
against the hospital's own chargemaster text and the official description. A printed code always \
wins over guesses. Guess the code the hospital billed, never a cheaper code you think it should have \
billed.

Each line gets a status: confirmed (printed code, description agrees) or likely (exactly one guess \
clearly matches) can go straight to get_negotiated_rate. For ambiguous, needs_confirmation or \
not_found, show the user the candidates' descriptions and let them choose; do not choose for them. \
Hospital, payer and plan have their own status fields; treat ambiguous or not_found the same way."""

RATE_DESCRIPTION = """\
Look up published negotiated rates for one bill line. Accepts only ids returned by \
resolve_bill_entities: code, code_type, modifiers, hospital_item_id, org_key (or provider_tin), \
provider_npi, payer_key, plan_key, billing_class. Unknown ids are rejected. Omit payer_key for \
uninsured / self-pay patients.

Returns the hospital's list price and cash price for the item; the payer/plan's rates from the \
hospital's own price list and from the insurer's file; the range across every insurer at this \
hospital; and, when billed_amount is given, a comparison against it.

Every rate states its basis. 'dollar_rate' is a full price for the line. Percent-of-charge rates are \
converted with your billed amount or the hospital's list price. Per-day and per-anesthesia-unit rates \
are not line totals: do not present them as one. Use line_total_equivalent and the comparison \
numbers as given; do not recompute them. If found is false, no rate is published: say so and never \
estimate one. Read warnings before using the numbers."""

TOOL_MODELS = {"resolve_bill_entities": ResolveRequest, "get_negotiated_rate": RateRequest}
_HANDLERS = {"resolve_bill_entities": resolve, "get_negotiated_rate": get_negotiated_rate}


def inline_schema(model):
    """Pydantic JSON schema with $refs inlined and titles dropped, as Claude tool input_schema."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def inline(node):
        if isinstance(node, dict):
            if "$ref" in node:
                target = defs[node["$ref"].rsplit("/", 1)[-1]]
                return inline({**target, **{k: v for k, v in node.items() if k != "$ref"}})
            return {k: inline(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [inline(v) for v in node]
        return node

    return inline(schema)


TOOLS = [
    {"name": "resolve_bill_entities", "description": RESOLVE_DESCRIPTION,
     "input_schema": inline_schema(ResolveRequest)},
    {"name": "get_negotiated_rate", "description": RATE_DESCRIPTION,
     "input_schema": inline_schema(RateRequest)},
]


def run_tool(name, tool_input):
    """Execute a tool call. Returns (json_string, is_error) for a tool_result block."""
    if name not in _HANDLERS:
        return json.dumps({"error": f"unknown tool '{name}'"}), True
    try:
        req = TOOL_MODELS[name].model_validate(tool_input).model_dump()
        return json.dumps(_HANDLERS[name](req), default=str), False
    except ValidationError as e:
        return json.dumps({"error": "invalid_input", "details": e.errors(include_url=False)}, default=str), True
    except InvalidIds as e:
        return json.dumps({"error": "invalid_ids", "details": e.details,
                           "hint": "Use only ids returned by resolve_bill_entities."}), True
