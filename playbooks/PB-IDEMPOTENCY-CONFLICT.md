---
id: PB-IDEMPOTENCY-CONFLICT
type: playbook
title: SAP already accepted this record with a different price
tags: [API_REJECTED:IDEMPOTENCY_CONFLICT]
owner: Data Platform team
severity: blocking
last_reviewed: 2026-09-21
---

## Symptoms
SAP rejected a record with the code IDEMPOTENCY_CONFLICT and the message that the same key was accepted with a different
price.

## Why it happens
Each record is keyed by run id, SKU, pack size, region and start date. Resending the same run id is safe, because an
identical record is simply acknowledged. But if the engine now recommends a different price for a record SAP has already
accepted under that run id (inputs changed between runs, or the engine changed), SAP refuses to overwrite it.

## How to confirm
- Compare the price SAP holds (SAP conditions check) with the price the engine now recommends.
- Look at the run's attempt count: several attempts with changes in between point at this.

## What to do
1. Decide which price is right.
2. If the accepted price is wrong, the SAP Pricing team must end that condition, and the corrected price needs a new run id
   because a rerun of the same id cannot change an accepted price.
3. The Data Platform team avoids changing engine inputs between reruns of one run.

## What not to do
- Do not keep rerunning the same run id hoping it passes.
- Do not start a new run id without ending the old condition: the new record would overlap it.

## Related
PB-VALIDITY-OVERLAP, REF-PIPELINE-STATUSES
