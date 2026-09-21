# Knowledge corpus (RAG)

Synthetic playbooks, incident notes, policies and reference pages for the clearance pricing system. They explain
**why** a problem happens, **who owns it** and **what to do**. They never state live facts (prices, statuses): the
diagnostic tools do that. Everything here is written from scratch; the teams below are fictional.

## Files

One Markdown file per document, named `<id>.md`, in this folder:

| Prefix | Type | Purpose |
|---|---|---|
| `PB-` | playbook | one problem the checks can find: symptoms, cause, how to confirm, what to do |
| `INC-` | incident | a synthetic past incident and its resolution |
| `POL-` | policy | how pricing rules work, in words |
| `REF-` | reference | glossary and how the systems fit together |

## Front matter

```
---
id: PB-PROMO-OVERRIDE          # same as the file name
type: playbook                 # playbook | incident | policy | reference
title: A promotion overrides the clearance markdown
tags: [NOT_EFFECTIVE:PROMOTION]
owner: Promotions team         # playbooks and incidents
severity: warning              # playbooks: blocking | warning
last_reviewed: 2026-09-21
---
```

Playbooks have these sections, in this order: `## Symptoms`, `## Why it happens`, `## How to confirm`,
`## What to do`, `## What not to do`, and optionally `## Related`.

## Tags

A tag is what the diagnostic tools report, written `CODE` or `CODE:reason`: for example `NOT_EFFECTIVE:PROMOTION`,
`API_REJECTED:VALIDITY_OVERLAP`, `MISSING_PRICE:MISSING_RULE`, `MISSING_IN_SAP`. A tag without a reason (`PRICE_MISMATCH`)
covers every reason of that code. `RULE_VIOLATION` reasons can list several rules joined by commas
(`BELOW_FLOOR,PRICE_ABOVE_LAST_WEEK`); each rule has its own tag. Lookup by tag comes first and needs no embeddings;
similarity search is the fallback for free-text questions.

A test checks that every code and reason the system can produce has a playbook tag, and that no playbook uses a tag
the system cannot produce. Adding a new verdict therefore fails the tests until it has a playbook.

## Teams (fictional)

Promotions team, SAP Pricing team, Merchandising team, Data Platform team, Pricing Data Science team, Product Data team.

## Adding or changing a document

Copy a similar file, keep the front matter and section names, set `last_reviewed`, and run
`python -m pytest tests/test_step6_corpus.py`. Do not paste real names, data or internal documents.
