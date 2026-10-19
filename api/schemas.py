"""
schemas.py — Pydantic request/response models for the FastAPI endpoints.
"""
from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


# ─── POST /classify ───────────────────────────────────────────────────────────

class ClassifyRequest(BaseModel):
    answer_id: str = Field(..., description="Unique identifier for this answer")
    brand_id: str = Field(..., description="Primary brand being discussed")
    question: str = Field(..., description="The original question asked")
    ai_answer: str = Field(..., description="The AI-generated answer to analyze")
    version: Literal["v1", "v2"] = Field("v1", description="Agent version to use")


class ClaimResponse(BaseModel):
    start_char: int
    end_char: int
    quote: str
    label: Literal["supported", "contradicted", "unknown"]
    fact_ids: list[str]
    reason: str


class ClassifyResponse(BaseModel):
    answer_id: str
    status: Literal["ok", "partial", "error"]
    claims: list[ClaimResponse]
    validation_errors: list[dict]
    latency_ms: float
    turns_used: int
    error_message: str


# ─── POST /evaluation-runs ────────────────────────────────────────────────────

class EvaluationRunRequest(BaseModel):
    version: Literal["v1", "v2"]
    split: Literal["dev", "check"]


class EvaluationRunResponse(BaseModel):
    run_id: str
    status: str
    message: str


# ─── GET /evaluation-runs/{run_id} ───────────────────────────────────────────

class AnswerResultResponse(BaseModel):
    answer_id: str
    status: str
    error_message: Optional[str]
    latency_ms: Optional[float]
    claims: list[ClaimResponse]


class EvaluationRunDetailResponse(BaseModel):
    run_id: str
    version: str
    split: str
    provider: str
    model: str
    status: str
    metrics: Optional[dict[str, Any]]   # null for check runs
    answer_results: list[AnswerResultResponse]
