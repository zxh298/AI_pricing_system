---
id: INC-2026-W36-VIC-PROMO
type: incident
title: W36, a VIC catalogue promotion held back seven Larkspur clearance prices
tags: [NOT_EFFECTIVE:PROMOTION]
owner: Promotions team
last_reviewed: 2026-09-21
---

## Summary
In week 2026-W36, seven Larkspur items in VIC were accepted by SAP at their clearance price but stayed at a different, lower
price in the shops. NSW and QLD were unaffected. Reconciliation flagged the seven records as NOT_EFFECTIVE.

## Timeline
- Monday: the pipeline finished with the status COMPLETED_WITH_ISSUES: all records accepted, seven not effective.
- Monday afternoon: a store manager asked why the VIC shelf prices had not dropped. The diagnostic assistant listed the
  seven items, all in VIC, with the effective source PROMOTION.
- Tuesday: the Promotions team confirmed a VIC-only catalogue promotion covering the same items for the whole week.
- Wednesday: the promotion was ended early for four items. Three kept it until Sunday.

## Root cause
A regional catalogue promotion had been set up for a range that overlapped the clearance list. SAP gives a promotion priority
over a markdown for the same item and region, so our accepted price never applied.

## Resolution
The Promotions team ended the promotion early for four items, and the clearance price took effect on the next
reconciliation. The other three ran their promotion to the end of the week, which was accepted as the outcome.

## Lessons
- The pricing side cannot see promotions, so the check that catches this is reconciliation, not the price rules.
- Ask the Promotions team for the promotion calendar before a clearance week starts in a region.
- A confirmed cause is recorded as a finding so later questions find it immediately.
