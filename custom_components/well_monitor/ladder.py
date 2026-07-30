"""Model-based level estimator — the evidence ladder.

Treats the quantized pressure reading as evidence about a physical process
rather than a signal to smooth.  The well's refill follows a double
exponential (fast borehole storage + slow aquifer inflow); quiet-zone
anchors — the centre of each quantization level's flat period, where the
sensor noise amplitude cancels — are exact calibration points on the true
curve.  The estimate uses as much evidence as the current fill episode has
produced (design: docs/model_estimator_design.md):

  rung 4  >= 6 anchors   double exponential, shared taus, constrained
                         amplitudes refit on every new anchor
  rung 3  2..5 anchors   fast-component-only fit
  rung 2  0..1 anchors   hold the current level
  rung 1  draining       track raw (multi-step drop = real usage)

Output shaping:
  * continuity — a refit or rung change never steps the output; the jump is
    captured into an offset that decays over a horizon scaled to the
    predicted time to the next anchor.
  * rate cap — the published value may never move faster than 2x the model's
    own slope, floored at one quantization step per 4 h (liveness), so even
    a band-veto correction lands as a gentle ramp.  Drains and multi-step
    jumps bypass the cap and snap.
  * band veto — the target is always clamped to the band the readings allow
    (threshold-centred, derived from the actual local level pair); band
    violations feed back into the curve so the model absorbs the evidence.

Validated against recorded data in analysis/sim_ladder.py; this module is a
dependency-free port (no numpy) of the `continuity + rate cap` variant.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

_LOGGER = logging.getLogger(__name__)

# Reference recharge time constants, fitted from the 2026-07 capture
# (analysis/fit_fill_curve.py).  Well properties; refit from long fills in a
# later phase.
TAU_FAST_H = 5.2
TAU_SLOW_H = 27.2
K1 = 1.0 / (TAU_FAST_H * 3600.0)
K2 = 1.0 / (TAU_SLOW_H * 3600.0)

STEP = 0.025            # nominal quantization step (V)
STEP_MAX = 0.035        # max gap between adjacent quantization levels
DROP_THRESHOLD = 0.05   # a drop this far below the ladder top = real usage
DEMOTE_AFTER = 4 * 3600.0   # top unseen this long during dips => slow drain
DITHER_WINDOW = 2700.0  # s — lower level seen this recently => dithering
BAND_HALF = 0.012       # V — dithering band half-width around the threshold
BAND_CORR_TAU = 1800.0  # s — band-violation feedback into the curve
OFFSET_TAU_MAX = 5400.0 # s — max continuity-offset decay
RUNG4_MIN_ANCHORS = 6
RUNG3_MIN_ANCHORS = 2
HEALTH_ERR = 0.015      # V — anchor prediction error worth logging
STALL_FACTOR = 2.5      # overdue factor on the predicted next crossing
RATE_CAP_SLOPE_MULT = 2.0
RATE_CAP_FLOOR = STEP / (4 * 3600.0)   # V/s — one step per 4 h liveness bound
MAX_ANCHORS = 200       # episode safety cap


def _key(v: float) -> int:
    return round(v * 1000)


def _solve_normal(cols: list, y: list) -> "list | None":
    """Least squares via normal equations for 2 or 3 columns, pure Python."""
    n = len(cols)
    ata = [[sum(a * b for a, b in zip(cols[i], cols[j])) for j in range(n)]
           for i in range(n)]
    aty = [sum(a * b for a, b in zip(cols[i], y)) for i in range(n)]
    # gaussian elimination with partial pivoting
    m = [row[:] + [rhs] for row, rhs in zip(ata, aty)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-30:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def _fit_double(te: list, v: list) -> "tuple | None":
    """A - B1 e^{-K1 t} - B2 e^{-K2 t}, with B1, B2 >= 0 (tiny active set)."""
    ones = [1.0] * len(te)
    e1 = [math.exp(-K1 * t) for t in te]
    e2 = [math.exp(-K2 * t) for t in te]
    sol = _solve_normal([ones, e1, e2], v)
    if sol is not None:
        a, nb1, nb2 = sol
        if -nb1 >= 0 and -nb2 >= 0:
            return a, -nb1, -nb2, _rms(v, a, -nb1, -nb2, e1, e2)
    candidates = []
    for basis, is_fast in ((e1, True), (e2, False)):
        sol = _solve_normal([ones, basis], v)
        if sol is None:
            continue
        a, nb = sol
        if -nb >= 0:
            b1, b2 = (-nb, 0.0) if is_fast else (0.0, -nb)
            candidates.append((_rms(v, a, b1, b2, e1, e2), a, b1, b2))
    if not candidates:
        a = sum(v) / len(v)
        return a, 0.0, 0.0, _rms(v, a, 0.0, 0.0, e1, e2)
    r, a, b1, b2 = min(candidates)
    return a, b1, b2, r


def _fit_fast_only(te: list, v: list) -> "tuple | None":
    ones = [1.0] * len(te)
    e1 = [math.exp(-K1 * t) for t in te]
    e2 = [math.exp(-K2 * t) for t in te]
    sol = _solve_normal([ones, e1], v)
    if sol is None:
        return None
    a, nb = sol
    b1 = -nb
    if b1 < 0:
        a, b1 = sum(v) / len(v), 0.0
    return a, b1, 0.0, _rms(v, a, b1, 0.0, e1, e2)


def _rms(v: list, a: float, b1: float, b2: float, e1: list, e2: list) -> float:
    sse = sum((vv - (a - b1 * x1 - b2 * x2)) ** 2
              for vv, x1, x2 in zip(v, e1, e2))
    return math.sqrt(sse / len(v))


@dataclass
class LadderEstimator:
    """Causal evidence-ladder estimator (continuity + rate cap variant)."""

    # ladder / episode state
    last_seen: dict = field(default_factory=dict)   # level_key -> last time
    top: float | None = None
    episode_t0: float | None = None
    anchors_t: list = field(default_factory=list)   # absolute seconds
    anchors_v: list = field(default_factory=list)

    # fitted curve (amplitudes on the episode clock)
    A: float | None = None
    B1: float = 0.0
    B2: float = 0.0
    fit_rms: float = 0.0
    frozen: bool = False
    last_anchor_reg_t: float | None = None
    pred_next_dt: float | None = None

    # continuity offset
    offset: float = 0.0
    offset_t: float | None = None
    offset_tau: float = OFFSET_TAU_MAX
    _family_ver: int = 0
    _seen_ver: int = 0

    rung: int = 2
    y: float | None = None
    last_t: float | None = None

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
        self.offset = 0.0
        self._family_ver += 1
        self._seen_ver = self._family_ver

    def _curve(self, t: float) -> float | None:
        if self.A is None or self.episode_t0 is None:
            return None
        te = t - self.episode_t0
        return self.A - self.B1 * math.exp(-K1 * te) - self.B2 * math.exp(-K2 * te)

    def _curve_slope(self, t: float) -> float:
        if self.A is None or self.episode_t0 is None:
            return 0.0
        te = t - self.episode_t0
        return (self.B1 * K1 * math.exp(-K1 * te)
                + self.B2 * K2 * math.exp(-K2 * te))

    def _refit(self, t: float) -> None:
        self._family_ver += 1
        te = [at - self.episode_t0 for at in self.anchors_t]
        vv = list(self.anchors_v)
        n = len(te)
        fit = None
        if n >= RUNG4_MIN_ANCHORS:
            fit = _fit_double(te, vv)
            self.rung = 4
        elif n >= RUNG3_MIN_ANCHORS:
            fit = _fit_fast_only(te, vv)
            self.rung = 3
        if fit is None:
            self.A = None
            self.rung = 2
            return
        self.A, self.B1, self.B2, self.fit_rms = fit
        # predicted time until the curve reaches the next crossing
        self.pred_next_dt = None
        if self.top is None:
            return
        target = self.top + STEP / 2
        x_now = self._curve(t)
        if x_now is not None and self.A is not None and self.A > target > x_now:
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
        pred = self._curve(t_mid)
        if pred is not None and self.rung >= 3:
            err = level - pred
            if abs(err) > max(HEALTH_ERR, 2.5 * self.fit_rms):
                _LOGGER.info(
                    "Well model: anchor %.2f V off-curve by %+.0f mV — refit",
                    level, err * 1000,
                )
        self.anchors_t.append(t_mid)
        self.anchors_v.append(level)
        if len(self.anchors_t) > MAX_ANCHORS:
            self.anchors_t = self.anchors_t[-MAX_ANCHORS:]
            self.anchors_v = self.anchors_v[-MAX_ANCHORS:]
        self.frozen = False
        self.last_anchor_reg_t = t_now
        self._refit(t_now)

    # ── main update (call on every event AND on the periodic tick) ──────────

    def update(self, t: float, v: float) -> float:
        if self.last_t is None:
            self._new_episode(t, v)
            self.y = v
            self.last_t = t
            return self.y

        k = _key(v)
        if self.top is not None and self.top - v > DROP_THRESHOLD:
            _LOGGER.info("Well model: drawdown %.2f → %.2f V — tracking raw",
                         self.top, v)
            self._new_episode(t, v)
            self.rung = 1
            self.y = v
        elif self.top is not None and v - self.top > STEP_MAX:
            _LOGGER.info("Well model: upward jump %.2f → %.2f V — reset",
                         self.top, v)
            self._new_episode(t, v)
            self.y = v
        elif self.top is not None and v > self.top:
            # fill progressed one level: anchor the old top at the centre of
            # its quiet zone (noise amplitude cancels at that midpoint)
            old_top = self.top
            below_lvls = [kk / 1000 for kk in self.last_seen
                          if 0 < old_top - kk / 1000 <= STEP_MAX]
            self.last_seen[k] = t
            self.top = v
            if below_lvls:
                t_last_below = self.last_seen[_key(max(below_lvls))]
                self._register_anchor((t_last_below + t) / 2.0, old_top, t)
            if self.rung == 1:
                self.rung = 2
        else:
            self.last_seen[k] = t
            if (self.top is not None and v < self.top
                    and t - self.last_seen.get(_key(self.top), t) > DEMOTE_AFTER):
                self.top = v    # slow drain: demote quietly

        # stall: predicted next crossing long overdue → freeze the curve
        if (not self.frozen and self.rung >= 3 and self.pred_next_dt is not None
                and self.last_anchor_reg_t is not None
                and t - self.last_anchor_reg_t
                    > STALL_FACTOR * max(self.pred_next_dt, 1800.0)):
            self.frozen = True
            self._family_ver += 1
            _LOGGER.warning(
                "Well model: fill stalled — next crossing overdue "
                "(predicted %.1f h, waited %.1f h); curve frozen",
                self.pred_next_dt / 3600, (t - self.last_anchor_reg_t) / 3600,
            )

        # target from the active rung
        if self.rung >= 3 and self.A is not None and not self.frozen:
            target = self._curve(t)
        else:
            target = self.top if self.top is not None else v

        # band from the actual local pair — the readings' veto
        band_lo = band_hi = None
        if self.top is not None:
            below_lvls = [kk / 1000 for kk in self.last_seen
                          if 0 < self.top - kk / 1000 <= STEP_MAX]
            if below_lvls:
                below = max(below_lvls)
                s = self.top - below
                m = (self.top + below) / 2.0
                if t - self.last_seen[_key(below)] < DITHER_WINDOW:
                    band_lo, band_hi = m - BAND_HALF, m + BAND_HALF
                else:
                    band_lo, band_hi = m, self.top + s / 2
            else:
                band_lo = self.top - 0.0175
                band_hi = self.top + 0.0175
        if band_lo is not None:
            # a band violation is an innovation — correct the curve so the
            # model absorbs the evidence and stays consistent going forward
            if self.rung >= 3 and self.A is not None and not self.frozen:
                clipped = min(max(target, band_lo), band_hi)
                violation = clipped - target
                if violation != 0.0:
                    gain = 1.0 - math.exp(-(t - self.last_t) / BAND_CORR_TAU)
                    self.A += gain * violation
                    target = self._curve(t)
            target = min(max(target, band_lo), band_hi)

        if self.rung == 1:
            self.y = v
            self.offset = 0.0
            self._seen_ver = self._family_ver
        else:
            # continuity: a refit / rung change becomes a decaying offset —
            # corrections manifest as slope, never as a step
            if self._seen_ver != self._family_ver:
                self.offset = (self.y if self.y is not None else target) - target
                self.offset_t = t
                horizon = self.pred_next_dt if self.pred_next_dt else OFFSET_TAU_MAX
                self.offset_tau = min(max(0.4 * horizon, 600.0), OFFSET_TAU_MAX)
                self._seen_ver = self._family_ver
            decay = (math.exp(-(t - self.offset_t) / self.offset_tau)
                     if self.offset_t is not None else 0.0)
            y_new = target + self.offset * decay
            if band_lo is not None:
                y_new = min(max(y_new, band_lo), band_hi)
            if self.rung >= 3 and self.A is not None and self.y is not None:
                # arrest rapid changes: cap output movement at 2x the model's
                # own slope, floored at one step per 4 h (liveness), so even a
                # band-veto correction lands as a gentle ramp
                cap = max(RATE_CAP_SLOPE_MULT * self._curve_slope(t), RATE_CAP_FLOOR)
                dt = max(t - self.last_t, 0.0)
                dy = min(max(y_new - self.y, -cap * dt), cap * dt)
                self.y += dy
            else:
                self.y = y_new

        self.last_t = t
        return self.y

    # ── persistence ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "last_seen": {str(k): v for k, v in self.last_seen.items()},
            "top": self.top,
            "episode_t0": self.episode_t0,
            "anchors_t": self.anchors_t,
            "anchors_v": self.anchors_v,
            "A": self.A, "B1": self.B1, "B2": self.B2,
            "fit_rms": self.fit_rms,
            "frozen": self.frozen,
            "last_anchor_reg_t": self.last_anchor_reg_t,
            "pred_next_dt": self.pred_next_dt,
            "offset": self.offset, "offset_t": self.offset_t,
            "offset_tau": self.offset_tau,
            "rung": self.rung,
            "y": self.y, "last_t": self.last_t,
        }

    def restore(self, state: dict) -> None:
        try:
            self.last_seen = {int(k): float(v)
                              for k, v in state.get("last_seen", {}).items()}
            for key, value in state.items():
                if key != "last_seen" and hasattr(self, key):
                    setattr(self, key, value)
        except (TypeError, ValueError) as exc:
            _LOGGER.warning("Well model: could not restore state (%s) — fresh start", exc)
