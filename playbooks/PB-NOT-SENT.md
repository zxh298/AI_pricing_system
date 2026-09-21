---
id: PB-NOT-SENT
type: playbook
title: A price was calculated but never sent to SAP
tags: [MISSING_PRICE:NOT_SENT]
owner: Data Platform team
severity: blocking
last_reviewed: 2026-09-21
---

## Symptoms
An item has a recommended price, but SAP holds nothing for it and the diagnosis reason is NOT_SENT.

## Why it happens
The recommendation exists but the send step did not deliver it. There are two causes. The pre-send validation found a rule
violation and blocked the whole run, so nothing at all reached SAP (run status BLOCKED). Or the run stopped or failed
before the send step.

## How to confirm
- Check the pipeline run's status and which step stopped it.
- If it is blocked, the price-rule check shows which rules failed; those items appear as RULE_VIOLATION.

## What to do
1. Fix the cause: for a blocked run, follow the rule-violation playbook; for a failed run, fix the step that failed.
2. Rerun with the same run id.

## What not to do
- Do not send prices to SAP by hand.
- Do not bypass the validation gate to get the run through.

## Related
PB-RULE-VIOLATION-PRICE, REF-PIPELINE-STATUSES
