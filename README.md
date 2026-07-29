# Well Monitor

A Home Assistant custom integration for monitoring a borehole / well using a submersible voltage-output depth sensor. Converts raw sensor voltage to water depth, volume, and fill/drain rate — all as a single device in Home Assistant.

## Features

- **Two-point linear calibration** — configure two voltage/depth reference pairs; the integration derives the depth scale automatically
- **Cylindrical volume estimation** — volume in litres from water depth and borehole diameter
- **Rolling fill/drain rate** — rate of change over a 10-minute window (L/h), positive = filling, negative = draining
- **Reactive updates** — no separate poll interval; the coordinator updates whenever the source voltage entity changes state (e.g. driven by Z-Wave or SmartThings)
- **Single device** — all eight sensors appear under one device in the HA device registry
- **Reconfigurable** — calibration and geometry can be updated via the options flow without re-adding the integration

## Sensors

| Sensor | Unit | Notes |
|---|---|---|
| Voltage | V | EMA-filtered sensor voltage; hidden by default |
| Water Depth | m | Calibrated height of water column |
| Water Volume | L | Depth × cylindrical cross-section |
| Water Level | % | Depth as fraction of calibrated maximum |
| Change Rate | L/h | Short-term rolling window (30 min); decays to 0 when idle |
| Recharge Rate | L/h | Max positive rate in a 24h window |
| Long Term Rate | L/h | Full-window rate; preserves last value when idle (no decay) |
| Water Table | L | Rolling max volume over 7 days (groundwater level) |

The Change Rate sensor also exposes a `direction` attribute: `filling`, `draining`, or `stable` (< 0.5 L/h deadband).

## Installation

1. Copy the `custom_components/well_monitor/` directory into your HA `config/custom_components/` directory.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and search for **Well Monitor**.

## Configuration

Setup is a three-step flow:

### Step 1 — Voltage source
Select the HA sensor entity that provides the raw voltage from your depth sensor.

### Step 2 — Calibration
Enter two known voltage/depth pairs. The sensor voltage and actual water column depth must both be measured at the same time at two different well levels.

| Field | Description |
|---|---|
| Low reference voltage (V) | Sensor voltage at the low reference level |
| Water depth at low reference (m) | Actual water column depth at that voltage |
| High reference voltage (V) | Sensor voltage at a higher known level |
| Water depth at high reference (m) | Actual water column depth at that voltage |

The relationship is linear: `depth = (voltage − V_zero) × depth_scale`, where the two calibration points determine both constants.

**If you have an existing multiplier** (e.g. from a template sensor), set low = `0.0 V → 0.0 m` and high = `1.0 V → <your multiplier> m`.

### Step 3 — Geometry
Enter the internal diameter of the borehole casing in millimetres. Common sizes:

| Size | mm |
|---|---|
| 3 inch | 76 |
| 4 inch | 102 |
| 5 inch | 127 |
| 5.5 inch | 140 |
| 6 inch | 152 |

Volume is calculated as `V = π × (d/2)² × depth × 1000` (litres).

## Reconfiguration

To update calibration or geometry after initial setup: go to **Settings → Devices & Services**, find the Well Monitor entry, and select **Configure**. The integration reloads immediately after saving.

## Automations

The Change Rate and Water Volume sensors are well suited for pump/fill automations. Example conditions:

```yaml
# Only run if well is recovering (filling) faster than you are drawing
condition:
  - condition: numeric_state
    entity_id: sensor.well_change_rate
    above: 50   # L/h net inflow

# Stop if well volume drops below a safety threshold
condition:
  - condition: numeric_state
    entity_id: sensor.well_water_volume
    above: 200  # litres minimum reserve
```

## Requirements

- Home Assistant 2024.1 or later
- A voltage-output submersible depth or pressure sensor wired to a HA-connected analogue input (Z-Wave, SmartThings, ESPHome, etc.)
