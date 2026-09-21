# Project Context: Retail Clearance Pricing & GenAI Diagnostics Simulator

> Working name: **TBD** (candidates in section 16). This document captures every design
> decision made so far. Read it before generating code, and keep code consistent with it.

---

## 0. Purpose and hard constraints

This is a **personal portfolio / interview-prep project**. It re-creates, on synthetic data,
the architecture of a production clearance (markdown) pricing system and a GenAI diagnostic
assistant that the author worked on at a retailer.

Hard constraints:

- **Synthetic data only.** No employer code, data, schemas, internal documents or playbooks.
  Playbooks and incident notes in `playbooks/` (tracked; `docs/` is gitignored) are written from scratch.
- **Generic naming.** Use fictional brands (e.g. "Brand A", "Harbour Ridge", "Stonefield"),
  not real product or employer names. The reference code in section 14 uses real wine brands
  as placeholders; rename them when porting.
- **Non-commercial.** The author does not want the project used commercially (licence
  decision pending, see section 16).
- **Local first, cloud ready.** Everything runs locally with Docker Compose; the same
  containers deploy to GCP Cloud Run later by changing environment variables only.
- **Cheap on GCP.** Never introduce always-on paid resources (see section 13).

---

## 1. The reference system being simulated

### 1.1 What it is (and is not)

A production ML pricing / decision-support system. It is **not an agent**: it is a
deterministic weekly pipeline that produces markdown prices. Only the separate GenAI
diagnostic assistant uses an LLM with tools.

```
Central SAP (system of record)
  ├─ weekly clearance candidate list (which SKUs to mark down)
  └─ business rules (price floor, max markdown %, ...)
              ↓  Airflow ingestion
GCP
  ├─ BigQuery   : sales / inventory history, features, model outputs history
  └─ Cloud SQL  : this week's candidate list + rules snapshot, run state
              ↓
Clearance Pricing Engine
  ML model recommends prices → rule engine validates against SAP rules
              ↓
Final prices → REST API → SAP → SAP makes prices effective (stores, POS, online)
```

Key positions:

- **SAP decides *what* and the constraints; the engine decides *how much*; SAP executes.**
  The model does not choose which products enter clearance.
- **SAP is the source of truth for rules.** Cloud SQL holds a per-run *snapshot*, so every
  run is reproducible and auditable ("which rule version produced this price?").

### 1.2 BigQuery vs Cloud SQL

| Store | Data | Why |
|---|---|---|
| BigQuery (OLAP, columnar) | historical POS sales (SKU × store × day), inventory snapshots, product master history, feature tables, training sets, model output history, reconciliation results, API/audit logs | large scans and aggregations, training and diagnosis across time and SKUs |
| Cloud SQL (OLTP, row-based) | weekly candidate list + rules snapshot, business overrides/approvals, clearance status, currently published prices, per-record send status, job run metadata, sessions, findings | small, frequently updated records, ACID, low-latency point lookups |

Note: with Cloud Composer, Airflow's own metadata DB is also Cloud SQL.

### 1.3 Sending prices to SAP (REST API)

The last Airflow tasks run automatically after pricing:

```
pre-send validation → (optional business approval) → export batch
  → POST to SAP REST API in batches → parse per-record response
  → write send status to Cloud SQL, full log to BigQuery
  → reconciliation: GET prices active in SAP, compare with what was sent
```

Design rules:

- **Batching** to respect SAP rate and payload limits.
- **Retry by error type:** 429 and 5xx → exponential backoff; 400/422 (data problems)
  → no retry, record and escalate.
- **Idempotency:** each record keyed by `run_id + sku + effective date`; reruns resend only
  records whose status is not `success`.
- **Partial failure:** track status per record, never judge a batch as all-pass/all-fail.
- **Reconciliation:** "API accepted" ≠ "price in effect" (manual override, higher-priority
  promotion), so always verify with GET afterwards.
- Auth via OAuth2 token from Secret Manager.

Example payload (illustrative field names):

```json
POST /pricing/markdown-prices
{
  "run_id": "2026-W39",
  "prices": [
    {"sku": "123456", "store_group": "VIC_METRO", "markdown_price": 12.00,
     "valid_from": "2026-09-21", "valid_to": "2026-09-27"}
  ]
}
```

---

## 2. Weekly error taxonomy

Errors grouped by the stage where they occur. Each should get an **error code, severity
(blocking / warning), owner (SAP team / DS team / business) and a playbook**.

1. **SAP input:** missing article (in candidate list but no master/sales/inventory data, or
   vice versa); missing or conflicting rules (no floor, category vs SKU conflict, floor above
   current price); stale or late input; article status conflict (delisted, blocked, active
   promotion); duplicate article.
2. **Data pipeline:** inventory anomalies (zero/negative/late stock); store or price-zone
   mapping errors; unit of measure / pack size mix-ups (single vs 6-pack vs case); missing or
   changed cost price.
3. **Model output:** missing price (cold start, missing features, fallback gaps); abnormal
   recommendation (price above last week's markdown, steep single-week drop, extreme
   elasticity); price ending / rounding rules (.99 / .00).
4. **Validation:** below floor / below cost / above max markdown; price ladder inconsistency
   (single × 6 below 6-pack price, store vs online mismatch); regulatory floors (e.g. an
   alcohol minimum unit price in some jurisdictions).
5. **REST API:** transient errors (token expiry, timeout, 429, 5xx); rejected records
   (400/422: bad format, past `valid_from`, overlapping condition validity); partial failure;
   duplicate submission.
6. **After SAP accepts:** accepted but not effective (manual override, higher-priority
   promotion); timezone / effective-date shift (GCP runs in UTC, Australia spans several
   timezones); downstream propagation failure (POS, shelf labels, online).
7. **Pipeline operations:** DAG failure or SLA miss before the weekly cutoff; duplicate run
   or wrong `run_id` / week.

Codes used in code (see section 17.2): `NOT_IN_LIST`, `MISSING_PRICE`, `RULE_VIOLATION`,
`API_REJECTED`, `NOT_EFFECTIVE`, `PRICE_MISMATCH`, `MISSING_IN_SAP`, `OK`. `PROMO_OVERLAP` as a
pre-send rule is still planned (section 3.3 step 9), not built.

---

## 3. GenAI diagnostic assistant

### 3.1 Roles of each part

| Part | Responsibility |
|---|---|
| RAG | the "why": policies, playbooks, historical rationale, past incidents |
| BigQuery (+ SAP GET) | the "what is": live pricing and operational data |
| Rule engine / deterministic checks | the "is it right": every judgement (below floor? accepted? effective?) |
| LLM | understand the question, choose and combine checks, retrieve docs, explain |

**Core principle: rules decide, the LLM explains.** The LLM never judges whether a price or
record is correct, not even a simple comparison; it calls a deterministic tool for that.

### 3.2 Why an LLM and not just if-else

- The **checks** are if-else and must stay if-else.
- If-else needs every **question shape** pre-built as a feature ("compare a brand across
  regions", "only red wine", "vs last week", follow-ups). The LLM writes that glue at
  runtime by composing existing tools.
- It can use **unstructured knowledge** (playbooks, rationale, incident notes).
- Honest trade-off: if ~90% of questions have one fixed shape and rarely need documents,
  a dashboard + rules is the better engineering choice.
- The LLM is **weak on genuinely new problems** (no playbook, possibly no tool coverage).
  There it only traces evidence along the data flow, points to the first divergence,
  proposes low-confidence hypotheses and escalates. A human confirms the root cause, which
  then goes into playbooks and, where possible, the rule engine. Over time the *share* of
  unclassified questions reaching the LLM changes; the LLM itself does not "focus".
- Metric worth tracking: share of questions matched to the taxonomy vs unclassified, and
  time from a new issue to a deterministic rule.

### 3.3 End-to-end example (canonical demo scenario)

Question: *"Brand A clearance this week: many prices didn't drop in VIC, NSW is fine. Why?"*

1. Parse intent: brand, week (W39), regions.
2. `resolve_products` → 45 SKUs on this week's list.
3. `diagnose_batch` over 45 SKUs × {VIC, NSW} → NSW 45 OK; VIC 33 OK, 9 `NOT_EFFECTIVE`,
   3 `API_REJECTED`.
4. `get_sap_conditions` on the 9 → a VIC-only catalogue promotion with higher priority
   overrides the markdown (explains why NSW is fine).
5. `get_api_log` on the 3 → 422 "validity period overlaps existing condition record"
   (last week's markdown created with open end date).
6. `search_docs` → playbook for promotion override (owner: Promotions team) and for 422
   overlap (owner: SAP Pricing team), plus a similar past incident.
7. Answer for a business user with counts, groups, owners and actions.
8. Follow-up *"If we end the promotion, would they go below floor?"* → `check_rules`
   (deterministic) → all pass.
9. Closing the loop: add a pre-send `PROMO_OVERLAP` rule so this class is caught before
   sending.

### 3.4 Tool design rules

- Tools are thin wrappers over the deterministic core; **read-only and parameterised**.
  The LLM never writes SQL. Production queries use BigQuery parameterised queries.
- Prefer **batch tools** (`diagnose_batch`) over per-SKU calls; collapse identical per-SKU
  results into **patterns** to keep outputs small.
- Allow-listed dispatch, an **audit log line per tool call**, errors returned to the LLM as
  `is_error` tool results, `MAX_STEPS` cap with an escalation message.
- System prompt: never judge prices yourself; only state facts from tool results; name the
  owner from the playbook; unmatched issues are "unclassified, low confidence, escalate".
- The **LLM loop is ~30 lines**; most engineering is in tools and the deterministic core.

---

## 4. Memory design

### 4.1 Problem with raw history

Passing the full message list back each turn resends every tool result on every LLM call:
cost and latency grow, quality drops, stale data gets reused, and in-process state is lost
on restart or across instances.

### 4.2 Three layers

1. **Session state (working memory, maintained by code).** Scope (brand, week, regions),
   SKU groups stored as **handles** (`G1`, `G2`) with `as_of` and `data_version`, and the
   last check per scope. Tools accept `group_id` instead of SKU lists. The state is rendered
   into a short block in the system prompt each turn. Facts are kept by code, not "remembered"
   by the LLM.
2. **Conversation history (compacted).** The latest turn keeps its tool traffic; older turns
   keep only question + final answer. Remove whole tool exchanges so every `tool_use` stays
   paired with its `tool_result`. Beyond a turn limit, oldest turns survive only as a list
   of earlier questions.
3. **Long-term memory = confirmed knowledge, not chat logs.** Confirmed root causes become
   incident notes in the RAG corpus and, where possible, deterministic rules.

### 4.3 Freshness

Every tool result carries `as_of`. Anything about current status must be **re-queried in
the current turn**; history is only for understanding references ("that promotion").

### 4.4 Repeated questions

- **Same user, same session:** they usually want the *current* state. Re-run the
  deterministic checks and return a **diff** vs the previous check
  (`changes_since_last_check`: resolved / still failing / new failures).
- **Different users:** do **not** cache by text/embedding similarity ("VIC" vs "NSW",
  "this week" vs "last week" look alike but differ). Parse to a structured intent, then
  cache **tool results** keyed on `(tool, permission-filtered args, data_version)`.
  - `data_version` = latest pipeline / API retry / reconciliation run for the week; any new
    run invalidates snapshot results automatically.
  - Live SAP reads change outside the pipeline → short TTL (e.g. 5 min) instead.
  - Filter by permissions **before** building the key, so users with the same access share
    entries and nobody reads outside their scope.
- **Active findings (shared memory):** confirmed issues stored with scope, root cause,
  owner, ticket, status. Checked before re-investigating; auto-resolved when the
  deterministic check shows zero failures.
- **Memory poisoning guard:** only deterministic facts and **human-confirmed** conclusions
  go into shared findings. The LLM has no tool that writes findings.

Lookup order for a new question: session state (repeat → diff) → active findings → tool
cache → full investigation → (after human confirmation) findings → incident note → RAG.

---

## 5. Session management

- **Identity** from the auth layer only (IAP on Cloud Run; header stub locally). Never from
  request parameters or the LLM.
- **Session** = one investigation. `session_id` generated server-side and bound to
  `user_id`; ownership checked on every request (avoid IDOR; production may return 404).
- **Schema (Cloud SQL):** `sessions(session_id, user_id, status, created_at, last_active_at,
  expires_at, state JSON, history JSON, version)` and `findings(finding_id, week, brand,
  region, error_code, root_cause, owner, ticket, status, confirmed_by, evidence, created_at)`.
- **Lifecycle:** idle timeout (e.g. 2 h) **and** end of pricing cycle (findings from one
  week's data can mislead the next week). Expired sessions stay readable via the audit log.
- **Concurrency:** one in-flight turn per session (lock; production: Redis `SET NX PX` or
  `pg_try_advisory_lock`) plus optimistic locking (`UPDATE ... WHERE version = ?`).
- **Atomic turns:** persist state and history only after the whole turn succeeds.
- **Authorization in the tool layer:** a `ToolContext` (user, permissions, state, cache,
  findings) is injected by the service and is not part of LLM-writable arguments; add
  BigQuery row-level security / authorized views as a second line.
- **Sharing:** read-only links; viewers see data under their own permissions.
- **Audit vs prompt:** compacted history goes to the model; the **full transcript** (every
  tool input/output) goes to BigQuery for audit and evaluation.
- **Observability:** turns, tokens, tool calls, latency, user feedback / "resolved?".

---

## 6. Chosen target architecture: 3 services + 2 jobs

Split by **permission boundary** and **workload type**, not for its own sake.

```
Cloud Run services (HTTP, scale to zero)
  ui              ← users (IAP)
  diagnostic-api  ← only ui may invoke
  mock-sap        ← stands in for the external SAP; pipeline + diagnostic-api may invoke

Cloud Run jobs (run to completion)
  weekly-pipeline ← Cloud Scheduler weekly: price → validate → push to SAP → reconcile
  rag-ingest      ← when playbooks/incident notes change: chunk → embed → pgvector
```

Why split: batch vs request workloads; **diagnostic service must not be able to change
prices** (enforced by IAM and credentials, not just code); different resource profiles;
independent releases; failure isolation (diagnostics down must not affect pricing).
`mock-sap` is separate so network failures (timeout, 429, 422) can be demonstrated.

### 6.1 Service accounts

| Service account | Permissions |
|---|---|
| `ui-sa` | `roles/run.invoker` on diagnostic-api only |
| `diagnostic-sa` | BigQuery `dataViewer` + `jobUser`, `cloudsql.client`, secrets `llm-key` and `sap-read-key`, invoke mock-sap |
| `pipeline-sa` | BigQuery `dataEditor` + `jobUser`, `cloudsql.client`, secret `sap-write-key`, invoke mock-sap |
| `rag-ingest-sa` | `cloudsql.client` (+ embedding key if using an API) |
| scheduler SA | may only trigger weekly-pipeline |

- mock-sap enforces **read vs write credentials**: POST prices requires the write key
  (only `pipeline-sa` can read it); GET requires the read key.
- **Service-to-service auth:** keep default ingress but disallow unauthenticated calls and
  grant `roles/run.invoker` only to the caller's SA. (Internal-only ingress would require
  VPC egress configuration on the caller; not worth it for this project.)
- `shared/service_client.py`: no token locally; fetches and attaches an ID token on Cloud Run.
- Streamlit uses WebSockets: enable **session affinity** on the ui service.

---

## 7. Local stack

| Production | Local |
|---|---|
| BigQuery | DuckDB file (`warehouse.duckdb`) |
| Cloud SQL | PostgreSQL in Docker (pgvector image) |
| RAG vector store | pgvector in the same Postgres |
| Embeddings | sentence-transformers (e.g. bge-small) or Ollama embeddings |
| LLM | provider abstraction: `scripted` (offline stub), `anthropic`, optional Ollama / Vertex |
| Memorystore | Redis locally if wanted; on GCP prefer Postgres advisory locks + cache table |
| Central SAP | `mock-sap` FastAPI service with fault injection |
| Airflow / Composer | Python pipeline job (optionally `airflow standalone` locally) |
| Cloud Run | Docker containers |
| IAP | header stub (e.g. `X-User`) |
| UI | Streamlit |

### 7.1 Docker Compose topology

| Container | Role |
|---|---|
| `postgres` | sessions, findings, rules snapshot, RAG vectors |
| `ui` :8501 | calls `http://diagnostic-api:8000` |
| `diagnostic-api` :8000 | reads DuckDB (read-only), reads/writes Postgres, GETs mock-sap, calls LLM |
| `mock-sap` :8001 | own condition-record state; fault rates set by env vars |
| `weekly-pipeline` | compose profile `jobs`; `docker compose run --rm weekly-pipeline` |
| `rag-ingest` | compose profile `jobs` |

**DuckDB caveat:** a single process may open the file for writing. diagnostic-api should open
short read-only connections per request and retry if the pipeline holds the write lock.

### 7.2 Configuration (env vars only; code never branches on "local vs cloud")

```
WAREHOUSE=duckdb | bigquery
DATABASE_URL=...            # local postgres / Cloud SQL
SAP_BASE_URL=...            # http://mock-sap:8001 / Cloud Run URL
AUTH_MODE=none | iam        # attach ID tokens for service-to-service calls
LLM_PROVIDER=scripted | anthropic
ANTHROPIC_API_KEY=...       # .env locally (gitignored), Secret Manager on GCP
SAP_READ_KEY=... / SAP_WRITE_KEY=...
ANTHROPIC_MODEL=claude-haiku-4-5   # claude-sonnet-5 for more reliable multi-step demos
DEFAULT_WEEK=2026-W39       # what "this week" means (default: today's ISO week)
SESSION_IDLE_MINUTES=120    # diagnostic-api session idle timeout
USER_REGIONS={"nsw-analyst": ["NSW"]}   # local stand-in for IAP permissions; unlisted users see all
TOOL_CACHE_TTL_SECONDS=300  # reuse of live (SAP) tool results
DIAGNOSTIC_SCHEMA=diagnostic            # Postgres schema for sessions, transcripts, cache, findings
SAP_SCHEMA=sap / PIPELINE_SCHEMA=pipeline
```
Local runs read `.env` (the server and chat CLI load it themselves; real environment variables win).

---

## 8. Repository structure

```
<project>/
├── CLAUDE.md
├── docker-compose.yml
├── .env.example
├── shared/
│   ├── config.py           # env-var config
│   ├── warehouse.py        # WarehouseClient: DuckDB / BigQuery implementations
│   ├── db.py               # Postgres connection (local / Cloud SQL)
│   ├── service_client.py   # service-to-service calls, optional ID token
│   └── schemas.py          # PriceRecord etc.
├── services/
│   ├── ui/                 # Streamlit
│   ├── diagnostic_api/     # FastAPI + tools + session/memory
│   └── mock_sap/           # POST prices, GET conditions, fault injection
├── jobs/
│   ├── weekly_pipeline/
│   └── rag_ingest/
├── docs/
│   ├── PROJECT_CONTEXT.md  # this file
│   └── (playbooks moved out: docs/ is gitignored)
└── infra/
    ├── iam.sh
    └── deploy.sh
```

Each service/job has its own Dockerfile and requirements; build context is the repo root so
`shared/` is copied into every image.

---

## 9. Build order (each step must run end to end before moving on)

**Status: steps 1-5 done and tested; step 6 in progress (corpus, loader, chunker, embedder, ingest job, `search_docs` tool and prompt change done; retrieval evaluation set next); steps 7-9 not started.** Step 5 as built: section 17.

1. **Synthetic data** → DuckDB: SKUs (fictional brands), stores/regions, sales, inventory,
   weekly candidate list, business rules. Seeded so scenarios are reproducible.
2. **Pricing engine:** simple price-elasticity model (e.g. log-log regression) + rule engine
   producing weekly markdown prices.
3. **mock-sap + sender:** batched REST push with retry, idempotency, record-level status,
   reconciliation. Inject scenarios: promotion overlap, 422 validity overlap, 429/5xx,
   missing article, missing price.
4. **weekly-pipeline job** chaining 1–3.
5. **diagnostic-api:** DONE (rebuilt from sections 3-5, not ported: the reference files in
   section 14 were never in the repo). Session, memory, cache, findings: section 17.
6. **rag-ingest + playbooks** into pgvector; implement `search_docs`. Plan: pgvector in the existing
   Postgres (schema `knowledge`); corpus in the tracked `playbooks/` folder (27 documents: playbooks, incidents,
   policies, reference; README explains format and tags); tag-first lookup by `CODE:reason`, similarity search as
   fallback with a relevance threshold; a test keeps every emittable code covered by a playbook. Done so far:
   corpus + loader (`jobs/rag_ingest/corpus.py`), section chunker (`chunker.py`, ~110 chunks, median ~350 chars),
   embedder interface (`shared/embedding.py`: `fastembed` = local BAAI/bge-small-en-v1.5, 384-wide, ONNX, no key;
   `hash` = offline stand-in for tests), schema (`shared/knowledge_schema.py`: `docs`, `chunks` with a pgvector
   column, `kb_meta.docs_version`) and the incremental, transactional ingest job (`python -m jobs.rag_ingest`;
   skips unchanged documents, re-embeds on content or model change, deletes removed documents).
   Observed with the real model: correct playbook first for 4 of 5 on-topic probes and in the top 3 for the fifth
   (an HTTP 429 question ranked the rejected-request playbook above the transient-errors one, which is why tag lookup
   comes first); on-topic top scores 0.66-0.84 but off-topic questions score 0.45-0.66, so a similarity threshold
   alone cannot separate them. Threshold and any keyword support are to be tuned on the retrieval evaluation set.
   `pgvector` needs `CREATE EXTENSION vector` (superuser locally; `cloudsqlsuperuser` once on Cloud SQL).
   `search_docs` (as built): tag lookup first (`tags_for` for a named code+reason, `tags_in_query` recognises
   reasons and HTTP numbers in free text), similarity fallback with `SEARCH_MIN_SCORE` (default 0.65; only very close
   passages, 0.80, are added once a tag found a playbook); returns `found: false` with an "unclassified" note when
   nothing is relevant; cached as a snapshot keyed on `docs_version`. `diagnose_batch` and `check_rules` also attach,
   in code, the playbook for each problem they report (id, owning team, severity, "What to do"), by tag only (no
   embedding model needed); a knowledge-base failure never fails a diagnosis. The server loads the embedding model
   at startup. Live finding: with only "call search_docs for each pattern" in the prompt, Haiku skipped the call
   and invented an explanation; attaching the playbook in code fixed it. Open: the threshold cannot separate
   "how many bottles did we sell last week?" (0.66) from a borderline on-topic question (0.66); tune on the
   retrieval evaluation set (6d).
7. **ui:** Streamlit chat; expandable panel showing tool calls per answer.
8. **infra:** IAM script and deploy script for GCP.
9. Optional: deploy to Cloud Run with a budget alert.

---

## 10. Deployment to GCP

- Services: `gcloud run deploy --source .` (per service directory / Dockerfile).
- weekly-pipeline and rag-ingest: Cloud Run **jobs**; Cloud Scheduler triggers the pipeline.
- DuckDB → BigQuery; local Postgres → Cloud SQL for PostgreSQL (pgvector supported);
  `.env` → Secret Manager; header stub → IAP.

---

## 11. RAG

- Corpus is small (hundreds to thousands of chunks: playbooks, policies, incident notes), so
  choose for operational simplicity, not retrieval performance.
- Chosen: **pgvector in Postgres** (local) → Cloud SQL for PostgreSQL on GCP.
- Other GCP options for reference: Vertex AI RAG Engine (managed; default store
  RagManagedDb), Vertex AI Vector Search (large scale, always-on endpoint), BigQuery vector
  search (`VECTOR_SEARCH`), AlloyDB + pgvector. Cloud SQL for MySQL has no pgvector.
- The author's production RAG store is not recorded here; do not assume it.

---

## 12. LLM usage

- Keep a provider interface (`create(system, tools, messages)` returning plain-dict content
  blocks so history is JSON-serialisable).
- `scripted` provider for offline dev and tests; real model for demos.
- Anthropic API models: `claude-haiku-4-5-20251001` for cheap development,
  `claude-sonnet-5` for more reliable multi-step tool use in demos.
- API keys in `.env` (gitignored) locally; Secret Manager on GCP.

---

## 13. Cost guardrails on GCP

- Cloud Run has a monthly free tier (vCPU-seconds, GiB-seconds, requests) and scales to
  zero: keep **min instances = 0** (one always-on 1 vCPU instance ≈ US$65/month).
- Cloud SQL has no free tier; smallest shared-core `db-f1-micro` ≈ US$7–10/month (no SLA).
  Stop or delete it after demos.
- **Do not use:** Cloud Composer (small env ≈ US$300+/month; use Cloud Run Job + Scheduler),
  Memorystore, Vertex AI Vector Search endpoints.
- Expected: ≈ US$0 (Firestore/BigQuery variant) to ≈ US$10–15/month (with Cloud SQL), plus
  small LLM API costs.
- Budget alerts only notify; they **do not stop spending**. Prefer a cheap region (e.g.
  us-central1) for demos.

---

## 14. Reference implementation (already written)

**Note: these two files are not in the repo. Step 5 was rebuilt from sections 3-5 instead (section 17).**
Two Python files produced during design, kept here as history of the intended design:

- `pricing_diagnostic_assistant.py`: mock data, deterministic core (`diagnose`,
  `check_rules_one`), tools (`resolve_products`, `diagnose_batch`, `get_sap_conditions`,
  `get_api_log`, `check_rules`, `search_docs`), tool schemas, allow-listed `execute_tool`,
  the LLM tool-use loop (Claude on Vertex via `AnthropicVertex`), and `manual_trace()` showing
  the same investigation hard-coded (the "glue" the LLM writes at runtime).
  `--tools-only` runs without an LLM.
- `diagnostic_session.py`: `SessionStore` (ownership, idle + cycle expiry, optimistic
  locking), `SessionLocks`, `SessionState` (group handles, last checks, render),
  `compact_history`, `ToolCache` (data_version + TTL, live vs snapshot), `FindingsStore`
  (human-confirmed only), session-aware tools with injected `ToolContext` and permission
  filtering, `DiagnosticService.handle_message`, `ScriptedLLM` and `VertexClaude`, and a
  9-step offline demo. SQLite stands in for Cloud SQL.

When porting: SQLite → Postgres, in-process lock → Postgres advisory lock (or Redis), mock
dicts → DuckDB / mock-sap queries, real brand placeholders → fictional brands,
`VertexClaude` → provider interface with an Anthropic API implementation.

---

## 15. Coding conventions for this repo

- Deterministic code makes every judgement; the LLM only plans, composes and explains.
- Tools: read-only, parameterised, batch-friendly, permission-filtered via injected context,
  small outputs (patterns, handles), audit-logged.
- All config from env vars; no secrets in code or git.
- Every step of section 9 must be runnable and have at least a smoke test.
- Synthetic data only; fictional names; no paid always-on GCP resources.
- Prefer simple, explicit code over frameworks; this is a teaching / portfolio codebase.

---

## 16. Open decisions

- **Name:** candidates *LastCall* (recommended: final clearance call + bar "last call"),
  *PriceTrace* (fully industry-neutral, emphasises diagnostics), *SellThrough*, *ShelfLife*.
  Avoid "markdown" alone in the name (collides with the Markdown format).
- **Licence (no commercial use):** standard OSI licences all allow commercial use, so this
  will be source-available. Options: **PolyForm Noncommercial 1.0.0** (learning and
  non-commercial use allowed) or **no licence** (all rights reserved, view-only portfolio).
  CC BY-NC 4.0 only for docs, not code.
- ~~LLM for demos~~ **Decided:** Anthropic API, `claude-haiku-4-5` for development
  (`claude-sonnet-5` optional for demos). Provider interface keeps `scripted` for offline use.
- ~~Airflow locally~~ **Decided: no.** Too heavy for six sequential steps; Composer is ruled out on cost
  (section 13), so the deployed shape is a Cloud Run Job + Scheduler. The pipeline is a plain job
  with per-step state in Postgres, idempotent reruns and a validation gate. An optional DAG wrapper
  could be added at the end.
- Open for step 6: embedding model (local sentence-transformers / Ollama vs an API).
- Open: cleanup of expired sessions (none yet); a "bypass the cache" option for urgent checks.

---

## 17. As built: step 5, the diagnostic-api

Status: steps 1-5 done, 207 tests. Nothing here needs BigQuery, Redis or any always-on paid resource.

### 17.1 Modules (`services/diagnostic_api/`)

| Module | Role |
|---|---|
| `core.py` | deterministic verdict per (sku, pack_qty, region); reuses the pipeline's pure `validate_rows` and `reconcile` |
| `tools.py` | the tools, schemas, allow-listed `execute_tool`, and the cache / diff / auto-close wrapper |
| `agent.py` | the LLM tool-use loop (`run_turn`), system prompt, step limit with escalation |
| `llm.py` | providers: `scripted` (offline, incl. the canonical demo) and `anthropic` |
| `state.py` | session state (groups, scope, last checks), history compaction, diffs |
| `runs.py` | newest finished pipeline run for a week, and its `data_version` |
| `cache.py` | shared tool-result cache in Postgres |
| `findings.py` | person-confirmed issues; auto-close when the failure is gone; renders open findings for the prompt |
| `sessions.py` | session store: ownership, expiry, per-session lock, optimistic locking, atomic save |
| `app.py` | FastAPI: sessions, messages, findings. `chat.py` is a terminal client |

Tools (all read-only): `resolve_products`, `diagnose_batch`, `get_sap_conditions`, `get_api_log`,
`check_rules`, `get_findings`, `search_docs` (stub until step 6). Tools take `skus` or a `group_id`.
The service never holds the SAP write key; mock-sap also gained a read-only `GET /pricing/submissions`.

### 17.2 Verdict codes (precedence order)

`NOT_IN_LIST` (not on this week's SAP list for that region) > `MISSING_PRICE` (engine skipped it, or
priced but never sent; `reason` is the skip reason or `NOT_SENT`) > `RULE_VIOLATION` (a hard rule fails;
`reason` lists the rules) > `API_REJECTED` (`reason` is SAP's code, e.g. `VALIDITY_OVERLAP`) >
`NOT_EFFECTIVE` (SAP accepted but a promotion overrides; `reason` = `PROMOTION`) > `PRICE_MISMATCH` >
`MISSING_IN_SAP` > `OK`.

### 17.3 What the model receives on each call

Rules (~700 tokens) + tool definitions (~1,100) + a short state block (~100, plus the week's open findings
in the user's regions, written by the service so the model need not remember to ask) + earlier turns
compacted to question + final answer + the current turn's tool traffic. Not sent: the cache, raw state JSON,
old tool results, full SKU lists (handles instead), transcripts, other users' data. A model call
inside a turn resends all of this (the API is stateless).

### 17.4 Memory layers and where they live (Postgres schema `diagnostic`)

| Layer | Table.column | Written by | Lifetime |
|---|---|---|---|
| Session state | `sessions.state` (JSONB) | tools/code, never the model | session |
| Compacted history | `sessions.history` | the loop, after a successful turn | session |
| Audit transcript | `turn_transcripts` (one row per turn, full tool traffic) | the loop, same transaction | kept after expiry (BigQuery on GCP) |
| Findings | `findings` | people only (`POST /findings`, `X-Role: analyst`) | until resolved |
| Tool cache | `tool_cache` | the tool wrapper | snapshot: until a new run (purged after 7 days); live: 5 min |

State holds: scope, groups (`G1`.. handles, never reused, max 10), last checks (max 5 scopes, for
diffs), earlier questions (max 30). Compaction runs when a question arrives: earlier turns shrink to
question + answer, whole turns at a time so tool_use / tool_result stay paired; beyond 10 earlier turns
the oldest survive only as a question in the state. Freshness rule kept from section 4.3: groups and
history only say what the user refers to; status always comes from a tool call in the current turn.

### 17.5 Cache

Key = hash of (tool, week, normalised skus, regions, the user's permitted regions, run, `data_version`).
Never the user's name or the question text. Users with the same access share entries; narrower access
gets a different key, so it can never be served a wider result. `data_version` = run id + finish time
of the newest finished run (SUCCEEDED / COMPLETED_WITH_ISSUES / BLOCKED) from `pipeline.pipeline_runs`;
a rerun finishes again and becomes current. Snapshot tools (`check_rules`, `get_api_log`) follow
`data_version`; live tools (`diagnose_batch`, `get_sap_conditions`) expire after 5 minutes.
Not cached: `resolve_products` (creates the group handle), `get_findings`, errors, and anything without a
`data_version`. A cache failure is treated as a miss. Session-specific parts (diff, closing findings)
are applied after the shared result, never stored in it. Diff scope = week + SKU list; a diff compares the
regions both checks covered, because the model may name regions differently from one call to the next.
The diff carries a `headline` sentence; the service puts the headline (and "findings closed
automatically") at the top of the answer itself, because small models did not reliably do it. Hits carry `cached: true` and the original `as_of`.

### 17.6 Sessions, identity, permissions

Identity: `X-User` header only (IAP stand-in), never body, query or model. Permissions: `USER_REGIONS`
env map injected into the tools. Writing findings needs `X-Role: analyst` and the region must be
permitted. Someone else's session is a 404, identical to an unknown id. Expiry: 120 min idle, and when
the week rolls over (410). One turn at a time per session (Postgres advisory lock, 409), optimistic
`version` check on save, state + history + audit written in one transaction only after the turn
succeeded (a failed model call returns 502 and saves nothing).

### 17.7 Findings

A finding = week, region, error code, reason, SKUs, root cause, owner, ticket, confirmed_by (from
`X-User`). The model sees open findings for the week in its prompt and via `get_findings` (both
region-filtered), where `owner` is called `owning_team` and `confirmed_by` is explained as "the person who
verified it, not the owning team" (a live run had conflated the two). It has no way to write them. After each `diagnose_batch`, deterministic code closes a finding only if the check covered its
whole scope (region and all SKUs) and none of those records still fail with its code and reason.

### 17.8 Deviations from sections 3-5

- `data_version` comes from the pipeline run table, not "latest run in BigQuery".
- Audit transcripts go to Postgres locally; BigQuery remains the GCP target.
- Findings carry SKUs and a reason, so auto-close is decidable.
- The service imports `validate_rows` and `reconcile` from `jobs/weekly_pipeline` (pure functions). The
  future diagnostic image must copy those files (and `jobs/__init__.py`, `jobs/weekly_pipeline/__init__.py`).
- Not built: cleanup of expired sessions, a cache bypass, the `PROMO_OVERLAP` pre-send rule.

### 17.9 Lessons from running it with a real model (Haiku 4.5)

- A model will misread an ambiguous field (`price`, `valid_from` became "list price" and "promotion
  dates"). Fix: remove the data or rename it, add a `fields` note. Making a tool unable to mislead beats
  asking the prompt nicely.
- Prompt rules are followed unevenly by a small model (report every pattern, lead with the diff, check
  findings first). Prefer moving the fact into code: per-region totals, the diff headline written by
  code and put above the answer, findings placed in the prompt by the service. Two prompt-only attempts
  at "start with the diff" failed live; the code version cannot.
- Known remaining Haiku weaknesses: it sometimes leaves out an unrelated pattern (a SKU with no SAP
  rule), calls a missing rule a "violation", and says promotions "take priority" although no tool gives
  priorities. Options: `claude-sonnet-5`, or a code-side completeness check on the answer.
- A diff keyed on arguments the model chooses (regions) silently disappears when it chooses differently;
  key bookkeeping on what the user means (week + SKUs), not on tool arguments.
- Counts and comparisons the model does itself go wrong ("13 of 13"); give it the totals.

### 17.10 Commands

```
docker compose up -d postgres mock-sap
.venv/bin/python -m pytest tests -q                                   # 200 tests; Postgres needed for some
.venv/bin/python -m jobs.data_gen.scenarios --week 2026-W39 --apply promo overlap
.venv/bin/python -m jobs.weekly_pipeline --week 2026-W39 --run-id 2026-W39-demo
.venv/bin/python -m services.diagnostic_api.chat "question"           # terminal client (--run-id to pin a run)
.venv/bin/uvicorn services.diagnostic_api.app:app --port 8000         # POST /sessions, /sessions/{id}/messages, /findings; see /docs
```

---

## Appendix A. Interview talking points (short)

- **System framing:** "SAP was the system of record: each week it provided the clearance
  candidate list and business rules such as price floors. Our engine on GCP, orchestrated by
  Airflow, generated optimised markdown prices within those constraints and returned them to
  SAP via a REST API for execution."
- **SAP push:** "Prices went out in batches with retries for transient errors and
  record-level status in Cloud SQL; a reconciliation step checked the prices actually active
  in SAP against what we sent."
- **Why an LLM:** "The diagnostic checks were deterministic and had to be. The LLM
  understood questions of many shapes, decided which checks to run and how to aggregate
  them, and combined results with playbooks to explain. If questions had been limited to a
  few fixed patterns, a dashboard on top of the rules would have been enough."
- **New problems:** "For issues the taxonomy didn't cover, the LLM traced evidence and
  suggested low-confidence hypotheses; once an engineer confirmed the root cause it went
  into the playbook and, where possible, the rule engine."
- **Memory:** "Structured facts lived in code-managed session state with group handles;
  older tool outputs were compacted; current status was always re-queried. Long-term
  memory was confirmed root causes, not chat history."
- **Repeat questions:** "We cached tool results keyed on a structured intent, the data
  version and permission scope, never on text similarity; repeat questions got a diff since
  the last check; only human-confirmed findings were shared."
- **Sessions:** "Stateless service, sessions bound to the SSO identity, optimistic locking
  plus a per-session lock, expiry at the end of the pricing cycle, access control in the
  tool layer, full transcripts to BigQuery for audit."
- **Service split:** "Split by permission boundary: the pipeline's service account could
  write prices to SAP, the diagnostic service's could only read, so no LLM output could
  change a price."
- **Honesty:** describe what was actually built in production; present the rest as
  "how I would improve it". Confirm real details (RAG store, SAP middleware, what was
  measured) before interviews.
