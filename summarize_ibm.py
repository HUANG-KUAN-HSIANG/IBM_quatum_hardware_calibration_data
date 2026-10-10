"""
Summary + cleaning script for ibm_calibration_history.csv (output of get_ibm.py).

Usage
-----
    python summarize_ibm.py ibm_calibration_history.csv
    python summarize_ibm.py ibm_calibration_history.csv --coupling coupling_map.csv
    python summarize_ibm.py --fetch-coupling          # one-off: needs your IBM account, writes coupling_map.csv

Outputs (in --outdir, default ./ibm_summary)
-------------------------------------------
    ibm_calibration_clean.csv    cleaned data (no *_unit columns, T1/T2 in microseconds)
    summary.txt                  everything printed to the terminal
    t1t2_windows.csv             Q1: T1/T2 stability per backend x window
    high_error_qubits.csv        Q2: per-qubit mean error + high-error flag
    glitches.csv                 Q3: every detected glitch event
    glitch_rates.csv             Q3: glitch counts per backend x metric

Only needs pandas + numpy (qiskit_ibm_runtime only for --fetch-coupling).
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ----------------------------- config ---------------------------------------
WINDOWS_DAYS = [30, 60, 90, 180]
METRICS = ["T1", "T2", "readout_error"]       # variables used for the 3 questions
HIGH_ERR_METRIC = "readout_error"
HIGH_ERR_MAD_K = 3.0                          # high error = median + K * MAD (across qubits)
GLITCH_WINDOW = 14                            # snapshots in the rolling baseline
GLITCH_MIN_PERIODS = 7
GLITCH_Z = 5.0                                # robust z-score threshold
GLITCH_MIN_REL_CHANGE = 0.30                  # and at least 30% away from baseline
GAP_DAYS = 1.5                                # flag gaps between snapshots longer than this
N_PERM = 10000
# ----------------------------------------------------------------------------

UNIT_TO_US = {"s": 1e6, "ms": 1e3, "us": 1.0, "µs": 1.0, "μs": 1.0, "ns": 1e-3}


class Tee:
    """Print to terminal and keep text for summary.txt."""

    def __init__(self):
        self.lines = []

    def __call__(self, *args):
        text = " ".join(str(a) for a in args)
        print(text)
        self.lines.append(text)


out = Tee()


def section(title):
    out("\n" + "=" * 70)
    out(title)
    out("=" * 70)


def df_str(d, **kw):
    with pd.option_context("display.width", 200, "display.max_columns", 50,
                           "display.max_rows", 200, "display.float_format", "{:,.3f}".format):
        return d.to_string(**kw)


def mad(x):
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    return np.median(np.abs(x - np.median(x))) if len(x) else np.nan


# ----------------------------- fetch coupling map ----------------------------
def fetch_coupling(path="coupling_map.csv", backends=("ibm_fez", "ibm_marrakesh", "ibm_kingston")):
    from qiskit_ibm_runtime import QiskitRuntimeService
    service = QiskitRuntimeService()
    rows = []
    for name in backends:
        be = service.backend(name)
        for a, b in be.coupling_map.get_edges():
            rows.append({"backend": name, "q1": a, "q2": b})
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f"Saved {len(rows)} edges to {path}")


# ----------------------------- 1. load + quality ----------------------------
def load_and_check(path):
    section("1. DATA QUALITY CHECK")
    size_mb = Path(path).stat().st_size / 1e6
    df = pd.read_csv(path, low_memory=False)
    out(f"File: {path}  ({size_mb:,.1f} MB)")
    out(f"Shape: {df.shape[0]:,} rows x {df.shape[1]} columns "
        f"(memory {df.memory_usage(deep=True).sum() / 1e6:,.1f} MB)")

    required = ["backend", "snapshot_time", "qubit"]
    missing_req = [c for c in required if c not in df.columns]
    if missing_req:
        out(f"!! Missing required columns: {missing_req}")
        sys.exit(1)

    unit_cols = [c for c in df.columns if c.endswith("_unit")]
    value_cols = [c for c in df.columns if c not in required + unit_cols]
    out(f"Value columns ({len(value_cols)}): {value_cols}")
    out(f"Unit columns  ({len(unit_cols)}): {unit_cols}")
    missing_metrics = [m for m in METRICS if m not in df.columns]
    if missing_metrics:
        out(f"!! Expected metric columns not found: {missing_metrics} (they will be skipped)")

    # time parsing
    df["snapshot_time"] = pd.to_datetime(df["snapshot_time"], utc=True, errors="coerce")
    n_bad_time = df["snapshot_time"].isna().sum()
    out(f"\nUnparseable snapshot_time: {n_bad_time:,}")

    # units consistency
    out("\nUnits (unique values per unit column):")
    for c in unit_cols:
        out(f"  {c:28s} {df[c].dropna().unique().tolist()}")

    # missing values
    na = df[value_cols].isna().sum()
    na = pd.DataFrame({"n_missing": na, "pct": 100 * na / len(df)})
    out("\nMissing values (value columns, only those > 0):")
    out(df_str(na[na.n_missing > 0]) if (na.n_missing > 0).any() else "  none")

    # duplicates
    dup = df.duplicated(["backend", "snapshot_time", "qubit"]).sum()
    full_dup = df.duplicated().sum()
    out(f"\nDuplicate (backend, snapshot_time, qubit): {dup:,}   fully identical rows: {full_dup:,}")

    # coverage
    cov = df.groupby("backend").agg(
        rows=("qubit", "size"),
        qubits=("qubit", "nunique"),
        snapshots=("snapshot_time", "nunique"),
        first=("snapshot_time", "min"),
        last=("snapshot_time", "max"),
    )
    cov["span_days"] = (cov["last"] - cov["first"]).dt.total_seconds() / 86400
    out("\nCoverage per backend:")
    out(df_str(cov))

    # snapshots with an incomplete qubit set
    per_snap = df.groupby(["backend", "snapshot_time"])["qubit"].nunique().rename("n_qubits").reset_index()
    full = per_snap.groupby("backend")["n_qubits"].transform("max")
    incomplete = per_snap[per_snap.n_qubits < full]
    out(f"\nSnapshots with fewer qubits than the backend max: {len(incomplete)}")
    if len(incomplete):
        out(df_str(incomplete.head(10)))

    # gaps between snapshots
    out(f"\nGaps between consecutive snapshots > {GAP_DAYS} days:")
    gap_found = False
    for b, g in per_snap.groupby("backend"):
        t = g["snapshot_time"].sort_values()
        d = t.diff().dt.total_seconds() / 86400
        big = d[d > GAP_DAYS]
        if len(big):
            gap_found = True
            out(f"  {b}: {len(big)} gaps, largest {big.max():.1f} d "
                f"(e.g. ending {t.loc[big.index[0]]:%Y-%m-%d})")
        med = d.median()
        out(f"  {b}: median spacing {med:.2f} d  ({len(t)} snapshots)")
    if not gap_found:
        out("  none")

    return df, unit_cols, value_cols


# ----------------------------- cleaning -------------------------------------
def clean(df, unit_cols):
    section("2. CLEANING")
    n0 = len(df)
    log = []

    # convert T1/T2 to microseconds using their unit column
    for m in ("T1", "T2"):
        u = f"{m}_unit"
        if m in df.columns and u in df.columns:
            factor = df[u].map(UNIT_TO_US)
            unknown = factor.isna() & df[m].notna()
            if unknown.any():
                log.append(f"{m}: {unknown.sum()} rows with unknown unit -> left as NaN")
            df[m] = pd.to_numeric(df[m], errors="coerce") * factor
    log.append("T1/T2 converted to microseconds")

    df = df.dropna(subset=["snapshot_time"])
    log.append(f"dropped rows with bad snapshot_time: {n0 - len(df)}")

    n1 = len(df)
    df = df.drop_duplicates(["backend", "snapshot_time", "qubit"], keep="last")
    log.append(f"dropped duplicate (backend, snapshot_time, qubit): {n1 - len(df)}")

    # physically impossible values -> set to NaN (flag, don't lose the row)
    flags = {}
    for m in ("T1", "T2"):
        if m in df.columns:
            bad = df[m] <= 0
            flags[f"{m} <= 0"] = int(bad.sum())
            df.loc[bad, m] = np.nan
    if "T1" in df.columns and "T2" in df.columns:
        bad = df["T2"] > 2 * df["T1"] * 1.001           # physical limit T2 <= 2*T1
        flags["T2 > 2*T1 (unphysical, kept but flagged)"] = int(bad.sum())
        df["flag_T2_gt_2T1"] = bad
    for m in ("readout_error", "prob_meas0_prep1", "prob_meas1_prep0"):
        if m in df.columns:
            bad = (df[m] < 0) | (df[m] > 1)
            flags[f"{m} outside [0,1]"] = int(bad.sum())
            df.loc[bad, m] = np.nan
    # extreme T1/T2 outliers for information only
    for m in ("T1", "T2"):
        if m in df.columns:
            hi = df[m] > 1000
            flags[f"{m} > 1000 us (suspicious, kept)"] = int(hi.sum())

    for k, v in flags.items():
        log.append(f"flag: {k}: {v}")

    # drop rows with no T1 and no T2 and no readout_error (dead qubits / empty rows)
    present = [m for m in METRICS if m in df.columns]
    n2 = len(df)
    df = df.dropna(subset=present, how="all")
    log.append(f"dropped rows where all of {present} are missing: {n2 - len(df)}")

    df = df.drop(columns=unit_cols).sort_values(["backend", "qubit", "snapshot_time"]).reset_index(drop=True)
    for line in log:
        out("  -", line)
    out(f"\nRows: {n0:,} -> {len(df):,}   columns: {df.shape[1]}")
    return df


# ----------------------------- 3. Q1: T1/T2 stability -----------------------
def t1t2_windows(df, outdir):
    section("3. Q1  T1 / T2 STABILITY OVER 30 / 60 / 90 / 180 DAYS")
    out("Windows end at the last snapshot of each backend. CV = std / mean per qubit "
        "(then the median over qubits is reported).")
    out("median_abs_daily_change is the median, across qubits, of the absolute relative change "
        "between consecutive calendar days (each day is that qubit's median).\n")
    rows = []
    for b, g in df.groupby("backend"):
        end = g["snapshot_time"].max()
        for w in WINDOWS_DAYS:
            gw = g[g["snapshot_time"] > end - pd.Timedelta(days=w)]
            for m in ("T1", "T2"):
                if m not in gw.columns:
                    continue
                per_q = gw.groupby("qubit")[m].agg(["mean", "std", "count"])
                per_q = per_q[per_q["count"] >= 3]
                cv = per_q["std"] / per_q["mean"]
                # day-to-day relative change (one median per qubit per calendar day)
                s = gw.sort_values("snapshot_time")
                daily_q = (
                    s.assign(day=s["snapshot_time"].dt.floor("D"))
                    .groupby(["qubit", "day"], sort=True)[m]
                    .median()
                )
                rel = daily_q.groupby(level="qubit").pct_change().abs()
                # backend-level trend: slope of the daily median, in % per 30 days
                daily = s.groupby(s["snapshot_time"].dt.floor("D"))[m].median().dropna()
                slope_pct = np.nan
                if len(daily) >= 5:
                    x = (daily.index - daily.index[0]).days.values.astype(float)
                    slope = np.polyfit(x, daily.values, 1)[0]
                    slope_pct = 100 * slope * 30 / daily.mean()
                rows.append({
                    "backend": b, "window_days": w, "metric": m,
                    "n_snapshots": gw["snapshot_time"].nunique(),
                    "mean_us": gw[m].mean(), "median_us": gw[m].median(),
                    "p10_us": gw[m].quantile(0.10), "p90_us": gw[m].quantile(0.90),
                    "median_qubit_CV": cv.median(),
                    "p90_qubit_CV": cv.quantile(0.90),
                    "median_abs_daily_change": rel.median(),
                    "trend_pct_per_30d": slope_pct,
                })
    res = pd.DataFrame(rows)
    res.to_csv(outdir / "t1t2_windows.csv", index=False)
    out(df_str(res, index=False))
    out("\nReading guide: lower CV / daily change = more stable. trend_pct_per_30d > 0 means the "
        "backend median is improving; < 0 degrading.")


# ----------------------------- 4. Q2: high-error qubits ---------------------
def high_error(df, outdir, coupling_path):
    section("4. Q2  HIGH-ERROR QUBITS (and spatial clustering)")
    m = HIGH_ERR_METRIC
    if m not in df.columns:
        out(f"{m} not in data, skipped")
        return
    out(f"High-error rule: per-qubit mean {m} over all snapshots > median + {HIGH_ERR_MAD_K} x MAD "
        "(computed across the qubits of one backend).\n")

    per_q = df.groupby(["backend", "qubit"])[[c for c in METRICS if c in df.columns]].mean().reset_index()
    flags = []
    for b, g in per_q.groupby("backend"):
        thr = g[m].median() + HIGH_ERR_MAD_K * mad(g[m])
        g = g.assign(high_error=g[m] > thr, threshold=thr)
        flags.append(g)
        out(f"{b}: threshold {thr:.4f}, high-error qubits {int(g.high_error.sum())}/{len(g)}")
        top = g.sort_values(m, ascending=False).head(10)
        out("  top 10 qubits:", top[["qubit", m]].round(4).values.tolist())
    flagged = pd.concat(flags, ignore_index=True)
    flagged.to_csv(outdir / "high_error_qubits.csv", index=False)

    # distribution of the metric
    q = df.groupby("backend")[m].describe(percentiles=[.5, .9, .99])
    out("\nDistribution of per-row " + m + ":")
    out(df_str(q))

    # spatial test
    if coupling_path is None or not Path(coupling_path).exists():
        out("\nSpatial clustering test SKIPPED: no coupling map.")
        out("  Run `python summarize_ibm.py --fetch-coupling` once (needs your IBM account), then pass "
            "--coupling coupling_map.csv.")
        out("  Note: qubit index order is NOT physical position, so neighbours must come from the coupling map.")
        return

    cm = pd.read_csv(coupling_path)
    out("\nSpatial test: are high-error qubits adjacent to each other more often than chance?")
    out(f"  statistic = number of coupled pairs where BOTH qubits are high-error; "
        f"null = {N_PERM:,} random relabelings of the same number of high-error qubits.\n")
    rng = np.random.default_rng(0)
    rows = []
    for b, g in flagged.groupby("backend"):
        edges = cm[cm.backend == b][["q1", "q2"]].drop_duplicates().values
        edges = np.unique(np.sort(edges, axis=1), axis=0)       # undirected
        n_q = int(max(g.qubit.max(), edges.max())) + 1
        hi = np.zeros(n_q, dtype=bool)
        hi[g.loc[g.high_error, "qubit"].values] = True
        k = int(hi.sum())
        obs = int((hi[edges[:, 0]] & hi[edges[:, 1]]).sum())
        null = np.empty(N_PERM)
        for i in range(N_PERM):
            r = np.zeros(n_q, dtype=bool)
            r[rng.choice(n_q, k, replace=False)] = True
            null[i] = (r[edges[:, 0]] & r[edges[:, 1]]).sum()
        p = (np.sum(null >= obs) + 1) / (N_PERM + 1)
        rows.append({"backend": b, "n_high": k, "adjacent_high_pairs": obs,
                     "expected_by_chance": null.mean(), "p_value_clustered": p})
    out(df_str(pd.DataFrame(rows), index=False))
    out("\nSmall p (< 0.05) = high-error qubits cluster; large p = consistent with uniform scatter. "
        "With few high-error qubits the test has low power.")


# ----------------------------- 5. Q3: glitches ------------------------------
def glitches(df, outdir):
    section("5. Q3  GLITCHES (sudden spikes vs. a qubit's own recent baseline)")
    out(f"Glitch = value differs from the rolling median of the previous {GLITCH_WINDOW} snapshots by "
        f"robust z > {GLITCH_Z} AND > {GLITCH_MIN_REL_CHANGE:.0%} relative change.")
    out("CAUTION: IBM publishes about one calibration per day, so this finds jumps between "
        "consecutive calibrations, not spikes inside a day.\n")

    events = []
    for m in [c for c in METRICS if c in df.columns]:
        for (b, q), g in df.groupby(["backend", "qubit"]):
            s = g.set_index("snapshot_time")[m].dropna()
            if len(s) < GLITCH_MIN_PERIODS + 1:
                continue
            prev = s.shift(1)
            base = prev.rolling(GLITCH_WINDOW, min_periods=GLITCH_MIN_PERIODS).median()
            dev = (prev - base).abs()
            scale = 1.4826 * dev.rolling(GLITCH_WINDOW, min_periods=GLITCH_MIN_PERIODS).median()
            scale = scale.clip(lower=0.02 * base.abs()).replace(0, np.nan)   # floor: 2% of baseline
            z = (s - base) / scale
            rel = (s - base) / base.abs()
            hit = (z.abs() > GLITCH_Z) & (rel.abs() > GLITCH_MIN_REL_CHANGE)
            for t in s.index[hit.fillna(False)]:
                events.append({"backend": b, "qubit": q, "metric": m, "snapshot_time": t,
                               "value": s[t], "baseline": base[t], "rel_change": rel[t],
                               "robust_z": z[t], "direction": "up" if rel[t] > 0 else "down"})
    ev = pd.DataFrame(events)
    ev.to_csv(outdir / "glitches.csv", index=False)
    if ev.empty:
        out("No glitches detected with these thresholds.")
        return

    obs = (df.melt(id_vars=["backend", "qubit", "snapshot_time"],
                   value_vars=[c for c in METRICS if c in df.columns], var_name="metric")
             .dropna(subset=["value"]).groupby(["backend", "metric"]).size().rename("n_obs"))
    rate = (ev.groupby(["backend", "metric", "direction"]).size().unstack(fill_value=0)
              .assign(total=lambda d: d.sum(axis=1)).join(obs))
    rate["glitches_per_1000_obs"] = 1000 * rate["total"] / rate["n_obs"]
    rate = rate.reset_index()
    rate.to_csv(outdir / "glitch_rates.csv", index=False)
    out(df_str(rate, index=False))

    out("\nQubits with the most glitches (top 10 per backend, all metrics):")
    top = ev.groupby(["backend", "qubit"]).size().rename("n_glitches").reset_index()
    for b, g in top.groupby("backend"):
        out(f"  {b}: {g.sort_values('n_glitches', ascending=False).head(10)[['qubit', 'n_glitches']].values.tolist()}")

    share = ev.groupby(["backend", "qubit"]).ngroups
    out(f"\nQubit-backend pairs with >= 1 glitch: {share}")
    out("Largest 5 events:")
    out(df_str(ev.reindex(ev.rel_change.abs().sort_values(ascending=False).index).head(5), index=False))


# ----------------------------- main -----------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="?", default="ibm_calibration_history.csv")
    ap.add_argument("--coupling", default=None, help="coupling_map.csv (backend,q1,q2)")
    ap.add_argument("--outdir", default="ibm_summary")
    ap.add_argument("--fetch-coupling", action="store_true")
    args = ap.parse_args()

    if args.fetch_coupling:
        fetch_coupling()
        return

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    df, unit_cols, _ = load_and_check(args.csv)
    df = clean(df, unit_cols)
    df.to_csv(outdir / "ibm_calibration_clean.csv", index=False)
    out(f"Saved cleaned data to {outdir / 'ibm_calibration_clean.csv'}")

    t1t2_windows(df, outdir)
    high_error(df, outdir, args.coupling)
    glitches(df, outdir)

    (outdir / "summary.txt").write_text("\n".join(out.lines), encoding="utf-8")
    print(f"\nAll outputs in {outdir}/  (paste summary.txt back to me to interpret)")


if __name__ == "__main__":
    main()