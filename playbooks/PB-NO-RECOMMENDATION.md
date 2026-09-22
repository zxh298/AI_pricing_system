---
id: PB-NO-RECOMMENDATION
type: playbook
title: The engine produced no row for a listed item
tags: [MISSING_PRICE:NO_RECOMMENDATION]
owner: Data Platform team
severity: blocking
last_reviewed: 2026-09-21
---

## Symptoms
A listed item has no engine result at all in the run that was checked: not a price, and not even a skip reason.

## Why it happens
Usually the pipeline has not run for this week yet, or the run being checked is older than the change that added the item to
the list. Less often the wrong run was selected.

## How to confirm
- Note which run and data version the diagnosis used, and whether a newer run exists.
- Check when the list was last fetched against when the run finished.

## What to do
1. Run the pipeline for the week, or rerun it if the list changed after the last run.
2. Diagnose again once the run has finished.

## What not to do
- Do not conclude the engine skipped the item on purpose: a skip always has a reason.

## Related
REF-PIPELINE-STATUSES, PB-MISSING-IN-SAP
