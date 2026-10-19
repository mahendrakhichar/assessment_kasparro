"""
runner.py — async harness runner.

Design:
  - asyncio.Semaphore(3) limits concurrent LLM requests (including retries).
  - One asyncio Task per answer; failures are isolated.
  - Persists predictions to DB before scoring (scoring reads from predictions).
  - Check runs: no gold, no scoring, quality fields stay null.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from agent.agent import AgentResult, run_agent
from harness.loader import AnswerRow, GoldClaim, load_answers, load_gold, sha256_file
from harness.scorer import Metrics, format_metrics_table, score
from dotenv import load_dotenv

load_dotenv()

MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "3"))
DATA_DIR = Path(__file__).parent.parent / "data"
RUNS_DIR = Path(__file__).parent.parent / "runs"


@dataclass
class AnswerRunResult:
    answer_id: str
    brand_id: str
    status: str
    error_message: str
    latency_ms: float
    turns_used: int
    claims: list[dict]
    validation_errors: list[dict]


@dataclass
class RunResult:
    run_id: str
    version: str
    split: Literal["dev", "check"]
    provider: str
    model: str
    answer_results: list[AnswerRunResult] = field(default_factory=list)
    metrics: Metrics | None = None  # None for check runs
    metrics_summary: str = ""
    file_hashes: dict[str, str] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""


async def _run_one(
    row: AnswerRow,
    version: str,
    sem: asyncio.Semaphore,
    langfuse_client=None,
) -> AnswerRunResult:
    """Run agent for one answer, bounded by semaphore."""
    trace = None
    if langfuse_client is not None:
        try:
            trace = langfuse_client.trace(
                name=f"claim_extraction_{version}",
                input={"answer_id": row.answer_id, "brand_id": row.brand_id},
                metadata={"version": version},
            )
        except Exception:
            trace = None

    async with sem:
        result: AgentResult = await run_agent(
            answer_id=row.answer_id,
            brand_id=row.brand_id,
            question=row.question,
            ai_answer=row.ai_answer,
            version=version,
            langfuse_trace=trace,
        )

    if trace is not None:
        try:
            trace.update(
                output={"status": result.status, "claim_count": len(result.claims)},
                metadata={"latency_ms": result.latency_ms, "turns": result.turns_used},
            )
        except Exception:
            pass

    print(f"      [{row.answer_id}] {result.status.upper()} ({result.latency_ms:.0f}ms, {len(result.claims)} claims)", flush=True)
    await asyncio.sleep(1.0)

    return AnswerRunResult(
        answer_id=row.answer_id,
        brand_id=row.brand_id,
        status=result.status,
        error_message=result.error_message,
        latency_ms=result.latency_ms,
        turns_used=result.turns_used,
        claims=[
            {
                "answer_id": c.answer_id,
                "start_char": c.start_char,
                "end_char": c.end_char,
                "quote": c.quote,
                "label": c.label,
                "fact_ids": c.fact_ids,
                "reason": c.reason,
            }
            for c in result.claims
        ],
        validation_errors=[{"code": e.code, "detail": e.detail} for e in result.errors],
    )


async def run_split(
    version: str,
    split: Literal["dev", "check"],
    *,
    answers_path: Path | None = None,
    gold_path: Path | None = None,
    langfuse_client=None,
    db_conn=None,
) -> RunResult:
    """
    Run the agent on all answers in the split.
    Scores on dev, skips scoring on check (per spec).
    """
    import time as _time
    from datetime import datetime, timezone

    if answers_path is None:
        answers_path = DATA_DIR / f"{split}_answers.csv"
    if gold_path is None:
        gold_path = DATA_DIR / f"{split}_gold.csv" if split == "dev" else None

    answers = load_answers(answers_path)
    run_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc).isoformat()

    file_hashes: dict[str, str] = {str(answers_path): sha256_file(answers_path)}
    if gold_path and gold_path.exists():
        file_hashes[str(gold_path)] = sha256_file(gold_path)

    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    tasks = [_run_one(row, version, sem, langfuse_client) for row in answers]
    answer_results: list[AnswerRunResult] = await asyncio.gather(*tasks)

    run = RunResult(
        run_id=run_id,
        version=version,
        split=split,
        provider=os.getenv("PROVIDER", "openai"),
        model=os.getenv("MODEL", "gpt-4o-mini"),
        answer_results=list(answer_results),
        file_hashes=file_hashes,
        started_at=started,
        finished_at=datetime.now(timezone.utc).isoformat(),
    )

    # ── Score dev runs ─────────────────────────────────────────────────────
    if split == "dev" and gold_path and gold_path.exists():
        gold = load_gold(gold_path)
        all_gold = [
            {
                "answer_id": aid,
                "start_char": c.start_char,
                "end_char": c.end_char,
                "label": c.label,
                "fact_ids": c.fact_ids,
            }
            for aid, claims in gold.items()
            for c in claims
        ]
        all_preds = [c for ar in answer_results for c in ar.claims]
        error_ids = {ar.answer_id for ar in answer_results if ar.status == "error"}
        run.metrics = score(all_gold, all_preds, error_ids)
        run.metrics_summary = format_metrics_table(run.metrics)

    # ── Save run JSON ──────────────────────────────────────────────────────
    RUNS_DIR.mkdir(exist_ok=True)
    run_path = RUNS_DIR / f"{version}_{split}.json"
    _save_run(run, run_path)

    # ── Persist to DB if connection provided ───────────────────────────────
    if db_conn is not None:
        await _persist_run(run, db_conn)

    return run


def _save_run(run: RunResult, path: Path) -> None:
    """Serialize RunResult to JSON (metrics converted to dict)."""
    data: dict[str, Any] = {
        "run_id": run.run_id,
        "version": run.version,
        "split": run.split,
        "provider": run.provider,
        "model": run.model,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "file_hashes": run.file_hashes,
        "metrics_summary": run.metrics_summary,
        "metrics": None,
        "answer_results": [
            {
                "answer_id": ar.answer_id,
                "brand_id": ar.brand_id,
                "status": ar.status,
                "error_message": ar.error_message,
                "latency_ms": ar.latency_ms,
                "turns_used": ar.turns_used,
                "claims": ar.claims,
                "validation_errors": ar.validation_errors,
            }
            for ar in run.answer_results
        ],
    }
    if run.metrics is not None:
        m = run.metrics
        data["metrics"] = {
            "G": m.G, "P": m.P, "M": m.M, "C": m.C,
            "extraction_f1": m.extraction_f1,
            "joint_f1": m.joint_f1,
            "macro_f1": m.macro_f1,
            "per_label": m.per_label,
            "confusion": m.confusion,
            "missed": m.missed,
            "extra": m.extra,
            "wrong_label": m.wrong_label,
        }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


async def _persist_run(run: RunResult, db_conn) -> None:
    """Write run and answer_results to PostgreSQL."""
    import json as _json
    metrics_json = None
    if run.metrics is not None:
        m = run.metrics
        metrics_json = _json.dumps({
            "extraction_f1": m.extraction_f1,
            "joint_f1": m.joint_f1,
            "macro_f1": m.macro_f1,
            "G": m.G, "P": m.P, "M": m.M, "C": m.C,
            "per_label": m.per_label,
        })

    await db_conn.execute(
        """
        INSERT INTO runs (run_id, split, version, provider, model, settings, file_hashes, metrics, status)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'completed')
        ON CONFLICT (run_id) DO NOTHING
        """,
        run.run_id, run.split, run.version, run.provider, run.model,
        _json.dumps({}), _json.dumps(run.file_hashes), metrics_json,
    )
    for ar in run.answer_results:
        await db_conn.execute(
            """
            INSERT INTO answer_results (run_id, answer_id, status, error_message, latency_ms)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (run_id, answer_id) DO NOTHING
            """,
            run.run_id, ar.answer_id, ar.status, ar.error_message or None, ar.latency_ms,
        )
        for c in ar.claims:
            await db_conn.execute(
                """
                INSERT INTO claim_results
                  (run_id, answer_id, start_char, end_char, quote, label, fact_ids, reason)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (run_id, answer_id, start_char, end_char) DO NOTHING
                """,
                run.run_id, c["answer_id"], c["start_char"], c["end_char"],
                c["quote"], c["label"], _json.dumps(c["fact_ids"]), c["reason"],
            )
