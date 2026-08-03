# Well Monitor — smoothing method comparison

**Capture** 30 Jul 22:00 → 03 Aug 09:26 UTC (83.4 h) · **raw span** 6.05–8.13 V (2.08 V)
**Regenerate:** `python analysis/report_two_methods.py` (reads `history-long-2methods.csv`)

Three series, all live: **raw** (quantized sensor), **new** (evidence-ladder
estimator, `ladder.py`), **old** (duty decoder + adaptive EMA, `filter.py`).
This is the first out-of-sample test — the ladder's reference taus were fitted
on the July capture, and it had never seen this cycle.

## Verdict

| | new (ladder) | old (EMA) |
|---|---|---|
| Drawdown lag, median | **0.0 min** | 1.4 min |
| Drawdown lag, p90 | **0.0 min** | 9.3 min |
| Fill lag, median | **11.0 min** | 16.0 min |
| Fill lag, p90 | **27.0 min** | 31.0 min |
| Agreement with model (best fill, rms) | **13.3 mV** | 20.9 mV |
| Smoothness (best fill) | **1.00** | 1.05 |

(Maxima are deliberately omitted — see the warning in §2. They measure
transient-blip chasing, not lag.)

**No defect was found in the deployed estimator.** The integration code has not
been changed since 30 July (`c4e1b26`), so these figures describe exactly what
ran over the weekend. Two bugs *were* found and fixed in the analysis script
during this work — see §6.

The new method is **effectively lag-free on drawdown** while being at least as
smooth as the old one. That was the design goal and it holds on unseen data.

## 1. Events detected

Not one drawdown and one fill — usage happened repeatedly, which matters
because measuring across a reversal produces meaningless numbers.

| kind | from | to | volts | hours | mV/h |
|---|---|---|---|---|---|
| fill | 30 Jul 22:00 | 30 Jul 22:28 | 8.11 → 8.13 | 0.5 | +43 |
| drain | 30 Jul 22:28 | 01 Aug 12:31 | 8.13 → 6.05 | 38.0 | −55 |
| fill | 01 Aug 12:31 | 02 Aug 05:47 | 6.05 → 7.67 | 17.3 | +94 |
| drain | 02 Aug 05:47 | 02 Aug 11:13 | 7.67 → 6.64 | 5.4 | −190 |
| fill | 02 Aug 11:13 | 03 Aug 09:26 | 6.64 → 7.96 | 22.2 | +59 |

Peak instantaneous drawdown rate: **7200 mV/h** (01 Aug ~12:30).

## 2. Time lag behind raw — the headline result

Measured horizontally: for each 50 mV level, the time each method crosses it
minus the time raw crosses it. This answers "how stale is the reading", which
is what matters during a drawdown.

| event | series | median | mean | p90 | max | n |
|---|---|---|---|---|---|---|
| drain 30 Jul 22:28 | new | 0.0 | 0.0 | 0.0 | **1.0** | 40 |
| | old | 1.4 | 19.1 | 11.1 | **558.0** | 38 |
| fill 01 Aug 12:31 | new | 10.0 | 16.6 | 26.9 | 149.4 | 32 |
| | old | 13.0 | 21.7 | 33.7 | 146.0 | 32 |
| drain 02 Aug 05:47 | new | 0.0 | 0.1 | 0.1 | **5.0** | 20 |
| | old | 1.0 | 7.9 | 8.5 | **117.0** | 19 |
| fill 02 Aug 11:13 | new | 11.0 | 13.8 | 24.2 | 39.9 | 27 |
| | old | 17.4 | 17.8 | 27.0 | 32.9 | 27 |
| **drain pooled** | **new** | **0.0** | **0.0** | **0.0** | **5.0** | 60 |
| | old | 1.4 | 15.4 | 9.3 | 558.0 | 57 |
| **fill pooled** | **new** | **11.0** | **15.4** | **27.0** | 149.4 | 59 |
| | old | 16.0 | 19.9 | 31.0 | 146.0 | 59 |

**Steepest hour** (01 Aug 11:59–12:59, 7200 mV/h peak), measured at 25 mV
resolution:

| series | median | mean | p90 | max |
|---|---|---|---|---|
| new | **0.0** | **0.0** | **0.0** | **0.0** |
| old | 1.0 | 1.7 | 2.3 | 8.0 |

Reading this: on drawdown the new method is *exactly* current — zero lag at
every level through the fastest hour, because a multi-step drop bypasses the
model and tracks raw directly.

> ⚠️ **Ignore the `max` column — it does not measure lag.** The metric asks
> "when did each series *first* reach level L", and raw frequently touches a
> level once as a transient blip and falls back for hours. Both maxima are
> artefacts of that, not delays:
>
> - **new, 149 min on the 01 Aug fill at 7.30 V** — raw blipped to 7.30 at
>   21:29, then oscillated between 7.18 and 7.30 for two hours and only settled
>   above it at ~23:40. The ladder crossed at 23:58. Declining to chase that
>   spike is exactly what the estimator is *for*; the metric charged it 149
>   minutes for being right. The old method scored 146 min on the same level.
> - **old, 558 min on the first drawdown** — same mechanism in reverse.
>
> Use **median and p90** for lag. They are dominated by levels raw crossed
> cleanly, and they are the figures to compare week on week.

Fill lag of ~11 min for the new method is *by design*, not a defect — the
estimator deliberately waits for boundary-crossing evidence before moving, and
`_LATCH`-style corrections are rate-capped. During a fill, an 11-minute lag on
a 60–95 mV/h signal is under 20 mV of level error.

## 3. Recharge model fit

Fitted per uninterrupted fill, on quiet-zone anchors (the quantization-exact
truth points).

| fill | anchors | tau_fast | tau_slow | V_top | rms | rms with July taus |
|---|---|---|---|---|---|---|
| 01 Aug 12:31 | 65 | 4.5 h | 36.0 h | 8.59 V | 25.4 mV | 25.9 mV |
| 02 Aug 11:13 | 53 | 2.9 h | 8.5 h | 8.05 V | **5.9 mV** | 6.6 mV |

Reference from July: **5.2 h + 27.2 h**.

Two observations:

- **The transfer test passes.** Pinning the July taus costs almost nothing
  (25.4 → 25.9 and 5.9 → 6.6 mV), confirming again that the taus are well
  properties rather than per-event fits. The ladder's use of fixed reference
  taus is sound.
- **The second fill fits far better than the first** (5.9 vs 25.4 mV). The
  first fill was interrupted at 7.67 V by the 02 Aug draw, so its tail is
  truncated and `V_top` extrapolates to an implausible 8.59 V. The clean
  22-hour second fill is the trustworthy one, and 5.9 mV there matches the
  July capture's 5.7 mV almost exactly.

## 4. Agreement with model and anchors

| fill | series | vs model rms | max | vs anchors rms | max |
|---|---|---|---|---|---|
| 01 Aug 12:31 | raw | 44.8 | 136.3 | 33.8 | 132.5 |
| | new | 49.6 | 118.8 | 40.1 | 132.5 |
| | old | 54.2 | 156.5 | 45.2 | 124.4 |
| 02 Aug 11:13 | raw | 9.0 | 25.5 | 9.2 | 28.6 |
| | new | **13.3** | 57.2 | **14.1** | 36.7 |
| | old | 20.9 | 57.2 | 22.1 | 49.8 |

On the clean fill the new method is **~35% closer to both the model and the
anchors** than the old one. Note that `raw` scores best on these vertical
metrics — unsurprising, since the anchors are derived from raw and it has no
lag; the point of smoothing is to remove the staircase, which vertical error
alone does not reward.

## 5. Smoothness

Total variation ÷ net change over each fill. 1.00 = perfectly monotone.

| fill | raw | new | old |
|---|---|---|---|
| 01 Aug 12:31 | 3.21 | 1.49 | 1.45 |
| 02 Aug 11:13 | 4.17 | **1.00** | 1.05 |

On the clean fill the new method is **perfectly monotone** — no reversals at
all — where raw wanders 4.17× its net change. The old method is fractionally
smoother on the interrupted fill (1.45 vs 1.49) and slightly worse on the
clean one.

## Conclusions

1. **The drawdown-lag goal is met decisively.** Zero median lag, 5-minute
   worst case pooled, versus the old method's 1.4-minute median and long tail.
   During the fastest hour the new method has literally no lag at any level.
2. **Smoothness is not sacrificed** — 1.00 on the clean fill against the old
   method's 1.05.
3. **The physics generalises.** July's taus fit this unseen cycle nearly as
   well as freshly-fitted ones, and the clean fill's 5.9 mV residual matches
   July's 5.7 mV.
4. **Fill lag of ~11 min is the remaining cost**, and it is inherent to
   waiting for crossing evidence. Worth revisiting only if it proves
   practically limiting.

## 6. Analysis bugs found while producing this report

Both were in `analysis/report_two_methods.py`, written for this report — not in
the integration. Recorded because they would otherwise recur next week.

- **Segmentation spanned a reversal.** A naive peak → trough → end split put
  the 02 Aug drawdown *inside* the "fill", so the single fitted curve and every
  error figure were meaningless (1006 mV "errors"). Now segmented with a
  zigzag on 300 mV reversals, which correctly finds 2 drains and 3 fills.
- **The curve fit degenerated.** Unbounded, the slow exponential always
  improves the fit by flattening into a linear term: it ran to tau = 20,407 h
  and V_top = 194 V — an excellent fit to a meaningless model. The search is
  now bounded to tau ≤ 72 h with the asymptote within 1 V of the data, and
  rejects negative amplitudes.
- **Maxima were quoted as lag** in the first draft. Corrected in §2; they
  measure blip-chasing.

## For next week's comparison

Re-run `python analysis/report_two_methods.py` against the new export and
compare against this file. The figures to watch:

- **drain pooled median / p90 lag** — should stay at 0.0 / 0.0 for the new method
- **fill pooled median / p90 lag** — currently 11.0 / 27.0 min; the numbers to
  try to reduce. Do *not* compare maxima (§2).
- **best-fill model rms** — 5.9 mV here; a rise suggests the taus are drifting
  seasonally and want refitting
- **best-fill smoothness** — 1.00 here; anything above ~1.1 means quantization
  artefacts are leaking back in
- **tau_fast / tau_slow per fill** — watch for seasonal drift away from
  5.2 h / 27.2 h

Caveat to carry forward: only the 02 Aug fill was uninterrupted, so the
single-fill sample size for the model figures is one. A capture with two or
more clean fills would firm up the tau-stability claim.
