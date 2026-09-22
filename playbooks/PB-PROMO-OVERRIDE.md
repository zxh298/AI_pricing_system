---
id: PB-PROMO-OVERRIDE
type: playbook
title: A promotion overrides the clearance markdown
tags: [NOT_EFFECTIVE:PROMOTION]
owner: Promotions team
severity: warning
last_reviewed: 2026-09-21
---

## Symptoms
SAP accepted our clearance price, but shoppers in some regions still pay a different, usually lower, price. Reconciliation
shows the record as NOT_EFFECTIVE, and SAP reports the effective source as PROMOTION. It is often limited to one region,
because catalogue promotions are regional, while the other regions are fine.

## Why it happens
When a promotion exists for the same item and region, SAP applies the promotion price in preference to a markdown. Our
price is stored, so the send counts as accepted, but it is not the price in effect. The pipeline cannot see promotion
dates, priorities or names.

## How to confirm
- The diagnosis shows the items as NOT_EFFECTIVE with reason PROMOTION, in the affected region only.
- The SAP conditions check lists each item with the clearance price we sent, the effective price and the source PROMOTION.
- Run the price-rule check on the same items: it shows whether our markdown itself is valid, so the only problem is the override.

## What to do
1. Give the Promotions team the SKUs, the region and both prices (sent and effective).
2. The Promotions team decides: end or shorten the promotion, or let it run. While it runs, the promotion price stands
   and the clearance price has no effect.
3. After any change in SAP, run the diagnosis again; the items should show OK.
4. If it is a known, confirmed cause, an analyst records it as a finding so others see it.

## What not to do
- Do not resend or re-price the items. SAP already holds our price; a resend under another run id is rejected with a
  validity overlap.
- Do not change our markdown to match the promotion.

## Related
REF-ACCEPTED-VS-EFFECTIVE, INC-2026-W36-VIC-PROMO
