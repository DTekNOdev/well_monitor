"""Simulate a PWM duty-cycle decoder for the well level signal.

The raw voltage is quantized in ~0.02-0.03 V steps. While the well slowly
fills, the reading toggles between two adjacent quantization levels; the
fraction of time spent at the upper level encodes the true sub-step level —
exactly a PWM duty cycle. Decoding it:

    output = lo + duty * (hi - lo)

where (lo, hi) is the currently active adjacent-level pair and duty is a
time-weighted estimate of the dwell fraction at hi (zero-order-hold EMA).

Regime handling:
  * toggle within the pair          -> update duty, smooth interpolation
  * step up to the next level       -> re-anchor pair (old hi becomes lo),
                                       duty seeds at 0 -> output continuous
  * step down one level             -> re-anchor pair downward, duty seeds
                                       for continuity, fast tau pulls down
  * jump > 1 step (real usage/draw) -> snap to raw immediately

Run:  python analysis/sim_duty_decode.py
Outputs PNGs next to this script.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd

HERE = Path(__file__).parent
CSV = HERE.parent / "well_monitor_occupancy_comparison.csv"

# Max gap between two quantization levels still considered "adjacent".
# Steps observed in the data alternate 0.02 / 0.03 V.
ADJACENT_MAX = 0.035


@dataclass
class DutyDecoder:
    tau_up: float = 900.0     # s — duty relaxation while evidence pushes up
    tau_down: float = 900.0   # s — in-pair smoothing is symmetric; fast
                              # down-tracking comes from pair re-anchor/snap
    adjacent_max: float = ADJACENT_MAX
    ratchet: bool = False     # while filling, duty may only rise (see below)

    lo: float | None = None
    hi: float | None = None
    duty: float = 0.0
    last_t: float | None = None
    last_raw: float | None = None
    last_anchor_t: float = 0.0   # when the pair last moved (activity signal)
    direction: str | None = None # 'up' while filling — enables the ratchet
    pending_down: bool = False   # first down-move is provisional (stray dip)

    def output(self) -> float:
        if self.lo is None:
            return float("nan")
        if self.hi is None or self.hi == self.lo:
            return self.lo
        return self.lo + self.duty * (self.hi - self.lo)

    def update(self, t: float, v: float) -> float:
        if self.last_t is None:
            self.lo, self.hi = v, v
            self.duty = 0.0
            self.last_t, self.last_raw = t, v
            self.last_anchor_t = t
            return self.output()

        dt = max(t - self.last_t, 0.0)

        # 1) Integrate the dwell of the PREVIOUS value over dt (zero-order hold)
        if self.hi is not None and self.lo is not None and self.hi > self.lo:
            ind = 1.0 if self.last_raw >= self.hi else 0.0
            tau = self.tau_up if ind > self.duty else self.tau_down
            alpha = 1.0 - math.exp(-dt / tau)
            new_duty = self.duty + alpha * (ind - self.duty)
            if self.ratchet and self.direction == "up":
                # Filling: the level physically cannot retreat, so dwell at lo
                # right after a blip to hi means "barely past the boundary",
                # not "receding" — duty may only rise.
                new_duty = max(new_duty, self.duty)
            self.duty = new_duty

        # 2) Classify the new sample against the active pair
        lo, hi = self.lo, self.hi
        if lo is None:
            self.lo = self.hi = v
        elif hi == lo:  # single anchored level so far
            if abs(v - lo) <= self.adjacent_max:
                if v > lo:
                    self.lo, self.hi, self.duty = lo, v, 0.0
                    self.direction, self.pending_down = "up", False
                elif v < lo:
                    self.lo, self.hi, self.duty = v, lo, 1.0
                    self.direction = "down"
            else:
                self.lo = self.hi = v  # jump: snap
                self.duty = 0.0
                self.direction = "up" if v > lo else "down"
                self.pending_down = False
        elif v > hi:
            if v - hi <= self.adjacent_max:
                # advance one step up: old hi becomes the new floor
                self.lo, self.hi, self.duty = hi, v, 0.0
            else:
                self.lo = self.hi = v  # big jump up: snap
                self.duty = 0.0
            self.direction, self.pending_down = "up", False
        elif v < lo:
            prev_out = self.output()
            if lo - v <= self.adjacent_max:
                # step down: seed duty so output stays continuous, then the
                # fast tau_down pulls it toward the new level as it dwells
                self.lo, self.hi = v, lo
                span = self.hi - self.lo
                self.duty = min(max((prev_out - self.lo) / span, 0.0), 1.0) if span > 0 else 0.0
                # First single-step dip during a fill is provisional — likely a
                # stray excursion, keep the ratchet armed. A second one is real.
                if self.direction == "up" and not self.pending_down:
                    self.pending_down = True
                else:
                    self.direction, self.pending_down = "down", False
            else:
                self.lo = self.hi = v  # big drop (real usage): snap
                self.duty = 0.0
                self.direction, self.pending_down = "down", False
        # v == lo or v == hi -> stay within pair, nothing to re-anchor

        if (self.lo, self.hi) != (lo, hi):
            self.last_anchor_t = t
        self.last_t, self.last_raw = t, v
        return self.output()


def total_variation(s: pd.Series) -> float:
    return s.diff().abs().sum()


@dataclass
class CycleDutyDecoder:
    """True PWM decode: duty updates once per completed toggle cycle.

    At each rising edge (lo->hi) the just-completed cycle is measured:
    duty_meas = dwell_at_hi / (dwell_at_hi + dwell_at_lo), blended into the
    duty estimate. Between edges the output holds — a static dithering level
    gives a flat line instead of ripple.

    A dwell timeout handles parking at one level: if the signal sits still
    longer than DWELL_TIMEOUT the duty relaxes toward that level, so a long
    flat stretch drifts smoothly to the quantized value.

    Pair re-anchoring and jump-snapping are identical to DutyDecoder.
    """
    tau_cycle: float = 1800.0     # s — blending horizon across cycles
    tau_dwell: float = 1800.0     # s — relaxation during a long one-level dwell
    dwell_timeout: float = 1800.0 # s — dwell longer than this = "parked"
    adjacent_max: float = ADJACENT_MAX

    lo: float | None = None
    hi: float | None = None
    duty: float = 0.0
    last_t: float | None = None
    last_raw: float | None = None
    level_entered_t: float | None = None
    prev_hi_dwell: float | None = None   # dwell of the last completed hi phase

    def output(self) -> float:
        if self.lo is None:
            return float("nan")
        if self.hi is None or self.hi == self.lo:
            return self.lo
        return self.lo + self.duty * (self.hi - self.lo)

    def _reset_cycle(self, t: float) -> None:
        self.level_entered_t = t
        self.prev_hi_dwell = None

    def update(self, t: float, v: float) -> float:
        if self.last_t is None:
            self.lo = self.hi = v
            self.duty = 0.0
            self.last_t, self.last_raw = t, v
            self._reset_cycle(t)
            return self.output()

        in_pair = (
            self.hi is not None and self.lo is not None and self.hi > self.lo
            and v in (self.lo, self.hi)
        )

        if in_pair and v != self.last_raw:
            # Edge within the pair: the level we just left dwelt this long
            dwell = t - (self.level_entered_t or t)
            if v == self.hi:
                # rising edge: cycle completed if we have the previous hi dwell
                if self.prev_hi_dwell is not None and dwell > 0:
                    period = self.prev_hi_dwell + dwell
                    duty_meas = self.prev_hi_dwell / period
                    alpha = 1.0 - math.exp(-period / self.tau_cycle)
                    self.duty += alpha * (duty_meas - self.duty)
            else:
                # falling edge: remember the hi dwell for the next rising edge
                self.prev_hi_dwell = dwell
            self.level_entered_t = t
        elif in_pair and v == self.last_raw:
            # No edge — check for a long park at one level (needs the tick)
            dwell = t - (self.level_entered_t or t)
            if dwell > self.dwell_timeout:
                ind = 1.0 if v >= self.hi else 0.0
                dt = max(t - self.last_t, 0.0)
                alpha = 1.0 - math.exp(-dt / self.tau_dwell)
                self.duty += alpha * (ind - self.duty)
        elif self.lo is not None:
            lo, hi = self.lo, self.hi
            if hi == lo:
                if abs(v - lo) <= self.adjacent_max and v != lo:
                    if v > lo:
                        self.lo, self.hi, self.duty = lo, v, 0.0
                    else:
                        self.lo, self.hi, self.duty = v, lo, 1.0
                    self._reset_cycle(t)
                elif v != lo:
                    self.lo = self.hi = v
                    self.duty = 0.0
                    self._reset_cycle(t)
            elif v > hi:
                if v - hi <= self.adjacent_max:
                    self.lo, self.hi, self.duty = hi, v, 0.0
                else:
                    self.lo = self.hi = v
                    self.duty = 0.0
                self._reset_cycle(t)
            elif v < lo:
                prev_out = self.output()
                if lo - v <= self.adjacent_max:
                    self.lo, self.hi = v, lo
                    span = self.hi - self.lo
                    self.duty = min(max((prev_out - self.lo) / span, 0.0), 1.0) if span else 0.0
                else:
                    self.lo = self.hi = v
                    self.duty = 0.0
                self._reset_cycle(t)

        self.last_t, self.last_raw = t, v
        return self.output()


@dataclass
class OutputSmoother:
    """Light secondary EMA on the decoder output.

    Symmetric slow smoothing removes the duty-estimate ripple; when the
    decoder output runs away by more than about one quantization step
    (draw-down, snap) the fast tau takes over so tracking stays responsive.
    """
    tau_slow: float = 360.0
    tau_fast: float = 90.0
    fast_gap: float = 0.025   # V — switch to fast follow beyond this gap

    quiet_ramp: float = 1800.0   # s of pair inactivity before tau starts growing
    quiet_max_scale: float = 5.0 # cap: tau_slow can grow to 5x when fully static

    value: float | None = None
    last_t: float | None = None

    def update(self, t: float, x: float, quiet: float = 0.0) -> float:
        """quiet = seconds since the decoder pair last moved. A static level
        earns progressively heavier smoothing; any pair advance resets it."""
        if self.value is None:
            self.value, self.last_t = x, t
            return x
        dt = max(t - self.last_t, 0.0)
        if abs(x - self.value) > self.fast_gap:
            tau = self.tau_fast
        else:
            scale = min(max(quiet / self.quiet_ramp, 1.0), self.quiet_max_scale)
            tau = self.tau_slow * scale
        alpha = 1.0 - math.exp(-dt / tau)
        self.value += alpha * (x - self.value)
        self.last_t = t
        return self.value


def main() -> None:
    df = pd.read_csv(CSV, parse_dates=["timestamp_utc"])
    t0 = df["timestamp_utc"].iloc[0]

    # 1-minute zero-order-hold grid: models an HA implementation that also
    # refreshes the published value on a periodic tick, not just on events.
    # (All source timestamps are minute-aligned, so no event is lost.)
    grid = df.set_index("timestamp_utc").resample("60s").ffill().reset_index()
    gsec = (grid["timestamp_utc"] - t0).dt.total_seconds()

    dec = DutyDecoder()
    sm = OutputSmoother()
    duty_raw, duty_smooth = [], []
    for t, v in zip(gsec, grid["raw"]):
        x = dec.update(t, v)
        duty_raw.append(x)
        duty_smooth.append(sm.update(t, x))  # fixed tau (quiet not passed)
    grid["duty_decode"] = duty_raw
    grid["duty_smooth"] = duty_smooth

    dec2 = DutyDecoder()
    sm3 = OutputSmoother()
    adaptive = []
    for t, v in zip(gsec, grid["raw"]):
        x = dec2.update(t, v)
        adaptive.append(sm3.update(t, x, quiet=t - dec2.last_anchor_t))
    grid["duty_adaptive"] = adaptive

    dec3 = DutyDecoder(ratchet=True)
    sm4 = OutputSmoother()
    ratchet = []
    for t, v in zip(gsec, grid["raw"]):
        x = dec3.update(t, v)
        ratchet.append(sm4.update(t, x, quiet=t - dec3.last_anchor_t))
    grid["duty_ratchet"] = ratchet
    df = grid

    # ── Metrics ──────────────────────────────────────────────────────────────
    # Fill segment: 26 Jul 12:00 – 22:00 (steady dithering climb)
    fill = df[(df.timestamp_utc >= "2026-07-26T12:00:00+00:00")
              & (df.timestamp_utc <= "2026-07-26T22:00:00+00:00")]
    # Draw-down: 26 Jul 06:45 – 07:45 (rapid emptying)
    draw = df[(df.timestamp_utc >= "2026-07-26T06:40:00+00:00")
              & (df.timestamp_utc <= "2026-07-26T07:45:00+00:00")]

    # Static tail: 28 Jul 11:30 – 16:30 — well essentially full, level static;
    # ideal output here is a flat line (TV ~ 0)
    tail = df[(df.timestamp_utc >= "2026-07-28T11:30:00+00:00")
              & (df.timestamp_utc <= "2026-07-28T16:30:00+00:00")]

    # Sag: total downward movement during the long monotone fill (27–28 Jul);
    # the level never falls here, so the ideal is 0
    mono = df[df.timestamp_utc >= "2026-07-27T00:00:00+00:00"]

    print(f"{'column':<22}{'fill rough':>11}{'draw max err':>14}{'tail TV (V)':>13}{'fill sag (V)':>14}")
    for col in ["raw", "ema_tau300", "occ_asym_f60_r600",
                "duty_smooth", "duty_adaptive", "duty_ratchet"]:
        tv = total_variation(fill[col])
        net = fill[col].iloc[-1] - fill[col].iloc[0]
        rough = tv / abs(net) if net else float("nan")
        err = (draw[col] - draw["raw"]).abs().max()
        tail_tv = total_variation(tail[col])
        sag = mono[col].diff().clip(upper=0).abs().sum()
        print(f"{col:<22}{rough:>11.2f}{err:>14.4f}{tail_tv:>13.4f}{sag:>14.4f}")

    # ── Plots ────────────────────────────────────────────────────────────────
    views = [
        ("full", None, None),
        ("fill_zoom", "2026-07-26T12:00:00+00:00", "2026-07-26T22:00:00+00:00"),
        ("drawdown_zoom", "2026-07-26T06:30:00+00:00", "2026-07-26T08:30:00+00:00"),
        ("slow_fill_zoom", "2026-07-27T14:00:00+00:00", "2026-07-27T20:00:00+00:00"),
        ("tail_zoom", "2026-07-28T04:00:00+00:00", "2026-07-28T23:59:00+00:00"),
        ("uptick_zoom", "2026-07-28T18:30:00+00:00", "2026-07-28T22:30:00+00:00"),
    ]
    for name, lo_t, hi_t in views:
        d = df
        if lo_t:
            d = df[(df.timestamp_utc >= lo_t) & (df.timestamp_utc <= hi_t)]
        fig, ax = plt.subplots(figsize=(14, 6))
        ax.step(d.timestamp_utc, d["raw"], where="post", color="#bbbbbb",
                lw=1, label="raw (quantized)")
        ax.plot(d.timestamp_utc, d["ema_tau300"], color="#e6a23c", lw=1.2,
                alpha=0.9, label="ema tau=300 (current)")
        ax.plot(d.timestamp_utc, d["duty_adaptive"], color="#2c5f8a", lw=1.2,
                alpha=0.7, label="adaptive duty decode (previous round)")
        ax.plot(d.timestamp_utc, d["duty_ratchet"], color="#c0392b", lw=1.8,
                label="adaptive duty decode + fill ratchet (new)")
        ax.set_title(f"Well level — duty-cycle decoder vs previous attempts ({name})")
        ax.set_ylabel("voltage (V)")
        ax.legend(loc="best")
        ax.grid(alpha=0.3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %H:%M"))
        fig.autofmt_xdate()
        fig.tight_layout()
        out = HERE / f"duty_decode_{name}.png"
        fig.savefig(out, dpi=110)
        print(f"saved {out}")


if __name__ == "__main__":
    main()
