# Customer Support Knowledge Base — Frequently Asked Questions

## Sensors

**Q: How long does the CT-300 battery last?**
Up to 24 months at the default 5-minute reporting interval. If you set 1-minute reporting, expect about 11 months. Cold environments below −20°C reduce battery life by roughly 15–25%. Replace with BAT-24.

**Q: My sensor shows "OFFLINE". What should I do?**
1. Check the last-seen timestamp in Live Monitor. 2. Confirm cellular coverage at the location (NB-IoT/LTE-M). 3. Press the button on the sensor for 3 seconds — the LED blinks green when connected. 4. If still offline after 30 minutes, open a ticket. Note: offline sensors keep recording locally for up to 90 days and will backfill data automatically.

**Q: How do I recalibrate a sensor?**
Calibration is valid for 12 months. Order the recalibration service ($18/sensor) from your account manager or the portal; we send a replacement unit with a fresh certificate and you return the old one within 30 days.

**Q: Can I use a CT-300 inside a −80°C freezer?**
No. The CT-300 operates down to −40°C. For ultra-low freezers use the CT-Probe P10 (−86°C) connected to a CT-300.

**Q: Is the CT-200 still supported?**
Yes for security patches until 2028-03-31, but it is no longer sold since 2026-03-31.

## Platform

**Q: How do I add a new alert rule?**
Go to Alert Engine → Rules → New Rule. Choose a condition (threshold, rate-of-change, or excursion duration), pick assets, and set notification channels. For vaccines we recommend a two-stage rule: warning at 7.0°C and critical at 8.0°C (for 2–8°C products).

**Q: What is an "excursion"?**
A period where measured temperature stays outside the product's allowed range for longer than the configured tolerance. Default tolerance is 10 minutes; it is configurable per product profile.

**Q: How long is my data stored?**
Starter 13 months, Growth 3 years, Enterprise 7 years (extendable to 10).

**Q: Can I export my data?**
Yes — CSV and PDF from any dashboard, and bulk export via the API. Exports of Compliance Vault reports include a digital signature and hash.

**Q: Do you support SSO?**
Growth: SAML/OIDC SSO. Enterprise: SSO plus SCIM user provisioning. Starter: no SSO.

## Billing

**Q: When am I billed?**
Annually in advance (or monthly with a 15% surcharge). Invoices are due in 30 days.

**Q: How do I add more sensors mid-term?**
Extra sensors are prorated; Starter $9, Growth $6 per sensor per month.

**Q: What are the SLA credits?**
If monthly uptime falls below your plan's SLA: 10% of that month's fee for each 0.5% below the SLA, up to 50%. Credits must be claimed within 30 days.

## Support
| Plan | Channels | First response (critical) |
|---|---|---|
| Starter | Email | 2 business days |
| Growth | Email + chat | 8 business hours (critical: 4 hours) |
| Enterprise | 24x7 phone + chat + email | 1 hour |

Status page: status.lumora.example
