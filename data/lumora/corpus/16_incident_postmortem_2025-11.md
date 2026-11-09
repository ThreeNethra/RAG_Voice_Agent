# Postmortem: Data Ingestion Delay — 7 November 2025 (INC-2025-1107)

**Severity:** SEV-2 | **Duration:** 52 minutes | **Author:** Tomás Herrera | **Status:** Closed (blameless review held 2025-11-14)

## Summary
On 7 November 2025, between 09:31 and 10:23 UTC, sensor readings reached customer dashboards up to 38 minutes late. Alerts evaluated at the edge continued to fire, but dashboards looked frozen. 17 customers contacted support or were proactively notified. No readings were lost, and no SLA credits were issued because monthly uptime stayed above every plan's SLA.

## Root cause
A deploy at 09:12 UTC changed the consumer group name of the ingestion service. The new pods therefore started reading from the end of the Kafka topic while a backlog of about 4.1 million readings was processed with limited throughput.

## Timeline (UTC)
| Time | Event |
|---|---|
| 09:12 | Deploy of ingestion service. |
| 09:31 | Ingestion lag exceeds 5 minutes; monitoring alert fires. |
| 09:37 | On-call engineer acknowledges the page. |
| 09:58 | Consumer group configuration rolled back. |
| 10:23 | Lag back under 1 minute; incident closed. |

## Action items
1. Pin consumer group names in configuration and add a deployment test. (Done)
2. Canary deploys for the ingestion service. (Done)
3. Lower the lag alert threshold from 10 minutes to 5 minutes. (Done)

## Lessons
Monitoring detected the problem within 19 minutes of the deploy, and the on-call engineer responded within 6 minutes of the alert.
