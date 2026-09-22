---
id: PB-API-TRANSIENT
type: playbook
title: Sending to SAP fails with a rate limit, outage or network error
tags: [API_REJECTED:HTTP_429, API_REJECTED:HTTP_500, API_REJECTED:HTTP_502, API_REJECTED:HTTP_503, API_REJECTED:HTTP_504, API_REJECTED:TRANSPORT]
owner: Data Platform team
severity: warning
last_reviewed: 2026-09-21
---

## Symptoms
Records ended as errors with the code HTTP_429, HTTP_500, HTTP_502, HTTP_503, HTTP_504 or TRANSPORT. Whole batches fail
together, and nothing is held in SAP for them.

## Why it happens
SAP is rate-limiting us (429), is temporarily unavailable (5xx), or the network failed (TRANSPORT). The sender already
retries these with exponential backoff, honouring the Retry-After header, up to four retries per batch. A batch that still
fails is marked as errors while the other batches carry on, so a partial failure is normal and is tracked per record.

## How to confirm
- The API log check shows the same HTTP code across many records, often the same region or a run of batches.
- The SAP conditions check finds nothing held for them.
- Errors that start and stop with SAP's own availability are transient; the same error every time is not.

## What to do
1. Wait for SAP to recover, then rerun the pipeline with the same run id. Only records SAP has not accepted are resent.
2. If 429 repeats, reduce the batch size or spread the runs out.
3. If 5xx lasts more than about an hour, escalate to the SAP Pricing team.

## What not to do
- Do not rerun under a new run id: records already accepted would then be rejected as overlapping.
- Do not treat it as a data problem; check SAP's availability first.

## Related
PB-API-REJECTED-REQUEST, REF-SAP-API
