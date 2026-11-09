# Lumora Product Catalog (v4.2, effective 2026-07-01)

## 1. ColdTrack Hardware

### CT-300 Smart Sensor (Gen 3) — flagship
- Temperature range: -40°C to +85°C, accuracy ±0.2°C (−20°C to +40°C), ±0.5°C outside that range
- Humidity: 0–100% RH, ±2% accuracy
- Connectivity: LTE-M / NB-IoT with 2G fallback; BLE 5.2 for local config
- Battery: replaceable lithium, **up to 24 months** at 5-minute reporting; 11 months at 1-minute reporting
- Memory: stores up to 90 days of readings offline and syncs when connection returns
- Ingress rating: IP67
- Certifications: CE, FCC, WHO PQS E006 (vaccines), NIST-traceable calibration certificate included
- Calibration validity: 12 months; recalibration service available
- List price: see pricing document

### CT-200 Smart Sensor (Gen 2) — **end of sale 2026-03-31, end of support 2028-03-31**
Older sensor with ±0.5°C accuracy, 12-month battery. Existing customers continue to receive firmware security patches until end of support. Migration discount of 25% on CT-300 is available through 2027-03-31.

### CT-Probe P10
Wired stainless-steel probe for ultra-low freezers (−86°C to +20°C), pairs with CT-300 via a probe port. Used for vaccines and biologics.

### Hub H5 Gateway
Wi-Fi/Ethernet/LTE gateway for warehouses. Supports up to 250 sensors per gateway, with local buffering and PoE. Runs LumoraEdge firmware.

### Accessories
Magnetic mounts, pallet-wrap pouches, shock-absorbing cases, replacement batteries (BAT-24), calibration kits.

## 2. LumoraOS (Cloud Platform)
Core modules:
- **Live Monitor** — dashboards, maps, live readings per shipment/asset
- **Alert Engine** — threshold, rate-of-change, and excursion-duration rules; notifications by SMS, email, WhatsApp, Slack, and webhooks. Escalation chains supported.
- **Compliance Vault** — auto-generated audit reports for FDA 21 CFR Part 11, EU GDP, WHO, HACCP. Reports are immutable and signed.
- **Asset Registry** — links sensors to pallets, trucks, rooms, and shipments
- **Admin & SSO** — SAML 2.0, OIDC, SCIM provisioning (Enterprise plan only)
- **Public API** — REST + webhooks (see API documentation)

**Data retention:** 13 months on Starter, 3 years on Growth, 7 years on Enterprise (configurable up to 10 years).

## 3. AI Add-ons

### RouteIQ
Predictive routing that recommends carriers and routes based on historical temperature excursions, weather, and border dwell times. In customer pilots, RouteIQ reduced excursion incidents by a median of **22%**. Available as an add-on to Growth and Enterprise plans. Internal codename: "Compass".

### Lumora Insights (GA April 2026)
Risk-scoring engine that predicts the probability of an excursion for each in-flight shipment, 6–48 hours ahead. Uses gradient-boosted models on sensor history plus external weather feeds. Model explanation (top 5 contributing factors) is shown for every score. Insights is **not** a regulatory substitute for monitoring; it is advisory only.

## 4. Professional services
- Onboarding & rollout (fixed-fee packages)
- Temperature mapping and validation studies
- Compliance audit preparation
- Custom integration (SAP, Oracle WMS, Manhattan, Blue Yonder)

## Roadmap (public)
- Q1 FY2028: CT-400 with solar-assist battery (target)
- Q4 FY2027: LumoraOS mobile app 3.0
- Insights for ocean freight (beta, Q3 FY2027)
