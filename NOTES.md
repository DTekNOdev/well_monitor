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

### Current filter (configuration.yaml)

```yaml
- platform: filter
  name: "filtered well monitor voltage measurement"
  entity_id: sensor.well_monitor_analog_input_2_voltage_measurement
  unique_id: ed2771e3-ae2c-40c9-a7ed-cb6a9eae8adb
  filters:
    - filter: lowpass
      time_constant: 4
    - filter: time_simple_moving_average
      window_size: "00:05"
      precision: 2
```

Works well but is slow — the 5-minute SMA introduces ~7-minute effective lag on the derived rate sensor,
making fill/drain detection sluggish.

### Preferred approach

Point the integration at the **raw** entity and let the coordinator apply an internal EMA:

```
ema = alpha × new_voltage + (1 − alpha) × ema
```

`alpha ≈ 0.2` gives comparable smoothing with much faster response to genuine level changes,
and the rate computation benefits directly from the reduced lag.

If switching to raw input, the configuration.yaml filter sensor can be retired.
