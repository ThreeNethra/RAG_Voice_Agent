# Vendors, Manufacturing and Infrastructure Overview

## Contract manufacturing
| Manufacturer | Location | Role | Share of units |
|---|---|---|---|
| Kestrel Electronics | Chennai, India | Primary | about 70% |
| Brightline Manufacturing | Penang, Malaysia | Secondary | about 30% |

- Standard production lead time: 8 weeks from purchase order to shipment at Kestrel and 11 weeks at Brightline.
- Defect target: below 0.5%. Units failing the end-of-line test are never shipped.
- The Operations liaison team (led by Ibrahim Farouk) audits both factories every quarter.
- The LTE-M modem currently has a single supplier. Brightline is qualifying a second source.

## Messaging and communication providers
- SMS and WhatsApp: Pigeon API (primary) and Zephyr SMS (secondary). Automatic failover between them has existed since LumoraOS 6.2.
- Email: Mailrun.
- Voice calls (Enterprise escalation chains): TelaVoice.

## Cloud hosting
- Hosted on AWS in three regions: Mumbai (ap-south-1), Frankfurt (eu-central-1) and N. Virginia (us-east-1).
- Customer data stays in the region chosen at contract signing. EU customers are hosted in Frankfurt by default.
- Disaster recovery targets for the platform: recovery point objective (RPO) of 15 minutes and recovery time objective (RTO) of 4 hours.

## Vendor governance
- Vendor contracts above $250,000 per year need CFO and CEO approval.
- Vendors handling Confidential or Restricted data must hold SOC 2 or ISO 27001 and sign a 48-hour breach-notification clause.
