# Sensor Deployment (Commissioning) Runbook v3.0
**Audience:** Customer Success engineers and customer installers | **Owner:** Customer Success (Vikram Sethi)

## Overview
Commissioning means taking a CT-300 from the box to trustworthy data: switching it on, pairing it, linking it to an asset, placing it correctly and verifying the readings. A team of two can commission about 100 sensors in 2 hours.

## Steps
1. **Self-test.** Press and hold the button for 3 seconds. Three green blinks mean the self-test passed. A red blink means the battery is below 10%; replace it with a BAT-24.
2. **Pair.** Open the Lumora mobile app, tap Add Sensor and scan the QR code on the sensor. Pairing uses Bluetooth Low Energy (BLE 5.2), so keep the phone within 5 metres.
3. **Link to an asset.** Choose the pallet, truck or room in the Asset Registry and attach the sensor. Select the product profile (for example "Vaccine 2–8°C").
4. **Set the reporting interval.** The default is 5 minutes. Shorter intervals shorten battery life (1-minute reporting gives about 11 months).
5. **Place the sensor.** In a room, mount it in the warmest expected spot. On a pallet, put it in the middle of the load. Never mount a sensor against an evaporator coil or within 30 cm of an air vent, because airflow makes readings jump.
6. **Verify.** Wait 15 minutes. The sensor should have reported at least 3 readings and show "online" in Live Monitor. An amber LED means there is no network coverage.
7. **Sign off.** Record the installation in Compliance Vault. This creates the signed installation record that auditors ask for.

## Temperature mapping
A formal temperature mapping study (offered by Professional Services) requires a minimum of 14 days of continuous data.

## Troubleshooting
| Symptom | Likely cause | Fix |
|---|---|---|
| Amber LED | No cellular coverage | Move the sensor or add an H5 gateway |
| Red LED | Battery below 10% | Replace with BAT-24 |
| Readings jump 3–4°C | Sensor too close to a cooling vent | Relocate the sensor |
| Cannot pair | Phone Bluetooth off or sensor asleep | Press the button for 3 seconds, retry |
