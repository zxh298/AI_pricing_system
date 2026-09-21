---
id: PB-NOT-ON-LIST
type: playbook
title: An item is not on this week's clearance list
tags: [NOT_IN_LIST:NOT_ON_LIST, API_REJECTED:NOT_ON_LIST]
owner: Merchandising team
severity: blocking
last_reviewed: 2026-09-21
---

## Symptoms
Either the diagnosis reports NOT_IN_LIST for a SKU and region, or SAP rejected a record with NOT_ON_LIST ("not on the
week's clearance list"). No price exists for the item.

## Why it happens
SAP decides which items are on clearance each week and in which regions. The engine only prices items on that list. The
usual causes: the item was never listed for that region, it dropped off the list (for example after its maximum
clearance weeks), the list we hold is older than SAP's, or the question named the wrong week, region or SKU.

## How to confirm
- Check the week, region and SKU in the question against the product lookup for that week.
- A SKU that appears for some regions but not others was listed only for those regions.
- If the item should be listed, compare when the list was last fetched with when SAP changed it.

## What to do
1. If the item should be on clearance, the Merchandising team adds it to SAP's list.
2. Rerun the pipeline so the list is fetched again and the item is priced.
3. If the list was simply stale, the rerun is enough.

## What not to do
- Do not force a price for an unlisted item.
- Do not treat it as an engine bug: the model and the engine never choose what is on clearance.

## Related
POL-CLEARANCE-LIFECYCLE
