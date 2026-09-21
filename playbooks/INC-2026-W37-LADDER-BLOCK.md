---
id: INC-2026-W37-LADDER-BLOCK
type: incident
title: W37, the validation gate blocked the whole weekly send over a pack price
tags: [RULE_VIOLATION:LADDER_VIOLATION, MISSING_PRICE:NOT_SENT]
owner: Pricing Data Science team
last_reviewed: 2026-09-21
---

## Summary
In week 2026-W37 the weekly run finished with the status BLOCKED. Nothing was sent to SAP for any item. The pre-send
validation had found three multipack rows whose per-item price was below their Single's price.

## Timeline
- Monday morning: the run stopped at the validation step. Every priced item showed as MISSING_PRICE with NOT_SENT.
- The price-rule check listed three LADDER_VIOLATION rows, all packs of the same three SKUs.
- Monday midday: the Pricing Data Science team found that the engine priced the packs against the Single's previous price,
  because the Singles of those SKUs were also on the list that week and were priced after the packs.
- Monday afternoon: the fix priced the Singles first and used their new price for the ladder. The run was repeated and
  succeeded before the weekly cutoff.

## Root cause
An ordering defect in the engine: a pack could be priced before its own Single in the same run.

## Resolution
The engine now prices Singles first. A regression test covers a Single and its pack both on the list.

## Lessons
- The gate did its job: a defect stopped in the run rather than reaching SAP.
- A blocked run makes every item look unsent, so the first question is the run status, then the rule check.
- One rule failing across a few related items points at the engine, not at the input.
