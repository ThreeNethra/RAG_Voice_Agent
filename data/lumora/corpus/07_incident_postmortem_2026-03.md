# Postmortem: Alert Delivery Outage — 12 March 2026 (INC-2026-0312)

**Severity:** SEV-1 | **Duration:** 3 hours 47 minutes | **Author:** Tomás Herrera | **Status:** Closed (blameless review held 2026-03-19)

## Summary
On 12 March 2026, between 02:14 and 06:01 UTC, the Alert Engine failed to deliver SMS and WhatsApp notifications to customers in EMEA and APAC. Sensor data ingestion and dashboards continued to work normally. 41 customers were affected, and 6 customers experienced real temperature excursions during the window that were only noticed on dashboards.

## Impact
- 1,208 alerts delayed; 317 were never delivered via SMS/WhatsApp (email was delivered).
- One pharmaceutical customer (a mid-sized distributor in Poland) lost one pallet of insulin worth approximately **$38,000** because the excursion went unnoticed for 2 hours.
- SLA credits issued: **$212,400** total across 41 customers.

## Timeline (UTC)
| Time | Event |
|---|---|
| 02:14 | Messaging provider "Pigeon API" begins returning 503 errors. |
| 02:20 | Alert Engine retry queue starts growing; no paging alert configured on queue depth. |
| 03:05 | First customer support ticket received (Support, Rotterdam). |
| 03:40 | Support escalates; on-call engineer paged. |
| 04:10 | Root cause identified: provider outage plus our retry logic using exponential backoff without a cap. |
| 04:55 | Failover to secondary provider "Zephyr SMS" started manually. |
| 06:01 | Alert delivery restored; backlog drained by 06:40. |

## Root cause
1. The single messaging provider was a single point of failure for SMS/WhatsApp.
2. Retry backoff had no ceiling, so retries grew to hours.
3. There was no alert on Alert Engine queue depth — we were blind to our own alerting failing.
4. Failover to the secondary provider was a manual runbook step that the on-call engineer had never practiced.

## What went well
Sensor ingestion was isolated; no data was lost. Support escalated within 35 minutes of the first ticket.

## Action items
| # | Action | Owner | Due | Status |
|---|---|---|---|---|
| 1 | Automatic multi-provider failover for SMS/WhatsApp | Platform team | 2026-05-15 | Done |
| 2 | Cap retry backoff at 60 seconds | Platform team | 2026-03-20 | Done |
| 3 | Add paging alert on queue depth > 500 for 5 min | SRE | 2026-03-22 | Done |
| 4 | Quarterly failover game-day drills | SRE | ongoing | Active |
| 5 | Proactive status-page notices for provider outages | Support | 2026-04-30 | Done |

## Lessons
We monitor other people's cold chains; our own alerting must be the most monitored system we run (LUMEN value: "Move carefully with safety-critical systems").

---
# Incident Severity Definitions (from Incident Management Policy)
- **SEV-1:** Customer-facing outage of a core function, data loss, or security breach. Response: 15 minutes. Postmortem mandatory within 7 days.
- **SEV-2:** Major degradation, workaround exists. Response: 30 minutes.
- **SEV-3:** Minor issue. Response: next business day.
