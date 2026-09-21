---
id: REF-VERDICT-CODES
type: reference
title: Glossary of the codes the diagnostic tools report
tags: [NOT_IN_LIST, MISSING_PRICE, RULE_VIOLATION, API_REJECTED, NOT_EFFECTIVE, PRICE_MISMATCH, MISSING_IN_SAP]
last_reviewed: 2026-09-21
---

## How a verdict is written
Each record gets one verdict code, plus a reason that says which case it is. A pattern is written `CODE:reason`, for example
`NOT_EFFECTIVE:PROMOTION`. A code without a reason (PRICE_MISMATCH, MISSING_IN_SAP) covers every case of that code.

## The codes, in the order they are decided
| Code | Meaning | Example reasons |
|---|---|---|
| NOT_IN_LIST | not on this week's SAP list for that region | NOT_ON_LIST |
| MISSING_PRICE | no price was produced or sent | MISSING_RULE, FLOOR_ABOVE_PRICE, LADDER_ABOVE_PRICE, NOT_IN_PRODUCTS, EXCEEDED_MAX_WEEKS, NOT_SENT, NO_RECOMMENDATION |
| RULE_VIOLATION | the price breaks a hard rule; the run is blocked | BELOW_FLOOR, BELOW_MAX_MARKDOWN, PRICE_ABOVE_LAST_WEEK, LADDER_VIOLATION, MISSING_RULE, EXCEEDS_MAX_WEEKS, DUPLICATE_KEY, BAD_PRICE_FORMAT |
| API_REJECTED | SAP rejected the record or the request failed | VALIDITY_OVERLAP, NOT_ON_LIST, BAD_FORMAT, IDEMPOTENCY_CONFLICT, HTTP_429, HTTP_503, TRANSPORT and other HTTP codes |
| NOT_EFFECTIVE | SAP accepted our price but something overrides it | PROMOTION |
| PRICE_MISMATCH | SAP holds a different price than we sent | |
| MISSING_IN_SAP | SAP accepted the record but holds no condition | |
| OK | SAP holds our price and it is the effective price | |

Only the first matching code is reported for a record.

## Words used in the tool results
- **clearance price sent:** the markdown price we sent to SAP. It is not the shelf price.
- **effective price:** what shoppers pay in SAP now.
- **effective source:** MARKDOWN or PROMOTION.
- **group:** a list of SKUs the session remembers under a handle such as G1. It says which products, never their status.
- **data version:** which pipeline run, and when it finished, a result was based on.

## Related
Each code has a playbook; search by the code and reason.
