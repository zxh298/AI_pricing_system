---
id: REF-ACCEPTED-VS-EFFECTIVE
type: reference
title: Accepted is not the same as effective
tags: [NOT_EFFECTIVE:PROMOTION, PRICE_MISMATCH, MISSING_IN_SAP]
last_reviewed: 2026-09-21
---

## Two different facts
- **Accepted:** SAP received our record, passed its checks (format, on the list, no overlapping record) and stored the price.
- **Effective:** the price shoppers actually pay in SAP right now.

They can differ. A promotion for the same item and region takes priority over a markdown, so our price is accepted but not
effective. A manual edit can change what is held. A later run can replace a record. So the API answering "accepted" is not the
end: the pipeline checks SAP again afterwards.

## The four outcomes of that check
| Outcome | Meaning |
|---|---|
| CONFIRMED | SAP holds our price and it is the effective price |
| NOT_EFFECTIVE | SAP holds our price but something overrides it, for example a promotion |
| PRICE_MISMATCH | SAP holds a different price than the one we sent |
| MISSING_IN_SAP | SAP holds no condition for an item it accepted |

## What the tools show
The SAP conditions check reports the clearance price we sent, the effective price, and the effective source (MARKDOWN when our
price applies, PROMOTION when a promotion overrides it). It has no promotion dates, priorities or names.

## Related
PB-PROMO-OVERRIDE, PB-PRICE-MISMATCH, PB-MISSING-IN-SAP
