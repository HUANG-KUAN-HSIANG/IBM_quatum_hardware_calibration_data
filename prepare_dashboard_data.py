"""
Build the small data files used by dashboard.py.

Run once (needs the cleaned CSV produced by summarize_ibm.py):

    python prepare_dashboard_data.py

Inputs
    ibm_summary/ibm_calibration_clean.csv   one row per (backend, snapshot, qubit), ~305k rows
    ibm_summary/glitches.csv                glitch events detected by summarize_ibm.py
    coupling_map.csv                        qubit connections (backend, q1, q2)

Outputs in dashboard_data/
    daily.csv.gz      per (backend, qubit, day) median of each metric  (~84k rows)
    glitches.csv.gz   glitch events, one row per event (repeated snapshots of the
                      same calibration are collapsed into one event)
    layout.csv        2-D position of every qubit (same kamada-kawai layout as plot_ibm.py)
    edges.csv         coupling map
"""

from pathlib import Path

import networkx as nx
import pandas as pd

ROOT = Path(__file__).resolve().parent
CLEAN = ROOT / "ibm_summary" / "ibm_calibration_clean.csv"
GLITCHES = ROOT / "ibm_summary" / "glitches.csv"
COUPLING = ROOT / "coupling_map.csv"
OUT = ROOT / "dashboard_data"

METRICS = ["T1", "T2", "readout_error", "prob_meas0_prep1", "prob_meas1_prep0", "readout_length"]


def build_daily():
    df = pd.read_csv(CLEAN, usecols=["backend", "snapshot_time", "qubit", *METRICS])
    df["date"] = pd.to_datetime(df["snapshot_time"], utc=True).dt.strftime("%Y-%m-%d")
    daily = (df.groupby(["backend", "qubit", "date"], as_index=False)[METRICS]
               .median()
               .sort_values(["backend", "date", "qubit"]))
    daily.to_csv(OUT / "daily.csv.gz", index=False, float_format="%.6g")
    print(f"daily.csv.gz      {len(daily):>7} rows (from {len(df)} snapshot rows)")


def build_glitches():
    g = pd.read_csv(GLITCHES, usecols=["backend", "qubit", "metric", "snapshot_time",
                                       "value", "baseline", "direction"])
    g["snapshot_time"] = pd.to_datetime(g["snapshot_time"], utc=True)
    g = g.sort_values(["backend", "qubit", "metric", "snapshot_time"])
    # The same calibration value is often reported in several consecutive 6-hour
    # snapshots; keep only the first snapshot of each run -> one row per event.
    key = ["backend", "qubit", "metric"]
    same_as_prev = (g[key + ["value"]] == g.groupby(key)[key + ["value"]].shift()).all(axis=1)
    events = g[~same_as_prev].copy()
    events["date"] = events["snapshot_time"].dt.strftime("%Y-%m-%d")
    events["snapshot_time"] = events["snapshot_time"].dt.strftime("%Y-%m-%d %H:%M")
    events.to_csv(OUT / "glitches.csv.gz", index=False, float_format="%.6g")
    print(f"glitches.csv.gz   {len(events):>7} events (from {len(g)} flagged snapshots)")


def build_layout():
    cm = pd.read_csv(COUPLING)
    union = nx.Graph()
    union.add_nodes_from(range(156))
    union.add_edges_from(map(tuple, cm[["q1", "q2"]].values))
    try:
        pos = nx.kamada_kawai_layout(union)
    except Exception:
        pos = nx.spring_layout(union, seed=0, iterations=500)
    pd.DataFrame([(q, x, y) for q, (x, y) in pos.items()], columns=["qubit", "x", "y"]) \
      .sort_values("qubit").to_csv(OUT / "layout.csv", index=False, float_format="%.5f")
    cm.to_csv(OUT / "edges.csv", index=False)
    print(f"layout.csv        {len(pos):>7} qubits; edges.csv {len(cm)} couplers")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    build_daily()
    build_glitches()
    build_layout()
    print("done ->", OUT)
