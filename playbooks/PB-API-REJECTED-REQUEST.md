---
id: PB-API-REJECTED-REQUEST
type: playbook
title: SAP refuses the request itself (bad credentials, size or body)
tags: [API_REJECTED:HTTP_400, API_REJECTED:HTTP_401, API_REJECTED:HTTP_403, API_REJECTED:HTTP_413, API_REJECTED:HTTP_422]
owner: Data Platform team
severity: blocking
last_reviewed: 2026-09-21
---

## Symptoms
Records ended as errors with HTTP_400, HTTP_401, HTTP_403, HTTP_413 or HTTP_422. They were not retried.

## Why it happens
These are not transient. 401 means the API key is missing or wrong. 403 means the write key is required but another key
was used. 413 means a batch is larger than SAP accepts. 400 and 422 mean the request body or its data is unacceptable.
Retrying cannot help, so the sender records the error straight away for escalation.

## How to confirm
- The API log message says which. 401 and 403 hit every record; 413 hits whole batches.
- A change in credentials, batch size or deployment just before the run is the usual trigger.

## What to do
1. Fix the credential (in the secret store or environment), lower the batch size, or correct the payload.
2. Rerun with the same run id; only unaccepted records are resent.

## What not to do
- Do not retry blindly.
- Never give the diagnostic service the write key. Only the pipeline may write prices to SAP.

## Related
PB-API-TRANSIENT, PB-BAD-FORMAT, REF-SAP-API
