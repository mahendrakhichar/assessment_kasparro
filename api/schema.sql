-- schema.sql — PostgreSQL schema for Kasparro claim extraction assessment

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ─── runs ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS runs (
    run_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    split        TEXT NOT NULL CHECK (split IN ('dev', 'check')),
    version      TEXT NOT NULL,
    provider     TEXT NOT NULL,
    model        TEXT NOT NULL,
    settings     JSONB NOT NULL DEFAULT '{}',
    file_hashes  JSONB NOT NULL DEFAULT '{}',
    metrics      JSONB,           -- NULL for check runs (per spec)
    status       TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at  TIMESTAMPTZ
);

-- ─── answer_results ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS answer_results (
    id           BIGSERIAL PRIMARY KEY,
    run_id       UUID NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    answer_id    TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('ok', 'partial', 'error')),
    error_message TEXT,           -- sanitized (no PII, no keys)
    latency_ms   DOUBLE PRECISION,
    trace_id     TEXT,            -- Langfuse trace ID
    UNIQUE (run_id, answer_id)
);

-- ─── claim_results ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS claim_results (
    id           BIGSERIAL PRIMARY KEY,
    run_id       UUID NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    answer_id    TEXT NOT NULL,
    start_char   INT NOT NULL,
    end_char     INT NOT NULL,
    quote        TEXT NOT NULL,
    label        TEXT NOT NULL CHECK (label IN ('supported', 'contradicted', 'unknown')),
    fact_ids     JSONB NOT NULL DEFAULT '[]',
    reason       TEXT,
    dev_match    BOOLEAN,         -- NULL for check runs, TRUE/FALSE for dev
    UNIQUE (run_id, answer_id, start_char, end_char),
    FOREIGN KEY (run_id, answer_id) REFERENCES answer_results(run_id, answer_id) ON DELETE CASCADE
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_answer_results_run_id ON answer_results(run_id);
CREATE INDEX IF NOT EXISTS idx_claim_results_run_answer ON claim_results(run_id, answer_id);
