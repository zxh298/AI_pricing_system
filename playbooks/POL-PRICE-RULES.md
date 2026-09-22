---
id: POL-PRICE-RULES
type: policy
title: The hard price rules
tags: []
last_reviewed: 2026-09-21
---

## The rules
1. **Floor.** A price is never below the SAP price floor for the item and pack size.
2. **Maximum markdown.** A price is never further below the shelf price than the item's maximum markdown allows (currently
   50%).
3. **Ladder.** A multipack's per-item price is never below the Single's price for the same SKU and region.
4. **Never up.** A price is never above last week's price for the same item and region.
Prices are positive, have at most two decimals, and there is one price per item, pack size and region.

## Which rule sets the price
The lowest allowed price is the highest of the floor, the maximum-markdown limit and the ladder, rounded up to the cent. The
scheduled price is used if it is at or above that. Otherwise the lowest allowed price is used. If that is above last week's
price, last week's price stays. The rule that set the price is recorded as SCHEDULE, FLOOR, MAX_MARKDOWN, LADDER or LAST_WEEK.

## When no valid price exists
If the lowest allowed price is above the current price, there is no valid new price and the item is skipped, not priced too
low. That shows as FLOOR_ABOVE_PRICE (floor or maximum markdown) or LADDER_ABOVE_PRICE.

## Two independent checks
The engine applies the rules. A separate gate re-checks every rule before anything is sent, so a defect in the engine can
never reach SAP. A violation there blocks the whole send.

## Related
POL-MARKDOWN-SCHEDULE, PB-RULE-VIOLATION-PRICE, PB-FLOOR-ABOVE-PRICE, PB-LADDER
