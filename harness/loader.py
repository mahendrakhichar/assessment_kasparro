"""
loader.py — CSV loaders for dev and check answer sets.

Key concerns:
  - Use csv module with encoding='utf-8' and newline='' to preserve \\r\\n.
  - Never strip/normalize ai_answer — offsets depend on exact content.
  - Validate expected columns on load.
"""
from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


ANSWERS_COLUMNS = {"answer_id", "brand_id", "question", "ai_answer"}
GOLD_COLUMNS = {"answer_id", "start_char", "end_char", "quote", "label", "fact_ids"}


@dataclass
class AnswerRow:
    answer_id: str
    brand_id: str
    question: str
    ai_answer: str  # NEVER touch this string — offsets depend on exact content


@dataclass
class GoldClaim:
    answer_id: str
    start_char: int
    end_char: int
    quote: str
    label: str
    fact_ids: list[str]


def load_answers(path: str | Path) -> list[AnswerRow]:
    """Load answers CSV. Raises ValueError if required columns are missing."""
    path = Path(path)
    rows: list[AnswerRow] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = ANSWERS_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"answers CSV missing columns: {missing}")
        for row in reader:
            rows.append(AnswerRow(
                answer_id=row["answer_id"].strip(),
                brand_id=row["brand_id"].strip(),
                question=row["question"],
                ai_answer=row["ai_answer"],  # preserve exactly
            ))
    return rows


def load_gold(path: str | Path) -> dict[str, list[GoldClaim]]:
    """
    Load gold claims CSV. Returns dict keyed by answer_id.
    Supports either 'label' or 'expected_label', and 'fact_ids' or 'evidence_fact_ids'.
    Supports ',' or '|' separated fact IDs.
    """
    path = Path(path)
    gold: dict[str, list[GoldClaim]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = set(reader.fieldnames or [])
        has_label = "label" in fieldnames or "expected_label" in fieldnames
        has_facts = "fact_ids" in fieldnames or "evidence_fact_ids" in fieldnames
        base_cols = {"answer_id", "start_char", "end_char", "quote"}
        if not base_cols.issubset(fieldnames) or not has_label or not has_facts:
            raise ValueError(f"gold CSV missing columns: {fieldnames}")

        for row in reader:
            aid = row["answer_id"].strip()
            fids_raw = row.get("fact_ids") or row.get("evidence_fact_ids") or ""
            # Split by comma or pipe
            delims = [",", "|"]
            fact_ids = [fids_raw]
            for d in delims:
                fact_ids = [part.strip() for item in fact_ids for part in item.split(d) if part.strip()]

            label_val = (row.get("label") or row.get("expected_label") or "").strip()
            claim = GoldClaim(
                answer_id=aid,
                start_char=int(row["start_char"]),
                end_char=int(row["end_char"]),
                quote=row["quote"],
                label=label_val,
                fact_ids=fact_ids,
            )
            gold.setdefault(aid, []).append(claim)
    return gold


def sha256_file(path: str | Path) -> str:
    """Return hex SHA-256 of a file for reproducibility tracking."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
