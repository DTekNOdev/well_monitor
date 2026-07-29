# Well Monitor — Installation Notes

## Calibration constants

Sensor: submersible voltage-output depth transducer, borehole at cottage.
Two calibration readings taken at time of initial setup (requires wellhead removal to re-calibrate).

| Parameter | Value | Notes |
|---|---|---|
| Low reference voltage | 0.0 V | Assumed zero — sensor reads 0 V at zero water depth |
| Low reference depth | 0.0 m | |
| High reference voltage | 7.96 V | Observed reading |
| High reference depth | 52.04 m | Observed HA-calculated depth at that voltage |
| Derived depth scale | 6.538 m/V | = 52.04 / 7.96 |
| Borehole diameter | 140 mm | 5.5 inch casing |
| Litres per metre | ~15.39 L/m | = π × (0.070)² × 1000 |

Original multiplier in configuration.yaml templates was `6.5455` — slight discrepancy is rounding in the reported readings.

## Voltage sensor

Raw entity: `sensor.well_monitor_analog_input_2_voltage_measurement`

The integration now reads the **raw** voltage entity directly and applies an internal
time-weighted EMA filter (`tau = 300s` default). The old lowpass+SMA filter sensor
has been removed from configuration.

### Filter approach

```
ema = alpha_t × raw + (1 − alpha_t) × ema
alpha_t = 1 − exp(−dt / tau)
```

Long gap → alpha_t → 1.0 (trust new reading fully), short gap → alpha_t → 0 (suppress noise).

## Sensors

The integration publishes 8 sensors under a single device:

| Sensor | Unit | Notes |
|---|---|---|
| Voltage | V | EMA-filtered voltage; hidden by default |
| Water Depth | m | Calibrated water column height |
| Water Volume | L | Depth × cylindrical cross-section |
| Water Level | % | Depth as fraction of calibrated max |
| Change Rate | L/h | Short-term rolling window (30 min); decays to 0 when idle |
| Recharge Rate | L/h | Max positive rate in a 24h window |
| Long Term Rate | L/h | Full-window rate; preserves last value when idle (no decay) |
| Water Table | L | Rolling max volume over 7 days (groundwater level) |

## Persistence

History is saved to `well_monitor_history.json` every 5 minutes and reloaded on restart,
so rate sensors recover their pre-restart state.
