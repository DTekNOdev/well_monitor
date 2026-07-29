"""PWM duty-cycle decoder for the quantized well level signal.

The depth transducer's voltage is quantized in ~0.02-0.03 V steps. While the
level sits between two steps the reading toggles between the two adjacent
quantization levels; the fraction of time spent at the upper level encodes the
true sub-step level — exactly a PWM duty cycle. The decoder tracks the active
adjacent-level pair (lo, hi) and a time-weighted duty estimate:

    output = lo + duty * (hi - lo)

Regime handling:
  * toggle within the pair          -> update duty, smooth interpolation
  * step up to the next level       -> re-anchor pair (old hi becomes lo),
                                       duty seeds at 0 -> output continuous
  * step down one level             -> re-anchor pair downward with a
                                       continuity-preserving duty seed
  * jump > 1 step (real usage/draw) -> snap to raw immediately

While filling (last pair move was upward) the duty estimate is ratcheted —
it may only rise. The level physically cannot retreat during a fill, so a
long dwell at lo right after a blip to hi means "barely past the boundary",
not "receding". The ratchet releases on a snap or on a second consecutive
downward pair move (a single one-step dip is treated as a stray excursion).

An OutputSmoother sits on the decoder output: a light EMA whose time constant
grows while the pair is inactive (static level -> progressively flatter
output) and drops to a fast tau the moment the output runs away by more than
about one quantization step (draw-down stays responsive).

Algorithm developed and tuned against recorded data in
well_monitor/analysis/sim_duty_decode.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Max gap between two quantization levels still considered "adjacent".
# Observed sensor steps alternate 0.02 / 0.03 V.
ADJACENT_MAX = 0.035


@dataclass
class DutyDecoder:
    """Decode the sub-step level from the toggle duty cycle."""

    tau: float = 900.0            # s — duty relaxation time constant
    adjacent_max: float = ADJACENT_MAX

    lo: float | None = None
    hi: float | None = None
    duty: float = 0.0
    last_t: float | None = None
    last_raw: float | None = None
    last_anchor_t: float = 0.0    # when the pair last moved (activity signal)
    direction: str | None = None  # 'up' while filling — enables the ratchet
    pending_down: bool = False    # first down-move is provisional (stray dip)

    def output(self) -> float | None:
        if self.lo is None:
            return None
        if self.hi is None or self.hi == self.lo:
            return self.lo
        return self.lo + self.duty * (self.hi - self.lo)

    def update(self, t: float, v: float) -> float:
        if self.last_t is None:
            self.lo, self.hi = v, v
            self.duty = 0.0
            self.last_t, self.last_raw = t, v
            self.last_anchor_t = t
            return v

        dt = max(t - self.last_t, 0.0)

        # 1) Integrate the dwell of the PREVIOUS value over dt (zero-order hold)
        if self.hi is not None and self.lo is not None and self.hi > self.lo:
            ind = 1.0 if self.last_raw >= self.hi else 0.0
            alpha = 1.0 - math.exp(-dt / self.tau)
            new_duty = self.duty + alpha * (ind - self.duty)
            if self.direction == "up":
                new_duty = max(new_duty, self.duty)  # fill ratchet
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
            elif v != lo:
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
            prev_out = self.output() or v
            if lo - v <= self.adjacent_max:
                # step down: seed duty so output stays continuous
                self.lo, self.hi = v, lo
                span = self.hi - self.lo
                self.duty = min(max((prev_out - self.lo) / span, 0.0), 1.0) if span > 0 else 0.0
                if self.direction == "up" and not self.pending_down:
                    self.pending_down = True   # provisional — likely stray dip
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
        out = self.output()
        return out if out is not None else v

    # ── Persistence ───────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "lo": self.lo, "hi": self.hi, "duty": self.duty,
            "last_t": self.last_t, "last_raw": self.last_raw,
            "last_anchor_t": self.last_anchor_t,
            "direction": self.direction, "pending_down": self.pending_down,
        }

    def restore(self, state: dict) -> None:
        for key, value in state.items():
            if hasattr(self, key):
                setattr(self, key, value)


@dataclass
class OutputSmoother:
    """Adaptive EMA on the decoder output (see module docstring)."""

    tau_slow: float = 360.0      # s — base smoothing while the level is moving
    tau_fast: float = 90.0       # s — fast follow when output runs away
    fast_gap: float = 0.025      # V — switch to fast follow beyond this gap
    quiet_ramp: float = 1800.0   # s of pair inactivity before tau grows
    quiet_max_scale: float = 5.0 # cap: tau_slow grows to 5x when fully static

    value: float | None = None
    last_t: float | None = None

    def update(self, t: float, x: float, quiet: float = 0.0) -> float:
        """quiet = seconds since the decoder pair last moved."""
        if self.value is None:
            self.value, self.last_t = x, t
            return x
        dt = max(t - (self.last_t or t), 0.0)
        if abs(x - self.value) > self.fast_gap:
            tau = self.tau_fast
        else:
            scale = min(max(quiet / self.quiet_ramp, 1.0), self.quiet_max_scale)
            tau = self.tau_slow * scale
        alpha = 1.0 - math.exp(-dt / tau)
        self.value += alpha * (x - self.value)
        self.last_t = t
        return self.value

    # ── Persistence ───────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {"value": self.value, "last_t": self.last_t}

    def restore(self, state: dict) -> None:
        for key, value in state.items():
            if hasattr(self, key):
                setattr(self, key, value)
