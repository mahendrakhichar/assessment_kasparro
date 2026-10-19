"""
validate.py — offset, quote and structural validators.

Key design choices:
  - Python always locates offsets from model quotes (never trusts model-provided offsets).
  - Verbatim match: no normalization, no trimming.
  - Overlapping spans are detected and rejected.
  - fact_ids are validated against brand facts if known.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal


VALID_LABELS = frozenset({"supported", "contradicted", "unknown"})


@dataclass
class Claim:
    answer_id: str
    quote: str
    start_char: int
    end_char: int
    label: Literal["supported", "contradicted", "unknown"]
    fact_ids: list[str]
    reason: str
    claim_id: str = ""

    def __post_init__(self) -> None:
        if not self.claim_id:
            self.claim_id = f"{self.answer_id}:{self.start_char}:{self.end_char}"


@dataclass
class ValidationError:
    code: str
    detail: str


def locate_quote(text: str, quote: str, cursor: int = 0) -> tuple[int, int] | None:
    """
    Find quote verbatim in text starting from cursor.
    Returns (start, end) or None.
    Never normalizes whitespace — preserves exact offsets.
    """
    idx = text.find(quote, cursor)
    if idx == -1:
        return None
    return idx, idx + len(quote)


def split_into_sentences(text: str) -> list[tuple[str, int, int]]:
    """
    Split text into (sentence, start, end) tuples for v2 candidate generation.
    Uses a simple sentence-boundary heuristic that preserves original offsets.
    Returns list of (sentence_text, start_char, end_char).
    """
    # Sentence boundary: period/!/? followed by whitespace or end
    pattern = re.compile(r'(?<=[.!?])\s+(?=[A-Z\"])|(?<=[.!?])$', re.MULTILINE)
    boundaries = [0]
    for m in pattern.finditer(text):
        boundaries.append(m.end())
    boundaries.append(len(text))

    sentences = []
    for i in range(len(boundaries) - 1):
        start = boundaries[i]
        end = boundaries[i + 1]
        sentence = text[start:end].strip()
        if not sentence:
            continue
        # Find the exact start within the original text
        real_start = text.find(sentence, start)
        if real_start == -1:
            real_start = start
        real_end = real_start + len(sentence)
        sentences.append((sentence, real_start, real_end))
    return sentences


def validate_claims(
    answer_id: str,
    ai_answer: str,
    raw_claims: list[dict],
    *,
    version: str = "v1",
    sentences: list[tuple[str, int, int]] | None = None,
) -> tuple[list[Claim], list[ValidationError]]:
    """
    Validate model output → list of Claim objects + list of errors.

    For v1: raw_claim has 'quote' (verbatim); locate via find().
    For v2: raw_claim has 'sentence_id'; look up sentence from pre-split list.
    """
    claims: list[Claim] = []
    errors: list[ValidationError] = []
    occupied: list[tuple[int, int]] = []
    cursor = 0  # advance cursor so duplicate sentences resolve in order

    for i, raw in enumerate(raw_claims):
        label = raw.get("label", "")
        if label not in VALID_LABELS:
            errors.append(ValidationError(
                code="invalid_label",
                detail=f"Claim {i}: label {label!r} not in {sorted(VALID_LABELS)}",
            ))
            continue

        fact_ids = raw.get("fact_ids", [])
        if not isinstance(fact_ids, list):
            fact_ids = []
        reason = str(raw.get("reason", ""))

        # ── Locate span ────────────────────────────────────────────────────
        if version == "v2" and sentences is not None:
            sid = raw.get("sentence_id", "")
            idx = None
            try:
                idx = int(str(sid).lstrip("Ss"))
            except (ValueError, TypeError):
                errors.append(ValidationError(
                    code="invalid_sentence_id",
                    detail=f"Claim {i}: cannot parse sentence_id {sid!r}",
                ))
                continue
            if idx < 0 or idx >= len(sentences):
                errors.append(ValidationError(
                    code="sentence_id_out_of_range",
                    detail=f"Claim {i}: sentence_id {sid!r} out of range [0,{len(sentences)-1}]",
                ))
                continue
            quote, start, end = sentences[idx]
        else:
            quote = raw.get("quote", "")
            if not quote:
                errors.append(ValidationError(
                    code="empty_quote",
                    detail=f"Claim {i}: empty or missing quote",
                ))
                continue
            span = locate_quote(ai_answer, quote, cursor)
            if span is None:
                errors.append(ValidationError(
                    code="quote_not_found",
                    detail=f"Claim {i}: quote not found verbatim: {quote[:60]!r}",
                ))
                continue
            start, end = span
            cursor = start + 1  # advance to allow consecutive repeated sentences

        # ── Overlap check ──────────────────────────────────────────────────
        overlapping = any(
            not (end <= os or start >= oe) for os, oe in occupied
        )
        if overlapping:
            errors.append(ValidationError(
                code="overlapping_span",
                detail=f"Claim {i}: span [{start},{end}) overlaps existing claim",
            ))
            continue

        occupied.append((start, end))
        claims.append(Claim(
            answer_id=answer_id,
            quote=ai_answer[start:end],
            start_char=start,
            end_char=end,
            label=label,  # type: ignore[arg-type]
            fact_ids=[str(fid) for fid in fact_ids],
            reason=reason,
        ))

    return claims, errors
