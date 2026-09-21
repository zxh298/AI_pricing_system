---
id: PB-RULE-VIOLATION-PRICE
type: playbook
title: The engine produced a price that breaks a hard rule
tags: [RULE_VIOLATION:BELOW_FLOOR, RULE_VIOLATION:BELOW_MAX_MARKDOWN, RULE_VIOLATION:PRICE_ABOVE_LAST_WEEK, RULE_VIOLATION:DUPLICATE_KEY, RULE_VIOLATION:BAD_PRICE_FORMAT]
owner: Pricing Data Science team
severity: blocking
last_reviewed: 2026-09-21
---

## Symptoms
The diagnosis reports RULE_VIOLATION with one or more rules: BELOW_FLOOR, BELOW_MAX_MARKDOWN, PRICE_ABOVE_LAST_WEEK,
DUPLICATE_KEY or BAD_PRICE_FORMAT. The pipeline run is BLOCKED and no price reaches SAP for any item.

## Why it happens
Before sending, an independent gate re-checks every hard rule on the engine's output: the price is at or above the SAP
floor; not below the maximum markdown; never above last week's price; one row per item, pack size and region; positive with
at most two decimals. A single violation blocks the whole send. It means an engine defect or bad input, never a SAP fault.

## How to confirm
- The price-rule check gives counts, the failing rule and sample SKUs.
- One rule across many items suggests a systematic engine bug. A few items suggest bad input, for example a rule or price
  that changed during the run.

## What to do
1. The Pricing Data Science team fixes the cause and reruns. Until then no prices go to SAP for the week, so raise the
   urgency ahead of the weekly cutoff.
2. Confirm afterwards that the run succeeded and the items are effective.

## What not to do
- Do not bypass the gate or relax a rule to release the run.
- Do not send the unaffected items by hand.

## Related
INC-2026-W37-LADDER-BLOCK, POL-PRICE-RULES, PB-NOT-SENT
