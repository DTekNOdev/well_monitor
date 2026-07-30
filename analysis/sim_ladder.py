"""Full causal simulation of the evidence-ladder estimator (design doc).

Runs the complete proposed pipeline over the whole capture, minute by minute,
exactly as the integration would see it — no lookahead — and compares it
head-to-head with the CURRENTLY SHIPPED duty-decoder filter (imported from
custom_components/well_monitor/filter.py, not a re-implementation).

Ladder rungs (docs/model_estimator_design.md):
  4  >= 6 anchors, healthy   double exp, shared taus, constrained amplitudes
  3  2..5 anchors            fast-component-only fit
  2  0..1 anchors            hold the current level (duty-decoder regime)
  1  draining                track raw

Health: every new anchor is a prediction test (error vs the current curve),
and a stall (predicted next crossing overdue 2.5x) freezes the curve and
demotes.  Output is always clamped to the band the readings allow and
slew-smoothed.

Honesty note: the reference taus (5.2 h / 27.2 h) were fitted on this same
capture's big fill.  This sim validates the machinery, not generalisation —
the next drawdown/refill provides the out-of-sample test.

Run:  python analysis/sim_ladder.py
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "custom_components" / "well_monitor"))

from fit_all_fills import detect_fill_segments, extract_anchors, fit_fixed_taus_nonneg
from fit_fill_curve import fit_double_exponential
from filter import DutyDecoder, OutputSmoother          # the shipped filter

CSV = HERE.parent / "history-long.csv"

# Reference taus (learned from this capture's big fill — see honesty note)
TAU_FAST_H, TAU_SLOW_H = 5.2, 27.2
K1 = 1.0 / (TAU_FAST_H * 3600)
K2 = 1.0 / (TAU_SLOW_H * 3600)

STEP = 0.025            # nominal quantization step
STEP_MAX = 0.035
DROP_THRESHOLD = 0.05   # real drawdown
DEMOTE_AFTER = 4 * 3600
SLEW_TAU = 600.0        # s — output smoothing
RUNG4_MIN_ANCHORS = 6
RUNG3_MIN_ANCHORS = 2
HEALTH_ERR = 0.015      # V — anchor prediction error that forces attention
STALL_FACTOR = 2.5      # overdue factor on the predicted next crossing
OFFSET_TAU = 5400.0     # s — continuity offset decay (corrections become slope)


def _key(v: float) -> int:
    return round(v * 1000)


def fit_amp_fast_only(t: np.ndarray, v: np.ndarray):
    """A - B1 exp(-K1 t), B1 >= 0."""
    X = np.vstack([np.ones_like(t), np.exp(-K1 * t)]).T
    coef, *_ = np.linalg.lstsq(X, v, rcond=None)
    A, B1 = coef[0], -coef[1]
    if B1 < 0:
        A, B1 = float(v.mean()), 0.0
    resid = v - (A - B1 * np.exp(-K1 * t))
    return A, B1, 0.0, math.sqrt(float(resid @ resid) / len(t))


@dataclass
class LadderEstimator:
    """Causal implementation of the evidence ladder.

    continuity=True: a refit / rung change never steps the output.  The jump
    between old and new target is captured into an offset that decays over
    OFFSET_TAU, so corrections manifest as a gentle change of slope rather
    than a bulb (steep chase followed by flattening).

    rate_cap=True: the published value may never move faster than
    max(3x the model's own slope, 0.6 mV/min) — so even a band-veto step
    (hard evidence) becomes a steep but finite ramp, corrected over the
    following timestamps.  Drains (rung 1) bypass the cap and snap.
    """

    continuity: bool = False
    rate_cap: bool = False

    # ladder / episode state
    last_seen: dict = field(default_factory=dict)
    top: float | None = None
    episode_t0: float | None = None
    anchors_t: list = field(default_factory=list)   # absolute seconds
    anchors_v: list = field(default_factory=list)
    all_anchors: list = field(default_factory=list)  # (t, v) — for reporting

    # continuity offset
    offset: float = 0.0
    offset_t: float | None = None
    _family_ver: int = 0          # bumped on refit / rung change / freeze
    _seen_ver: int = 0

    # fitted curve (amplitudes on episode clock)
    A: float | None = None
    B1: float = 0.0
    B2: float = 0.0
    fit_rms: float = 0.0
    frozen: bool = False
    last_anchor_reg_t: float | None = None
    pred_next_dt: float | None = None   # predicted seconds to next crossing

    rung: int = 2
    y: float | None = None
    last_t: float | None = None
    events: list = field(default_factory=list)

    def _log(self, t: float, msg: str) -> None:
        self.events.append((t, msg))

    # ── episode / curve management ───────────────────────────────────────────

    def _new_episode(self, t: float, v: float) -> None:
        self.last_seen = {_key(v): t}
        self.top = v
        self.episode_t0 = t
        self.anchors_t, self.anchors_v = [], []
        self.A, self.B1, self.B2 = None, 0.0, 0.0
        self.frozen = False
        self.pred_next_dt = None
        self.rung = 2
        # an episode reset is an intentional discontinuity — no offset capture
        self.offset = 0.0
        self._family_ver += 1
        self._seen_ver = self._family_ver

    def _curve(self, t: float) -> float | None:
        if self.A is None or self.episode_t0 is None:
            return None
        te = t - self.episode_t0
        return self.A - self.B1 * math.exp(-K1 * te) - self.B2 * math.exp(-K2 * te)

    def _curve_slope(self, t: float) -> float:
        """Analytic dx/dt of the fitted curve (V/s); 0 when no curve."""
        if self.A is None or self.episode_t0 is None:
            return 0.0
        te = t - self.episode_t0
        return (self.B1 * K1 * math.exp(-K1 * te)
                + self.B2 * K2 * math.exp(-K2 * te))

    def _refit(self, t: float) -> None:
        self._family_ver += 1
        te = np.array(self.anchors_t) - self.episode_t0
        vv = np.array(self.anchors_v)
        n = len(te)
        if n >= RUNG4_MIN_ANCHORS:
            self.A, self.B1, self.B2, self.fit_rms = fit_fixed_taus_nonneg(te, vv, K1, K2)
            self.rung = 4
        elif n >= RUNG3_MIN_ANCHORS:
            self.A, self.B1, self.B2, self.fit_rms = fit_amp_fast_only(te, vv)
            self.rung = 3
        else:
            self.A = None
            self.rung = 2
            return
        # predicted time to the next crossing (curve reaching top + STEP/2)
        target = self.top + STEP / 2
        x_now = self._curve(t)
        self.pred_next_dt = None
        if x_now is not None and self.A is not None and self.A > target > x_now:
            # solve A - B1 e^{-k1 s} - B2 e^{-k2 s} = target numerically
            lo_s, hi_s = 0.0, 7 * 86400.0
            te_now = t - self.episode_t0
            for _ in range(60):
                mid = (lo_s + hi_s) / 2
                xm = (self.A - self.B1 * math.exp(-K1 * (te_now + mid))
                      - self.B2 * math.exp(-K2 * (te_now + mid)))
                if xm < target:
                    lo_s = mid
                else:
                    hi_s = mid
            if hi_s < 7 * 86400.0 - 1:
                self.pred_next_dt = hi_s

    def _register_anchor(self, t_mid: float, level: float, t_now: float) -> None:
        # health: prediction test before folding in
        pred = self._curve(t_mid)
        if pred is not None and self.rung >= 3:
            err = level - pred
            if abs(err) > max(HEALTH_ERR, 2.5 * self.fit_rms):
                self._log(t_now, f"anchor {level:.2f}V off-curve by {err*1000:+.0f} mV — refit")
        self.anchors_t.append(t_mid)
        self.anchors_v.append(level)
        self.all_anchors.append((t_mid, level))
        self.frozen = False
        self.last_anchor_reg_t = t_now
        self._refit(t_now)

    # ── main update (one grid tick) ──────────────────────────────────────────

    def update(self, t: float, v: float) -> tuple:
        if self.last_t is None:
            self._new_episode(t, v)
            self.y = v
            self.last_t = t
            return self.y, self.rung

        k = _key(v)
        if self.top is not None and self.top - v > DROP_THRESHOLD:
            # rung 1: real drawdown — track raw, fresh episode
            self._log(t, f"drawdown: {self.top:.2f} → {v:.2f} V")
            self._new_episode(t, v)
            self.rung = 1
            self.y = v
        elif self.top is not None and v - self.top > STEP_MAX:
            self._log(t, f"upward jump: {self.top:.2f} → {v:.2f} V")
            self._new_episode(t, v)
            self.y = v
        elif self.top is not None and v > self.top:
            # fill progressed one level: register the old top's anchor
            old_top = self.top
            below = [lv for kk, lv in ((kk, kk / 1000) for kk in self.last_seen)
                     if 0 < old_top - lv <= STEP_MAX]
            self.last_seen[k] = t
            self.top = v
            if below:
                t_last_below = self.last_seen[_key(max(below))]
                self._register_anchor((t_last_below + t) / 2.0, old_top, t)
            if self.rung == 1:
                self.rung = 2   # refill has begun
        else:
            self.last_seen[k] = t
            if (self.top is not None and v < self.top
                    and t - self.last_seen.get(_key(self.top), t) > DEMOTE_AFTER):
                self.top = v    # slow drain: demote the top quietly

        # stall check: predicted next crossing long overdue → freeze the curve
        if (not self.frozen and self.rung >= 3 and self.pred_next_dt is not None
                and self.last_anchor_reg_t is not None
                and t - self.last_anchor_reg_t > STALL_FACTOR * max(self.pred_next_dt, 1800)):
            self.frozen = True
            self._family_ver += 1
            self._log(t, f"stall: next crossing overdue "
                         f"(predicted {self.pred_next_dt/3600:.1f} h, "
                         f"waited {(t - self.last_anchor_reg_t)/3600:.1f} h) — frozen")

        # target from the active rung
        if self.rung >= 3 and self.A is not None and not self.frozen:
            target = self._curve(t)
        else:
            target = self.top if self.top is not None else v

        # ── the readings as an observation, not just a box ───────────────────
        # Work out the band the truth must be in, from the ACTUAL local pair:
        #   dithering (below,top):  truth within ~noise of the threshold m
        #   solid at top:           truth between m and the next threshold up
        band_lo = band_hi = None
        if self.top is not None:
            below_lvls = [kk / 1000 for kk in self.last_seen
                          if 0 < self.top - kk / 1000 <= STEP_MAX]
            if below_lvls:
                below = max(below_lvls)
                s = self.top - below
                m = (self.top + below) / 2.0
                dithering = t - self.last_seen[_key(below)] < 2700
                if dithering:
                    band_lo, band_hi = m - 0.012, m + 0.012
                else:
                    band_lo, band_hi = m, self.top + s / 2
            else:
                band_lo = self.top - 0.0175
                band_hi = self.top + 0.0175
        if band_lo is not None:
            # Band violation is an innovation: correct the CURVE (offset via A)
            # so the model absorbs it and stays consistent going forward,
            # instead of pinning the output against a wall.
            if self.rung >= 3 and self.A is not None and not self.frozen:
                clipped = min(max(target, band_lo), band_hi)
                violation = clipped - target
                if violation != 0.0:
                    gain = 1.0 - math.exp(-(t - self.last_t) / 1800.0)
                    self.A += gain * violation
                    target = self._curve(t)
            target = min(max(target, band_lo), band_hi)

        if self.rung == 1:
            # drain: track raw, intentional discontinuity
            self.y = v
            self.offset = 0.0
            self._seen_ver = self._family_ver
        elif self.continuity:
            # continuity mode: capture any target-family change into a decaying
            # offset so the output never steps — corrections become slope.
            # The decay must outpace the anchor cadence, or the output lags a
            # recaptured offset forever: scale it to the predicted time until
            # the next anchor (fast fill → fast absorb; slow tail → gentle).
            if self._seen_ver != self._family_ver:
                self.offset = (self.y if self.y is not None else target) - target
                self.offset_t = t
                horizon = self.pred_next_dt if self.pred_next_dt else OFFSET_TAU
                self.offset_tau = min(max(0.4 * horizon, 600.0), OFFSET_TAU)
                self._seen_ver = self._family_ver
            decay = (math.exp(-(t - self.offset_t) / getattr(self, "offset_tau", OFFSET_TAU))
                     if self.offset_t is not None else 0.0)
            y_new = target + self.offset * decay
            # the readings' veto is applied to the target — the offset may not
            # escape it
            if band_lo is not None:
                y_new = min(max(y_new, band_lo), band_hi)
            if (self.rate_cap and self.y is not None
                    and self.rung >= 3 and self.A is not None):
                # arrest rapid changes: even a band-veto step is corrected over
                # the following timestamps, never as a jump.  The cap is
                # proportional to the model's own slope — when the exponential
                # has gone flat near full, corrections crawl in accordingly.
                # The only floor is a liveness bound: never take longer than
                # ~4 h to cross one quantization step, so the output can still
                # follow genuine drift (rain raising V_top) at a bounded pace.
                # A fire-hose refill jumps multiple steps and resets the
                # episode (snap), bypassing the cap entirely.
                # Only meaningful once a curve exists — at rung 2 the slope is
                # unknown and a floor-capped output would lag a real refill.
                cap = max(2.0 * self._curve_slope(t), STEP / (4 * 3600.0))  # V/s
                dt = max(t - self.last_t, 0.0)
                dy = min(max(y_new - self.y, -cap * dt), cap * dt)
                self.y += dy
            else:
                self.y = y_new
        else:
            blend = 1.0 - math.exp(-(t - self.last_t) / SLEW_TAU)
            self.y += blend * (target - self.y)

        self.last_t = t
        return self.y, self.rung


def main() -> None:
    df = pd.read_csv(CSV, parse_dates=["last_changed"])
    raw = df[df.entity_id.str.contains("pressure_sensor")].copy()
    raw["state"] = pd.to_numeric(raw["state"], errors="coerce")
    raw = raw.dropna(subset=["state"]).sort_values("last_changed").reset_index(drop=True)

    t0 = raw["last_changed"].iloc[0]
    grid = raw.set_index("last_changed")[["state"]].resample("60s").ffill().dropna().reset_index()
    gsec = (grid["last_changed"] - t0).dt.total_seconds()

    # ladder variants: the proposed one is continuity + rate cap
    lad0 = LadderEstimator(continuity=False)
    grid["ladder_slew"] = [lad0.update(t, v)[0] for t, v in zip(gsec, grid["state"])]

    lad1 = LadderEstimator(continuity=True)
    grid["ladder_step"] = [lad1.update(t, v)[0] for t, v in zip(gsec, grid["state"])]

    lad = LadderEstimator(continuity=True, rate_cap=True)
    out = [lad.update(t, v) for t, v in zip(gsec, grid["state"])]
    grid["ladder"] = [o[0] for o in out]
    grid["rung"] = [o[1] for o in out]

    LBL = "ladder: continuity + rate cap (proposed)"

    # event-sampled view: the ladder value only at raw sensor event times
    # (what HA history would actually record if we published on events only)
    ev = pd.merge_asof(
        raw[["last_changed"]], grid[["last_changed", "ladder"]],
        on="last_changed", direction="backward",
    ).dropna()

    # shipped filter (the real code from the integration)
    dec, sm = DutyDecoder(), OutputSmoother()
    shipped = []
    for t, v in zip(gsec, grid["state"]):
        x = dec.update(t, v)
        shipped.append(sm.update(t, x, quiet=t - dec.last_anchor_t))
    grid["shipped"] = shipped

    # ── ground truth: offline revisit-tolerant anchors per segment ──────────
    segs = detect_fill_segments(raw)
    truth_rows = []
    for a, b in segs:
        seg = raw.iloc[a:b + 1].copy().reset_index(drop=True)
        s0 = seg["last_changed"].iloc[0]
        seg["t"] = (seg["last_changed"] - s0).dt.total_seconds()
        anch = extract_anchors(seg)
        for _, r in anch.iterrows():
            truth_rows.append((s0 + pd.Timedelta(seconds=r.t), r.v))
    truth = pd.DataFrame(truth_rows, columns=["t", "v"]).sort_values("t")
    print(f"hindsight anchors: {len(truth)} across {len(segs)} segments")

    # offline double-exponential on the big fill: the hindsight ceiling curve
    big_a, big_b = max(segs, key=lambda ab: ab[1] - ab[0])
    big = raw.iloc[big_a:big_b + 1].copy().reset_index(drop=True)
    big_t0 = big["last_changed"].iloc[0]
    big["t"] = (big["last_changed"] - big_t0).dt.total_seconds()
    big_anch = extract_anchors(big)
    bk1, bk2, bA, bB1, bB2, brms = fit_double_exponential(
        big_anch["t"].to_numpy(), big_anch["v"].to_numpy())
    print(f"offline double-exp (big fill): rms {brms*1000:.1f} mV")
    big_tt = np.linspace(0, big["t"].iloc[-1], 1200)
    offline = pd.DataFrame({
        "t": [big_t0 + pd.Timedelta(seconds=s) for s in big_tt],
        "v": bA - bB1 * np.exp(-bk1 * big_tt) - bB2 * np.exp(-bk2 * big_tt),
    })

    # metrics: strictly per segment, between that segment's own first and last
    # anchor — never interpolate truth across a drawdown
    interp = pd.Series(np.nan, index=grid.index)
    for a, b in segs:
        seg = raw.iloc[a:b + 1]
        s0, s1 = seg["last_changed"].iloc[0], seg["last_changed"].iloc[-1]
        tr = truth[(truth.t >= s0) & (truth.t <= s1)]
        if len(tr) < 2:
            continue
        tv = tr.set_index("t")["v"]
        tv = tv[~tv.index.duplicated()]
        in_span = ((grid.last_changed >= tv.index.min())
                   & (grid.last_changed <= tv.index.max()))
        gi = grid.loc[in_span, "last_changed"]
        vals = (tv.reindex(tv.index.union(gi))
                  .interpolate(method="time")
                  .reindex(gi))
        interp.loc[in_span] = vals.to_numpy()
    mask = ~interp.isna().to_numpy()
    names = {
        "ladder": "continuity + rate cap (proposed)",
        "ladder_step": "continuity, no cap",
        "ladder_slew": "slew chase (bulb)",
        "shipped": "shipped duty decoder",
    }
    for col, name in names.items():
        err = grid[col].to_numpy()[mask] - interp.to_numpy()[mask]
        print(f"{name:>34}: rms {1000*float((err**2).mean())**0.5:6.1f} mV, "
              f"max {1000*np.abs(err).max():6.1f} mV")

    print("\nevents:")
    for te, msg in lad.events:
        print(f"  {t0 + pd.Timedelta(seconds=te):%d %H:%M}  {msg}")

    # ── plots ────────────────────────────────────────────────────────────────
    views = [
        ("full", None, None),
        ("busy_day", pd.Timestamp("2026-07-25T15:00:00Z"), pd.Timestamp("2026-07-26T14:00:00Z")),
        ("near_full", pd.Timestamp("2026-07-28T00:00:00Z"), pd.Timestamp("2026-07-30T10:00:00Z")),
        ("quiet_zone", pd.Timestamp("2026-07-29T08:00:00Z"), pd.Timestamp("2026-07-30T06:00:00Z")),
    ]
    # ── dedicated bulb comparison: tight zoom on the midnight correction ────
    lo_b = pd.Timestamp("2026-07-29T20:00:00Z")
    hi_b = pd.Timestamp("2026-07-30T04:00:00Z")
    gb = grid[(grid.last_changed >= lo_b) & (grid.last_changed <= hi_b)]
    rb = raw[(raw.last_changed >= lo_b) & (raw.last_changed <= hi_b)]
    eb = ev[(ev.last_changed >= lo_b) & (ev.last_changed <= hi_b)]
    fig, ax = plt.subplots(figsize=(14, 6))
    ax.step(rb.last_changed, rb.state, where="post", color="#dddddd", lw=1,
            label="raw (quantized)")
    ax.plot(gb.last_changed, gb["ladder_slew"], color="#e6a23c", lw=1.4,
            label="ladder: slew chase (the 'bulb')")
    ax.plot(gb.last_changed, gb["ladder_step"], color="#7cb342", lw=1.4,
            label="ladder: continuity, no cap (the 'step')")
    ax.plot(gb.last_changed, gb["ladder"], color="#c0392b", lw=2.0,
            label=LBL)
    ax.set_ylabel("voltage (V)")
    ax.set_ylim(8.072, 8.092)
    ax.set_title("The midnight correction — bulb vs step vs rate-capped ramp")
    ax.legend(loc="lower right", fontsize=9)
    ax.grid(alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %H:%M"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(HERE / "ladder_bulb_zoom.png", dpi=110)
    print(f"saved {HERE / 'ladder_bulb_zoom.png'}")

    for name, lo_t, hi_t in views:
        g, r, tr = grid, raw, truth
        if lo_t is not None:
            g = grid[(grid.last_changed >= lo_t) & (grid.last_changed <= hi_t)]
            r = raw[(raw.last_changed >= lo_t) & (raw.last_changed <= hi_t)]
            tr = truth[(truth.t >= lo_t) & (truth.t <= hi_t)]
        fig, (ax, axr) = plt.subplots(
            2, 1, figsize=(15, 8), sharex=True,
            gridspec_kw={"height_ratios": [5, 1]},
        )
        ax.step(r.last_changed, r.state, where="post", color="#bbbbbb", lw=1,
                label="raw (quantized)")
        ax.plot(g.last_changed, g["shipped"], color="#7cb342", lw=1.2, alpha=0.85,
                label="shipped duty decoder (production)")
        ax.plot(g.last_changed, g["ladder"], color="#c0392b", lw=1.8,
                label=LBL)
        off = offline
        if lo_t is not None:
            off = offline[(offline.t >= lo_t) & (offline.t <= hi_t)]
        ax.plot(off.t, off.v, "--", color="#2c5f8a", lw=1.6,
                label="offline double-exp fit (hindsight ceiling)")
        ax.plot(tr.t, tr.v, "o", color="#333333", ms=4, mfc="white", zorder=5,
                label="hindsight anchors (truth)")
        ax.set_ylabel("voltage (V)")
        ax.set_title(f"Evidence-ladder simulation — {name}")
        ax.legend(loc="lower right", fontsize=9)
        ax.grid(alpha=0.3)

        axr.step(g.last_changed, g["rung"], where="post", color="#2c5f8a", lw=1.5)
        axr.set_ylim(0.5, 4.5)
        axr.set_yticks([1, 2, 3, 4])
        axr.set_ylabel("rung")
        axr.grid(alpha=0.3)
        axr.xaxis.set_major_formatter(mdates.DateFormatter("%d %H:%M"))

        fig.autofmt_xdate()
        fig.tight_layout()
        outp = HERE / f"ladder_{name}.png"
        fig.savefig(outp, dpi=110)
        print(f"saved {outp}")


if __name__ == "__main__":
    main()
