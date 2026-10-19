"""
agent.py — claim extraction and classification agent loop.

Turn plan:
  Turn 1: model calls get_brand_facts (possibly multiple in parallel via tool_calls)
  Turn 2: model calls submit_claims with classified claims
  Turn 3: spare repair turn if output invalid on turn 2

Design principles:
  - MAX_TURNS = 3, no silent fallback to unknown on parse errors
  - submit_claims signals end; Python does offset location and validation
  - v2 mode pre-splits answer into numbered sentences before calling model
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from agent.provider import (
    ProviderError,
    chat_completion,
    extract_message,
    extract_text,
    extract_tool_calls,
)
from agent.tools import TOOLS, execute_tool
from agent.validate import (
    Claim,
    ValidationError,
    split_into_sentences,
    validate_claims,
)

MAX_TURNS = int(os.getenv("MAX_TURNS", "3"))
_PROMPT_DIR = Path(__file__).parent / "prompts"


@dataclass
class AgentResult:
    answer_id: str
    claims: list[Claim] = field(default_factory=list)
    errors: list[ValidationError] = field(default_factory=list)
    status: Literal["ok", "error", "partial"] = "ok"
    error_message: str = ""
    turns_used: int = 0
    latency_ms: float = 0.0
    trace_id: str = ""
    # Raw exchanges for Langfuse / debugging
    exchanges: list[dict] = field(default_factory=list)


def _load_prompt(version: str) -> str:
    path = _PROMPT_DIR / f"{version}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {path}")
    return path.read_text(encoding="utf-8")


def _build_user_message(
    question: str,
    ai_answer: str,
    version: str,
    sentences: list[tuple[str, int, int]] | None,
    brand_id: str = "",
) -> str:
    """Build the user-turn content, optionally with pre-numbered sentences for v2."""
    parts = [
        f"**Question:** {question}",
        "",
        f"**Answer:**\n{ai_answer}",
    ]
    if brand_id:
        parts.insert(0, f"**Brand ID:** {brand_id}")
    if version == "v2" and sentences:
        parts.append("")
        parts.append("**Candidate sentences (select IDs of factual claims only):**")
        for i, (sentence, _, _) in enumerate(sentences):
            parts.append(f"[S{i}] {sentence}")
    return "\n".join(parts)


async def run_agent(
    answer_id: str,
    brand_id: str,
    question: str,
    ai_answer: str,
    *,
    version: str = "v1",
    langfuse_trace=None,
) -> AgentResult:
    """
    Run the claim extraction agent for one answer.
    Returns AgentResult with claims, errors, status and latency.
    """
    t_start = time.monotonic()
    result = AgentResult(answer_id=answer_id)

    try:
        system_prompt = _load_prompt(version)
    except FileNotFoundError as e:
        result.status = "error"
        result.error_message = str(e)
        result.latency_ms = (time.monotonic() - t_start) * 1000
        return result

    # v2: pre-split into candidate sentences
    sentences: list[tuple[str, int, int]] | None = None
    if version == "v2":
        sentences = split_into_sentences(ai_answer)

    user_content = _build_user_message(question, ai_answer, version, sentences, brand_id=brand_id)

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    submitted_claims: list[dict] | None = None

    for turn in range(MAX_TURNS):
        result.turns_used = turn + 1

        # ── LLM call ───────────────────────────────────────────────────────
        obs_start = time.monotonic()
        try:
            response = await chat_completion(messages, tools=TOOLS)
        except ProviderError as e:
            result.status = "error"
            result.error_message = f"Provider error on turn {turn+1}: {e}"
            break
        obs_latency = (time.monotonic() - obs_start) * 1000

        message = extract_message(response)
        text = extract_text(message)
        tool_calls = extract_tool_calls(message)

        result.exchanges.append({
            "turn": turn + 1,
            "request_messages": [m.copy() for m in messages],
            "response_message": message,
            "latency_ms": obs_latency,
        })

        # ── Langfuse generation observation ───────────────────────────────
        if langfuse_trace is not None:
            try:
                langfuse_trace.generation(
                    name=f"turn_{turn+1}",
                    input=messages,
                    output=message,
                    metadata={"latency_ms": obs_latency, "version": version},
                )
            except Exception:
                pass  # Langfuse failures never crash the agent

        # ── Process tool calls ─────────────────────────────────────────────
        if not tool_calls:
            # No tool calls — unexpected
            if turn < MAX_TURNS - 1:
                messages.append({"role": "assistant", "content": text})
                messages.append({
                    "role": "user",
                    "content": (
                        "You must call either get_brand_facts or submit_claims. "
                        "Please call submit_claims with your findings now."
                    ),
                })
                continue
            else:
                result.status = "error"
                result.error_message = "Model did not call submit_claims within turn limit"
                break

        # Append assistant message with tool calls
        messages.append({"role": "assistant", "content": text, "tool_calls": tool_calls})

        tool_results = []
        for tc in tool_calls:
            tc_name = tc["function"]["name"]
            try:
                tc_args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                tc_args = {}

            if tc_name == "submit_claims":
                submitted_claims = tc_args.get("claims", [])
                tool_results.append({
                    "tool_call_id": tc["id"],
                    "role": "tool",
                    "name": tc_name,
                    "content": json.dumps({"status": "received"}),
                })
                # Log tool span in Langfuse
                if langfuse_trace is not None:
                    try:
                        langfuse_trace.span(
                            name="submit_claims",
                            input=submitted_claims,
                            output={"status": "received"},
                        )
                    except Exception:
                        pass
                break  # submit_claims ends the loop
            else:
                tool_output = execute_tool(tc_name, tc_args)
                tool_results.append({
                    "tool_call_id": tc["id"],
                    "role": "tool",
                    "name": tc_name,
                    "content": tool_output,
                })
                # Log tool span in Langfuse
                if langfuse_trace is not None:
                    try:
                        langfuse_trace.span(
                            name=tc_name,
                            input=tc_args,
                            output=json.loads(tool_output),
                        )
                    except Exception:
                        pass

        messages.extend(tool_results)

        if submitted_claims is not None:
            break  # got submit_claims

    # ── Validate submitted claims ─────────────────────────────────────────
    if submitted_claims is not None:
        claims, validation_errors = validate_claims(
            answer_id=answer_id,
            ai_answer=ai_answer,
            raw_claims=submitted_claims,
            version=version,
            sentences=sentences,
        )
        result.claims = claims
        result.errors = validation_errors
        if validation_errors and not claims:
            result.status = "error"
            result.error_message = f"{len(validation_errors)} validation errors, 0 valid claims"
        elif validation_errors:
            result.status = "partial"
    elif result.status == "ok":
        result.status = "error"
        result.error_message = "submit_claims was never called"

    result.latency_ms = (time.monotonic() - t_start) * 1000
    return result
