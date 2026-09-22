---
id: PB-FLOOR-ABOVE-PRICE
type: playbook
title: The lowest allowed price is above the current price
tags: [MISSING_PRICE:FLOOR_ABOVE_PRICE]
owner: Merchandising team
severity: warning
last_reviewed: 2026-09-21
---

## Symptoms
An item is on the list and has rules, but the engine produced no price. The reason is FLOOR_ABOVE_PRICE.

## Why it happens
The lowest price a markdown may take is the higher of the SAP price floor and the maximum-markdown limit (for example no
more than 50% off the shelf price). Here that lowest allowed price is already above the item's current price. A markdown
must never raise a price, so no valid new price exists this week. Typical causes: a floor set close to or above the
current price, or an item already marked down to its limit.

## How to confirm
- The diagnosis reason is FLOOR_ABOVE_PRICE, often for a few items rather than a whole category.
- The Merchandising team can compare the item's floor and maximum markdown with its current price in SAP.

## What to do
1. The Merchandising team decides: lower the floor if margin allows, or take the item off clearance.
2. Until then the item keeps its current price. That is intended behaviour, not an error to bypass.
3. After a rule change in SAP, rerun the pipeline.

## What not to do
- Do not override the engine's refusal or price below the floor.
- Do not treat it as a data error; the rules did exactly what they should.

## Related
POL-PRICE-RULES, PB-LADDER
