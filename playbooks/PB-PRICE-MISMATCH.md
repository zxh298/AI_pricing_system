---
id: PB-PRICE-MISMATCH
type: playbook
title: SAP holds a different price than the one we sent
tags: [PRICE_MISMATCH]
owner: SAP Pricing team
severity: warning
last_reviewed: 2026-09-21
---

## Symptoms
SAP accepted our record, but the condition it now holds has a different price from the one we sent.

## Why it happens
Someone or something changed the SAP condition after it was accepted, for example a manual edit, or another process writing
the same item and region.

## How to confirm
- Compare the price SAP holds with the recommended price.
- The API log shows the record as accepted at send time, so the change came afterwards.

## What to do
1. The SAP Pricing team explains the change and decides which price stands.
2. If ours must apply, they restore it in SAP. Resending does not work: the same key was already accepted.

## What not to do
- Do not resend the record or start a new run id to overwrite it.

## Related
PB-IDEMPOTENCY-CONFLICT, REF-ACCEPTED-VS-EFFECTIVE
