# Release Notes — LumoraOS, Firmware and Mobile App
**Owner:** Product & Engineering | **Last updated:** 2026-10-10 | **Status:** current

## LumoraOS 6.4 (released 2026-09-22)
- Alert Engine: added "quiet hours" for non-critical alerts. Critical alerts always bypass quiet hours.
- Compliance Vault: reports can now be exported as PDF/A-3 for long-term archiving.
- Public API: new endpoint `GET /shipments/{id}/excursions` returns all excursions for a shipment.
- Fixed: dashboards showed a stale battery percentage for sensors that had just back-filled offline data.

## LumoraOS 6.3 (released 2026-07-14)
- Enterprise: SCIM group-to-role mapping.
- Alert Engine: WhatsApp message templates can be edited per customer.
- Infrastructure: the legacy API v2 gateway servers were decommissioned. (API v2 itself stopped serving traffic on 2026-06-30.)
- Fixed: duplicate webhook deliveries when a customer endpoint timed out.

## LumoraOS 6.2 (released 2026-04-28)
- Lumora Insights became generally available inside the platform (advisory risk scores per in-flight shipment).
- Alert Engine: automatic multi-provider failover for SMS and WhatsApp messages. This was the follow-up to INC-2026-0312.
- Fixed: CSV export was truncated at 100,000 rows.

## CT-300 firmware v4.2.1 (released 2026-08-05)
- Fixed a bug that caused about 3% faster battery drain in sensors operating below −25°C. Published battery specifications are unchanged.
- Improved reconnect time after a network outage from about 90 seconds to about 40 seconds.

## CT-300 firmware v4.1 (released 2026-02-11)
- Offline buffer extended from 60 days to 90 days of readings.
- Added support for CT-Probe P10 calibration offsets.

## CT-200 firmware v3.9.4 (released 2026-06-02) — security patch only
CT-200 is end-of-sale. This release contains security fixes only and no new features.

## Mobile app 2.9 (released 2026-05-30)
- Faster Bluetooth (BLE) sync for sensors with a large offline backlog.
- Background sync on iOS.
- Mobile app 3.0 is planned for Q4 FY2027.
