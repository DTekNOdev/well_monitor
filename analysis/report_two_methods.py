"""Compare the live smoothing methods against the physical model, and measure lag.

Inputs (history-long-2methods.csv), three HA entities:
  ...smart_implant_pressure_sensor_voltage   raw quantized sensor
  ...well_monitor_140_mm_bore_801_l_max_...  NEW: evidence-ladder estimator
  ...blind_cellar_well_monitor_voltage       OLD: duty-decoder + adaptive EMA

Questions answered:
  1. Does either method track the double-exponential recharge model, and how
     closely?  Fitted fresh on THIS capture, and separately with the taus
     transferred from the July capture (a true out-of-sample test).
  2. How far does each method LAG the raw signal — in fill, and especially in
     drawdown, where lag means reporting water you no longer have.
  3. How smooth is each — the quantization artefacts we set out to remove.

Lag method: on a monotone segment both raw and the smoothed series pass through
the same voltages, so for each quantization level we compare the time each
series first crosses it.  That is a horizontal (time) lag, which is the
meaningful one for "how stale is this reading", rather than a vertical error.

Run:  python analysis/report_two_methods.py
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
CSV = HERE.parent / "history-long-2methods.csv"

RAW = "smart_implant_pressure_sensor_voltage"
NEW = "well_monitor_140_mm_bore_801_l_max_voltage"
OLD = "blind_cellar_well_monitor_voltage"

STEP_MAX = 0.035
# Taus fitted from the July 2026 capture (analysis/fit_fill_curve.py).
JULY_TAU_FAST_H, JULY_TAU_SLOW_H = 5.2, 27.2


# ── loading ──────────────────────────────────────────────────────────────────

def load():
    df = pd.read_csv(CSV, parse_dates=["last_changed"])
    df["state"] = pd.to_numeric(df["state"], errors="coerce")
    df = df.dropna(subset=["state"])
    out = {}
    for key, frag in (("raw", RAW), ("new", NEW), ("old", OLD)):
        s = (df[df.entity_id.str.contains(frag, regex=False)]
             .sort_values("last_changed")[["last_changed", "state"]]
             .reset_index(drop=True))
        out[key] = s
    return out


def as_series(frame: pd.DataFrame) -> pd.Series:
    s = frame.set_index("last_changed")["state"]
    return s[~s.index.duplicated(keep="last")].sort_index()


# ── segmentation ─────────────────────────────────────────────────────────────

def find_events(raw: pd.DataFrame, min_swing=0.30):
    """Split the capture into alternating DRAIN and FILL events.

    The capture is not one drawdown and one fill: usage happens repeatedly, so
    a naive peak→trough→end split spans a second drawdown and makes both the
    model fit and the lag measurement meaningless.  Turning points are taken
    where the signal reverses by at least min_swing, which ignores quantization
    chatter and small dips while catching every real event.
    """
    v = raw["state"].to_numpy()
    n = len(v)
    if n < 3:
        return []
    # Standard zigzag.  trend 0 until the first move of min_swing decides it;
    # thereafter follow the running extreme and record a turning point only
    # when the signal has reversed by min_swing from it.
    turns: list[int] = []
    trend = 0
    ext = 0
    lo = hi = 0
    for i in range(1, n):
        if trend == 0:
            if v[i] < v[lo]:
                lo = i
            if v[i] > v[hi]:
                hi = i
            if v[hi] - v[i] >= min_swing:
                turns.append(hi); trend = -1; ext = i
            elif v[i] - v[lo] >= min_swing:
                turns.append(lo); trend = 1; ext = i
        elif trend == 1:
            if v[i] > v[ext]:
                ext = i
            elif v[ext] - v[i] >= min_swing:
                turns.append(ext); trend = -1; ext = i
        else:
            if v[i] < v[ext]:
                ext = i
            elif v[i] - v[ext] >= min_swing:
                turns.append(ext); trend = 1; ext = i
    if not turns:
        return []
    if turns[0] != 0:
        turns.insert(0, 0)
    turns.append(n - 1)
    events = []
    for a, b in zip(turns, turns[1:]):
        if b <= a:
            continue
        kind = "fill" if v[b] > v[a] else "drain"
        events.append({
            "kind": kind, "i0": a, "i1": b,
            "t0": raw.loc[a, "last_changed"], "t1": raw.loc[b, "last_changed"],
            "v0": v[a], "v1": v[b],
            "hours": (raw.loc[b, "last_changed"]
                      - raw.loc[a, "last_changed"]).total_seconds() / 3600,
        })
    return events


# ── model fitting ────────────────────────────────────────────────────────────

def extract_anchors(seg: pd.DataFrame) -> pd.DataFrame:
    """Quiet-zone anchors: centre of each level's flat period, valued at the level."""
    levels = sorted(seg["state"].unique())
    rows = []
    for below, level, above in zip(levels, levels[1:], levels[2:]):
        if level - below > STEP_MAX or above - level > STEP_MAX:
            continue
        t_above = seg.loc[seg.state.eq(above), "t"].min()
        t_below = seg.loc[seg.state.eq(below) & (seg.t < t_above), "t"]
        if t_below.empty:
            continue
        rows.append(((t_below.max() + t_above) / 2.0, level))
    return pd.DataFrame(rows, columns=["t", "v"]).sort_values("t").reset_index(drop=True)


def fit_double(t, v, k_fixed=None):
    """v(t) = A - B1 e^{-k1 t} - B2 e^{-k2 t}.  k_fixed pins (k1, k2)."""
    def solve(k1, k2):
        X = np.vstack([np.ones_like(t), np.exp(-k1 * t), np.exp(-k2 * t)]).T
        coef, *_ = np.linalg.lstsq(X, v, rcond=None)
        r = v - X @ coef
        return float(r @ r), coef

    if k_fixed:
        k1, k2 = k_fixed
        sse, coef = solve(k1, k2)
        return k1, k2, coef[0], -coef[1], -coef[2], math.sqrt(sse / len(t))

    # Physically bounded search.  Unbounded, the slow term degenerates: over a
    # 20-hour window it can always improve the fit by becoming linear, running
    # tau to 20000 h and V_top to 194 V — an excellent fit to a meaningless
    # model.  tau is capped at 72 h (a well that slow is not measurable here)
    # and the asymptote must stay within a volt of the data.
    TAU_MIN_H, TAU_MAX_H = 0.5, 72.0
    ks = np.logspace(math.log10(1 / (TAU_MAX_H * 3600)),
                     math.log10(1 / (TAU_MIN_H * 3600)), 80)
    a_max = float(v.max()) + 1.0
    best = (math.inf, None, None)
    for i, a in enumerate(ks):
        for b in ks[i + 1:]:
            sse, coef = solve(a, b)
            if coef[0] > a_max or -coef[1] < 0 or -coef[2] < 0:
                continue          # asymptote or amplitude unphysical
            if sse < best[0]:
                best = (sse, a, b)
    if best[1] is None:           # nothing physical: fall back to single exp
        for a in ks:
            X = np.vstack([np.ones_like(t), np.exp(-a * t)]).T
            coef, *_ = np.linalg.lstsq(X, v, rcond=None)
            r = v - X @ coef
            sse = float(r @ r)
            if coef[0] <= a_max and -coef[1] >= 0 and sse < best[0]:
                best = (sse, a, a * 1.0001)
    _, k1, k2 = best
    sse, coef = solve(k1, k2)
    return k1, k2, coef[0], -coef[1], -coef[2], math.sqrt(sse / len(t))


# ── lag measurement ──────────────────────────────────────────────────────────

def crossing_times(s: pd.Series, levels, rising: bool):
    """First time the series reaches each level (monotone segment)."""
    out = {}
    vals = s.to_numpy()
    idx = s.index.to_numpy()
    for lv in levels:
        hit = np.nonzero(vals >= lv)[0] if rising else np.nonzero(vals <= lv)[0]
        if hit.size:
            out[lv] = idx[hit[0]]
    return out


def lag_table(raw_s, meth_s, t0, t1, rising: bool, step=0.05):
    """Per-level time lag of meth behind raw over [t0, t1]."""
    r = raw_s[(raw_s.index >= t0) & (raw_s.index <= t1)]
    m = meth_s[(meth_s.index >= t0) & (meth_s.index <= t1)]
    if r.empty or m.empty:
        return pd.DataFrame(columns=["level", "lag_min"])
    lo, hi = min(r.min(), m.min()), max(r.max(), m.max())
    levels = np.arange(math.ceil(lo / step) * step, hi, step)
    if not rising:
        levels = levels[::-1]
    cr = crossing_times(r, levels, rising)
    cm = crossing_times(m, levels, rising)
    rows = []
    for lv in levels:
        if lv in cr and lv in cm:
            rows.append((round(lv, 3),
                         (cm[lv] - cr[lv]).total_seconds() / 60.0))
    return pd.DataFrame(rows, columns=["level", "lag_min"])


def stats(name, d):
    if d.empty:
        return f"{name:<28} no overlap"
    l = d["lag_min"]
    return (f"{name:<28}{l.median():>9.1f}{l.mean():>9.1f}"
            f"{l.min():>9.1f}{l.max():>9.1f}{len(l):>7}")


def main() -> None:
    data = load()
    raw, new, old = data["raw"], data["new"], data["old"]
    raw_s, new_s, old_s = (as_series(raw), as_series(new), as_series(old))
    t_start, t_end = raw["last_changed"].iloc[0], raw["last_changed"].iloc[-1]

    events = find_events(raw)
    drains = [e for e in events if e["kind"] == "drain"]
    fills = [e for e in events if e["kind"] == "fill"]

    print("=" * 78)
    print("WELL MONITOR - SMOOTHING METHOD COMPARISON")
    print("=" * 78)
    print(f"capture     : {t_start:%d %b %H:%M} -> {t_end:%d %b %H:%M}"
          f"  ({(t_end - t_start).total_seconds()/3600:.1f} h)")
    print(f"raw samples : {len(raw)}    new: {len(new)}    old: {len(old)}")
    print(f"raw range   : {raw['state'].min():.2f} - {raw['state'].max():.2f} V"
          f"  (span {raw['state'].max()-raw['state'].min():.2f} V)")
    print()
    print(f"events detected: {len(drains)} drain, {len(fills)} fill")
    print(f"  {'kind':<7}{'from':<15}{'to':<15}{'volts':<18}{'hours':>6}{'mV/h':>8}")
    for e in events:
        rate = (e["v1"] - e["v0"]) / e["hours"] * 1000 if e["hours"] else 0
        vv = f"{e['v0']:.2f} -> {e['v1']:.2f}"
        print(f"  {e['kind']:<7}{e['t0']:%d %b %H:%M}  {e['t1']:%d %b %H:%M}  "
              f"{vv:<18}{e['hours']:>6.1f}{rate:>8.0f}")

    # ---- 1. model fit, per uninterrupted fill -----------------------------
    print()
    print("=" * 78)
    print("1. DOUBLE-EXPONENTIAL RECHARGE MODEL, fitted per fill")
    print("=" * 78)
    jk1, jk2 = 1 / (JULY_TAU_FAST_H * 3600), 1 / (JULY_TAU_SLOW_H * 3600)
    models = []
    print(f"{'fill':<16}{'anch':>5}{'tau_fast':>10}{'tau_slow':>10}"
          f"{'V_top':>8}{'rms mV':>8}{'July-tau rms':>14}")
    for e in fills:
        if e["hours"] < 6:
            continue
        seg = raw.iloc[e["i0"]:e["i1"] + 1].copy().reset_index(drop=True)
        f0 = seg["last_changed"].iloc[0]
        seg["t"] = (seg["last_changed"] - f0).dt.total_seconds()
        anch = extract_anchors(seg)
        if len(anch) < 6:
            continue
        t, v = anch["t"].to_numpy(), anch["v"].to_numpy()
        k1, k2, A, B1, B2, rms = fit_double(t, v)
        if 1 / k1 > 1 / k2:
            k1, k2, B1, B2 = k2, k1, B2, B1
        _, _, Aj, B1j, B2j, rmsj = fit_double(t, v, k_fixed=(jk1, jk2))
        label = f"{f0:%d %b %H:%M}"
        print(f"{label:<16}{len(anch):>5}{1/k1/3600:>9.1f}h{1/k2/3600:>9.1f}h"
              f"{A:>8.2f}{rms*1000:>8.1f}{rmsj*1000:>14.1f}")
        mt = np.linspace(0, seg["t"].iloc[-1], 1200)
        models.append({
            "t0": f0, "t1": seg["last_changed"].iloc[-1], "anchors": anch,
            "series": pd.Series(A - B1 * np.exp(-k1 * mt) - B2 * np.exp(-k2 * mt),
                                index=[f0 + pd.Timedelta(seconds=x) for x in mt]),
        })
    print(f"(July reference taus: {JULY_TAU_FAST_H} h + {JULY_TAU_SLOW_H} h)")

    # ---- 2. agreement -----------------------------------------------------
    print()
    print("=" * 78)
    print("2. AGREEMENT per fill (mV).  anchors = the quantization-exact truth")
    print("=" * 78)
    print(f"{'fill':<16}{'series':<7}{'model rms':>11}{'max':>7}"
          f"{'anchor rms':>13}{'max':>7}")
    for m in models:
        grid = pd.date_range(m["t0"], m["t1"], freq="5min")
        mg = (m["series"].reindex(m["series"].index.union(grid))
              .interpolate("time").reindex(grid))
        av = m["anchors"].copy()
        av["ts"] = [m["t0"] + pd.Timedelta(seconds=x) for x in av["t"]]
        a_s = av.set_index("ts")["v"]
        a_s = a_s[~a_s.index.duplicated()]
        ag = (a_s.reindex(a_s.index.union(grid))
              .interpolate("time", limit_area="inside").reindex(grid))
        for name, s in (("raw", raw_s), ("new", new_s), ("old", old_s)):
            sg = s.reindex(s.index.union(grid)).ffill().reindex(grid)
            em = (sg - mg).dropna()
            ea = (sg - ag).dropna()
            lab = f"{m['t0']:%d %b %H:%M}"
            print(f"{lab:<16}{name:<7}"
                  f"{1000*float((em**2).mean())**0.5:>11.1f}{1000*em.abs().max():>7.1f}"
                  f"{1000*float((ea**2).mean())**0.5:>13.1f}{1000*ea.abs().max():>7.1f}")

    # ---- 3. lag -----------------------------------------------------------
    print()
    print("=" * 78)
    print("3. TIME LAG BEHIND RAW, per event (minutes, + = trailing)")
    print("=" * 78)
    print(f"{'event':<24}{'series':<7}{'median':>8}{'mean':>8}{'p90':>8}{'max':>8}{'n':>5}")
    summary = {"drain": {"new": [], "old": []}, "fill": {"new": [], "old": []}}
    for e in events:
        if e["hours"] < 0.5 or abs(e["v1"] - e["v0"]) < 0.3:
            continue
        rising = e["kind"] == "fill"
        label = f"{e['kind']} {e['t0']:%d %b %H:%M}"
        for name, s in (("new", new_s), ("old", old_s)):
            d = lag_table(raw_s, s, e["t0"], e["t1"], rising=rising, step=0.05)
            if d.empty:
                continue
            l = d["lag_min"]
            summary[e["kind"]][name].extend(l.tolist())
            print(f"{label:<24}{name:<7}{l.median():>8.1f}{l.mean():>8.1f}"
                  f"{l.quantile(0.9):>8.1f}{l.max():>8.1f}{len(l):>5}")
    print()
    print("POOLED")
    for kind in ("drain", "fill"):
        for name in ("new", "old"):
            arr = np.array(summary[kind][name])
            if arr.size == 0:
                continue
            lab = kind + " (all)"
            print(f"{lab:<24}{name:<7}{np.median(arr):>8.1f}"
                  f"{arr.mean():>8.1f}{np.quantile(arr, 0.9):>8.1f}"
                  f"{arr.max():>8.1f}{arr.size:>5}")

    rate = raw_s.diff() / raw_s.index.to_series().diff().dt.total_seconds()
    worst = rate.idxmin()
    w0, w1 = worst - pd.Timedelta(minutes=30), worst + pd.Timedelta(minutes=30)
    print()
    print(f"STEEPEST HOUR ({w0:%d %b %H:%M}-{w1:%H:%M}, "
          f"{-rate.min()*3600*1000:.0f} mV/h peak)")
    for name, s in (("new", new_s), ("old", old_s)):
        d = lag_table(raw_s, s, w0, w1, rising=False, step=0.025)
        if not d.empty:
            l = d["lag_min"]
            print(f"{'':24}{name:<7}{l.median():>8.1f}{l.mean():>8.1f}"
                  f"{l.quantile(0.9):>8.1f}{l.max():>8.1f}{len(l):>5}")

    # ---- 4. smoothness ----------------------------------------------------
    print()
    print("=" * 78)
    print("4. SMOOTHNESS per fill (total variation / net change; 1.00 = monotone)")
    print("=" * 78)
    print(f"{'fill':<16}{'raw':>8}{'new':>8}{'old':>8}")
    for e in fills:
        if e["hours"] < 6:
            continue
        cells = []
        for name, s in (("raw", raw_s), ("new", new_s), ("old", old_s)):
            seg = s[(s.index >= e["t0"]) & (s.index <= e["t1"])]
            if len(seg) < 3:
                cells.append(f"{'-':>8}")
                continue
            tv = seg.diff().abs().sum()
            net = seg.iloc[-1] - seg.iloc[0]
            cells.append(f"{tv/abs(net):>8.2f}" if net else f"{'-':>8}")
        lab = f"{e['t0']:%d %b %H:%M}"
        print(f"{lab:<16}" + "".join(cells))

    # ---- plots ------------------------------------------------------------
    views = [("full", t_start, t_end)]
    for n, e in enumerate(drains, 1):
        if e["hours"] >= 0.5 and abs(e["v1"] - e["v0"]) >= 0.3:
            views.append((f"drain{n}", e["t0"] - pd.Timedelta(hours=1),
                          e["t1"] + pd.Timedelta(hours=1)))
    for n, e in enumerate(fills, 1):
        if e["hours"] >= 6:
            views.append((f"fill{n}", e["t0"], e["t1"]))
    for name, a, b in views:
        fig, ax = plt.subplots(figsize=(14, 6))
        r = raw_s[(raw_s.index >= a) & (raw_s.index <= b)]
        ax.step(r.index, r.values, where="post", color="#c9c9c9", lw=1,
                label="raw (quantized)")
        for key, s, col, lw in (("old (EMA)", old_s, "#b8a978", 1.3),
                                ("new (ladder)", new_s, "#c0392b", 1.9)):
            seg = s[(s.index >= a) & (s.index <= b)]
            ax.plot(seg.index, seg.values, color=col, lw=lw, label=key)
        for m in models:
            mseg = m["series"][(m["series"].index >= a) & (m["series"].index <= b)]
            if len(mseg) > 1:
                ax.plot(mseg.index, mseg.values, "--", color="#2c5f8a", lw=1.5,
                        label="double-exp model (hindsight)")
                av = m["anchors"]
                ts = [m["t0"] + pd.Timedelta(seconds=x) for x in av["t"]]
                sel = [(x >= a) and (x <= b) for x in ts]
                if any(sel):
                    ax.plot([x for x, k in zip(ts, sel) if k],
                            av["v"][sel].to_numpy(), "o", color="#333333", ms=3,
                            mfc="white", zorder=5, label="quiet-zone anchors")
        h, lb = ax.get_legend_handles_labels()
        seen, hh, ll = set(), [], []
        for x, y in zip(h, lb):
            if y not in seen:
                seen.add(y); hh.append(x); ll.append(y)
        ax.legend(hh, ll, loc="best", fontsize=9)
        ax.set_ylabel("voltage (V)")
        ax.set_title(f"Well monitor - raw vs old EMA vs new ladder vs model ({name})")
        ax.grid(alpha=0.3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %H:%M"))
        fig.autofmt_xdate()
        fig.tight_layout()
        out = HERE / f"report_{name}.png"
        fig.savefig(out, dpi=110)
        print(f"saved {out}")


if __name__ == "__main__":
    main()
