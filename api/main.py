"""
main.py — FastAPI application with 3 endpoints.

Endpoints:
  POST /classify              — run agent on a single answer
  POST /evaluation-runs       — trigger full dev or check evaluation run
  GET  /evaluation-runs/{id}  — retrieve run results with joined claims
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

load_dotenv()

from api.db import apply_schema, close_pool, get_pool
from api.schemas import (
    ClassifyRequest,
    ClassifyResponse,
    ClaimResponse,
    EvaluationRunDetailResponse,
    EvaluationRunRequest,
    EvaluationRunResponse,
    AnswerResultResponse,
)
from agent.agent import run_agent
from harness.runner import run_split


# ─── Lifecycle ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await get_pool()
    await apply_schema(pool)
    yield
    await close_pool()


app = FastAPI(
    title="Kasparro Claim Extraction API",
    description="FSE I Assessment — claim extraction and classification service",
    version="1.0.0",
    lifespan=lifespan,
)


# ─── POST /classify ───────────────────────────────────────────────────────────

@app.post("/classify", response_model=ClassifyResponse)
async def classify(req: ClassifyRequest) -> ClassifyResponse:
    """
    Run the claim extraction agent on a single answer.
    Returns claims with character offsets, labels and reasons.
    """
    result = await run_agent(
        answer_id=req.answer_id,
        brand_id=req.brand_id,
        question=req.question,
        ai_answer=req.ai_answer,
        version=req.version,
    )
    return ClassifyResponse(
        answer_id=result.answer_id,
        status=result.status,
        claims=[
            ClaimResponse(
                start_char=c.start_char,
                end_char=c.end_char,
                quote=c.quote,
                label=c.label,
                fact_ids=c.fact_ids,
                reason=c.reason,
            )
            for c in result.claims
        ],
        validation_errors=[{"code": e.code, "detail": e.detail} for e in result.errors],
        latency_ms=result.latency_ms,
        turns_used=result.turns_used,
        error_message=result.error_message,
    )


# ─── POST /evaluation-runs ────────────────────────────────────────────────────

_active_runs: dict[str, str] = {}  # run_id → status


@app.post("/evaluation-runs", response_model=EvaluationRunResponse, status_code=202)
async def create_evaluation_run(
    req: EvaluationRunRequest,
    background_tasks: BackgroundTasks,
) -> EvaluationRunResponse:
    """
    Trigger an evaluation run (dev or check) in the background.
    Returns immediately with a run_id; poll GET /evaluation-runs/{id} for results.
    """
    import uuid
    run_id = str(uuid.uuid4())
    _active_runs[run_id] = "running"

    async def _run():
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                run = await run_split(
                    version=req.version,
                    split=req.split,
                    db_conn=conn,
                )
            _active_runs[run_id] = "completed"
        except Exception as e:
            _active_runs[run_id] = f"failed: {e}"

    background_tasks.add_task(_run)
    return EvaluationRunResponse(
        run_id=run_id,
        status="running",
        message=f"Evaluation run started: {req.version} on {req.split}",
    )


# ─── GET /evaluation-runs/{run_id} ───────────────────────────────────────────

@app.get("/evaluation-runs/{run_id}", response_model=EvaluationRunDetailResponse)
async def get_evaluation_run(run_id: str) -> EvaluationRunDetailResponse:
    """
    Retrieve a run's results, joining answer_results and claim_results from the DB.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        run_row = await conn.fetchrow(
            "SELECT * FROM runs WHERE run_id = $1", run_id
        )
        if run_row is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")

        answer_rows = await conn.fetch(
            "SELECT * FROM answer_results WHERE run_id = $1 ORDER BY answer_id",
            run_id,
        )
        claim_rows = await conn.fetch(
            "SELECT * FROM claim_results WHERE run_id = $1 ORDER BY answer_id, start_char",
            run_id,
        )

    # Group claims by answer_id
    claims_by_answer: dict[str, list[ClaimResponse]] = {}
    for cr in claim_rows:
        aid = cr["answer_id"]
        claims_by_answer.setdefault(aid, []).append(ClaimResponse(
            start_char=cr["start_char"],
            end_char=cr["end_char"],
            quote=cr["quote"],
            label=cr["label"],
            fact_ids=json.loads(cr["fact_ids"] or "[]"),
            reason=cr["reason"] or "",
        ))

    answer_results = [
        AnswerResultResponse(
            answer_id=ar["answer_id"],
            status=ar["status"],
            error_message=ar["error_message"],
            latency_ms=ar["latency_ms"],
            claims=claims_by_answer.get(ar["answer_id"], []),
        )
        for ar in answer_rows
    ]

    metrics_raw = run_row["metrics"]
    metrics = json.loads(metrics_raw) if metrics_raw else None

    return EvaluationRunDetailResponse(
        run_id=str(run_row["run_id"]),
        version=run_row["version"],
        split=run_row["split"],
        provider=run_row["provider"],
        model=run_row["model"],
        status=run_row["status"],
        metrics=metrics,
        answer_results=answer_results,
    )


# ─── Health check ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": app.version}
