"""
tools.py — tool definitions and executor for the claim agent.

Tools exposed to the model:
  get_brand_facts(brand_id)  → list of fact dicts
  submit_claims(claims)      → final structured output signal
"""
from __future__ import annotations

import os
import csv
import json
import io
from pathlib import Path
from typing import Any

# ─── Tool schema (OpenAI function-calling format) ────────────────────────────

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_brand_facts",
            "description": (
                "Return all known facts for a brand. Call this once per brand "
                "mentioned in the answer. You may call it in parallel for multiple brands."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "brand_id": {
                        "type": "string",
                        "description": "The brand identifier (e.g. 'brand_001').",
                    }
                },
                "required": ["brand_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_claims",
            "description": (
                "Submit your final extracted and classified claims. "
                "Call this exactly once after classifying all factual sentences."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "claims": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "quote": {
                                    "type": "string",
                                    "description": "Exact verbatim substring from the answer (v1 only).",
                                },
                                "sentence_id": {
                                    "type": "string",
                                    "description": "Sentence ID like S0, S1 (v2 only).",
                                },
                                "label": {
                                    "type": "string",
                                    "enum": ["supported", "contradicted", "unknown"],
                                },
                                "fact_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "reason": {
                                    "type": "string",
                                    "description": "1-2 sentence explanation.",
                                },
                            },
                            "required": ["label", "fact_ids", "reason"],
                        },
                    }
                },
                "required": ["claims"],
            },
        },
    },
]


# ─── Brand facts loader ───────────────────────────────────────────────────────

_FACTS_CACHE: dict[str, list[dict]] = {}
_ALLOWLIST: set[str] = set()


def _load_brand_facts_csv(path: str | Path) -> None:
    """Load brand_facts.csv into the in-process cache."""
    global _FACTS_CACHE, _ALLOWLIST
    path = Path(path)
    if not path.exists():
        return
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bid = row["brand_id"]
            _FACTS_CACHE.setdefault(bid, []).append(dict(row))
            _ALLOWLIST.add(bid)


# Attempt to load on import
_csv_path = Path(__file__).parent.parent / "data" / "brand_facts.csv"
_load_brand_facts_csv(_csv_path)


def get_brand_facts(brand_id: str) -> list[dict]:
    """Return facts for brand_id or raise ValueError for unknown brands."""
    bid = brand_id.strip().lower()
    for known in _ALLOWLIST:
        if bid == known or known in bid:
            return _FACTS_CACHE.get(known, [])
    if _ALLOWLIST and bid not in _ALLOWLIST:
        raise ValueError(f"Unknown brand_id: {brand_id!r}. Known brands: {sorted(_ALLOWLIST)}")
    return _FACTS_CACHE.get(bid, [])


# ─── Tool executor (called by agent loop) ────────────────────────────────────

def execute_tool(name: str, args: dict[str, Any]) -> str:
    """
    Execute a tool call from the model. Only whitelisted tool names are allowed.
    Returns a JSON string to send back as the tool result.
    """
    if name == "get_brand_facts":
        brand_id = args.get("brand_id", "")
        try:
            facts = get_brand_facts(brand_id)
            return json.dumps({"brand_id": brand_id, "facts": facts})
        except ValueError as e:
            return json.dumps({"error": str(e)})

    if name == "submit_claims":
        # Signal only — the agent loop handles the data
        return json.dumps({"status": "received"})

    raise ValueError(f"Tool not in allowlist: {name!r}")
