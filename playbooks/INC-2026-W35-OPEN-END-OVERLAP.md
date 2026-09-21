---
id: INC-2026-W35-OPEN-END-OVERLAP
type: incident
title: W35, old open-ended price records blocked nine items in every region
tags: [API_REJECTED:VALIDITY_OVERLAP]
owner: SAP Pricing team
last_reviewed: 2026-09-21
---

## Summary
In week 2026-W35, SAP rejected the prices for nine items in all three regions with VALIDITY_OVERLAP. The items kept the
previous week's price. The other records in the run were accepted.

## Timeline
- Monday: the send step reported nine rejected records in the API log, all with the same message naming an earlier condition
  record with the end date 2099-12-31.
- Monday afternoon: the SAP Pricing team traced the earlier records to an import done during migration, which had created
  price conditions with no real end date.
- Tuesday morning: the records were ended, and the pipeline was rerun with the same run id.
- Tuesday: the nine items were accepted and confirmed effective. Nothing was duplicated, because the rerun only resent the
  records that had not been accepted.

## Root cause
A one-off legacy import left open-ended condition records. SAP allows one record at a time per item and region, so each
new weekly record overlapped them.

## Resolution
The SAP Pricing team ended the legacy records. The rerun under the same run id fixed the nine items.

## Lessons
- Rejections that name an end date in 2099 point at an open-ended record, not at our data.
- Rerun under the same run id. A new id would have overlapped the records that were already accepted.
- Every record our pipeline creates has an end date at the end of the week.
