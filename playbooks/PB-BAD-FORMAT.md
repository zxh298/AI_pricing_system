---
id: PB-BAD-FORMAT
type: playbook
title: SAP rejects a record as malformed
tags: [API_REJECTED:BAD_FORMAT]
owner: Pricing Data Science team
severity: blocking
last_reviewed: 2026-09-21
---

## Symptoms
SAP rejected records with the code BAD_FORMAT. The message says what is wrong: a missing field, an unparseable value, a
price that is not positive or has more than two decimals, or a valid-from date after the valid-to date.

## Why it happens
SAP checks the format of each record. The pre-send validation is meant to catch bad prices first, so a BAD_FORMAT from SAP
usually means a gap between our validation gate and SAP's checks, for example a date or field problem in how the payload is
built.

## How to confirm
- The API log check groups the rejections by message. The same message on many records means the payload builder; a few
  records with different messages point at bad input.
- Compare with the price-rule check: if it shows no violations, the problem is not the price itself.

## What to do
1. The Pricing Data Science team fixes the payload builder or the engine output.
2. Add the missing check to the validation gate so the problem is caught before sending next time.
3. Rerun with the same run id; only unaccepted records are resent.

## What not to do
- Do not retry unchanged: a format error fails the same way every time.
- Do not edit prices by hand to get past the check.

## Related
PB-RULE-VIOLATION-PRICE, REF-SAP-API
