---
id: PB-VALIDITY-OVERLAP
type: playbook
title: SAP rejects a price because its validity overlaps an existing record
tags: [API_REJECTED:VALIDITY_OVERLAP]
owner: SAP Pricing team
severity: blocking
last_reviewed: 2026-09-21
---

## Symptoms
SAP rejected records with the code VALIDITY_OVERLAP. The message names the earlier condition record: the run that
created it and its dates, often an end date far in the future such as 2099-12-31. It usually hits the same items in
every region, and the items keep their previous price.

## Why it happens
For one item and region, SAP allows a single condition record at a time. An earlier record, for example last week's
markdown created without a proper end date, or a manual entry, still covers this week's dates, so SAP refuses to create
an overlapping one. It is a data problem in SAP, not a transient failure.

## How to confirm
- The API log check groups the rejections by code and message: the message shows the earlier run and its dates.
- Confirm the earlier record is not from the current run. If it is, see the idempotency conflict playbook instead.
- Note whether all regions of an item are affected, which points at an old open-ended record.

## What to do
1. The SAP Pricing team ends or shortens the earlier condition record, with an end date before this week's start.
2. Rerun the pipeline with the same run id. Only records SAP has not accepted are resent, so nothing is duplicated.
3. Run the diagnosis again to confirm the items are accepted and effective.
4. If our own past runs created open-ended records, tell the Data Platform team.

## What not to do
- Do not change the valid-from date to dodge the overlap.
- Do not rerun under a new run id: the records already accepted would overlap again.
- Do not try to delete SAP records from our side; the diagnostic service is read-only.

## Related
INC-2026-W35-OPEN-END-OVERLAP, PB-API-TRANSIENT
