---
id: PB-NOT-IN-PRODUCTS
type: playbook
title: A listed item is missing from the product master
tags: [MISSING_PRICE:NOT_IN_PRODUCTS]
owner: Product Data team
severity: blocking
last_reviewed: 2026-09-21
---

## Symptoms
An item is on SAP's list but has no price with the reason NOT_IN_PRODUCTS. Product lookups show no name, brand or category
for it.

## Why it happens
The item, identified by SKU and pack size, is on SAP's list but has no record in our product master, which holds the name,
brand, category and cost. The engine will not price something it cannot identify. The commonest cause is a pack size that
does not match between the two systems.

## How to confirm
- The diagnosis shows MISSING_PRICE with NOT_IN_PRODUCTS.
- Product lookup returns nothing for that SKU and pack size, or only for another pack size.

## What to do
1. The Product Data team adds or corrects the product master record, checking the pack size first.
2. Rerun the pipeline so the item is priced.

## What not to do
- Do not create placeholder product rows to get past the check.

## Related
PB-NOT-ON-LIST
