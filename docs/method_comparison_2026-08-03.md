# Well Monitor — smoothing method comparison

**Capture** 30 Jul 22:00 → 03 Aug 09:26 UTC (83.4 h) · **raw span** 6.05–8.13 V (2.08 V)
**Regenerate:** `analysis/scorecard.py` for the summary, `analysis/report_two_methods.py`
for the detail (both read `history-long-2methods.csv`)

Three series, all live: **raw** (quantized sensor), **new** (evidence-ladder
estimator, `ladder.py`), **old** (duty decoder + adaptive EMA, `filter.py`).
This is the first out-of-sample test — the ladder's reference taus were fitted
on the July capture, and it had never seen this cycle.

## Summary

Distance from the hindsight truth, mV and minutes, **lower is better**
(`python analysis/scorecard.py`):

| method | fill dist | fill delay | fill resid | draw dist | draw delay | draw resid | **SCORE** |
|---|---|---|---|---|---|---|---|
| raw *(noise floor)* | 9.5 | 3 | 8.7 | 11.7 | 0 | 11.7 | *10.9* |
| **new** (ladder) | **13.4** | 10 | **3.4** | **16.3** | **0** | 16.3 | **15.3** |
| old (EMA) | 21.1 | 16 | 5.2 | 15.7 | 2 | 13.2 | 17.5 |

**`new` is 13% closer to the truth than `old`, and its fill error is almost all
delay, not mis-shape** — take the 10 min out and fill distance falls 13.4 →
3.4 mV, a better shape than raw itself. On drawdown it has zero delay against
old's 2 min. **No defect found**; the integration is unchanged since 30 July
(`c4e1b26`). The two bugs fixed during this work were in the analysis script
(§7).

<details>
<summary><b>What the columns mean, and how truth is defined</b></summary>

- **dist** — RMS distance from the reference value, mV.
- **delay** — the time shift that best lines the estimate up: the effective lag
  in picking up change. Aggregate, and immune to the blip-chasing that corrupts
  per-level crossing times (§2).
- **resid** — RMS once that delay is removed: the error that is *not* lag. This
  is what separates "late" from "wrong shape".
- **SCORE** — duration-weighted mean dist. The single number to minimise.

Truth is the best available hindsight reconstruction, chosen per regime.
**Fills → the double-exponential physics curve** fitted to quiet-zone anchors;
being a two-amplitude fit to a few exact points it is independent of raw's
noise, so raw gets no structural advantage. **Drawdown → a centred zero-lag fit
of raw**, because no physics curve exists there (demand is unknown): centred so
a trailing estimator is penalised, order-2 so the 7200 mV/h drop is not smeared
into a slope the well never had.

Two things to know before using SCORE as an optimisation target:

- **Raw is the floor, not a competitor.** Zero delay by definition, so it wins
  on vertical distance; 10.9 is the quantization noise budget the smoothers work
  against. What smoothing buys — the monotone trace, §5, 1.00 vs raw's 4.17 —
  is not on this axis, so optimising SCORE alone converges on raw.
- **Only 22.2 of the 40 fill hours are scored.** The 01 Aug fill was truncated
  by a second drawdown and its own fit residual is 25.9 mV, so there the
  *reference* is the unreliable party. Excluded rather than allowed to pollute
  the number.

Validation: where both references exist they agree to 6.6 mV — exactly the
model's own anchor residual. The ranking is stable across hindsight windows from
11 to 61 min, so it is not an artefact of that choice.
</details>

<details>
<summary>Older headline table (lag by level crossing) — superseded, kept for continuity</summary>

| | new (ladder) | old (EMA) |
|---|---|---|
| Drawdown lag, median / p90 | **0.0 / 0.0 min** | 1.4 / 9.3 min |
| Fill lag, median / p90 | **11.0 / 27.0 min** | 16.0 / 31.0 min |
| Agreement with model (best fill, rms) | **13.3 mV** | 20.9 mV |
| Smoothness (best fill) | **1.00** | 1.05 |

Maxima are deliberately omitted — see the warning in §2. Prefer the scorecard
above: crossing-time lag is inflated by raw's transient blips, and only ~3 min
of the 11 is the estimator's (§6).
</details>

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
4. **The fill error is delay, not mis-shape** — and the delay is smaller than
   the crossing metric suggests. Take the 10 min delay out and fill distance
   falls 13.4 → 3.4 mV, beating raw. Of the 11 min crossing-time figure only
   ~3 min is the estimator's; the rest is raw's dither settling (§6). Relaxing
   the constraints that cause it recovers ~1 min and ~1.2 mV. **No change
   warranted**, but this is where the remaining headroom is if it ever matters.

## 6. How much of the fill lag is real, and can prediction remove it?

`analysis/exp_predict_ahead.py` replays the recorded raw signal through the
production estimator and through variants with each suppressor relaxed. It was
written to test the hypothesis that the ~11 min fill lag is the *cost of waiting
for crossing evidence* and could be recovered by extrapolating the fitted curve
further forward. **The hypothesis was wrong on both counts.**

First, the ladder already extrapolates. At rung 3–4 the published target is
`self._curve(t)` evaluated at the current time on every 60 s tick
([ladder.py:305](../custom_components/well_monitor/ladder.py#L305)) — forward
modelling is what it does between crossings, not something it waits to begin.

Second, the mechanisms that hold that prediction back cost far less than
expected:

| variant | fill med | fill p90 | drain med | model rms |
|---|---|---|---|---|
| live (production) | 7.0 | 23.6 | 0.0 | 7.7 mV |
| rate cap removed | 7.0 | 23.6 | 0.0 | 7.7 mV |
| band ceiling +1 level | 6.0 | 23.3 | 0.0 | **6.6 mV** |
| both | 6.0 | 23.3 | 0.0 | 6.6 mV |

- **The rate cap never binds.** Identical to four figures with it removed — the
  band ceiling already constrains the target more tightly, so the cap has
  nothing to clip. It costs nothing and can stay.
- **The band ceiling is the only suppressor that bites**, and it is worth about
  **1 minute** of median lag. (It does cost ~1 mV of model accuracy, which is
  the one measurable argument for relaxing it.)

Re-scored against the physics curve (the metric the scorecard uses, and the one
that matters — crossing times are corrupted by blips), the same variants:

| variant | fill dist | delay | resid |
|---|---|---|---|
| live | 8.6 mV | 6 min | 3.2 mV |
| rate cap removed | 8.5 | 6 | 3.1 |
| band ceiling +1 level | 7.5 | 5 | 3.3 |
| both | **7.4** | **5** | 3.2 |

So relaxing the ceiling is worth ~1.2 mV and ~1 minute. Real but small, and it
buys nothing on shape. (These replay figures run ~7 mV ahead of the recorded live
series — see the caveat at the end of this section — so compare variants against
each other, not against the scorecard.)

Third, **most of the *crossing-time* lag is not the estimator's at all.** That
metric compares against raw's *first touch* of a level, which is usually a
transient blip:

| | median | p90 | max |
|---|---|---|---|
| raw's own settle time after first touch | 1.0 | 50.7 | 146.0 |
| **lag vs raw's *settled* crossing (live)** | **+3.0** | **+13.2** | — |
| lag vs raw's settled crossing (predict-ahead) | +2.5 | +12.2 | — |

Against a fair reference the estimator's true fill lag is **3 min median /
13 min p90**, not 11 / 27. The rest is raw dithering, and closing it would mean
chasing blips — the exact behaviour the ladder exists to prevent. Predicting
further ahead recovers about **30 seconds** of that 3 minutes.

**Conclusion: no change is warranted.** The available win is under a minute of
median lag, against a signal moving at 60–95 mV/h (~1 mV of level error), and it
would be bought by loosening the constraint that currently makes over-prediction
impossible. Note that the replay's 7.0 min baseline differs from §2's 11.0 min
because the replay starts cold with no prior episode state and runs ~7 mV ahead
of the recorded series; use the replay only to compare variants against each
other, not against §2.

## 7. Analysis bugs found while producing this report

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

**Start with `python analysis/scorecard.py`** — that is the skim layer, and its
SCORE column is the one number to compare week on week (and the objective to
optimise against if the estimator is ever tuned). Current baseline: **new 15.3,
old 17.5, raw floor 10.9**; fill delay 10 min, drawdown delay 0 min.

Then re-run `python analysis/report_two_methods.py` for the detail. The figures
to watch:

- **drain pooled median / p90 lag** — should stay at 0.0 / 0.0 for the new method
- **fill pooled median / p90 lag** — currently 11.0 / 27.0 min. Do *not* compare
  maxima (§2), and read §6 before treating this as a defect: only ~3 min of it
  is the estimator's, the rest is raw's dither settling.
- **`python analysis/exp_predict_ahead.py`** — re-run alongside the main report.
  Its "lag vs raw's settled crossing" figures (3.0 / 13.2 min) are the honest
  lag metric and the better week-on-week comparison.
- **best-fill model rms** — 5.9 mV here; a rise suggests the taus are drifting
  seasonally and want refitting
- **best-fill smoothness** — 1.00 here; anything above ~1.1 means quantization
  artefacts are leaking back in
- **tau_fast / tau_slow per fill** — watch for seasonal drift away from
  5.2 h / 27.2 h

Caveat to carry forward: only the 02 Aug fill was uninterrupted, so the
single-fill sample size for the model figures is one. A capture with two or
more clean fills would firm up the tau-stability claim.
