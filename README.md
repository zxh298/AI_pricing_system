# AI Pricing System

A personal portfolio project: a local-first simulator of a retail clearance (markdown) pricing system, plus
a weekly triage that finds what went wrong and who should act, and a GenAI assistant for follow-up questions.

It re-creates, on **synthetic data with fictional brands**, the shape of a production pricing pipeline: a
system of record (SAP) supplies the weekly clearance list and business rules, a pricing engine computes
markdowns within those constraints, and the prices are sent back to SAP and reconciled. On top of that sits
a diagnostic assistant that answers questions like *"why didn't prices drop in VIC this week?"* by calling
deterministic tools and citing a small knowledge base of synthetic playbooks — never by guessing.

No employer code, data or internal documents are used anywhere in this repository.

## Architecture

- **The pricing side is deterministic.** A rule engine sets prices within SAP's floor, maximum markdown and
  ladder rules; an independent validation gate re-checks every rule before anything is sent, so a defect can
  never reach SAP.
- **The diagnostic side never judges a price itself.** In the default router mode the LLM only classifies
  the question (an intent plus slots such as brand and region, checked by code) and explains the result;
  each intent runs a fixed sequence of read-only tools, and the SKUs, prices and document ids the answer
  quotes are checked against the results. Questions no workflow covers fall back to an open tool-use loop,
  and the answer says so (`AGENT_MODE=loop` uses that loop for everything). Every verdict (`NOT_EFFECTIVE`,
  `RULE_VIOLATION`, `API_REJECTED`, ...) is computed by plain Python from live data. A separate knowledge base of playbooks (tag-matched, with similarity search
  as a fallback) supplies the *why* and *who owns it* — see `playbooks/README.md`.
- The diagnostic service holds a **read-only** SAP key and a **read-only** mount of the warehouse; it has no
  way to change a price, enforced by its container's environment and filesystem, not just by code.

## Why this version

The first version was a chat assistant: it explained a problem well, but only after someone asked. That left four gaps
in how a pricing team actually works.

- **Nobody asks about a problem they have not noticed.** In the synthetic W36 incident, reconciliation flagged the
  affected prices on Monday morning, and a store manager raised them that afternoon. The system already knew; it had no
  way to say so. Clearance prices run for a week, so lost days cost real sales.
- **Chat only covers what it is asked about.** Now every record is checked, every week.
- **An answer is not an outcome.** A chat reply names an owner, but nothing tracks whether anyone acted. Now each problem
  is a case with an owner, a decision and a deadline.
- **Most weeks have the same question.** "What went wrong, and who handles it?" has one fixed shape, and rules answer
  that better than a model.

So this version moves the main workflow from *asking* to *being told*:

| | Before | Now |
|---|---|---|
| Starts when | someone asks | the pipeline finishes |
| Finds problems | the model picks which checks to run | human-written checks, on every record |
| Known errors | an LLM explains them | playbook templates, no LLM |
| Result | an answer in a chat | a case with an owner and a decision |
| Done when | the answer is read | a later scan no longer finds the problem |
| Chat | the main interface | kept, for follow-up questions |

**LLM use went down, deliberately.** On the seeded demo week the scan found 24 failing records in 5 cases and used no LLM
at all. The agent is kept for the case rules cannot reach: every check passes, sales still collapse, and there is no error
code to look up. Multi-agent was considered and rejected, because the investigation is sequential and each "specialist"
would just be a tool.

**What this version does not prove.** The agent found the planted cause in 12 of 12 evaluation runs, but only one of the
six scenarios needs a result before it can ask its next question; a fixed workflow would probably handle the others. The
scenarios are synthetic, and a cause nobody has seen is not measured. The agent's value rests on that long tail of unknown
problems: as confirmed causes become rules it should do less, and if new problems become rare it should be turned off.

## The weekly triage

After each pipeline run, `weekly-triage` scans every record and reports to whoever can act. It is mostly a scheduled
job, not an agent: human-written checks find the problems, and an LLM only appears where rules cannot go.

| Case | How it is found | Who writes the explanation | Goes to |
|---|---|---|---|
| **Confirmed**: the evidence names the cause | `core.diagnose()` | playbook template, no LLM | the owner in the playbook |
| **Likely**: a playbook fits, nothing proves it | `core.diagnose()` | same template, marked unproven, no LLM | the owner in the playbook |
| **Unclassified**: no playbook | `core.diagnose()` | one LLM call with no data tools, for developers | development team |
| **`LOW_SELL_THROUGH`**: every record is `OK`, but it stopped selling | sales against each record's own baseline | a bounded read-only agent | set by code, if code confirms the cause |

Technical problems skip the business and go straight to the development team. Owners accept, waive, or reject with a
reason, and the reason routes it: `wrong_cause` to development, `wrong_owner` to the new owner, `action_refused` to a
business decision-maker, and a missed deadline escalates by itself. An owner's Accept writes findings under their name
but does not close anything: **only a later scan that no longer finds the problem closes a case**.

Sales for the week being priced do not exist yet, so the sales check looks at the last completed week.

**Why an agent at all, and only there.** Known errors have an error code, a fixed set of evidence and a playbook; a
template handles them better than a model. `LOW_SELL_THROUGH` has no code and no playbook, and what to look at next can
depend on the last result: in the `substitute` scenario a promotion on another brand takes the sales, and that product
is not in the case, so the agent has to find it before it can ask why. The agent cannot change anything. It names a
hypothesis, and code then re-checks each cause with the same functions the tools use; a cause with no signal behind it
is dropped and the case goes to development. Confirmed causes should become rules, so the agent's share shrinks over time.

**Mass failures.** If half the records fail at once they share a cause upstream, so a circuit breaker stops: no LLM, no
owners paged, no deadlines, one incident to Data Platform. LLM steps also have a budget per scan, largest cases first.

```bash
infra/run_week.sh 2026-W39 demo                # pipeline, then triage; triage runs even if the pipeline is BLOCKED
                                               # rerun a week with its existing run id: a new id overlaps its own records
.venv/bin/python -m jobs.weekly_triage --sweep # escalate cases past their deadline

# plant a cause in a copy of the warehouse, for a demo
cp data/warehouse.duckdb data/demo.duckdb
.venv/bin/python -m jobs.data_gen.outcome_scenarios --path data/demo.duckdb --apply cannibal substitute
DUCKDB_PATH=data/demo.duckdb .venv/bin/python -m jobs.weekly_triage --week 2026-W39
.venv/bin/python -m jobs.data_gen.outcome_scenarios --clear   # removes the planted promotions
```

Results and their limits: `evals/TRIAGE_RESULTS.md`. Notifications are an outbox shown in the ui, not real email or
Slack. Investigations run one at a time.

## What's built

| Step | | |
|---|---|---|
| 1 | Synthetic data generator (DuckDB + SAP) | `list_products` | done |
| 2 | Pricing engine (deterministic markdown rules) | `diagnose` | done |
| 3 | mock-sap + retrying sender, fault injection | `sap_prices` | done |
| 4 | Weekly pipeline (ingest → price → validate → send → reconcile → history) | `api_log` | done |
| 5 | diagnostic-api: tools, LLM loop, sessions, cache, findings | `rule_check` | done |
| 6 | Knowledge base (RAG): playbooks, embeddings, search, retrieval eval | `known_issues` | done |
| 7 | Streamlit chat ui | `how_to` | done |
| 8 | Containers for diagnostic-api and ui | `out_of_scope` | done |
| 8 | IAM / deploy scripts for GCP | `out_of_scope` | not started |
| 9 | Actual GCP deployment | *(no chat turn)* | not started |
| – | Router mode: classify → fixed workflow → explain, answer check, routing eval | done |
| 10 | Weekly triage: full-week scan, cases, owner decisions, investigation agent, triage eval | done |

507 automated tests, most of them requiring nothing but Python; the rest need the Postgres container. See
`docs/PROJECT_CONTEXT.md` (local, not tracked in git) for the full design write-up.

## Requirements

- **Python 3.11+** (the Anthropic SDK's 1.x line needs it). On macOS, `python3` on `PATH` is often the
  system's old bundled Python (3.9), not a modern one — check `python3 --version` first, or point the venv
  at a specific interpreter (e.g. `/opt/homebrew/bin/python3.13`, or whatever `pyenv`/`conda` gives you).
  Using the wrong one won't fail loudly; pip will just silently resolve several packages down to old
  versions still compatible with it.
- **Docker** and **Docker Compose** (for Postgres, mock-sap, and optionally diagnostic-api/ui as containers).
- An [Anthropic API key](https://console.anthropic.com) for real answers — optional; `LLM_PROVIDER=scripted`
  runs the assistant offline with no key, replaying a canned investigation.
- **`requirements.txt`** is the human-edited list of what's needed, unpinned except where a version genuinely
  matters (`duckdb`, pinned to match across the containers that share the same warehouse file). For an exact,
  reproducible set of versions, install from **`requirements-lock.txt`** instead (regenerate it with a clean
  venv built from the *correct* interpreter — see `requirements-lock.txt`'s own header for the exact command).

## Quick start

```bash
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt   # or requirements-lock.txt to pin exact versions
cp .env.example .env    # then set ANTHROPIC_API_KEY and LLM_PROVIDER=anthropic
```

Bring up the databases first, seed the synthetic data, price and send a week, build the knowledge base,
then bring up the assistant:

```bash
docker compose up -d postgres mock-sap

.venv/bin/python -m jobs.data_gen.generate --force                    # seed DuckDB + SAP (full reset)
.venv/bin/python -m jobs.data_gen.scenarios --apply promo overlap     # plant two demo problems (optional)
.venv/bin/python -m jobs.weekly_pipeline --week 2026-W39 --run-id demo
.venv/bin/python -m jobs.rag_ingest                                   # embed the playbooks into Postgres

docker compose up -d diagnostic-api ui
```

Open **http://localhost:8501**, start a session, and ask:

> Larkspur clearance this week: many prices didn't drop in VIC, NSW is fine. Why?

## Example questions and how they're answered

The LLM never touches a database or writes SQL. It classifies each question into an intent, and code runs
that intent's fixed set of read-only tools, each backed by a specific store; the table below traces that path
for real questions against this repo's own seeded demo data (`2026-W39`), with the answers either pulled
straight from the tool or from an actual live run of the assistant. The quoted model answers (rows 2, 5 and 7)
come from a live run in the earlier loop mode, before router mode existed; the tools behind them are the same.

| # | Question | Intent | Tool(s) | What runs underneath | Terminates in | Answer |
|---|---|---|---|---|---|---|
| 1 | "What's on this week's list for Larkspur?" | `list_products` | `resolve_products` | `ctx.wh.get_candidates(week)` — filters in Python by brand after | DuckDB SQL (`shared/warehouse.py`) | 13 SKUs: `100002, 100012, 100013, 100014, 100024, 100041, 100042, 100048, 100050, 100079, 100084, 100088, 100097` |
| 2 | "Why didn't VIC prices drop?" | `diagnose` | `diagnose_batch`, then `get_sap_conditions` / `get_api_log` only if it found overrides / rejections | `ctx.wh.get_candidates`/`get_recommendations`/`get_rules` **+** `GET /pricing/conditions` on mock-sap, then `core.diagnose()` (pure Python) combines them | **Two** stores: DuckDB SQL, and an HTTP call (mock-sap runs its own SQL invisibly to diagnostic-api) | "Here are the three VIC products with active promotions overriding clearance: [100012 \$16.79→\$12.49, 100013 \$13.49→\$10.07, 100014 \$17.99→\$12.59]... The Promotions team needs to review these promotions and decide whether to let them continue or adjust them." |
| 3 | "What price does SAP actually hold for these SKUs?" | `sap_prices` | `get_sap_conditions` | `GET /pricing/conditions` on mock-sap | HTTP only — no direct DB access from diagnostic-api | All three overridden by `PROMOTION`: 100012 sent 16.79 / effective 12.49; 100013 sent 13.49 / effective 10.07; 100014 sent 17.99 / effective 12.59 |
| 4 | "What did SAP say when it rejected these records?" | `api_log` | `get_api_log` | `GET /pricing/submissions` on mock-sap | HTTP only | 9 problems, all `VALIDITY_OVERLAP`, 3 per region for SKUs 100017/100019/100020 — "overlaps existing condition record (run LEGACY-PREV-WEEK, 2026-09-14 to 2099-12-31)" |
| 5 | "Would these go below floor if the promotion ended?" | `rule_check` | `check_rules` | `ctx.wh.get_recommendations`/`get_rules` (SQL), then `validate_rows()` — the same pure function the pipeline itself uses | DuckDB SQL **+** pure Python (no I/O in the check itself) | "The three SKUs passed the price floor rule check with no violations... it does not show the floor amount itself... someone with access to the floor thresholds would need to compare them." |
| 6 | "Is this a known issue?" | `known_issues` | `get_findings` | `ctx.findings.list()` | Postgres SQL (`findings.py`) | 0 findings for `2026-W39` — none recorded yet |
| 7 | "What do I do about a 429?" | `how_to` | `search_docs` | `ctx.kb.search()` — tag lookup (SQL array match) first, pgvector similarity as fallback | Postgres SQL (`knowledge.py`) | "**Next steps** [PB-API-TRANSIENT]: 1. Wait for SAP to recover, then rerun with the same run id... 2. If 429 repeats, reduce the batch size... 3. If it lasts more than an hour, escalate to the SAP Pricing team." |
| 8 | "What's the weather today?" | `out_of_scope` | *(none)* | classify returns `out_of_scope`; the service sends a fixed reply — no tool and no second model call | nothing — no store, no HTTP, no SQL touched | "I can only help with the weekly clearance pricing system: which products are on the list, whether their prices took effect in SAP and why not, rule checks, known issues and what to do about a problem." |
| 9 | *(an analyst records a finding — not a chat question)* | *(no chat turn)* | *(none)* | `POST /findings` → `FindingsStore.create()` directly | Postgres SQL — bypasses the model entirely | example response shape: `{"finding_id": 1, "status": "open", "confirmed_by": "ana", ...}` |

Row 5 is left exactly as the model produced it, not touched up: `check_rules` already answered the question
(zero violations on the recommended prices *is* "no, it would not go below floor"), but the model hedged
instead of stating that plainly — a real, first-try answer, kept here because it's a fair example of where
the prompt could be tightened rather than only showing the cases that went well.

## Tests

```bash
docker compose up -d postgres    # most tests need it; a few run without any database
.venv/bin/python -m pytest tests -q
.venv/bin/python -m evals.routing   # how often the real model picks the right intent and slots (needs ANTHROPIC_API_KEY)
.venv/bin/python -m evals.triage --check    # is each planted scenario flagged? (no key, no cost)
.venv/bin/python -m evals.triage --runs 2   # does the agent find the planted cause? (needs ANTHROPIC_API_KEY)
```

## Repository layout

```
jobs/                weekly pipeline, weekly triage, synthetic data generator, RAG ingest job
services/mock_sap/    stand-in for SAP: clearance list, price submission, fault injection
services/diagnostic_api/   tools, LLM loop, sessions, tool cache, findings, RAG search, triage rules and cases
services/ui/          Streamlit client: weekly triage, chat, findings
shared/               config, warehouse client, Postgres schemas, embeddings
playbooks/            synthetic playbooks, incident notes, policies (the RAG corpus)
evals/                retrieval, routing and triage evaluations
tests/                pytest suite
```

## License

No license has been chosen yet; treat this as "all rights reserved, portfolio viewing only" until that's
decided.
