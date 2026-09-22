# Retrieval evaluation

Embedding model: `fastembed:BAAI/bge-small-en-v1.5` · documents in the knowledge base: 27 · questions: 18 named, 20 plain, 3 vague, 10 offtopic

## Similarity alone (no tags, no threshold)

| questions | hit@1 | hit@3 |
|---|---|---|
| named (mention a code or reason) (18) | 83% | 94% |
| plain words (20) | 80% | 85% |
| all on-topic (38) | 82% | 89% |

## The search as built (tags first, similarity above 0.67)

* on-topic questions (38): right document first 89%, in the first three 92%, something found 97%
* off-topic questions (10): wrongly found something 0%

### Questions where the right document was not first

* `p01` 'why are prices in one region not dropping when the others are fine': got ['PB-PRICE-MISMATCH', 'PB-VALIDITY-OVERLAP'], wanted one of ['PB-PROMO-OVERRIDE', 'INC-2026-W36-VIC-PROMO']; similarity ranked ['PB-PRICE-MISMATCH', 'PB-VALIDITY-OVERLAP']
* `p14` 'the key we use is not allowed to write prices': got ['PB-RULE-VIOLATION-PRICE', 'PB-PRICE-MISMATCH'], wanted one of ['PB-API-REJECTED-REQUEST']; similarity ranked ['PB-RULE-VIOLATION-PRICE', 'PB-PRICE-MISMATCH']
* `p16` 'the same record was accepted earlier with a different price': got ['PB-PRICE-MISMATCH', 'PB-IDEMPOTENCY-CONFLICT'], wanted one of ['PB-IDEMPOTENCY-CONFLICT']; similarity ranked ['PB-PRICE-MISMATCH', 'PB-IDEMPOTENCY-CONFLICT']
* `p19` 'the payload has a start date that is after the end date': got nothing, wanted one of ['PB-BAD-FORMAT']; similarity ranked ['PB-NO-RECOMMENDATION', 'INC-2026-W35-OPEN-END-OVERLAP']

### Off-topic questions that found something

* none

## Threshold sweep

| threshold | on-topic hit@1 | on-topic found | off-topic false positives |
|---|---|---|---|
| 0.50 | 89% | 100% | 70% |
| 0.55 | 89% | 100% | 40% |
| 0.60 | 89% | 100% | 20% |
| 0.65 | 89% | 100% | 10% |
| 0.70 | 82% | 89% | 0% |
| 0.75 | 82% | 84% | 0% |
| 0.80 | 71% | 74% | 0% |
| 0.85 | 53% | 53% | 0% |

## Which measure separates on-topic from off-topic best

On-topic questions that only similarity can answer against off-topic ones. AUC 1.00 is perfect separation; `kept` is the share of those on-topic questions still answered with a cut-off that silences every off-topic one.

| measure | AUC | highest off-topic | lowest on-topic | cut-off | on-topic kept |
|---|---|---|---|---|---|
| top1 | 0.99 | 0.66 | 0.66 | 0.66 | 95% |
| margin | 0.67 | 0.05 | 0.00 | 0.05 | 40% |
| zdoc | 0.66 | 2.94 | 1.30 | 2.94 | 25% |
| z | 0.59 | 3.40 | 1.70 | 3.40 | 30% |

## Best-score distributions

* on-topic, answered by similarity: min 0.66, median 0.80, max 0.89
* off-topic: min 0.46, median 0.54, max 0.66
