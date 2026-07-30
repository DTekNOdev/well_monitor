"""Model-based voltage estimation for the well monitor — v2.

Anchor rule (quiet-zone symmetry, per discussion):

    The anchor for level L sits at the CENTRE of L's quiet zone — halfway
    between the last blip down to the level below and the first blip up to
    the level above — with value exactly L.  The sensor noise amplitude
    cancels at that midpoint, so the anchor lies on the true curve.

v2 fixes the v1 failure modes:

  * v1 reset the whole fill sequence on ANY reading below the dithering
    pair, so noise dips and micro-drains destroyed the ladder — 41 resets
    over 5 days and almost no anchors in the near-full regime.  v2 keeps a
    tolerant level ladder: dips up to two steps below the top are just
    dwell-time updates; only a genuine multi-step drop (real usage) resets.
  * v1's online propagation (ceiling clamps, fragile exponential fit)
    fought the anchors.  v2's online model has one job: ride the anchor
    polyline.  At each new anchor it recomputes the segment rate and
    converges onto the new segment line exponentially (tau ~20 min), so the
    output passes through the anchors the hindsight line uses — one segment
    behind, which is all causality allows.

Rate prediction between anchors: the last completed segment's rate, softened
by the observed deceleration ratio between the last two segments (geometric
continuation — the discrete form of exponential recharge, with no asymptote
parameter to mis-fit).

Run:  python analysis/sim_model_voltage.py
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd

HERE = Path(__file__).parent
CSV = HERE.parent / "history-long.csv"

STEP_MAX = 0.035       # max gap between adjacent quantization levels
DRAIN_DROP = 0.06      # a drop this far below the ladder top = real usage
DEMOTE_AFTER = 4 * 3600  # top level unseen this long => slow drain, demote
CONV_TAU = 1200.0      # s — output convergence onto a new segment line
DECEL_MIN = 0.4        # clamp on the segment-to-segment deceleration ratio


def _key(v: float) -> int:
    return round(v * 1000)


@dataclass
class Anchor:
    t: float
    v: float


@dataclass
class ModelEstimator:
    """Quiet-zone-anchored voltage model, tolerant ladder version."""

    # ladder state
    last_seen: dict = field(default_factory=dict)   # level_key -> last time
    levels: dict = field(default_factory=dict)      # level_key -> value
    top: float | None = None
    anchored: set = field(default_factory=set)      # level_keys anchored this climb

    # anchors and rate
    anchors: list = field(default_factory=list)         # current episode
    all_anchors: list = field(default_factory=list)     # (Anchor, episode)
    episode: int = 0
    r_pred: float | None = None      # predicted rate for the open segment
    r_prev_seg: float | None = None  # previous completed segment's rate

    # output
    y: float | None = None
    last_t: float | None = None

    # ── ladder helpers ────────────────────────────────────────────────────────

    def _level_below(self, v: float) -> float | None:
        cands = [lv for lv in self.levels.values() if 0 < v - lv <= STEP_MAX]
        return max(cands) if cands else None

    def _reset(self, t: float, v: float) -> None:
        self.last_seen = {_key(v): t}
        self.levels = {_key(v): v}
        self.top = v
        self.anchored = set()
        self.anchors = []
        self.r_pred = None
        self.r_prev_seg = None
        self.y = v
        self.episode += 1

    def _register_anchor(self, t_mid: float, level: float) -> None:
        a = Anchor(t_mid, level)
        if self.anchors:
            prev = self.anchors[-1]
            dt, dv = a.t - prev.t, a.v - prev.v
            if dt > 0 and dv > 0:
                r_seg = dv / dt
                if self.r_prev_seg and self.r_prev_seg > 0:
                    # geometric continuation of the deceleration, clamped
                    ratio = max(min(r_seg / self.r_prev_seg, 1.0), DECEL_MIN)
                    self.r_pred = r_seg * ratio
                else:
                    self.r_pred = r_seg
                self.r_prev_seg = r_seg
        self.anchors.append(a)
        self.all_anchors.append((a, self.episode))

    # ── main update ───────────────────────────────────────────────────────────

    def update(self, t: float, v: float) -> float:
        if self.last_t is None:
            self._reset(t, v)
            self.episode = 0
            self.last_t = t
            return self.y

        k = _key(v)

        if self.top is not None and self.top - v > DRAIN_DROP:
            # real usage: track immediately, fresh episode
            self._reset(t, v)
        elif self.top is not None and v - self.top > STEP_MAX:
            # multi-step jump upward — discontinuity, fresh episode
            self._reset(t, v)
        elif self.top is not None and v > self.top:
            # fill progressed one step: the old top's quiet zone just ended.
            old_top = self.top
            below = self._level_below(old_top)
            bk = _key(old_top)
            if below is not None and bk not in self.anchored:
                t_mid = (self.last_seen[_key(below)] + t) / 2.0
                self._register_anchor(t_mid, old_top)
                self.anchored.add(bk)
            self.levels[k] = v
            self.last_seen[k] = t
            self.top = v
        else:
            # at or below top by at most ~two steps: dwell / dither dip.
            self.levels[k] = v
            self.last_seen[k] = t
            # slow drain: the top stopped being touched a long time ago
            if (self.top is not None and v < self.top
                    and t - self.last_seen.get(_key(self.top), t) > DEMOTE_AFTER):
                self.top = v
                self.anchored.discard(_key(v))

        # ── output: converge onto the current segment line ───────────────────
        if self.anchors:
            a = self.anchors[-1]
            target = a.v + (self.r_pred or 0.0) * (t - a.t)
        else:
            target = self.top if self.top is not None else v

        dt = max(t - self.last_t, 0.0)
        blend = 1.0 - math.exp(-dt / CONV_TAU)
        self.y += blend * (target - self.y)

        self.last_t = t
        return self.y


def main() -> None:
    df = pd.read_csv(CSV, parse_dates=["last_changed"])
    raw = df[df.entity_id.str.contains("pressure_sensor")].copy()
    shipped = df[df.entity_id.str.contains("well_monitor_voltage")].copy()
    for d in (raw, shipped):
        d["state"] = pd.to_numeric(d["state"], errors="coerce")
        d.dropna(subset=["state"], inplace=True)
        d.sort_values("last_changed", inplace=True)

    t0 = raw["last_changed"].iloc[0]
    grid = raw.set_index("last_changed")[["state"]].resample("60s").ffill().dropna().reset_index()
    gsec = (grid["last_changed"] - t0).dt.total_seconds()

    m = ModelEstimator()
    grid["model"] = [m.update(t, v) for t, v in zip(gsec, grid["state"])]

    print(f"raw samples: {len(raw)}, grid points: {len(grid)}")
    print(f"episodes: {m.episode}, anchors: {len(m.all_anchors)}")
    per_day: dict = {}
    for a, ep in m.all_anchors:
        d = (t0 + pd.Timedelta(seconds=a.t)).date()
        per_day[d] = per_day.get(d, 0) + 1
    for d in sorted(per_day):
        print(f"  {d}: {per_day[d]} anchors")

    # hindsight polyline through the anchors, broken per episode
    if len(m.all_anchors) >= 2:
        rows, prev_ep = [], None
        for a, ep in m.all_anchors:
            if prev_ep is not None and ep != prev_ep:
                rows.append((t0 + pd.Timedelta(seconds=a.t), float("nan")))
            rows.append((t0 + pd.Timedelta(seconds=a.t), a.v))
            prev_ep = ep
        hind = pd.DataFrame(rows, columns=["t", "v"])
    else:
        hind = None

    # fit metric: online vs hindsight interpolation, per grid point
    if hind is not None:
        hv = hind.dropna().set_index("t")["v"]
        hv = hv[~hv.index.duplicated()]
        interp = (hv.reindex(hv.index.union(grid["last_changed"]))
                    .interpolate(method="time")
                    .reindex(grid["last_changed"]))
        err = (grid["model"].values - interp.values)
        err = err[~pd.isna(err)]
        print(f"online vs hindsight: rms {1000*float((err**2).mean())**0.5:.1f} mV, "
              f"max {1000*abs(err).max():.1f} mV")

    views = [
        ("full", None, None),
        ("drawdown", pd.Timestamp("2026-07-26T05:00:00Z"), pd.Timestamp("2026-07-26T14:00:00Z")),
        ("mid_refill", pd.Timestamp("2026-07-26T12:00:00Z"), pd.Timestamp("2026-07-27T12:00:00Z")),
        ("near_full", pd.Timestamp("2026-07-28T00:00:00Z"), pd.Timestamp("2026-07-30T10:00:00Z")),
        ("quiet_zone", pd.Timestamp("2026-07-29T12:00:00Z"), pd.Timestamp("2026-07-30T06:00:00Z")),
    ]
    for name, lo_t, hi_t in views:
        g = grid; r = raw; s = shipped
        if lo_t is not None:
            g = grid[(grid.last_changed >= lo_t) & (grid.last_changed <= hi_t)]
            r = raw[(raw.last_changed >= lo_t) & (raw.last_changed <= hi_t)]
            s = shipped[(shipped.last_changed >= lo_t) & (shipped.last_changed <= hi_t)]
        fig, ax = plt.subplots(figsize=(14, 6))
        ax.step(r.last_changed, r.state, where="post", color="#bbbbbb", lw=1,
                label="raw (quantized)")
        ax.plot(s.last_changed, s.state, color="#b8a978", lw=1.0, alpha=0.7,
                label="old EMA filter (reference only)")
        ax.plot(g.last_changed, g["model"], color="#c0392b", lw=1.8,
                label="model (online)")
        if hind is not None:
            h = hind if lo_t is None else hind[(hind.t >= lo_t) & (hind.t <= hi_t)]
            ax.plot(h.t, h.v, "--", color="#333333", lw=1.2, alpha=0.8,
                    label="hindsight: line through anchors")
            ax.plot(h.t, h.v, "o", color="#333333", ms=4, mfc="white", zorder=5)
        ax.set_title(f"Well voltage — quiet-zone-anchored model v2 ({name})")
        ax.set_ylabel("voltage (V)")
        ax.legend(loc="best")
        ax.grid(alpha=0.3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %H:%M"))
        fig.autofmt_xdate()
        fig.tight_layout()
        out = HERE / f"model_voltage_{name}.png"
        fig.savefig(out, dpi=110)
        print(f"saved {out}")


if __name__ == "__main__":
    main()
