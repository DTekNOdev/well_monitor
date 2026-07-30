"""Characterise the well's fill curve from the one clean, unbroken fill.

Approach (deliberately offline — physics first, estimation later):

  1. Find the global minimum voltage in the capture (the deepest drawdown,
     ~26 Jul mid-morning) and take everything from there to the end: one
     uninterrupted fill from lowest observed level to nearly full.
  2. Extract quiet-zone anchors: for each quantization level L the fill
     passes through, the anchor is halfway between the last sample at the
     level below and the first sample at the level above, valued at L.
     (Noise amplitude cancels at that midpoint.)
  3. Fit the recharge model  x(t) = V_top - (V_top - x0) * exp(-k t)
     to the anchors (grid search on k, exact linear LSQ for the rest).
  4. The decisive diagnostic: plot segment rate dv/dt against level v.
     A single exponential demands these points lie on a straight line
     r = k * (V_top - v).  Curvature here means the physics needs a
     different form (two time constants, power law, ...).

Run:  python analysis/fit_fill_curve.py
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

HERE = Path(__file__).parent
CSV = HERE.parent / "history-long.csv"

STEP_MAX = 0.035


def extract_anchors(seg: pd.DataFrame) -> pd.DataFrame:
    """Quiet-zone anchors for a monotone fill segment.

    For each level L (ascending), anchor time = midpoint(last time at the
    level below L, first time at the level above L), anchor value = L.
    """
    levels = sorted(seg["state"].unique())
    first_t = {v: seg.loc[seg.state.eq(v), "t"].min() for v in levels}
    last_t = {v: seg.loc[seg.state.eq(v), "t"].max() for v in levels}

    rows = []
    for below, level, above in zip(levels, levels[1:], levels[2:]):
        if level - below > STEP_MAX or above - level > STEP_MAX:
            continue    # non-adjacent — gap in the ladder, skip
        t_mid = (last_t[below] + first_t[above]) / 2.0
        rows.append((t_mid, level))
    return pd.DataFrame(rows, columns=["t", "v"]).sort_values("t").reset_index(drop=True)


def fit_exponential(t: np.ndarray, v: np.ndarray):
    """Fit v(t) = A - B exp(-k t) — grid+refine on k, linear LSQ for A, B."""
    def sse_for(k: float):
        e = np.exp(-k * t)
        X = np.vstack([np.ones_like(t), e]).T
        coef, *_ = np.linalg.lstsq(X, v, rcond=None)
        resid = v - X @ coef
        return float(resid @ resid), coef

    ks = np.logspace(-7, -3.5, 300)
    scores = [sse_for(k)[0] for k in ks]
    k0 = ks[int(np.argmin(scores))]
    # local refinement
    for span in (3.0, 1.5, 1.2, 1.05):
        cand = k0 * np.linspace(1 / span, span, 60)
        scores = [sse_for(k)[0] for k in cand]
        k0 = cand[int(np.argmin(scores))]
    sse, (A, negB) = sse_for(k0)
    return k0, A, -negB, math.sqrt(sse / len(t))


def fit_double_exponential(t: np.ndarray, v: np.ndarray):
    """Fit v(t) = A - B1 exp(-k1 t) - B2 exp(-k2 t).

    Grid over (k1, k2) with exact linear LSQ for (A, B1, B2) at each pair —
    no scipy needed, and immune to local minima at this problem size.
    """
    def sse_for(k1: float, k2: float):
        X = np.vstack([np.ones_like(t), np.exp(-k1 * t), np.exp(-k2 * t)]).T
        coef, *_ = np.linalg.lstsq(X, v, rcond=None)
        resid = v - X @ coef
        return float(resid @ resid), coef

    ks = np.logspace(-7.5, -3.5, 80)
    best = (math.inf, None, None, None)
    for i, k1 in enumerate(ks):
        for k2 in ks[i + 1:]:
            sse, coef = sse_for(k1, k2)
            if sse < best[0]:
                best = (sse, k1, k2, coef)
    sse, k1, k2, _ = best
    # local refinement, alternating
    for _ in range(3):
        for span in (1.6, 1.25, 1.1):
            cand = k1 * np.linspace(1 / span, span, 40)
            scores = [sse_for(c, k2)[0] for c in cand]
            k1 = cand[int(np.argmin(scores))]
            cand = k2 * np.linspace(1 / span, span, 40)
            scores = [sse_for(k1, c)[0] for c in cand]
            k2 = cand[int(np.argmin(scores))]
    sse, (A, nB1, nB2) = sse_for(k1, k2)
    return k1, k2, A, -nB1, -nB2, math.sqrt(sse / len(t))


def fit_double_fixed_taus(t: np.ndarray, v: np.ndarray, k1: float, k2: float):
    """Fit only the amplitudes (A, B1, B2) with the time constants given.

    The transfer test: if the taus are properties of the well, a fill
    episode from a different day should fit well with someone else's taus.
    """
    X = np.vstack([np.ones_like(t), np.exp(-k1 * t), np.exp(-k2 * t)]).T
    coef, *_ = np.linalg.lstsq(X, v, rcond=None)
    resid = v - X @ coef
    A, nB1, nB2 = coef
    return A, -nB1, -nB2, math.sqrt(float(resid @ resid) / len(t))


def analyse_fill(raw: pd.DataFrame, t_start, t_end, label: str,
                 fixed_taus: "tuple[float, float] | None" = None):
    """Anchor-extract and fit one fill window; returns the double-exp params."""
    seg = raw[(raw.last_changed >= t_start) & (raw.last_changed <= t_end)].copy()
    seg = seg.reset_index(drop=True)
    t0 = seg["last_changed"].iloc[0]
    seg["t"] = (seg["last_changed"] - t0).dt.total_seconds()
    print(f"\n── {label}: {t_start} → {t_end} "
          f"({len(seg)} samples, {seg['t'].iloc[-1]/3600:.1f} h, "
          f"{seg.state.min():.2f}→{seg.state.max():.2f} V)")

    # 2) anchors
    anch = extract_anchors(seg)
    print(f"anchors: {len(anch)}  ({anch.v.iloc[0]:.2f} V → {anch.v.iloc[-1]:.2f} V)")

    # 3) exponential fit
    t = anch["t"].to_numpy()
    v = anch["v"].to_numpy()
    k, A, B, rms = fit_exponential(t, v)
    tau_h = 1 / k / 3600
    x0 = A - B
    print(f"single: V_top={A:.4f} V, x0={x0:.4f} V, tau={tau_h:.1f} h, "
          f"rms={rms*1000:.2f} mV")

    k1, k2, A2, B1, B2, rms2 = fit_double_exponential(t, v)
    tau1_h, tau2_h = 1 / k1 / 3600, 1 / k2 / 3600
    if tau1_h > tau2_h:
        (k1, B1, tau1_h), (k2, B2, tau2_h) = (k2, B2, tau2_h), (k1, B1, tau1_h)
    print(f"double: V_top={A2:.4f} V, fast tau={tau1_h:.1f} h (B={B1:.3f}), "
          f"slow tau={tau2_h:.1f} h (B={B2:.3f}), rms={rms2*1000:.2f} mV")

    fitted = A - B * np.exp(-k * t)
    resid_mv = (v - fitted) * 1000
    fitted2 = A2 - B1 * np.exp(-k1 * t) - B2 * np.exp(-k2 * t)
    resid2_mv = (v - fitted2) * 1000

    # transfer test: amplitudes-only fit with the reference fill's taus
    xfer = None
    if fixed_taus is not None:
        fk1, fk2 = fixed_taus
        Af, B1f, B2f, rmsf = fit_double_fixed_taus(t, v, fk1, fk2)
        print(f"transfer (taus fixed at {1/fk1/3600:.1f} h / {1/fk2/3600:.1f} h): "
              f"V_top={Af:.4f} V, B1={B1f:.3f}, B2={B2f:.3f}, rms={rmsf*1000:.2f} mV")
        xfer = (fk1, fk2, Af, B1f, B2f, rmsf)

    # 4) segment rates vs level
    dv = np.diff(v)
    dt = np.diff(t)
    seg_rate = dv / dt * 3600 * 1000        # mV/h
    seg_mid = (v[1:] + v[:-1]) / 2.0

    # ── plots ────────────────────────────────────────────────────────────────
    fig, axes = plt.subplots(3, 1, figsize=(14, 13),
                             gridspec_kw={"height_ratios": [3, 1.2, 2]})

    ax = axes[0]
    ax.step(seg.last_changed, seg.state, where="post", color="#bbbbbb", lw=1,
            label="raw (quantized)")
    ax.plot(anch.t.map(lambda s: t0 + pd.Timedelta(seconds=s)), anch.v, "o",
            color="#333333", ms=4, mfc="white", label="quiet-zone anchors")
    tt = np.linspace(0, seg["t"].iloc[-1], 800)
    ax.plot([t0 + pd.Timedelta(seconds=s) for s in tt],
            A - B * np.exp(-k * tt), color="#e6a23c", lw=1.4, alpha=0.9,
            label=f"single exp: tau {tau_h:.1f} h (rms {rms*1000:.0f} mV)")
    ax.plot([t0 + pd.Timedelta(seconds=s) for s in tt],
            A2 - B1 * np.exp(-k1 * tt) - B2 * np.exp(-k2 * tt),
            color="#c0392b", lw=1.8,
            label=f"double exp: tau {tau1_h:.1f} h + {tau2_h:.0f} h (rms {rms2*1000:.0f} mV)")
    if xfer is not None:
        fk1, fk2, Af, B1f, B2f, rmsf = xfer
        ax.plot([t0 + pd.Timedelta(seconds=s) for s in tt],
                Af - B1f * np.exp(-fk1 * tt) - B2f * np.exp(-fk2 * tt),
                "--", color="#2c5f8a", lw=1.6,
                label=f"transfer: reference taus, amplitudes refit (rms {rmsf*1000:.0f} mV)")
    ax.set_ylabel("voltage (V)")
    ax.set_title(f"{label} — recharge model fits ({len(anch)} quiet-zone anchors)")
    ax.legend(loc="lower right")
    ax.grid(alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %H:%M"))

    ax = axes[1]
    ax.axhline(0, color="#999999", lw=0.8)
    ax.plot(anch.t.map(lambda s: t0 + pd.Timedelta(seconds=s)), resid_mv,
            "o-", color="#e6a23c", ms=4, lw=1, label="single exp")
    ax.plot(anch.t.map(lambda s: t0 + pd.Timedelta(seconds=s)), resid2_mv,
            "o-", color="#c0392b", ms=4, lw=1, label="double exp")
    ax.set_ylabel("residual (mV)")
    ax.set_title("Anchor residuals")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %H:%M"))

    ax = axes[2]
    ax.plot(seg_mid, seg_rate, "o", color="#333333", ms=5, mfc="white",
            label="segment rate between anchors")
    vv = np.linspace(v.min(), A, 200)
    ax.plot(vv, k * (A - vv) * 3600 * 1000, color="#e6a23c", lw=1.4,
            label="single exponential: r = k·(V_top − v)")
    # double-exp rate vs level is parametric in t — trace it
    rr = (B1 * k1 * np.exp(-k1 * tt) + B2 * k2 * np.exp(-k2 * tt)) * 3600 * 1000
    xx = A2 - B1 * np.exp(-k1 * tt) - B2 * np.exp(-k2 * tt)
    ax.plot(xx, rr, color="#c0392b", lw=1.8, label="double exponential")
    ax.set_xlabel("level (V)")
    ax.set_ylabel("fill rate (mV/h)")
    ax.set_title("Rate vs level — curvature is the signature of two time constants")
    ax.legend(loc="upper right")
    ax.grid(alpha=0.3)

    fig.autofmt_xdate()
    fig.tight_layout()
    out = HERE / f"fill_curve_fit_{label.replace(' ', '_')}.png"
    fig.savefig(out, dpi=110)
    print(f"saved {out}")
    return k1, k2, A2, B1, B2


def main() -> None:
    df = pd.read_csv(CSV, parse_dates=["last_changed"])
    raw = df[df.entity_id.str.contains("pressure_sensor")].copy()
    raw["state"] = pd.to_numeric(raw["state"], errors="coerce")
    raw = raw.dropna(subset=["state"]).sort_values("last_changed").reset_index(drop=True)

    # Reference: the big clean fill, from the global minimum to end of capture
    i_min = raw["state"].idxmin()
    t_min = raw.loc[i_min, "last_changed"]
    k1, k2, A2, B1, B2 = analyse_fill(
        raw, t_min, raw["last_changed"].iloc[-1], "big fill"
    )

    # Robustness test: the short night fill, 26 Jul 01:03–07:21 local (UTC+2)
    analyse_fill(
        raw,
        pd.Timestamp("2026-07-25T23:03:00Z"),
        pd.Timestamp("2026-07-26T05:21:00Z"),
        "night fill",
        fixed_taus=(k1, k2),
    )


if __name__ == "__main__":
    main()
