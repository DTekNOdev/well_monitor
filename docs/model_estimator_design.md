# Model-Based Level Estimation — Design

Status: validated offline against 2026-07-25 → 07-30 capture (`analysis/`).
Not yet implemented in the integration. The shipped duty decoder
(`filter.py`) remains the production output and becomes the fallback rung of
this design.

## Findings the design rests on

All from `analysis/fit_fill_curve.py` and `analysis/fit_all_fills.py` on
`history-long.csv` (the only stable capture since the sensor moved Z-wave
networks):

1. **Quiet-zone anchors are exact calibration points.** The anchor for
   quantization level L sits at the centre of L's quiet zone — halfway
   between the last blip down to the level below and the first blip up to
   the level above — valued at L. The sensor noise amplitude cancels at
   that midpoint, so anchors lie on the true curve regardless of noise.

2. **The fill is a double exponential.**
   `x(t) = V_top − B1·exp(−t/tau_fast) − B2·exp(−t/tau_slow)`
   fits the 95 h fill to 5.7 mV rms (a quarter of a quantization step).
   A single exponential leaves a systematic ±85 mV S-residual.
   Physical read: fast borehole/fracture storage + slow aquifer inflow.

3. **The taus are well properties; the amplitudes are episode properties.**
   Fast tau reproduced across independent fills (5.2 h vs 5.4 h). With taus
   fixed, an amplitudes-only fit matches each fill as well as its own free
   fit (6–8 mV rms on 2 h, 8.5 h and 95 h segments).

4. **Amplitudes must be constrained non-negative.** Unconstrained linear
   least squares on short windows produced B2 < 0 — a curve that peaks and
   declines when extrapolated. With B1, B2 ≥ 0 (two-line active set), short
   windows collapse honestly to fast-component-only.

5. **Short windows cannot identify tau_slow or V_top.** A 6 h window said
   V_top ≈ 7.75 when the truth was 8.10. V_top must come from long fills
   and/or the existing water-table tracking, never from a per-episode fit.

Fitted reference (summer 2026): tau_fast ≈ 5.2 h, tau_slow ≈ 27 h,
V_top ≈ 8.10 V.

## Architecture: an evidence ladder, not a mode switch

The estimate uses exactly as much evidence as the current fill episode has
produced. Rungs, top to bottom:

| rung | evidence | estimate |
|------|----------|----------|
| 4 | ≥ ~6 anchors, residuals healthy | double exp: shared taus, amplitudes refit (constrained, closed-form) on each new anchor |
| 3 | 2–5 anchors | fast-component-only fit (B2 = 0) |
| 2 | 0–1 anchors | duty decoder (`filter.py`) — the shipped filter is this rung |
| 1 | draining (multi-step drop) | track raw |

Promotion is automatic: anchors arrive within minutes-to-tens-of-minutes
after a drawdown (guaranteed by the fast tau), so the ladder climbs quickly.

### Demotion: the health check

Every new anchor is a prediction test. The current curve predicts the next
level-crossing time and value.

- Anchor arrives, error small → refit amplitudes, stay.
- Error > ~2× episode rms → refit; still bad → demote one rung.
- **Timeout** (the observed "fill just stops" anomaly): predicted next
  crossing is overdue by 2–3× → freeze the curve, demote, raise an anomaly
  flag. The stall becomes a detectable event instead of a silent error.

### The safety property: readings hold veto

Whatever the rung, the output is clamped to the band the quantized readings
currently allow (~±half a step around the reported level, tightened to the
dithering pair when toggling). The model interpolates *within* what
quantization permits; a wrong curve is bounded to ~15 mV of error before
the clamp stops it. The model can never run away from the sensor.

### Output smoothing

Corrections (anchor registration moves the curve; demotions) are eased in by
slew/EMA — same principle as the shipped `OutputSmoother` — so the published
sensor never steps.

## Parameter lifecycle

| parameter | lifetime | source |
|-----------|----------|--------|
| B1, B2, (episode V_top') | one fill episode | constrained LSQ over the episode's anchors, refit per anchor |
| tau_fast | slow drift | refit only from fills spanning > ~2× tau_fast; EMA across episodes |
| tau_slow | slow drift | refit only from multi-day fills; EMA across episodes |
| V_top | seasonal | long fills + existing water-table sensor; never from short fits |

Seasonal adaptation (user requirement — rates vary by season, fills have
been observed to stall entirely): amplitudes adapt per episode by
construction; taus and V_top drift slowly with explicit provenance; stalls
are flagged, not absorbed.

## Rollout

- **Phase 0 — instrument.** Add online anchor extraction (level-ladder
  bookkeeping) to the coordinator and persist anchors. Output unchanged.
  Accumulates training data across the coming drawdown/refill cycles.
- **Phase 1 — parallel sensor.** Publish the model estimate as a second
  sensor ("Water level (modelled)") alongside the shipped filter. Compare
  in HA history over weeks of real use, including tau/V_top stability
  against season and rain.
- **Phase 2 — promote.** Model becomes the voltage feeding depth/volume/
  rates; duty decoder remains as rungs 1–2.

Byproducts once live: analytic fill rate (curve derivative), time-to-full
prediction, fill-stall anomaly flag.

## Open questions

- Tau stability across seasons and saturation states — needs autumn/winter
  drawdown-refill cycles.
- V_top vs rainfall: correlate with the water-table sensor over weeks.
- Whether episode fits should share strength across small fragments
  (the 25th-evening chop produced 12 unfittable fragments; currently they
  ride rung 2, which is fine but wastes their few anchors).
- Anchor timing jitter during fast fills (quiet zones of minutes) — currently
  the dominant error term; a duty-weighted anchor time could tighten it.
