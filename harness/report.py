"""
report.py — generate comparison reports and regression analysis.
"""
from __future__ import annotations

import json
from pathlib import Path
from harness.scorer import Metrics, format_metrics_table, LABELS


def load_run_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def compare_versions(v1_path: str | Path, v2_path: str | Path) -> str:
    """Generate a textual comparison of v1 vs v2 dev runs."""
    v1 = load_run_json(v1_path)
    v2 = load_run_json(v2_path)

    lines = ["=" * 60, "v1 vs v2 Comparison Report", "=" * 60]

    def safe_m(run: dict) -> dict:
        return run.get("metrics") or {}

    def fmt(val) -> str:
        if val is None:
            return "N/A"
        return f"{float(val):.4f}"

    for key, label in [
        ("extraction_f1", "Extraction F1"),
        ("joint_f1", "Joint F1"),
        ("macro_f1", "Macro F1"),
    ]:
        v1v = safe_m(v1).get(key)
        v2v = safe_m(v2).get(key)
        delta = ""
        if v1v is not None and v2v is not None:
            d = float(v2v) - float(v1v)
            delta = f"  Δ={d:+.4f}"
        lines.append(f"{label:<20} v1={fmt(v1v)}  v2={fmt(v2v)}{delta}")

    lines.append("")
    lines.append("Per-label F1:")
    for lbl in LABELS:
        v1f = (safe_m(v1).get("per_label") or {}).get(lbl, {}).get("f1")
        v2f = (safe_m(v2).get("per_label") or {}).get(lbl, {}).get("f1")
        delta = ""
        if v1f is not None and v2f is not None:
            d = float(v2f) - float(v1f)
            delta = f"  Δ={d:+.4f}"
        lines.append(f"  {lbl:<15} v1={fmt(v1f)}  v2={fmt(v2f)}{delta}")

    lines.append("")
    lines.append("Counts:")
    for key in ("G", "P", "M", "C"):
        v1v = safe_m(v1).get(key, "N/A")
        v2v = safe_m(v2).get(key, "N/A")
        lines.append(f"  {key}  v1={v1v}  v2={v2v}")

    # Regression analysis
    lines.append("")
    lines.append("─" * 60)
    lines.append("Regression Analysis (answers that got WORSE in v2):")
    v1_by_id = {ar["answer_id"]: ar for ar in v1.get("answer_results", [])}
    v2_by_id = {ar["answer_id"]: ar for ar in v2.get("answer_results", [])}

    regressions = []
    for aid, v2ar in v2_by_id.items():
        v1ar = v1_by_id.get(aid, {})
        if v1ar.get("status") == "ok" and v2ar.get("status") == "error":
            regressions.append(f"  {aid}: v1=ok → v2=error ({v2ar.get('error_message','')})")
        elif (v1ar.get("status") in ("ok", "partial")) and v2ar.get("status") == "error":
            regressions.append(f"  {aid}: v1={v1ar.get('status')} → v2=error")

    if regressions:
        lines.extend(regressions)
    else:
        lines.append("  No answer-level regressions detected.")

    return "\n".join(lines)
