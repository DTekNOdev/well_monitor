"""One-glance scorecard: how far is each estimate from the truth, per regime?

A table you can skim in ten seconds and use as an optimisation objective.  Run
it on every new capture and compare against docs/method_comparison_*.md.

    python analysis/scorecard.py

WHAT "TRUTH" IS, AND WHY NOT RAW
    Raw is not truth; it is a ~25 mV quantization of truth, and it dithers.
    Distance-to-raw is minimised by reproducing the staircase, which is the
    opposite of what the smoothing is for.  So each regime is scored against the
    best available hindsight reconstruction instead:

    FILL -> PHYSICS.  The double-exponential recharge curve
        x(t) = V_top - B1 e^-t/tau_fast - B2 e^-t/tau_slow
    fitted to the quiet-zone anchors (the centre of each quantization level's
    flat period, where the sensor's noise amplitude cancels, so the anchor sits
    exactly on the true curve).  This reference is INDEPENDENT of raw's noise --
    it is a two-amplitude fit to a handful of exact points -- so raw gets no
    structural advantage.  A fill is only scored when the physics actually fits;
    fills whose model residual is poor are reported but excluded, because there
    the reference is the unreliable part.

    DRAWDOWN -> ZERO-LAG HINDSIGHT.  There is no physics curve for a drawdown:
    it is driven by unknown demand, not by a recharge law.  The reference is
    therefore a CENTRED local quadratic fit of raw (Savitzky-Golay, order 2):
      * centred => no lag, so a causal estimator that trails is penalised
      * order 2 => a ramp passes through untouched, so the fast drawdown is not
        smeared into a slope the well never had
      * on dither between L and L-s it averages to the threshold, which is
        where truth actually is
    Validated against the physics curve on the fills, where both exist -- that
    agreement is printed every run.

THE THREE NUMBERS PER REGIME (all aggregate, all lower-is-better)
    DIST   RMS(estimate - reference) in mV at zero time shift.  How far from the
           true value, counting everything: noise, lag, shape error.
    DELAY  the time shift tau that minimises that RMS, in minutes.  This is the
           effective delay in picking up change -- an aggregate alternative to
           per-level crossing times, which are corrupted by raw's transient
           blips (see docs/method_comparison_2026-08-03.md section 2).
    RESID  RMS at the optimal shift.  The error that is NOT delay -- i.e. the
           shape error that remains after the lag is taken out.

    DIST is what the user sees.  DELAY says how much of it is staleness and
    RESID how much is genuine mis-shape, so the two together say WHERE to spend
    effort.  SCORE is the duration-weighted mean DIST: the single figure to
    minimise if you want just one.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from report_two_methods import (
    JULY_TAU_FAST_H, JULY_TAU_SLOW_H, as_series, extract_anchors, find_events,
    fit_double, load,
)

HERE = Path(__file__).parent
GRID_MIN = 1
WINDOW_MIN = 21          # centred window for the truth reference
POLY_ORDER = 2


# ── truth reference ──────────────────────────────────────────────────────────

def savgol(y: np.ndarray, window: int, order: int = POLY_ORDER) -> np.ndarray:
    """Centred local polynomial smoother on a uniform grid (no scipy).

    The value at the window centre is a fixed linear combination of the window's
    samples, so the whole filter is one convolution.  Edges fall back to raw.
    """
    if window % 2 == 0:
        window += 1
    half = window // 2
    x = np.arange(-half, half + 1, dtype=float)
    A = np.vander(x, order + 1, increasing=True)
    coef = np.linalg.pinv(A)[0]          # row giving the fit's value at x = 0
    out = np.convolve(y, coef[::-1], mode="same")
    out[:half] = y[:half]
    out[-half:] = y[-half:]
    return out


def build_truth(raw_g: pd.Series, window_min: int) -> pd.Series:
    win = max(3, int(round(window_min / GRID_MIN)))
    return pd.Series(savgol(raw_g.to_numpy(float), win), index=raw_g.index)


MODEL_TRUST_MV = 15.0    # a fill's physics fit must be this good to be scored


def build_physics(raw: pd.DataFrame, events: list, grid: pd.DatetimeIndex,
                  truth: pd.Series):
    """Double-exp reference per fill.  Returns (series on grid, per-fill info).

    Fills whose model residual exceeds MODEL_TRUST_MV are fitted and reported
    but left out of the reference, because there the physics curve — not the
    estimator — is the unreliable party.
    """
    ref = pd.Series(np.nan, index=grid)
    info = []
    jk = (1 / (JULY_TAU_FAST_H * 3600), 1 / (JULY_TAU_SLOW_H * 3600))
    for e in events:
        if e["kind"] != "fill" or e["hours"] < 6:
            continue
        seg = raw.iloc[e["i0"]:e["i1"] + 1].copy().reset_index(drop=True)
        f0 = seg["last_changed"].iloc[0]
        seg["t"] = (seg["last_changed"] - f0).dt.total_seconds()
        anch = extract_anchors(seg)
        if len(anch) < 6:
            continue
        k1, k2, A, B1, B2, rms = fit_double(anch["t"].to_numpy(),
                                            anch["v"].to_numpy(), k_fixed=jk)
        sel = (grid >= e["t0"]) & (grid <= e["t1"])
        te = np.array([(x - f0).total_seconds() for x in grid[sel]])
        curve = A - B1 * np.exp(-k1 * te) - B2 * np.exp(-k2 * te)
        trusted = rms * 1000 <= MODEL_TRUST_MV
        if trusted:
            ref[sel] = curve
        # cross-check the drawdown reference against the physics curve here
        d = (truth[sel].to_numpy() - curve) * 1000
        info.append({
            "label": f"{e['t0']:%d %b %H:%M}", "hours": e["hours"],
            "anchors": len(anch), "rms": rms * 1000, "trusted": trusted,
            "vs_hindsight": float((d ** 2).mean()) ** 0.5,
        })
    return ref, info


def dist_delay(est: pd.Series, ref: pd.Series, mask: np.ndarray,
               lo: int = -30, hi: int = 121) -> dict:
    """Vertical distance, effective delay, and residual-after-delay (all mV/min).

    DELAY is the shift tau minimising RMS(est(t+tau) - ref(t)): if the estimate
    trails the reference, advancing it by tau lines them up, so tau > 0 is a
    genuine delay.  Aggregate, and immune to the blip-chasing that corrupts
    per-level crossing times.
    """
    best = (float("inf"), 0)
    d0 = float("nan")
    for tau in range(lo, hi):
        d = ((est.shift(-tau) - ref) * 1000)[mask].dropna()
        if d.empty:
            continue
        rms = float((d ** 2).mean()) ** 0.5
        if tau == 0:
            d0 = rms
        if rms < best[0]:
            best = (rms, tau)
    resid, delay = best
    bias = ((est - ref) * 1000)[mask].dropna()
    return {"dist": d0, "delay": float(delay), "resid": resid,
            "bias": float(bias.mean()) if not bias.empty else float("nan")}


# ── scoring ──────────────────────────────────────────────────────────────────

def regime_mask(grid: pd.DatetimeIndex, events: list, kind: str) -> np.ndarray:
    m = np.zeros(len(grid), dtype=bool)
    for e in events:
        if e["kind"] == kind:
            m |= (grid >= e["t0"]) & (grid <= e["t1"])
    return m


def main() -> None:
    data = load()
    raw_s = as_series(data["raw"])
    events = find_events(data["raw"])
    t0, t1 = raw_s.index[0], raw_s.index[-1]
    grid = pd.date_range(t0, t1, freq=f"{GRID_MIN}min")

    def on_grid(s: pd.Series) -> pd.Series:
        return s.reindex(s.index.union(grid)).ffill().reindex(grid)

    raw_g = on_grid(raw_s)
    series = {"raw": raw_g, "new": on_grid(as_series(data["new"])),
              "old": on_grid(as_series(data["old"]))}
    hindsight = build_truth(raw_g, WINDOW_MIN)
    physics, fill_info = build_physics(data["raw"], events, grid, hindsight)

    m_drain = regime_mask(grid, events, "drain")
    m_fill = physics.notna().to_numpy()          # only fills the physics fits
    drain_h = sum(e["hours"] for e in events if e["kind"] == "drain")
    fill_h = sum(i["hours"] for i in fill_info if i["trusted"])

    print("=" * 78)
    print("WELL MONITOR SCORECARD          all columns mV / minutes, lower is better")
    print("=" * 78)
    print(f"capture   {t0:%d %b %H:%M} → {t1:%d %b %H:%M}"
          f"  ({(t1-t0).total_seconds()/3600:.1f} h)"
          f"   scored: {drain_h:.1f} h drawdown / {fill_h:.1f} h fill")
    print("reference fill      = double-exp physics curve on quiet-zone anchors")
    print(f"          drawdown  = centred order-{POLY_ORDER} hindsight fit of raw, "
          f"{WINDOW_MIN} min (zero lag)")
    print()
    print(f"{'':<7}{'──── FILL (vs physics) ────':^30}"
          f"{'── DRAWDOWN (vs hindsight) ─':^30}{'':>10}")
    print(f"{'method':<7}{'dist':>8}{'delay':>8}{'resid':>8}{'bias':>7}"
          f"{'dist':>9}{'delay':>8}{'resid':>8}{'bias':>7}{'SCORE':>10}")
    print("-" * 78)
    rows = {}
    for name, s in series.items():
        f = dist_delay(s, physics, m_fill)
        d = dist_delay(s, hindsight, m_drain)
        score = (d["dist"] * drain_h + f["dist"] * fill_h) / (drain_h + fill_h)
        rows[name] = (f, d, score)
        print(f"{name:<7}{f['dist']:>8.1f}{f['delay']:>8.0f}{f['resid']:>8.1f}"
              f"{f['bias']:>+7.1f}{d['dist']:>9.1f}{d['delay']:>8.0f}"
              f"{d['resid']:>8.1f}{d['bias']:>+7.1f}{score:>10.1f}")

    print()
    print("dist  = RMS distance from the reference value, mV")
    print("delay = time shift that best lines the estimate up: the effective")
    print("        delay in picking up change, minutes")
    print("resid = RMS once that delay is removed — the error that is NOT lag")
    print("bias  = signed mean error; − = reads low (trails a rise), + = reads high")
    print("SCORE = duration-weighted mean dist — the single number to minimise")
    print()
    print("Read RAW as the floor, not a competitor: it has zero delay by definition,")
    print("so its dist is pure quantization noise — the noise budget the smoothers")
    print("work against. What smoothing buys (a monotone, readable trace) is scored")
    print("in the main report, not here.")

    best = min(("new", "old"), key=lambda n: rows[n][2])
    other = "old" if best == "new" else "new"
    print()
    print(f"→ {best} wins overall: {rows[best][2]:.1f} vs {rows[other][2]:.1f} mV"
          f"  ({(rows[other][2]-rows[best][2])/rows[other][2]*100:.0f}% closer)")
    for name in ("new", "old"):
        f, d, _ = rows[name]
        print(f"→ {name:<4} fill: removing its {f['delay']:.0f} min delay would take "
              f"{f['dist']:.1f} → {f['resid']:.1f} mV;"
              f"  drawdown delay {d['delay']:.0f} min")

    # ── are the references credible? ────────────────────────────────────────
    print()
    print("reference health — physics fit per fill, and the two references' "
          "agreement:")
    print(f"  {'fill':<15}{'hours':>6}{'anchors':>8}{'model rms':>11}"
          f"{'vs hindsight':>14}   status")
    for i in fill_info:
        status = "scored" if i["trusted"] else f"EXCLUDED (>{MODEL_TRUST_MV:.0f} mV)"
        print(f"  {i['label']:<15}{i['hours']:>6.1f}{i['anchors']:>8}"
              f"{i['rms']:>10.1f}mV{i['vs_hindsight']:>13.1f}mV   {status}")

    # ── does the ranking survive a different hindsight window? ──────────────
    print()
    print("sensitivity — SCORE vs hindsight window (min); fill half is unaffected:")
    print(f"  {'window':>7}" + "".join(f"{n:>9}" for n in series))
    for w in (11, 15, 21, 31, 45, 61):
        hs = build_truth(raw_g, w)
        cells = []
        for name, s in series.items():
            f = dist_delay(s, physics, m_fill, lo=0, hi=1)
            d = dist_delay(s, hs, m_drain, lo=0, hi=1)
            cells.append((d["dist"] * drain_h + f["dist"] * fill_h)
                         / (drain_h + fill_h))
        mark = "  ← default" if w == WINDOW_MIN else ""
        print(f"  {w:>7}" + "".join(f"{c:>9.1f}" for c in cells) + mark)


if __name__ == "__main__":
    main()
