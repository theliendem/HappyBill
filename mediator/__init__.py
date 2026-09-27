"""ClarityBill mediator: the only way the AI reaches the rate database.

Tool 1, resolve: bill text -> exact ids (hospital, payer, plan, codes). Fuzzy, returns no prices.
Tool 2, get_negotiated_rate: exact ids -> published rates. Exact lookups only.
get_market_rate: a code's typical rate across all loaded providers (fallback when the bill's provider isn't loaded).
"""
from .rates import InvalidIds, get_market_rate, get_negotiated_rate
from .resolve import resolve
from .tools import TOOLS, run_tool

__all__ = ["resolve", "get_negotiated_rate", "get_market_rate", "InvalidIds", "TOOLS", "run_tool"]
