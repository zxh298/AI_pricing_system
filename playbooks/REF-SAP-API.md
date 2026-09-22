---
id: REF-SAP-API
type: reference
title: How our pipeline talks to SAP
tags: [API_REJECTED:HTTP_429, API_REJECTED:HTTP_503, API_REJECTED:VALIDITY_OVERLAP, API_REJECTED:NOT_ON_LIST]
last_reviewed: 2026-09-21
---

## What SAP provides
SAP is the system of record. It gives us the weekly clearance list with its rules, accepts our prices, and reports the prices it
holds. In this project it is a stand-in service with the same behaviour, including injectable faults.

## Keys
There are two API keys. The read key can fetch the list, the conditions and the submission log. Only the write key can post
prices, and only the pipeline holds it. The diagnostic service has the read key only, so nothing it says or does can change a
price.

## Sending prices
Prices go in batches. SAP answers per record: ACCEPTED, or REJECTED with a code. It checks the format, that the item is on
that week's list for that region, and that no other condition record overlaps the validity dates. It does not check business
rules; those are the pipeline's job.

## Idempotency
A record's key is the run id, SKU, pack size, region and start date. Sending an identical record again is acknowledged as
already accepted. The same key with a different price is rejected as an idempotency conflict.

## Retries
Rate limits (429), server errors (5xx) and network errors are retried with exponential backoff, honouring Retry-After. Data or
authentication errors (400, 401, 403, 413, 422) are not retried and are recorded for escalation. A batch that keeps failing does
not stop the other batches.

## Related
PB-API-TRANSIENT, PB-API-REJECTED-REQUEST, PB-VALIDITY-OVERLAP, PB-IDEMPOTENCY-CONFLICT
