---
id: PB-LADDER
type: playbook
title: A multipack price conflicts with its Single's price
tags: [MISSING_PRICE:LADDER_ABOVE_PRICE, RULE_VIOLATION:LADDER_VIOLATION]
owner: Pricing Data Science team
severity: blocking
last_reviewed: 2026-09-21
---

## Symptoms
A multipack (for example a 6 or 12 pack) has no price with the reason LADDER_ABOVE_PRICE, or the pre-send validation reports
RULE_VIOLATION with LADDER_VIOLATION and blocks the whole run.

## Why it happens
Price ladder rule: a multipack's per-item price must not go below its Single's price for the same SKU and region, so a pack
never undercuts its own Single. LADDER_ABOVE_PRICE means the lowest allowed pack price (pack size times the Single's price)
is above the pack's current price, so no valid pack price exists. LADDER_VIOLATION means the engine produced a pack price
that breaks the rule, which is an engine defect.

## How to confirm
- The price-rule check lists the pack rows and the rule that fails.
- Compare the Single's price and the pack's per-item price in the same region.

## What to do
1. For LADDER_ABOVE_PRICE, the Merchandising team reviews the Single and the pack: adjust the Single's markdown, or accept
   that the pack keeps its price.
2. For LADDER_VIOLATION, the Pricing Data Science team fixes the engine (typically the Single's price it used), then reruns.

## What not to do
- Do not hand-edit the pack price to pass the gate.
- Do not relax the ladder rule to clear a blocked run.

## Related
INC-2026-W37-LADDER-BLOCK, POL-PRICE-RULES
