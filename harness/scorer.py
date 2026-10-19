"""
scorer.py — evaluation metrics for claim extraction and classification.

Formulas (from spec):
  Let M = matched (same answer_id, start, end)
      C = correctly labeled among matched
      P = total predictions (valid claims, excluding error answers)
      G = total gold claims

  extraction_f1 = 2M / (P + G)   -- measures span quality
  joint_f1      = 2C / (P + G)   -- measures span + label quality

  per_label F1: standard TP/FP/FN over the three label classes
  macro_f1    = unweighted mean of per-label F1s

Confusion matrix:
  rows:    gold labels  [supported, contradicted, unknown, no_gold]
  columns: pred labels  [supported, contradicted, unknown, missing_or_error]
  total = G + unmatched predictions

Unit test target: G=10, P=10, M=8, C=7 → ext_f1=0.80, joint_f1=0.70
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

LABELS = ("supported", "contradicted", "unknown")


@dataclass
class ClaimKey:
    answer_id: str
    start_char: int
    end_char: int

    def __hash__(self) -> int:
        return hash((self.answer_id, self.start_char, self.end_char))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ClaimKey) and (
            self.answer_id == other.answer_id
            and self.start_char == other.start_char
            and self.end_char == other.end_char
        )


@dataclass
class Metrics:
    # Core counts
    G: int = 0  # total gold claims
    P: int = 0  # total valid predictions (error answers contribute 0)
    M: int = 0  # matched (same span)
    C: int = 0  # correctly labeled matched

    # Derived
    extraction_f1: float = 0.0
    joint_f1: float = 0.0

    # Per-label {label: {tp, fp, fn, f1}}
    per_label: dict[str, dict[str, float]] = field(default_factory=dict)
    macro_f1: float = 0.0

    # Confusion matrix: rows = gold, cols = pred
    confusion: dict[str, dict[str, int]] = field(default_factory=dict)

    # Diagnostics
    missed: list[dict] = field(default_factory=list)   # gold unmatched
    extra: list[dict] = field(default_factory=list)    # pred unmatched
    wrong_label: list[dict] = field(default_factory=list)  # matched but wrong label


def score(
    gold_claims: list[dict],     # list of {answer_id, start_char, end_char, label, ...}
    pred_claims: list[dict],     # list of {answer_id, start_char, end_char, label, ...}
    error_answer_ids: set[str],  # answers that errored — their preds are excluded from P
) -> Metrics:
    """
    Compute all metrics per spec.

    gold_claims: every gold claim in the split (include all, even from error answers)
    pred_claims: every valid prediction from the agent (exclude error answer preds)
    error_answer_ids: set of answer_ids that failed; their gold counts in G but no preds
    """
    m = Metrics()

    # Build gold index
    gold_index: dict[ClaimKey, dict] = {}
    for gc in gold_claims:
        key = ClaimKey(gc["answer_id"], int(gc["start_char"]), int(gc["end_char"]))
        gold_index[key] = gc

    # Build pred index (exclude predictions from errored answers per spec)
    pred_index: dict[ClaimKey, dict] = {}
    for pc in pred_claims:
        if pc["answer_id"] in error_answer_ids:
            continue
        key = ClaimKey(pc["answer_id"], int(pc["start_char"]), int(pc["end_char"]))
        pred_index[key] = pc

    m.G = len(gold_index)
    m.P = len(pred_index)

    # Match
    matched_gold_keys: set[ClaimKey] = set()
    matched_pred_keys: set[ClaimKey] = set()

    for key, gc in gold_index.items():
        if key in pred_index:
            m.M += 1
            matched_gold_keys.add(key)
            matched_pred_keys.add(key)
            if pred_index[key]["label"] == gc["label"]:
                m.C += 1
            else:
                m.wrong_label.append({
                    "answer_id": key.answer_id,
                    "start": key.start_char,
                    "end": key.end_char,
                    "gold_label": gc["label"],
                    "pred_label": pred_index[key]["label"],
                })

    # F1s
    denom = m.P + m.G
    m.extraction_f1 = (2 * m.M / denom) if denom > 0 else 0.0
    m.joint_f1 = (2 * m.C / denom) if denom > 0 else 0.0

    # Per-label F1
    label_tp: dict[str, int] = defaultdict(int)
    label_fp: dict[str, int] = defaultdict(int)
    label_fn: dict[str, int] = defaultdict(int)

    # TP + FN: from gold
    for key, gc in gold_index.items():
        gl = gc["label"]
        if key in matched_pred_keys:
            pl = pred_index[key]["label"]
            if pl == gl:
                label_tp[gl] += 1
            else:
                label_fn[gl] += 1
                label_fp[pl] += 1
        else:
            label_fn[gl] += 1

    # FP from unmatched preds
    for key, pc in pred_index.items():
        if key not in matched_pred_keys:
            label_fp[pc["label"]] += 1

    for lbl in LABELS:
        tp = label_tp[lbl]
        fp = label_fp[lbl]
        fn = label_fn[lbl]
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
        m.per_label[lbl] = {"tp": tp, "fp": fp, "fn": fn, "precision": prec, "recall": rec, "f1": f1}

    m.macro_f1 = sum(v["f1"] for v in m.per_label.values()) / len(LABELS)

    # Confusion matrix
    # rows: gold labels + no_gold  | cols: pred labels + missing_or_error
    row_labels = list(LABELS) + ["no_gold"]
    col_labels = list(LABELS) + ["missing_or_error"]
    conf: dict[str, dict[str, int]] = {r: {c: 0 for c in col_labels} for r in row_labels}

    for key, gc in gold_index.items():
        if key in pred_index:
            conf[gc["label"]][pred_index[key]["label"]] += 1
        else:
            conf[gc["label"]]["missing_or_error"] += 1

    for key, pc in pred_index.items():
        if key not in matched_gold_keys:
            conf["no_gold"][pc["label"]] += 1

    m.confusion = conf

    # Diagnostics
    for key, gc in gold_index.items():
        if key not in matched_gold_keys:
            m.missed.append({"answer_id": key.answer_id, "start": key.start_char,
                             "end": key.end_char, "label": gc["label"]})
    for key, pc in pred_index.items():
        if key not in matched_pred_keys:
            m.extra.append({"answer_id": key.answer_id, "start": key.start_char,
                            "end": key.end_char, "label": pc["label"]})

    return m


def format_metrics_table(m: Metrics) -> str:
    """Human-readable summary for reports."""
    lines = [
        f"G={m.G}  P={m.P}  M={m.M}  C={m.C}",
        f"Extraction F1 : {m.extraction_f1:.4f}",
        f"Joint F1      : {m.joint_f1:.4f}",
        f"Macro F1      : {m.macro_f1:.4f}",
        "",
        "Per-label F1:",
    ]
    for lbl in LABELS:
        v = m.per_label.get(lbl, {})
        lines.append(
            f"  {lbl:<15} P={v.get('precision',0):.3f}  R={v.get('recall',0):.3f}  F1={v.get('f1',0):.3f}"
            f"  (TP={v.get('tp',0)} FP={v.get('fp',0)} FN={v.get('fn',0)})"
        )
    lines.append("")
    lines.append("Confusion matrix (rows=gold, cols=pred):")
    col_order = list(LABELS) + ["missing_or_error"]
    header = f"{'':18}" + "".join(f"{c:>22}" for c in col_order)
    lines.append(header)
    for r in list(LABELS) + ["no_gold"]:
        row_str = f"{r:<18}" + "".join(f"{m.confusion.get(r,{}).get(c,0):>22}" for c in col_order)
        lines.append(row_str)
    return "\n".join(lines)
