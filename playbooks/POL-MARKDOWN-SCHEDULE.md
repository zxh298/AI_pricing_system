---
id: POL-MARKDOWN-SCHEDULE
type: policy
title: The weekly markdown schedule
tags: []
last_reviewed: 2026-09-21
---

## The schedule
An item's first week on clearance has no discount. Each later week takes another 5 percentage points off the shelf (full)
price: week 2 is 5% off, week 3 is 10% off, and so on. The scheduled price is the shelf price times one minus the discount,
rounded to the nearest cent, halves rounding up.

For example, an item with a shelf price of 20.00 in its fourth week is 15% off and is scheduled at 17.00.

## The schedule is a starting point
The scheduled price is used only if the hard rules allow it. A price never goes below the SAP floor, never more than the
maximum markdown below the shelf price, never below the ladder for a multipack, and never above last week's price. Where a
rule is higher than the schedule, the rule sets the price. See POL-PRICE-RULES.

## Why the schedule is fixed
Shoppers see a predictable, gradual reduction, and every price can be reproduced from the shelf price and the week number.
The engine adds no judgement of its own to the schedule: the model and the diagnostic tools never set prices.

## Related
POL-PRICE-RULES, POL-CLEARANCE-LIFECYCLE
