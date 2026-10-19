# Kasparro FSE I Assessment — Claim Extraction & Classification Agent

**Mahendra Khichar** | Python 3.12 | FastAPI · asyncpg · Langfuse · OpenAI/Anthropic

---

## Quick Start

```bash
# 1. Clone and install
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env: add OPENAI_API_KEY (or ANTHROPIC_API_KEY), DATABASE_URL, Langfuse keys

# 3. Start PostgreSQL (Docker recommended)
docker run -d --name kasparro-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16

# 4. Place CSVs in data/
#    data/dev_answers.csv    (A01–A40)
#    data/dev_gold.csv
#    data/check_answers.csv  (A41–A50)
#    data/brand_facts.csv

# 5. Run tests
python -m pytest -v

# 6. Run 4 evaluation runs
python -m scripts.run_all

# 7. Start API
uvicorn api.main:app --reload
```

---

## Architecture

```
POST /classify ──► agent.run_agent() ──► provider.chat_completion()
                         │                       │
                         │                 get_brand_facts tool
                         │                 submit_claims tool
                         ▼
POST /evaluation-runs ──► harness/runner.py (asyncio + Semaphore(3))
                               │
                         harness/scorer.py ──► metrics (dev only)
                               │
                         PostgreSQL + Langfuse
GET  /evaluation-runs/{id} ──► DB join (runs ► answer_results ► claim_results)
```

### Key Design Decisions

| Decision | Rationale |
|---|---|
| Raw httpx, no SDK | Avoids SDK version fragility; normalizes responses ourselves |
| Python locates offsets | Model returns verbatim quotes; `str.find()` gives exact `(start, end)` |
| cursor-advancing find | Handles repeated sentences: finds each at its correct position |
| Semaphore(3) around retry | Retry counts toward concurrency limit per spec |
| submit_claims tool | Structured output via tool call; no free-text parsing |
| v2 pre-splits sentences | Eliminates partial/merged span errors (most common v1 failure) |
| Error ≠ unknown | Errors stay as errors in `status` field; never silently labeled |
| Gold joined after save | Prevents any gold leakage into predictions |

---

## Scoring Formulas

```
extraction_f1 = 2M / (P + G)    # span quality
joint_f1      = 2C / (P + G)    # span + label quality
macro_f1      = mean(per_label F1 over supported / contradicted / unknown)
```

**Spec validation:** G=10, P=10, M=8, C=7 → extraction_f1=0.80, joint_f1=0.70 ✅

---

## v1 vs v2

| Version | Approach |
|---|---|
| v1 | Model quotes verbatim sentences freely; Python finds offsets |
| v2 | Python pre-splits into `[S0]`…`[Sn]` candidates; model picks IDs |

**v2 fixes:** Partial spans, merged sentences, sentences the model re-phrases. The ID-selection approach guarantees that every selected span is an exact match to the original answer.

---

## File Layout

```
agent/      prompts/v1.md  prompts/v2.md  agent.py  tools.py  validate.py  provider.py
harness/    loader.py  scorer.py  runner.py  report.py
api/        main.py  schemas.py  db.py  schema.sql
tests/      test_loader  test_validate  test_scorer  test_agent_fake  test_concurrency
data/       CSVs (or SHA256 hashes in HASHES.md)
runs/       v1_dev.json  v2_dev.json  v1_check.json  v2_check.json
docs/       EXPLANATION.pdf
.env.example  README.md  requirements.txt (pinned)
```

---

## Tests

```
32 passed in 0.47s
```

Covers: scorer (spec example + edge cases), offset location, overlap detection,
cursor-advancing duplicates, v2 sentence ID lookup, CSV loader (CRLF preservation),
SHA-256 hashing, Semaphore(3) enforcement, failure isolation.

Agent fake tests: happy path, error propagation, turn limits, quote-not-found handling.

---

## Reliability

- **Timeout:** 15 s per provider call (`asyncio.wait_for`)
- **Retry:** exactly once, on 429 / timeout / 5xx only. Never on auth (401) or malformed (400).
- **Concurrency:** `asyncio.Semaphore(3)` wraps the full attempt including retry
- **Failure isolation:** `asyncio.gather(..., return_exceptions=True)` — one answer failing doesn't stop others

---

## Langfuse Observability

Each answer gets one trace with:
- `generation` observation per LLM turn (input messages, output message, latency)
- `span` observation per tool call (`get_brand_facts`, `submit_claims`)
- Trace metadata: `version`, `answer_id`, `brand_id`, claim count, total latency

---

## AI Tool Disclosure

This codebase was developed with AI assistance (Antigravity/Claude). The following were verified and validated by hand:
- All scorer formulas against the spec examples
- All 32 test assertions
- Offset-location logic and cursor-advancing behavior
- The `no silent unknown` rule for errors
- Retry-inside-semaphore implementation

---

## Honesty Section

- Gold data is loaded **after** predictions are saved to DB.
- The check set (A41–A50) was **visible** during development. No tuning was performed against it.
- "No score gain" in v2 is reported honestly (per spec). Honesty > good-looking numbers.
- No hardcoded values, no test-set peeking in any prompt or tool payload.
- `.env` is gitignored; `.env.example` contains no real keys.

---

## Known Limitations / Unfinished Work

- `test_agent_fake.py` uses AsyncMock; some complex coroutine chaining may need adjustment with real provider responses.
- `split_into_sentences` uses a regex heuristic; edge cases (abbreviations, URLs) may produce suboptimal splits. A sentence-splitting library (e.g., spaCy) would be more robust.
- No React UI (cut per priority — harness and tests score higher).
- DB schema applies on startup; no migration tooling.
