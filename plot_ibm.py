"""
Visualisations for the IBM calibration data: qubit connection map + qubit features.

Needs:  pip install pandas numpy matplotlib seaborn networkx scipy

Usage
-----
    python plot_ibm.py                                   # uses defaults below
    python plot_ibm.py --clean ibm_summary/ibm_calibration_clean.csv \
                       --coupling coupling_map.csv --outdir figures

Inputs
    --clean     cleaned CSV written by summarize_ibm.py
    --coupling  coupling_map.csv (backend,q1,q2) written by `summarize_ibm.py --fetch-coupling`

Figures (PNG, 200 dpi) in --outdir
    fig1_chip_map_T1.png / _T2.png / _readout_error.png   chip graph, nodes colored by per-qubit median
    fig2_distributions.png                                 histograms + box plots per backend
    fig3_heatmap_T1.png / _readout_error.png               qubit x day heatmap (drift + failure events)
    fig4_correlations.png                                  Spearman correlation of per-qubit features
"""

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker  # noqa: F401
import matplotlib.dates  # noqa: F401
import networkx as nx
import numpy as np
import pandas as pd
import seaborn as sns
from matplotlib.colors import LinearSegmentedColormap, LogNorm, Normalize

# ----------------------------- style ----------------------------------------
BACKEND_COLORS = {            # categorical slots 1-3 (colorblind-validated order)
    "ibm_fez": "#2a78d6",       # blue
    "ibm_kingston": "#eb6834",  # orange
    "ibm_marrakesh": "#1baf7a", # aqua
}
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e6e5e1"

CMAP_T = "Blues"              # one-hue sequential (coherence times: darker = longer)
CMAP_ERR = "Oranges"          # one-hue sequential (errors: darker = worse)
CMAP_DIV = LinearSegmentedColormap.from_list(   # blue <-> grey <-> red, grey midpoint
    "div", ["#256abf", "#f0efec", "#e34948"])

FEATURES = {
    "T1": {"label": "T1 (µs)", "cmap": CMAP_T, "log": False},
    "T2": {"label": "T2 (µs)", "cmap": CMAP_T, "log": False},
    "readout_error": {"label": "Readout error", "cmap": CMAP_ERR, "log": True},
}


def set_style():
    sns.set_theme(style="whitegrid", rc={
        "figure.facecolor": "white", "axes.facecolor": "white",
        "axes.edgecolor": GRID, "grid.color": GRID, "grid.linewidth": 0.6,
        "text.color": INK, "axes.labelcolor": INK_2, "xtick.color": INK_2,
        "ytick.color": INK_2, "axes.titlecolor": INK, "axes.titlesize": 11,
        "axes.titleweight": "bold", "axes.titlelocation": "left",
        "font.size": 9, "axes.spines.top": False, "axes.spines.right": False,
        "legend.frameon": False,
    })


def save(fig, outdir, name):
    path = outdir / name
    fig.savefig(path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print("saved", path)


# ----------------------------- data -----------------------------------------
def load(clean_path, coupling_path):
    df = pd.read_csv(clean_path, low_memory=False)
    df["snapshot_time"] = pd.to_datetime(df["snapshot_time"], utc=True)
    cm = pd.read_csv(coupling_path)
    backends = [b for b in BACKEND_COLORS if b in df.backend.unique()]
    graphs = {}
    for b in backends:
        e = cm[cm.backend == b][["q1", "q2"]].values
        g = nx.Graph()
        g.add_nodes_from(sorted(df[df.backend == b].qubit.unique()))
        g.add_edges_from(map(tuple, e))
        graphs[b] = g
    return df, graphs, backends


def shared_layout(graphs):
    """One layout for all chips so the three panels are directly comparable."""
    union = nx.Graph()
    for g in graphs.values():
        union.add_nodes_from(g.nodes)
        union.add_edges_from(g.edges)
    try:
        pos = nx.kamada_kawai_layout(union)
    except Exception:
        pos = nx.spring_layout(union, seed=0, iterations=500)
    return pos


# ----------------------------- fig 1: chip maps -----------------------------
def fig_chip_maps(df, graphs, backends, pos, outdir):
    for feat, meta in FEATURES.items():
        if feat not in df.columns:
            continue
        med = df.groupby(["backend", "qubit"])[feat].median()
        vals = med.dropna()
        vmin, vmax = vals.quantile(0.01), vals.quantile(0.99)
        norm = LogNorm(vmin=max(vmin, 1e-4), vmax=vmax) if meta["log"] else Normalize(vmin, vmax)

        fig, axes = plt.subplots(1, len(backends), figsize=(5.4 * len(backends), 6), squeeze=False)
        for ax, b in zip(axes[0], backends):
            g = graphs[b]
            nodes = list(g.nodes)
            v = med[b].reindex(nodes).values
            nx.draw_networkx_edges(g, pos, ax=ax, edge_color="#b9b8b2", width=1.0)
            nx.draw_networkx_nodes(g, pos, nodelist=nodes, ax=ax, node_size=170,
                                   node_color=v, cmap=meta["cmap"], vmin=norm.vmin, vmax=norm.vmax,
                                   edgecolors="white", linewidths=1.5)
            if meta["log"]:                           # LogNorm for color: re-draw with norm
                ax.collections[-1].set_norm(norm)
            # label the 5 worst qubits (lowest coherence / highest error)
            worst = (med[b].nlargest(5) if feat == "readout_error" else med[b].nsmallest(5)).index
            nx.draw_networkx_labels(g, pos, labels={q: str(q) for q in worst}, ax=ax,
                                    font_size=7, font_color=INK, font_weight="bold")
            ax.set_title(f"{b}  ({g.number_of_edges()} couplers)")
            ax.set_axis_off()
        sm = plt.cm.ScalarMappable(norm=norm, cmap=meta["cmap"])
        cb = fig.colorbar(sm, ax=axes.ravel().tolist(), shrink=0.6, pad=0.02, aspect=30)
        cb.set_label(f"Median {meta['label']} over the full period")
        if meta["log"]:
            cb.formatter = plt.matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}")
            cb.minorticks_off(); cb.update_ticks()
        cb.outline.set_visible(False)
        fig.suptitle(f"Chip connectivity colored by median {meta['label']}  (numbers = 5 worst qubits)",
                     x=0.01, ha="left", fontsize=12, fontweight="bold", color=INK)
        save(fig, outdir, f"fig1_chip_map_{feat}.png")


# ----------------------------- fig 2: distributions -------------------------
def fig_distributions(df, backends, outdir):
    feats = [f for f in FEATURES if f in df.columns]
    fig, axes = plt.subplots(2, len(feats), figsize=(4.6 * len(feats), 7))
    axes = np.atleast_2d(axes).reshape(2, -1)
    for j, feat in enumerate(feats):
        meta = FEATURES[feat]
        data = df[["backend", feat]].dropna()
        if meta["log"]:
            data = data[data[feat] > 0]
        ax = axes[0, j]
        sns.histplot(data=data, x=feat, hue="backend", hue_order=backends, palette=BACKEND_COLORS,
                     element="step", fill=False, stat="proportion", common_norm=False,
                     log_scale=meta["log"], bins=60, linewidth=1.6, ax=ax, legend=(j == 0))
        ax.set_xlabel(meta["label"])
        ax.set_ylabel("Proportion of rows" if j == 0 else "")
        ax.set_title(f"Distribution of {meta['label']}")
        ax = axes[1, j]
        sns.boxplot(data=data, y="backend", x=feat, order=backends, palette=BACKEND_COLORS, hue="backend",
                    legend=False, width=0.5, linewidth=1.0, fliersize=1.5, flierprops={"alpha": 0.35},
                    ax=ax)
        if meta["log"]:
            ax.set_xscale("log")
        ax.set_xlabel(meta["label"])
        ax.set_ylabel("")
        ax.set_title("Median, IQR, outliers")
    fig.suptitle("Qubit features by backend (all calibrations, all qubits)", x=0.01, ha="left",
                 fontsize=12, fontweight="bold", color=INK)
    fig.tight_layout()
    save(fig, outdir, "fig2_distributions.png")


# ----------------------------- fig 3: qubit x time heatmaps -----------------
def fig_heatmaps(df, backends, outdir):
    for feat in ("T1", "readout_error"):
        if feat not in df.columns:
            continue
        meta = FEATURES[feat]
        fig, axes = plt.subplots(len(backends), 1, figsize=(13, 3.6 * len(backends)), sharex=True)
        axes = np.atleast_1d(axes)
        daily = df.assign(day=df.snapshot_time.dt.floor("D"))
        allvals = daily[feat].dropna()
        lo, hi = allvals.quantile(0.01), allvals.quantile(0.99)
        norm = LogNorm(vmin=max(lo, 1e-3), vmax=hi) if meta["log"] else Normalize(lo, hi)
        for ax, b in zip(axes, backends):
            p = (daily[daily.backend == b].groupby(["qubit", "day"])[feat].median()
                 .unstack("day").sort_index())
            x0, x1 = p.columns.min(), p.columns.max()
            im = ax.imshow(p.values, aspect="auto", cmap=meta["cmap"], norm=norm, interpolation="nearest",
                           extent=[plt.matplotlib.dates.date2num(x0), plt.matplotlib.dates.date2num(x1),
                                   p.index.max() + 0.5, p.index.min() - 0.5])
            ax.xaxis_date()
            ax.set_ylabel("Qubit")
            ax.set_title(b)
            ax.grid(False)
        axes[-1].xaxis.set_major_formatter(plt.matplotlib.dates.DateFormatter("%b %d"))
        cb = fig.colorbar(im, ax=axes.tolist(), shrink=0.6, pad=0.01, aspect=30)
        cb.set_label(f"Daily median {meta['label']}")
        cb.outline.set_visible(False)
        fig.suptitle(f"{meta['label']} per qubit over time  (vertical stripes = chip-wide events, "
                     "horizontal stripes = persistently bad qubits)",
                     x=0.01, ha="left", fontsize=12, fontweight="bold", color=INK)
        save(fig, outdir, f"fig3_heatmap_{feat}.png")


# ----------------------------- fig 4: correlations --------------------------
def fig_correlations(df, graphs, backends, outdir):
    cols = [c for c in ["T1", "T2", "readout_error", "prob_meas0_prep1", "prob_meas1_prep0", "readout_length"]
            if c in df.columns]
    fig, axes = plt.subplots(1, len(backends), figsize=(5.6 * len(backends), 5.6), squeeze=False,
                             constrained_layout=True)
    for ax, b in zip(axes[0], backends):
        q = df[df.backend == b].groupby("qubit")[cols].median()
        q["degree"] = pd.Series(dict(graphs[b].degree())).reindex(q.index)
        q = q.loc[:, q.nunique() > 1]                 # drop constant columns (correlation undefined)
        corr = q.corr(method="spearman")
        mask = np.triu(np.ones_like(corr, dtype=bool), k=1)
        sns.heatmap(corr, mask=mask, cmap=CMAP_DIV, vmin=-1, vmax=1, center=0, annot=True, fmt=".2f",
                    annot_kws={"size": 8}, linewidths=1.5, linecolor="white", square=True,
                    cbar=False, ax=ax)
        ax.set_title(b)
        ax.tick_params(axis="x", rotation=45)
        for lbl in ax.get_xticklabels():
            lbl.set_ha("right")
        ax.grid(False)
    sm = plt.cm.ScalarMappable(norm=Normalize(-1, 1), cmap=CMAP_DIV)
    cb = fig.colorbar(sm, ax=axes.ravel().tolist(), shrink=0.6, aspect=25)
    cb.set_label("Spearman ρ")
    cb.outline.set_visible(False)
    fig.suptitle("Correlation between per-qubit median features (blue = negative, red = positive)",
                 x=0.01, ha="left", fontsize=12, fontweight="bold", color=INK)
    save(fig, outdir, "fig4_correlations.png")


# ----------------------------- main -----------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", default="ibm_summary/ibm_calibration_clean.csv")
    ap.add_argument("--coupling", default="coupling_map.csv")
    ap.add_argument("--outdir", default="figures")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    set_style()
    df, graphs, backends = load(args.clean, args.coupling)
    pos = shared_layout(graphs)

    fig_chip_maps(df, graphs, backends, pos, outdir)
    fig_distributions(df, backends, outdir)
    fig_heatmaps(df, backends, outdir)
    fig_correlations(df, graphs, backends, outdir)
    print(f"\nDone. Figures in {outdir}/")


if __name__ == "__main__":
    main()