---
id: PB-MISSING-IN-SAP
type: playbook
title: Our records were accepted but SAP holds no condition for them
tags: [MISSING_IN_SAP]
owner: SAP Pricing team
severity: blocking
last_reviewed: 2026-09-21
---

## Symptoms
Our records show as accepted, but SAP holds no price condition for the items.

## Why it happens
Either the condition was deleted or expired in SAP after it was accepted, or the diagnosis compared an older run with SAP's
current state. A run's records can be replaced by a later run, so the older run's accepted records no longer exist.

## How to confirm
- First check which run and data version the diagnosis used, and whether a newer run exists. If it is not the newest run,
  diagnose again with the newest run; the mismatch is an artefact.
- If the run is the newest, the SAP conditions check confirms nothing is held.

## What to do
1. If the run was stale, nothing is wrong: repeat the diagnosis on the newest run.
2. If the condition is truly missing, the SAP Pricing team investigates the deletion or expiry, then the pipeline is rerun.

## What not to do
- Do not assume our pipeline lost the price before checking which run was diagnosed.

## Related
PB-NO-RECOMMENDATION, PB-PRICE-MISMATCH, REF-PIPELINE-STATUSES
