---
id: PB-MISSING-RULE
type: playbook
title: An item has no SAP business rule
tags: [MISSING_PRICE:MISSING_RULE, RULE_VIOLATION:MISSING_RULE]
owner: Merchandising team
severity: blocking
last_reviewed: 2026-09-21
---

## Symptoms
An item is on this week's list but has no price, with the reason MISSING_RULE. It appears in every region where it is
listed, and it has nothing to do with promotions.

## Why it happens
SAP holds the business rules per item and pack size: the price floor, the maximum markdown and the maximum clearance
weeks. This item has no rule for the week, so the engine will not price it: it cannot invent a floor. If the engine did
price a row without a rule, the pre-send validation reports RULE_VIOLATION with MISSING_RULE and blocks the whole run,
which then points at a defect for the Pricing Data Science team.

## How to confirm
- The diagnosis shows MISSING_PRICE with MISSING_RULE for the item in every listed region, since rules are per item, not per region.
- A RULE_VIOLATION version, with a blocked run, means the engine and the gate disagree.

## What to do
1. The Merchandising team enters the rules for the item in SAP.
2. Rerun the pipeline: the next fetch picks the rules up and the item is priced.
3. For the RULE_VIOLATION version, the Pricing Data Science team fixes the engine before rerunning.

## What not to do
- Do not assume a default floor or price the item by hand outside SAP.

## Related
PB-RULE-VIOLATION-PRICE, POL-PRICE-RULES
