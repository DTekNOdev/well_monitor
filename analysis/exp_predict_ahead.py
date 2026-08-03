"""Why does the ladder lag ~11 min on a fill, and what would predicting ahead cost?

The ladder ALREADY extrapolates: at rung 3/4 the published target is
`self._curve(t)` evaluated at the current time, recomputed on every 60 s tick.
So "model the curve forward instead of waiting for a crossing" is not a missing
feature — it is already what happens.  The fill lag comes from three mechanisms
that deliberately suppress that prediction:

  1. BAND CEILING.  The target is clamped to `top + s/2` — the voltage at which
     raw would report the *next* level.  So before raw reports level L the
     output is structurally forbidden from reaching L.  Positive fill lag is
     mandatory by construction, whatever the model predicts.
  2. RATE CAP.  Output may move at most 2x the model's own slope.  At a real
     60-95 mV/h fill that is 120-190 mV/h, so climbing one 25 mV step after the
     band opens takes 8-12 minutes.
  3. CONTINUITY OFFSET.  Each refit is absorbed as a decaying lag term.

This script replays the recorded raw signal through the production estimator and
through variants that relax each mechanism, so the cost of each is measured
rather than argued.  Nothing in custom_components/ is modified.

Variants:
  live      production settings (baseline; should reproduce the report)
  no-cap    rate cap removed
  ahead     band ceiling raised to one full level above top
  both      no-cap + ahead  — "predict forward, correct on next measurement"
  both-mono both + output may never fall during a fill

Reported per variant: fill lag (the thing to improve), drain lag (must not
regress), smoothness and the worst downward excursion during a fill (the cost),
and rms against the hindsight model curve (is it actually more accurate?).

Run:  python analysis/exp_predict_ahead.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "custom_components" / "well_monitor"))
sys.path.insert(0, str(HERE))

import ladder as L                                      # noqa: E402
from ladder import LadderEstimator                      # noqa: E402
from report_two_methods import (                        # noqa: E402
    as_series, extract_anchors, find_events, fit_double, lag_table, load,
)

TICK = 60.0          # coordinator FILTER_TICK_SECONDS
JULY_K = (1 / (5.2 * 3600), 1 / (27.2 * 3600))


class Variant(LadderEstimator):
    """LadderEstimator with the three suppressors parameterised.

    update() is a copy of the production method with three edits, marked
    <<VARIANT>>.  Copied rather than refactored so production stays untouched
    while the experiment runs.
    """

    predict_ahead: float = 0.5      # band ceiling = top + predict_ahead * step
    rate_cap: bool = True
    monotone: bool = False

    def update(self, t: float, v: float) -> float:
        if self.last_t is None:
            self._new_episode(t, v)
            self.y = v
            self.last_t = t
            return self.y

        k = L._key(v)
        if self.top is not None and self.top - v > L.DROP_THRESHOLD:
            self._new_episode(t, v)
            self.rung = 1
            self.y = v
        elif self.top is not None and v - self.top > L.STEP_MAX:
            self._new_episode(t, v)
            self.y = v
        elif self.top is not None and v > self.top:
            old_top = self.top
            below_lvls = [kk / 1000 for kk in self.last_seen
                          if 0 < old_top - kk / 1000 <= L.STEP_MAX]
            self.last_seen[k] = t
            self.top = v
            if below_lvls:
                t_last_below = self.last_seen[L._key(max(below_lvls))]
                self._register_anchor((t_last_below + t) / 2.0, old_top, t)
            if self.rung == 1:
                self.rung = 2
        else:
            self.last_seen[k] = t
            if (self.top is not None and v < self.top
                    and t - self.last_seen.get(L._key(self.top), t) > L.DEMOTE_AFTER):
                self.top = v

        if (not self.frozen and self.rung >= 3 and self.pred_next_dt is not None
                and self.last_anchor_reg_t is not None
                and t - self.last_anchor_reg_t
                    > L.STALL_FACTOR * max(self.pred_next_dt, 1800.0)):
            self.frozen = True
            self._family_ver += 1

        if self.rung >= 3 and self.A is not None and not self.frozen:
            target = self._curve(t)
        else:
            target = self.top if self.top is not None else v

        band_lo = band_hi = None
        if self.top is not None:
            below_lvls = [kk / 1000 for kk in self.last_seen
                          if 0 < self.top - kk / 1000 <= L.STEP_MAX]
            if below_lvls:
                below = max(below_lvls)
                s = self.top - below
                m = (self.top + below) / 2.0
                # <<VARIANT>> ceiling is top + predict_ahead * s, not top + s/2
                ceil = self.top + self.predict_ahead * s
                if t - self.last_seen[L._key(below)] < L.DITHER_WINDOW:
                    band_lo = m - L.BAND_HALF
                    band_hi = max(m + L.BAND_HALF, ceil)
                else:
                    band_lo, band_hi = m, ceil
            else:
                band_lo = self.top - 0.0175
                band_hi = self.top + max(0.0175, self.predict_ahead * L.STEP)
        if band_lo is not None:
            if self.rung >= 3 and self.A is not None and not self.frozen:
                clipped = min(max(target, band_lo), band_hi)
                violation = clipped - target
                if violation != 0.0:
                    gain = 1.0 - math.exp(-(t - self.last_t) / L.BAND_CORR_TAU)
                    self.A += gain * violation
                    target = self._curve(t)
            target = min(max(target, band_lo), band_hi)

        if self.rung == 1:
            self.y = v
            self.offset = 0.0
            self._seen_ver = self._family_ver
        else:
            if self._seen_ver != self._family_ver:
                self.offset = (self.y if self.y is not None else target) - target
                self.offset_t = t
                horizon = self.pred_next_dt if self.pred_next_dt else L.OFFSET_TAU_MAX
                self.offset_tau = min(max(0.4 * horizon, 600.0), L.OFFSET_TAU_MAX)
                self._seen_ver = self._family_ver
            decay = (math.exp(-(t - self.offset_t) / self.offset_tau)
                     if self.offset_t is not None else 0.0)
            y_new = target + self.offset * decay
            if band_lo is not None:
                y_new = min(max(y_new, band_lo), band_hi)
            if self.rate_cap and self.rung >= 3 and self.A is not None \
                    and self.y is not None:
                cap = max(L.RATE_CAP_SLOPE_MULT * self._curve_slope(t),
                          L.RATE_CAP_FLOOR)
                dt = max(t - self.last_t, 0.0)
                dy = min(max(y_new - self.y, -cap * dt), cap * dt)
                self.y += dy
            else:
                self.y = y_new
            # <<VARIANT>> never fall while filling
            if self.monotone and self.rung >= 2 and self.y is not None:
                self.y = max(self.y, self._mono_floor)
        if self.monotone:
            self._mono_floor = (v if self.rung == 1
                                else max(getattr(self, "_mono_floor", v), self.y))

        self.last_t = t
        return self.y


VARIANTS = {
    "live":      dict(predict_ahead=0.5, rate_cap=True,  monotone=False),
    "no-cap":    dict(predict_ahead=0.5, rate_cap=False, monotone=False),
    "ahead":     dict(predict_ahead=1.0, rate_cap=True,  monotone=False),
    "both":      dict(predict_ahead=1.0, rate_cap=False, monotone=False),
    "both-mono": dict(predict_ahead=1.0, rate_cap=False, monotone=True),
}


def replay(raw: pd.DataFrame, **kw) -> pd.Series:
    """Feed raw events + a 60 s tick through a variant, as the coordinator does."""
    est = Variant()
    for key, val in kw.items():
        setattr(est, key, val)
    est._mono_floor = float(raw["state"].iloc[0])
    ts = raw["last_changed"].to_numpy()
    vs = raw["state"].to_numpy()
    t0 = pd.Timestamp(ts[0])
    epoch = [(pd.Timestamp(x) - t0).total_seconds() for x in ts]
    out_t, out_v = [], []
    tick = epoch[0]
    last_v = vs[0]
    for te, vv in zip(epoch, vs):
        while tick < te:                      # periodic ticks between events
            out_t.append(tick)
            out_v.append(est.update(tick, last_v))
            tick += TICK
        out_t.append(te)
        out_v.append(est.update(te, vv))
        last_v = vv
        tick = max(tick, te + TICK)
    idx = [t0 + pd.Timedelta(seconds=x) for x in out_t]
    s = pd.Series(out_v, index=pd.DatetimeIndex(idx))
    return s[~s.index.duplicated(keep="last")].sort_index()


def hindsight_model(raw: pd.DataFrame, e: dict):
    """Offline double-exp fit on quiet-zone anchors — the accuracy reference."""
    seg = raw.iloc[e["i0"]:e["i1"] + 1].copy().reset_index(drop=True)
    f0 = seg["last_changed"].iloc[0]
    seg["t"] = (seg["last_changed"] - f0).dt.total_seconds()
    anch = extract_anchors(seg)
    if len(anch) < 6:
        return None
    t, v = anch["t"].to_numpy(), anch["v"].to_numpy()
    k1, k2, A, B1, B2, _ = fit_double(t, v, k_fixed=JULY_K)
    grid = pd.date_range(f0, seg["last_changed"].iloc[-1], freq="5min")
    te = np.array([(x - f0).total_seconds() for x in grid])
    return pd.Series(A - B1 * np.exp(-k1 * te) - B2 * np.exp(-k2 * te), index=grid)


def main() -> None:
    data = load()
    raw = data["raw"]
    raw_s = as_series(raw)
    events = find_events(raw)
    fills = [e for e in events if e["kind"] == "fill" and e["hours"] >= 6]
    drains = [e for e in events if e["kind"] == "drain"
              and e["hours"] >= 0.5 and abs(e["v1"] - e["v0"]) >= 0.3]
    clean = max(fills, key=lambda e: e["hours"])

    print("=" * 74)
    print("PREDICT-AHEAD EXPERIMENT — where the fill lag actually comes from")
    print("=" * 74)
    print(f"replay: {len(raw)} raw events + {TICK:.0f}s tick   "
          f"fills>=6h: {len(fills)}   drains: {len(drains)}")
    print(f"clean fill (accuracy/smoothness reference): "
          f"{clean['t0']:%d %b %H:%M} -> {clean['t1']:%d %b %H:%M} "
          f"({clean['hours']:.1f} h)")

    model = hindsight_model(raw, clean)
    live_series = as_series(data["new"])

    rows = []
    for name, kw in VARIANTS.items():
        s = replay(raw, **kw)

        fl, dl = [], []
        for e in fills:
            d = lag_table(raw_s, s, e["t0"], e["t1"], rising=True, step=0.05)
            fl.extend(d["lag_min"].tolist())
        for e in drains:
            d = lag_table(raw_s, s, e["t0"], e["t1"], rising=False, step=0.05)
            dl.extend(d["lag_min"].tolist())
        fl, dl = np.array(fl), np.array(dl)

        seg = s[(s.index >= clean["t0"]) & (s.index <= clean["t1"])]
        tv = seg.diff().abs().sum()
        net = seg.iloc[-1] - seg.iloc[0]
        smooth = tv / abs(net) if net else float("nan")
        # worst downward excursion during any fill = the monotonicity cost
        worst_dip = 0.0
        for e in fills:
            f = s[(s.index >= e["t0"]) & (s.index <= e["t1"])]
            if len(f) > 1:
                worst_dip = max(worst_dip, float(-f.diff().min()))

        rms = float("nan")
        if model is not None:
            g = seg.reindex(seg.index.union(model.index)).ffill().reindex(model.index)
            err = (g - model).dropna()
            rms = float((err ** 2).mean()) ** 0.5 * 1000

        rows.append((name, np.median(fl), np.quantile(fl, 0.9),
                     np.median(dl), np.quantile(dl, 0.9),
                     smooth, worst_dip * 1000, rms))

    print()
    print(f"{'variant':<11}{'fill med':>9}{'fill p90':>9}"
          f"{'drain med':>10}{'drain p90':>10}{'smooth':>8}{'dip mV':>8}{'rms mV':>8}")
    for r in rows:
        print(f"{r[0]:<11}{r[1]:>9.1f}{r[2]:>9.1f}{r[3]:>10.1f}{r[4]:>10.1f}"
              f"{r[5]:>8.2f}{r[6]:>8.1f}{r[7]:>8.1f}")

    # ── is the "lag" actually raw's own dither-settling time? ────────────────
    # For each level, raw's FIRST touch is a transient blip; the level is only
    # established when raw stops falling back below it.  If the settle time
    # accounts for the lag, no filter can do better without chasing blips.
    print()
    print("=" * 74)
    print("IS THE LAG REAL?  raw's first touch of a level vs raw settling there")
    print("=" * 74)
    settle, fair_live, fair_ahead = [], [], []
    s_live = replay(raw, **VARIANTS["live"])
    s_ahead = replay(raw, **VARIANTS["both"])
    for e in fills:
        r = raw_s[(raw_s.index >= e["t0"]) & (raw_s.index <= e["t1"])]
        lo, hi = r.min(), r.max()
        for lv in np.arange(math.ceil(lo / 0.05) * 0.05, hi, 0.05):
            above = r.index[r.to_numpy() >= lv]
            below = r.index[r.to_numpy() < lv]
            if not len(above):
                continue
            first = above[0]
            after = below[below > first]
            settled = after[-1] if len(after) else first   # last fallback below
            settle.append((settled - first).total_seconds() / 60)
            for series, bucket in ((s_live, fair_live), (s_ahead, fair_ahead)):
                m = series[(series.index >= e["t0"]) & (series.index <= e["t1"])]
                hit = m.index[m.to_numpy() >= lv]
                if len(hit):
                    bucket.append((hit[0] - settled).total_seconds() / 60)
    settle = np.array(settle)
    print(f"raw settle time after first touch   "
          f"median {np.median(settle):>6.1f}   p90 {np.quantile(settle, 0.9):>6.1f}"
          f"   max {settle.max():>6.1f}   n={settle.size}")
    for label, arr in (("live", np.array(fair_live)), ("predict-ahead", np.array(fair_ahead))):
        print(f"lag vs raw's SETTLED crossing ({label:<13}) "
              f"median {np.median(arr):>+6.1f}   p90 {np.quantile(arr, 0.9):>+6.1f}"
              f"   n={arr.size}")
    print("negative = the estimator reaches the level BEFORE raw settles there")

    # sanity: does the 'live' replay reproduce the recorded live series?
    lv = live_series[(live_series.index >= clean["t0"])
                     & (live_series.index <= clean["t1"])]
    rp = replay(raw, **VARIANTS["live"])
    rp = rp.reindex(rp.index.union(lv.index)).ffill().reindex(lv.index)
    diff = (rp - lv).dropna()
    print()
    print(f"replay fidelity vs recorded live series on the clean fill: "
          f"rms {1000*float((diff**2).mean())**0.5:.1f} mV, "
          f"max {1000*diff.abs().max():.1f} mV  (n={len(diff)})")
    print("lag columns are minutes; dip = worst single-step fall during a fill")


if __name__ == "__main__":
    main()
