---
id: PB-EXCEEDED-MAX-WEEKS
type: playbook
title: An item has been on clearance longer than its limit
tags: [MISSING_PRICE:EXCEEDED_MAX_WEEKS, RULE_VIOLATION:EXCEEDS_MAX_WEEKS]
owner: Merchandising team
severity: warning
last_reviewed: 2026-09-21
---

## Symptoms
An item is still on the list but has no price, with the reason EXCEEDED_MAX_WEEKS. Or, less often, the pre-send validation
reports RULE_VIOLATION with EXCEEDS_MAX_WEEKS and blocks the run.

## Why it happens
Each item may stay on clearance for a limited number of consecutive weeks (its maximum clearance weeks, currently 10 for the
rules in use). Beyond that no new markdown is set. SAP normally stops listing the item at the limit; if it is still listed,
its week number is above its limit. The RULE_VIOLATION version means the engine priced past the limit, which is a defect.

## How to confirm
- Compare the item's clearance week number with its maximum in the rules.
- Check whether it was on the list in the previous weeks without a gap.

## What to do
1. The Merchandising team decides: take the item off the list (the normal outcome), or extend the limit in SAP.
2. For the RULE_VIOLATION version, the Pricing Data Science team fixes the engine before rerunning.

## What not to do
- Do not price the item anyway to keep it moving.

## Related
POL-CLEARANCE-LIFECYCLE, PB-RULE-VIOLATION-PRICE
