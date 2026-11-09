# Information Security Policy (v5.0, owner: CISO Grace Mbeki)

## 1. Scope
Applies to all employees, contractors, and systems that process Lumora or customer data.

## 2. Data classification
| Class | Examples | Handling |
|---|---|---|
| Public | Marketing pages, public docs | No restrictions |
| Internal | Org charts, internal wikis | Employees and contractors only |
| Confidential | Customer lists, pricing contracts, source code | Need-to-know, encrypted at rest |
| Restricted | Customer sensor data tied to regulated shipments, credentials, signing keys | MFA + audit logging + CISO approval for access |

## 3. Access control
- MFA is mandatory for all accounts. Hardware security keys (FIDO2) required for production access.
- Production access is granted just-in-time for a **maximum of 4 hours**, approved by a team lead.
- Access reviews run quarterly; unused accounts are disabled after **45 days** of inactivity.
- Shared accounts are prohibited.

## 4. Encryption
- Data in transit: TLS 1.2 minimum, TLS 1.3 preferred.
- Data at rest: AES-256 via cloud KMS. Customer data is encrypted with per-tenant keys on the Enterprise plan.
- Sensor-to-cloud traffic uses mutual TLS with device certificates rotated every 12 months.

## 5. Passwords and devices
Minimum 14 characters, managed through the company password manager. Company laptops use full-disk encryption and are managed through MDM. Personal devices may not store Confidential or Restricted data.

## 6. Incident reporting
Suspected security incidents must be reported to security@lumora.example or via the #sec-incident Slack channel **within 1 hour of discovery**. The CISO decides on customer notification. Where personal data is involved, regulators are notified within **72 hours** (GDPR).

## 7. Vulnerability management
| Severity | Patch deadline |
|---|---|
| Critical (CVSS 9.0+) | 7 days |
| High (7.0–8.9) | 30 days |
| Medium (4.0–6.9) | 90 days |
| Low | Next maintenance window |

A bug-bounty program runs through a third-party platform; rewards from $100 to $10,000.

## 8. Vendors
All vendors handling Confidential or Restricted data must complete a security questionnaire and have SOC 2 or ISO 27001. Contracts include breach notification within 48 hours.

## 9. Certifications
SOC 2 Type II (since 2023, audited annually), ISO 27001 (since 2025), ISO 9001 (since 2019), HIPAA-aligned controls for US pharma customers. Penetration tests are conducted twice a year by an external firm.

## 10. Exceptions
Policy exceptions can only be granted by the CISO, documented in the risk register, and expire after at most 6 months.
