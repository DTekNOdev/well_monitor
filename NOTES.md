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

The integration reads the **raw** voltage entity directly and applies an internal
**PWM duty-cycle decoder** (`filter.py`). The old time-weighted EMA filter (and its
`ema_tau_seconds` config option) has been replaced as of v1.1.0.

### Filter approach — duty-cycle decoding

The sensor voltage is quantized in ~0.02–0.03 V steps. Between steps the reading
toggles between the two adjacent quantization levels; the fraction of time spent at
the upper level encodes the true sub-step level — a PWM duty cycle. The decoder
tracks the active pair `(lo, hi)` and a time-weighted duty estimate:

```
output = lo + duty × (hi − lo)
```

- Toggle within pair → duty updates (ZOH EMA, tau 900 s) → smooth interpolation
- Step up one level → pair re-anchors, output continuous (fill tracking)
- Jump > 1 step → snap to raw immediately (fast draw-down tracking)
- **Fill ratchet**: while filling, duty may only rise — output strictly monotone
- **Adaptive smoothing**: output EMA (tau 360 s) grows to 5× while the pair is
  static (flat line through dithering), drops to 90 s when output runs > 0.025 V
  away (draw-down stays responsive)

A 60 s internal tick advances the filter with the held reading between source
events (the source entity emits nothing while its value is unchanged). Filter
state persists across restarts via the history file (wall-clock time base).

Algorithm developed and tuned against recorded data — see
`analysis/sim_duty_decode.py`. Versus the old EMA on the July 2026 sample:
fill roughness 1.62 → 1.00 (perfectly monotone), draw-down lag 0.123 → 0.056 V,
static-level flatness 0.376 → 0.019 V total variation.

### Experimental alternative — recharge-model estimator (`ladder.py`)

A **Level estimator** option (integration configure dialog) selects which
filter feeds depth/volume/rates:

- **Duty-cycle decoder** (default) — the filter above.
- **Recharge model** (experimental) — models the fill physics directly:
  double-exponential recharge (fast tau 5.2 h borehole storage + slow tau
  27.2 h aquifer, fitted from the July 2026 capture), anchored on quiet-zone
  centres, with an evidence ladder that degrades gracefully to level-hold /
  raw tracking when a fill has too few anchors or a drawdown is under way.
  Corrections never step: they are continuity-absorbed and rate-capped at
  max(2× model slope, one quantization step per 4 h).
  Design: `docs/model_estimator_design.md`; validation: `analysis/sim_ladder.py`
  (9.2 mV rms vs 13.2 mV for the duty decoder against hindsight anchors).

Both estimators run in parallel regardless of selection, so switching in the
options is warm. The voltage sensor exposes `filter_method` and `model_rung`
attributes for monitoring. Multiple config entries on the same input voltage
are supported (per-entry persistence files), so a second device can run the
other method side by side for comparison.

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
