# Lumora Public API Documentation (v3)

Base URL: `https://api.lumora.example/v3`

## Authentication
Use an API key in the `Authorization` header: `Authorization: Bearer <API_KEY>`. Keys are created in LumoraOS → Settings → Developers. Keys can be scoped to `read:readings`, `write:assets`, `read:alerts`, `admin`. Rotate keys at least every 90 days (recommended). Enterprise customers can also use OAuth 2.0 client credentials.

## Rate limits
| Plan | Limit |
|---|---|
| Starter | 60 requests/min |
| Growth | 300 requests/min |
| Enterprise | 1,200 requests/min |

Exceeding returns **HTTP 429** with a `Retry-After` header. Use exponential backoff with jitter.

## Endpoints

### GET /sensors
List sensors. Query params: `status` (online|offline), `asset_id`, `limit` (max 200), `cursor`.

### GET /sensors/{sensor_id}/readings
Returns temperature/humidity readings.
Params: `from`, `to` (ISO 8601), `interval` (1m, 5m, 1h), `limit` (max 5000).
Example response:
```json
{
  "sensor_id": "CT3-00A91F",
  "readings": [
    {"ts": "2026-10-01T10:00:00Z", "temp_c": 4.6, "humidity": 61.2, "battery_pct": 88}
  ],
  "next_cursor": null
}
```

### POST /assets
Create an asset (pallet, truck, room). Body: `name`, `type`, `sensor_ids[]`, `product_profile_id`.

### GET /alerts
List alerts. Filters: `severity` (warning|critical), `state` (open|acknowledged|resolved).

### POST /alerts/{alert_id}/acknowledge
Acknowledge an alert with optional `note`.

### POST /webhooks
Register a webhook. Events: `alert.opened`, `alert.resolved`, `sensor.offline`, `shipment.delivered`. Payloads are signed with HMAC-SHA256 in the `X-Lumora-Signature` header. Retries: 5 attempts over 30 minutes.

## Error codes
| Code | Meaning |
|---|---|
| 400 | Invalid parameters |
| 401 | Missing/invalid API key |
| 403 | Key lacks required scope |
| 404 | Resource not found |
| 409 | Conflict (e.g., sensor already assigned) |
| 429 | Rate limit exceeded |
| 500 | Server error — retry with backoff |

## SDKs
Official SDKs: Python (`pip install lumora`), Node.js (`npm i @lumora/sdk`). Community SDK for Go.

## Versioning and deprecation
v2 of the API was shut down on **2026-06-30**. Breaking changes are announced 6 months in advance. v3 is the current stable version.

## Data model glossary
- **Sensor:** physical CT device identified by an ID like `CT3-XXXXXX` (CT-300) or `CT2-XXXXXX` (CT-200).
- **Asset:** a thing being monitored (pallet, truck, room).
- **Shipment:** an asset moving between locations with start/end events.
- **Excursion:** out-of-range event as defined by a product profile.
- **Product profile:** allowed temperature range and tolerance (e.g., "Vaccine 2–8°C, 10 min tolerance").
