---
id: POL-CLEARANCE-LIFECYCLE
type: policy
title: What is on clearance and for how long
tags: []
last_reviewed: 2026-09-21
---

## SAP decides what; the engine decides how much
Each week SAP provides the clearance list, by item, pack size and region, together with the business rules (floor, maximum
markdown, maximum clearance weeks). The pricing engine never chooses which products enter clearance. It sets a price for
each listed item, and SAP makes the price effective in the shops.

## Weeks on clearance
An item's clearance week number counts its consecutive weeks on the list, starting at 1. An item may stay for at most its
maximum clearance weeks (currently 10). After that SAP stops listing it. It keeps its last price in the history, but gets
no new markdown.

## The weekly cycle
The clearance week runs Monday to Sunday. The pipeline fetches the list and rules, prices the items, validates the prices,
sends them to SAP in batches, checks that SAP holds and applies them, and records the outcome. All of this has to finish
before the weekly cutoff agreed with the SAP Pricing team.

## Who owns what
Merchandising owns the list and the rules. Promotions owns promotions. The SAP Pricing team owns SAP's price conditions. The
Data Platform team owns the pipeline. The Pricing Data Science team owns the engine. The Product Data team owns the product
master.

## Related
POL-MARKDOWN-SCHEDULE, REF-PIPELINE-STATUSES
