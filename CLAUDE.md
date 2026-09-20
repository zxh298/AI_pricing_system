# CLAUDE.md

Personal portfolio project: a local-first, cloud-ready simulator of a retail clearance
(markdown) pricing system plus a GenAI diagnostic assistant. Full design decisions:

@docs/PROJECT_CONTEXT.md

## Non-negotiable rules

- Synthetic data and fictional brand names only. No employer code, data or documents.
- Deterministic code makes every judgement about prices and records. The LLM only
  plans tool calls, combines results and explains. Never let the LLM compare prices itself.
- Tools are read-only, parameterised (no LLM-written SQL), batch-friendly and receive
  user/permission context injected by the service, never from LLM arguments.
- The diagnostic service must never be able to write prices to SAP (separate service
  accounts and credentials; see PROJECT_CONTEXT section 6).
- All configuration comes from environment variables; no secrets in code or git.
- Never add always-on paid GCP resources (Composer, Memorystore, Vector Search endpoints,
  Cloud Run min instances > 0).

## Architecture in one line

Services: `ui` (Streamlit) → `diagnostic-api` (FastAPI) → `mock-sap` (FastAPI).
Jobs: `weekly-pipeline`, `rag-ingest`. Local: Docker Compose + DuckDB + Postgres/pgvector.

## Working style

- Follow the build order in PROJECT_CONTEXT section 9; each step must run end to end with
  a smoke test before starting the next.
- Keep code simple and explicit; this is a teaching and portfolio codebase.
- When a decision is not covered by PROJECT_CONTEXT, ask before choosing.
