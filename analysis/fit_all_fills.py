"""Detect every fill period in the capture and fit the well formula to each.

Segmentation: a fill segment runs until the level drops more than two
quantization steps below its running maximum (a real drawdown — pump, bath).
Small dips — washing up, a glass of water, dithering — stay inside the
segment; the anchor extraction tolerates them by only considering dips to
the level below that happen BEFORE the level above first appears.

Fitting: the reference taus come from a free 5-parameter fit on the longest
segment.  Every segment then gets the ADAPTIVE treatment proposed for the
integration: amplitudes-only linear least squares with the taus fixed —
cheap, closed-form, and the thing that must work per-episode online.

Run:  python analysis/fit_all_fills.py
"""
from __future__ import annotations

import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

from fit_fill_curve import fit_double_exponential, fit_double_fixed_taus


def fit_fixed_taus_nonneg(t: np.ndarray, v: np.ndarray, k1: float, k2: float):
    """Amplitudes-only fit with the physical constraint B1, B2 >= 0.

    A negative amplitude means a 'reservoir' pushing the level DOWN during a
    fill — meaningless, and it produces curves that peak and decline when
    extrapolated.  Tiny active-set: try unconstrained; clamp any negative
    amplitude to zero and refit the rest.
    """
    A, B1, B2, rms = fit_double_fixed_taus(t, v, k1, k2)
    if B1 >= 0 and B2 >= 0:
        return A, B1, B2, rms

    def fit_single(k: float):
        X = np.vstack([np.ones_like(t), np.exp(-k * t)]).T
        coef, *_ = np.linalg.lstsq(X, v, rcond=None)
        resid = v - X @ coef
        return coef[0], -coef[1], math.sqrt(float(resid @ resid) / len(t))

    candidates = []
    a, b, r = fit_single(k1)           # B2 = 0: fast component only
    if b >= 0:
        candidates.append((r, a, b, 0.0))
    a, b, r = fit_single(k2)           # B1 = 0: slow component only
    if b >= 0:
        candidates.append((r, a, 0.0, b))
    if not candidates:                 # both degenerate: flat level
        a = float(v.mean())
        r = float(np.sqrt(((v - a) ** 2).mean()))
        candidates.append((r, a, 0.0, 0.0))
    r, a, b1, b2 = min(candidates)
    return a, b1, b2, r

HERE = Path(__file__).parent
CSV = HERE.parent / "history-long.csv"

STEP_MAX = 0.035
DROP_THRESHOLD = 0.05       # > two steps below running max = real drawdown
MIN_ANCHORS = 5
MIN_SPAN_H = 2.0
FREE_FIT_MIN_H = 24.0       # only segments this long can identify the taus


def extract_anchors(seg: pd.DataFrame) -> pd.DataFrame:
    """Quiet-zone anchors, tolerant of small in-fill dips.

    Anchor for level L = midpoint(last time at the level below L *before*
    the level above L first appeared, first time at the level above L),
    valued at L.  Dips back to lower levels after the fill moved on do not
    drag the anchor.
    """
    levels = sorted(seg["state"].unique())
    rows = []
    for below, level, above in zip(levels, levels[1:], levels[2:]):
        if level - below > STEP_MAX or above - level > STEP_MAX:
            continue
        t_first_above = seg.loc[seg.state.eq(above), "t"].min()
        t_below = seg.loc[seg.state.eq(below) & (seg.t < t_first_above), "t"]
        if t_below.empty:
            continue
        rows.append(((t_below.max() + t_first_above) / 2.0, level))
    return pd.DataFrame(rows, columns=["t", "v"]).sort_values("t").reset_index(drop=True)


def detect_fill_segments(raw: pd.DataFrame) -> list:
    """Split the capture into fill segments at real drawdowns."""
    segs = []
    state = "fill"
    start_i = 0
    run_max, last_max_i = raw.loc[0, "state"], 0
    drain_min, drain_min_i = None, None

    for i in range(1, len(raw)):
        v = raw.loc[i, "state"]
        if state == "fill":
            if v > run_max:
                run_max, last_max_i = v, i
            elif run_max - v >= DROP_THRESHOLD:
                segs.append((start_i, last_max_i))
                state = "drain"
                drain_min, drain_min_i = v, i
        else:  # drain
            if v <= drain_min:
                drain_min, drain_min_i = v, i
            elif v - drain_min >= 0.02:      # one step up from the trough
                state = "fill"
                start_i = drain_min_i
                run_max, last_max_i = v, i
    if state == "fill":
        segs.append((start_i, len(raw) - 1))
    return segs


def main() -> None:
    df = pd.read_csv(CSV, parse_dates=["last_changed"])
    raw = df[df.entity_id.str.contains("pressure_sensor")].copy()
    raw["state"] = pd.to_numeric(raw["state"], errors="coerce")
    raw = raw.dropna(subset=["state"]).sort_values("last_changed").reset_index(drop=True)

    segs = detect_fill_segments(raw)
    print(f"detected {len(segs)} fill segments")

    # prepare per-segment frames + anchors
    prepared = []
    for si, (a, b) in enumerate(segs):
        seg = raw.iloc[a:b + 1].copy().reset_index(drop=True)
        t0 = seg["last_changed"].iloc[0]
        seg["t"] = (seg["last_changed"] - t0).dt.total_seconds()
        span_h = seg["t"].iloc[-1] / 3600
        anch = extract_anchors(seg)
        prepared.append({"i": si, "seg": seg, "t0": t0, "span_h": span_h, "anch": anch})

    # reference taus from the longest usable segment
    usable = [p for p in prepared if len(p["anch"]) >= MIN_ANCHORS and p["span_h"] >= FREE_FIT_MIN_H]
    ref = max(usable, key=lambda p: p["span_h"])
    t, v = ref["anch"]["t"].to_numpy(), ref["anch"]["v"].to_numpy()
    k1, k2, A, B1, B2, rms = fit_double_exponential(t, v)
    if 1 / k1 > 1 / k2:
        k1, k2 = k2, k1
    print(f"reference segment #{ref['i']} ({ref['span_h']:.1f} h): "
          f"fast tau {1/k1/3600:.1f} h, slow tau {1/k2/3600:.1f} h, rms {rms*1000:.1f} mV")

    # transfer-fit every segment
    print(f"\n{'seg':>4} {'start (UTC)':<17} {'hours':>6} {'range (V)':<13} "
          f"{'anch':>5} {'rms mV':>7}  {'V_top':>7} {'B1':>6} {'B2':>6}")
    results = []
    for p in prepared:
        anch, span_h = p["anch"], p["span_h"]
        label = f"{p['t0']:%d %H:%M}"
        rng = f"{p['seg'].state.min():.2f}-{p['seg'].state.max():.2f}"
        if len(anch) < MIN_ANCHORS or span_h < MIN_SPAN_H:
            print(f"{p['i']:>4} {label:<17} {span_h:>6.1f} {rng:<13} {len(anch):>5}"
                  f"    — skipped (too short / too few anchors)")
            continue
        t = anch["t"].to_numpy()
        v = anch["v"].to_numpy()
        Af, B1f, B2f, rmsf = fit_fixed_taus_nonneg(t, v, k1, k2)
        results.append({**p, "A": Af, "B1": B1f, "B2": B2f, "rms": rmsf})
        print(f"{p['i']:>4} {label:<17} {span_h:>6.1f} {rng:<13} {len(anch):>5} "
              f"{rmsf*1000:>7.1f}  {Af:>7.3f} {B1f:>6.3f} {B2f:>6.3f}")

    # ── overview plot ────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(15, 7))
    ax.step(raw.last_changed, raw.state, where="post", color="#bbbbbb", lw=1,
            label="raw (quantized)")
    colors = plt.cm.tab10.colors
    for n, res in enumerate(results):
        seg, t0 = res["seg"], res["t0"]
        tt = np.linspace(0, seg["t"].iloc[-1], 400)
        y = res["A"] - res["B1"] * np.exp(-k1 * tt) - res["B2"] * np.exp(-k2 * tt)
        c = colors[n % len(colors)]
        ax.plot([t0 + pd.Timedelta(seconds=s) for s in tt], y, color=c, lw=2.0,
                label=f"#{res['i']} ({res['span_h']:.0f} h, rms {res['rms']*1000:.0f} mV)")
        a_t = res["anch"]["t"].map(lambda s: t0 + pd.Timedelta(seconds=s))
        ax.plot(a_t, res["anch"]["v"], "o", color=c, ms=4, mfc="white", zorder=5)
    ax.set_ylabel("voltage (V)")
    ax.set_title(f"All fill segments — amplitudes-only fits with shared taus "
                 f"({1/k1/3600:.1f} h / {1/k2/3600:.1f} h)")
    ax.legend(loc="lower right", fontsize=9)
    ax.grid(alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %H:%M"))
    fig.autofmt_xdate()
    fig.tight_layout()
    out = HERE / "fill_all_segments.png"
    fig.savefig(out, dpi=110)
    print(f"\nsaved {out}")


if __name__ == "__main__":
    main()
