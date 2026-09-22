---
id: REF-PIPELINE-STATUSES
type: reference
title: The weekly pipeline: steps, statuses and reruns
tags: [MISSING_PRICE:NOT_SENT, MISSING_PRICE:NO_RECOMMENDATION]
last_reviewed: 2026-09-21
---

## Steps
1. **ingest:** fetch the clearance list and rules from SAP.
2. **price:** the engine sets a price per listed item, or skips it with a reason.
3. **validate:** an independent gate re-checks every hard rule. Any violation blocks the run.
4. **send:** post prices to SAP in batches, with retries for transient errors and a result per record.
5. **reconcile:** ask SAP what it holds and applies, and compare.
6. **history:** record the week's effective prices.

## Run statuses
| Status | Meaning |
|---|---|
| SUCCEEDED | every priced record was accepted and confirmed effective |
| COMPLETED_WITH_ISSUES | the run finished, but some records were rejected, errored or not effective |
| BLOCKED | validation found a violation; nothing was sent |
| FAILED | a step raised an exception, for example SAP was unreachable during ingest |

## Reruns
Rerunning the same run id resends only the records SAP has not accepted, so it is safe and idempotent. A new run id is a
separate run; its records overlap those already accepted and are rejected as validity overlaps. The current picture of a
week is the newest finished run, and a run that is rerun finishes again and becomes the newest.

## Related
PB-NOT-SENT, PB-NO-RECOMMENDATION, PB-VALIDITY-OVERLAP
