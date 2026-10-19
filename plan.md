# Kasparro FSE I Assessment: Requirements, Strategy and Architecture

Prepared for Mahendra Khichar. A 3-hour plan for the claim extraction and classification agent assessment.

## 1. What they are asking for

You are building a **claim extraction and verification agent**, plus the **evaluation harness** that measures it. The harness is what most of the marks reward.

**The agent**

- Input: `answer_id`, `brand_id`, `question`, `ai_answer`.
- The model must call `get_brand_facts(brand_id)` through real tool calling. It may call it for several brands if the answer compares brands.
- The agent extracts every factual sentence. It skips questions, advice, opinions and instructions.
- For each claim it returns exact character offsets, a label (`supported`, `contradicted` or `unknown`), `fact_ids` and a reason of at most two sentences.
- The limit is 3 model turns per answer.

**The harness**

- It runs v1 and v2 on the 40 development answers, and scores extraction F1, joint F1, per-label F1, macro F1 and a confusion matrix.
- It also runs both versions on the 10 check answers (A41 to A50) with no scoring.
- That makes 4 live runs: v1 dev, v2 dev, v1 check, v2 check.

**The service and reliability layer**

- FastAPI with 3 endpoints, and PostgreSQL with 3 tables.
- Langfuse traces: one per answer, with generation and tool observations.
- Async execution capped at 3 concurrent provider requests, a 15s timeout, and 1 retry only on transient errors.

**Marking (100 points)**

| Area | Points |
| --- | --- |
| Harness and metrics | 20 |
| Agent | 15 |
| Improvement and regression analysis | 15 |
| FastAPI and PostgreSQL | 15 |
| Langfuse observability | 15 |
| Reliability and tests | 15 |
| Reproducibility and ownership | 5 |

The assessment says outright that "no score gain" is acceptable. Honesty and correct accounting score higher than a good-looking number. Don't hardcode anything, don't tune on the gold in a way you hide, and disclose any tuning on the check inputs.

## 2. Best strategy for 3 hours

Spend your time in line with the marks. The agent is only 15 points, while the harness, tests, DB, Langfuse and reliability together are 65. Don't spend 90 minutes polishing prompts.

**Decisions that will save you**

1. **No framework.** Use the provider SDK directly (OpenAI, Anthropic or Gemini, whichever key you have). Skip LangGraph, since 3 turns doesn't need it and it adds debugging risk.
2. **The model returns exact quotes, and Python finds the offsets.** The spec says so explicitly.
   - Locate with `ai_answer.find(quote, cursor)`, moving the cursor forward so repeated sentences resolve from context.
   - Reject the claim if the quote is not found verbatim.
   - Never normalize whitespace, and never resave `ai_answer`.
3. **Ask the model to submit its output through a final tool or structured output**, for example `submit_claims`. This is easier to validate than free text.
4. **Turn plan:** turn 1 makes `get_brand_facts` calls (possibly several in parallel), turn 2 returns the claims, and turn 3 is a spare for one repair. Anything invalid after that becomes an answer-level error, never silently `unknown`.
5. **Keep the CSV parsing correct.** Use the `csv` module or pandas with `encoding="utf-8"` and `newline=""`. The `\r\n` and embedded newlines inside `ai_answer` shift offsets if you mishandle them.

**The v1 to v2 change.** Pick one change and justify it from a baseline trace. Candidates:

- **Option A (extraction-focused):** v1 lets the model free-quote sentences. v2 has Python pre-split the answer into candidate sentences with IDs, and the model selects IDs and labels them. This fixes partial and merged spans, which score as unmatched.
- **Option B (label-focused):** v2 adds explicit label rules to the prompt. The rules cover condition and scope matching, 2025 vs 2026 rows, unit conversion, "any refuted component makes it contradicted, otherwise any unresolved component makes it unknown", and "absence is not a negative fact".

Don't choose in advance. Run v1, look at the errors, then pick whichever failure dominates. Expect label errors on `unknown` vs `contradicted` for missing negatives, and span errors on long sentences with connected qualifications. Also handle prompt-injection text in answers as data.

## 3. Architecture

```
                  +-------------- FastAPI (thin routes) ---------------+
 POST /classify ---+  validate -> agent.run(answer)                      |
 POST /evaluation-runs --> runner.run_split(version, split)             |
 GET  /evaluation-runs/{id} --> db join (run -> answers -> claims)      |
                  +--------------+-------------------------------------+
                                 |
        +------------------------v-----------------------+
        | Runner: asyncio + Semaphore(3) (incl. retries)  |
        | one task per answer, failures isolated          |
        +------------------------+------------------------+
                                 |
        +------------------------v------------------------+
        | Agent loop (max 3 turns)                         |
        |  provider adapter (timeout 15s, 1 retry on       |
        |   429/timeout/5xx only, short backoff)           |
        |  turn1: model -> tool_calls get_brand_facts(...) |
        |  executor: allowlist tool + known brand -> facts |
        |  turn2: model -> claims (quote, label, fact_ids) |
        |  Python: locate offsets, validate, build ids     |
        +------------------------+------------------------+
                                 | predictions (saved first)
        +------------------------v------------------------+
        | Evaluator (dev only, joins gold AFTER save)      |
        | exact span match -> P/R/F1, joint, per-label,    |
        | macro F1, confusion matrix, missed/extra/wrong   |
        +----------+-----------------------+---------------+
                   v                       v
             PostgreSQL               Langfuse
       runs / answer_results /     trace per answer, generation +
       claim_results               tool spans, dev scores
```

**Repo layout**

```
agent/      prompts/v1.md  prompts/v2.md  agent.py  tools.py  validate.py  provider.py
harness/    loader.py  scorer.py  runner.py  report.py
api/        main.py  schemas.py  db.py  schema.sql
tests/      test_loader  test_validate  test_scorer  test_agent_fake  test_concurrency  test_api_db
data/       CSVs (or SHA256 hashes in HASHES.md)
runs/       v1_dev.json  v2_dev.json  v1_check.json  v2_check.json
docs/       EXPLANATION.pdf (+ .md)  langfuse_exports/  sanitized_exchanges/
.env.example  README.md  requirements.txt (pinned)
```

**Database**

- `runs`: `run_id` UUID, split, versions, provider, model, settings (JSONB), file hashes, metrics (JSONB, null for check), status.
- `answer_results`: `UNIQUE(run_id, answer_id)`, status, sanitized error, latency, trace id.
- `claim_results`: `UNIQUE(run_id, answer_id, start_char, end_char)`, with FKs, quote, label, fact IDs, reason and an optional dev match.
- Use parameterized SQL only (asyncpg or psycopg). Docker Postgres is the quickest setup.

**Scorer (the most important part, 20 points)**

- Match on identical `(answer_id, start, end)`, after the quote-equality check.
- Compute M, C, P, G with these formulas:
  - extraction F1 = 2M/(P+G)
  - joint F1 = 2C/(P+G)
- Per-label TP, FP and FN follow the spec's rules. Macro F1 is the unweighted mean over the three labels.
- The confusion matrix has rows supported / contradicted / unknown / no\_gold and columns supported / contradicted / unknown / missing\_or\_error. Its total is G plus the unmatched predictions.
- An answer error adds no predictions, but its gold claims stay in G.
- Check runs leave all quality fields null (not 0).
- Unit-test it with the G=10, P=10, M=8, C=7 example, which must give 0.80 and 0.70.

## 4. Time plan (180 min)

| Min | Task |
| --- | --- |
| 0-15 | Read the CSVs, set up the repo, venv, Postgres, Langfuse and `.env`. |
| 15-50 | Loader, validators (offsets, quote equality, overlaps, fact IDs) and the scorer, with tests. Write these first, because they are deterministic and heavily weighted. |
| 50-90 | Provider adapter, tool executor, agent loop and the v1 prompt. Smoke test on 3 answers, with a fake provider for the failure tests. |
| 90-110 | Async runner (Semaphore 3, timeout, retry), then run **v1 on dev**. |
| 110-130 | Inspect errors and one Langfuse trace, choose the change, write v2, and run **v2 on dev**. |
| 130-150 | Freeze both versions, then run both on **check**. |
| 150-170 | FastAPI endpoints, DB persistence and an API/DB test. |
| 170-180 | README, then the PDF. |

**If you run short:** cut FastAPI polish and the optional React UI first. Never cut the scorer, the four live runs, the Langfuse traces, or the honesty section. Say what is unfinished rather than faking it.

## 5. What goes in the PDF

This is where you stand out. Keep it to 3 to 5 pages.

1. **Approach:** the architecture diagram, and why quote-then-locate-in-Python.
2. **v1 vs v2:** a metrics table with extraction F1, joint F1, macro F1, per-label F1 and G/P/M/C.
3. **Trace-based diagnosis:** one baseline Langfuse trace showing a concrete failure, how it led to your change, and the before and after.
4. **Regressions:** any answers or labels that got worse. Say plainly if there was no gain.
5. **Reliability and testing:** tests actually run, with the pytest output pasted, versus tests only written.
6. **AI-tool disclosure:** what Claude Code or Codex wrote, and what you verified by hand.
7. **Limitations, unfinished work and time spent.** Also state that the check set was visible and what tuning, if any, was done on it.

## 6. Traps to avoid

- Including gold, difficulty tags or explanations in any model or tool payload.
- Using `bool("false")`. Parse `value_type` properly, and honor `valid_from`/`valid_to` and conditions.
- Splitting or merging sentences (any mismatch is unmatched), or extracting advice and questions (this lowers precision).
- Mapping errors to `unknown`. Errors must be preserved as errors.
- Retrying malformed output or auth failures. Retry only timeouts, 429s and transient errors, at most once.
- Letting retries bypass the semaphore.
- Claiming the check set is blind, or reporting matched-only label accuracy as overall accuracy.
- Committing keys, or leaving a `.env` in the repo.
