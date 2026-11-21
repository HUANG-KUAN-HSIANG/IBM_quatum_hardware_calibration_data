"""
Interactive dashboard for exploring the IBM quantum calibration data.

Setup (once):
    pip install pandas numpy networkx scipy dash
    python prepare_dashboard_data.py      # only if dashboard_data/ is missing

Run:
    python dashboard.py
then open http://127.0.0.1:8050 in a browser.

The dashboard reads the small files in dashboard_data/ (per-qubit DAILY medians,
~80k rows), not the 305k-row raw snapshot file, so it starts in a few seconds.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, ctx, dcc, html, no_update
from plotly.subplots import make_subplots

DATA = Path(__file__).resolve().parent / "dashboard_data"

# ----------------------------- style -----------------------------------------
BACKENDS = ["ibm_fez", "ibm_kingston", "ibm_marrakesh"]
BACKEND_COLORS = {            # same categorical order as plot_ibm.py
    "ibm_fez": "#2a78d6",
    "ibm_kingston": "#eb6834",
    "ibm_marrakesh": "#1baf7a",
}
INK, INK_2, MUTED, GRID, SURFACE, PAGE = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1", "#ffffff", "#f6f5f2"
FONT = "Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"

LOG_FLOOR = 1e-4

# One-hue sequential scales, trimmed so the lightest step is still a visible tint
# (pure white would be confused with "no data").
SEQ_BLUE = [[0, "#d4e4f7"], [0.5, "#5b9bd9"], [1, "#0b3d7a"]]
SEQ_ORANGE = [[0, "#fde0c8"], [0.5, "#f08a4b"], [1, "#8a2c06"]]
SEQ_PURPLE = [[0, "#e4dcf3"], [0.5, "#9a7fd0"], [1, "#43257f"]]              # probabilities can be exactly 0; clip before log10

METRICS = {
    # key: label, unit, log by default, colorscale, short description
    "T1": dict(label="T1", unit="µs", log=False, cmap=SEQ_BLUE,
               desc="Relaxation time (longer is better)"),
    "T2": dict(label="T2", unit="µs", log=False, cmap=SEQ_BLUE,
               desc="Dephasing time (longer is better)"),
    "readout_error": dict(label="Readout error", unit="", log=True, cmap=SEQ_ORANGE,
                          desc="Probability of a wrong measurement (lower is better)"),
    "prob_meas0_prep1": dict(label="P(meas 0 | prep 1)", unit="", log=True, cmap=SEQ_ORANGE,
                             desc="Prepared |1>, measured 0 - mostly decay during readout"),
    "prob_meas1_prep0": dict(label="P(meas 1 | prep 0)", unit="", log=True, cmap=SEQ_ORANGE,
                             desc="Prepared |0>, measured 1 - thermal excitation / misassignment"),
    "readout_length": dict(label="Readout length", unit="ns", log=False, cmap=SEQ_PURPLE,
                           desc="Duration of the measurement pulse"),
}


def mlabel(m):
    u = METRICS[m]["unit"]
    return f"{METRICS[m]['label']} ({u})" if u else METRICS[m]["label"]


# ----------------------------- data ------------------------------------------
if not (DATA / "daily.csv.gz").exists():
    sys.exit("dashboard_data/ not found - run `python prepare_dashboard_data.py` first.")

DAILY = pd.read_csv(DATA / "daily.csv.gz")
GLITCH = pd.read_csv(DATA / "glitches.csv.gz")
GLITCH["t"] = pd.to_datetime(GLITCH["snapshot_time"])
LAYOUT = pd.read_csv(DATA / "layout.csv").set_index("qubit")
EDGES = pd.read_csv(DATA / "edges.csv")

DATES = sorted(DAILY["date"].unique())          # ISO strings, union over backends
QUBITS = list(range(156))

# Fixed color range per (backend, metric): 2nd-98th percentile over the whole
# period, so colors stay comparable while moving the day slider.
def _range(series, log):
    v = series.dropna().to_numpy()
    if log:
        v = np.log10(np.clip(v, LOG_FLOOR, None))
    lo, hi = np.percentile(v, [2, 98])
    return float(lo), float(hi if hi > lo else lo + 1e-9)

CRANGE = {(b, m, lg): _range(DAILY.loc[DAILY.backend == b, m], lg)
          for b in BACKENDS for m in METRICS for lg in (False, True)}

# Edge coordinates (union of couplers of one backend), built once per backend
def _edge_xy(backend):
    e = EDGES[EDGES.backend == backend]
    xs, ys = [], []
    for q1, q2 in zip(e.q1, e.q2):
        xs += [LAYOUT.at[q1, "x"], LAYOUT.at[q2, "x"], None]
        ys += [LAYOUT.at[q1, "y"], LAYOUT.at[q2, "y"], None]
    return xs, ys

EDGE_XY = {b: _edge_xy(b) for b in BACKENDS}

# Default qubit: the one with the most glitch events on the first backend
DEFAULT_QUBIT = int(GLITCH[GLITCH.backend == BACKENDS[0]].qubit.value_counts().idxmax())


def transform(v, log):
    return np.log10(np.clip(v, LOG_FLOOR, None)) if log else v


def colorbar(metric, log, lo, hi):
    cb = dict(title=dict(text=mlabel(metric), side="right", font=dict(size=11, color=INK_2)),
              thickness=10, len=0.85, outlinewidth=0, tickfont=dict(size=10, color=INK_2))
    if log:
        ticks = np.arange(np.floor(lo), np.ceil(hi) + 0.01, 1.0)
        fine = sorted({t + np.log10(k) for t in ticks for k in (1, 2, 5)})
        fine = [t for t in fine if lo - 1e-9 <= t <= hi + 1e-9] or [lo, hi]
        cb.update(tickvals=fine, ticktext=[f"{10 ** t:.2g}" for t in fine])
    return cb


def base_layout(fig, **kw):
    kw.setdefault("margin", dict(l=50, r=20, t=10, b=40))
    fig.update_layout(template="plotly_white", font=dict(family=FONT, size=11, color=INK_2),
                      paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
                      hoverlabel=dict(bgcolor="white", font=dict(family=FONT, color=INK)), **kw)
    fig.update_xaxes(gridcolor=GRID, zeroline=False, linecolor=GRID)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor=GRID)
    return fig


def filt(backend, start, end):
    d = DAILY[(DAILY.backend == backend) & (DAILY.date >= start) & (DAILY.date <= end)]
    g = GLITCH[(GLITCH.backend == backend) & (GLITCH.date >= start) & (GLITCH.date <= end)]
    return d, g


# ----------------------------- figures ---------------------------------------
def fig_chip(backend, metric, log, start, end, mode, day, sel_q):
    d, g = filt(backend, start, end)
    if mode == "day":
        vals = d[d.date == day].set_index("qubit")[metric]
        title_txt = f"{day}"
    else:
        vals = d.groupby("qubit")[metric].median()
        title_txt = f"median {start} → {end}"
    vals = vals.reindex(QUBITS)
    gcount = g.groupby("qubit").size().reindex(QUBITS, fill_value=0)
    lo, hi = CRANGE[(backend, metric, log)]
    ex, ey = EDGE_XY[backend]

    have = vals.notna()
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=ex, y=ey, mode="lines", line=dict(color="#cfcec9", width=1),
                             hoverinfo="skip", showlegend=False))
    if (~have).any():        # qubits with no data that day
        q = vals.index[~have]
        fig.add_trace(go.Scatter(
            x=LAYOUT.loc[q, "x"], y=LAYOUT.loc[q, "y"], mode="markers", customdata=list(q),
            marker=dict(size=13, color="#ecebe7", line=dict(color="#b9b8b2", width=1)),
            hovertemplate="Qubit %{customdata}<br>no data<extra></extra>", showlegend=False))
    q = vals.index[have]
    hover = [f"<b>Qubit {qq}</b><br>{mlabel(metric)}: {v:.4g}<br>glitch events: {gcount[qq]}"
             for qq, v in zip(q, vals[have])]
    fig.add_trace(go.Scatter(
        x=LAYOUT.loc[q, "x"], y=LAYOUT.loc[q, "y"], mode="markers", customdata=list(q),
        text=hover, hovertemplate="%{text}<extra></extra>", showlegend=False,
        marker=dict(size=13, color=transform(vals[have].to_numpy(), log),
                    colorscale=METRICS[metric]["cmap"], cmin=lo, cmax=hi,
                    colorbar=colorbar(metric, log, lo, hi),
                    line=dict(color="#9a9994", width=0.8))))
    if sel_q is not None:    # ring around the selected qubit
        fig.add_trace(go.Scatter(
            x=[LAYOUT.at[sel_q, "x"]], y=[LAYOUT.at[sel_q, "y"]], mode="markers",
            hoverinfo="skip", showlegend=False,
            marker=dict(size=22, color="rgba(0,0,0,0)", line=dict(color=INK, width=2))))
    base_layout(fig, height=470, margin=dict(l=10, r=10, t=30, b=10),
                title=dict(text=title_txt, x=0.01, y=0.98, font=dict(size=12, color=INK_2)))
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False, scaleanchor="x", scaleratio=1)
    return fig


def fig_heatmap(backend, metric, log, start, end, sel_q):
    d, _ = filt(backend, start, end)
    piv = d.pivot(index="qubit", columns="date", values=metric).reindex(QUBITS)
    lo, hi = CRANGE[(backend, metric, log)]
    z = transform(piv.to_numpy(dtype=float), log)
    fig = go.Figure(go.Heatmap(
        z=z, x=pd.to_datetime(piv.columns), y=piv.index, customdata=piv.to_numpy(),
        colorscale=METRICS[metric]["cmap"], zmin=lo, zmax=hi,
        colorbar=colorbar(metric, log, lo, hi), xgap=0, ygap=0,
        hovertemplate="Qubit %{y}<br>%{x|%Y-%m-%d}<br>" + mlabel(metric)
                      + ": %{customdata:.4g}<extra></extra>"))
    if sel_q is not None:
        fig.add_hrect(y0=sel_q - 0.5, y1=sel_q + 0.5, line=dict(color=INK, width=1.5),
                      fillcolor="rgba(0,0,0,0)")
    base_layout(fig, height=520, margin=dict(l=55, r=20, t=10, b=40))
    fig.update_yaxes(title="Qubit index", autorange="reversed", showgrid=False)
    fig.update_xaxes(showgrid=False)
    return fig


def fig_qubit(backend, qubit, start, end):
    d, g = filt(backend, start, end)
    d = d.assign(t=pd.to_datetime(d.date))
    one = d[d.qubit == qubit]
    chip = d.groupby("t")[["T1", "T2", "readout_error"]].median()
    g = g[g.qubit == qubit]
    rows = ["T1", "T2", "readout_error"]
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.05,
                        subplot_titles=[mlabel(m) for m in rows])
    col = BACKEND_COLORS[backend]
    for i, m in enumerate(rows, start=1):
        first = i == 1
        fig.add_trace(go.Scatter(x=chip.index, y=chip[m], mode="lines", name="Chip median",
                                 line=dict(color=MUTED, width=1.5, dash="dot"),
                                 legendgroup="chip", showlegend=first,
                                 hovertemplate="chip median: %{y:.4g}<extra></extra>"), row=i, col=1)
        fig.add_trace(go.Scatter(x=one.t, y=one[m], mode="lines", name=f"Qubit {qubit} (daily median)",
                                 line=dict(color=col, width=2), legendgroup="q", showlegend=first,
                                 hovertemplate=f"qubit {qubit}: %{{y:.4g}}<extra></extra>"), row=i, col=1)
        for direction, sym in (("up", "triangle-up"), ("down", "triangle-down")):
            gg = g[(g.metric == m) & (g.direction == direction)]
            fig.add_trace(go.Scatter(
                x=gg.t, y=gg.value, mode="markers", name=f"Glitch ({direction})",
                legendgroup=f"g{direction}", showlegend=False,
                marker=dict(symbol=sym, size=9, color=INK, line=dict(color="white", width=1)),
                customdata=gg.baseline,
                hovertemplate="glitch %{x|%Y-%m-%d %H:%M}<br>value: %{y:.4g}"
                              "<br>baseline: %{customdata:.4g}<extra></extra>"), row=i, col=1)
    # make sure both glitch legend entries appear once if any glitch exists
    seen = set()
    for tr in fig.data:
        if tr.name and tr.name.startswith("Glitch") and len(tr.x):
            tr.showlegend = tr.name not in seen
            seen.add(tr.name)
    fig.update_yaxes(type="log", dtick=1, row=3, col=1)
    base_layout(fig, height=470, margin=dict(l=55, r=15, t=30, b=30), hovermode="x unified",
                legend=dict(orientation="h", y=-0.08, x=0, font=dict(size=10)))
    for a in fig.layout.annotations:
        a.update(x=0, xanchor="left", font=dict(size=11, color=INK))
    return fig


def fig_compare(metric, log, start, end):
    d = DAILY[(DAILY.date >= start) & (DAILY.date <= end)]
    q = (d.groupby(["backend", "date"])[metric]
           .quantile([0.1, 0.5, 0.9]).unstack().reset_index())
    q["t"] = pd.to_datetime(q.date)
    fig = go.Figure()
    for b in BACKENDS:
        s = q[q.backend == b]
        c = BACKEND_COLORS[b]
        rgba = "rgba({},{},{},0.10)".format(*(int(c[i:i + 2], 16) for i in (1, 3, 5)))
        fig.add_trace(go.Scatter(x=s.t, y=s[0.9], mode="lines", line=dict(width=0),
                                 legendgroup=b, showlegend=False, hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=s.t, y=s[0.1], mode="lines", line=dict(width=0), fill="tonexty",
                                 fillcolor=rgba, legendgroup=b, showlegend=False, hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=s.t, y=s[0.5], mode="lines", name=b, legendgroup=b,
                                 line=dict(color=c, width=2),
                                 customdata=np.c_[s[0.1], s[0.9]],
                                 hovertemplate=f"{b}: %{{y:.4g}} "
                                               "(10–90%: %{customdata[0]:.3g}–%{customdata[1]:.3g})"
                                               "<extra></extra>"))
    base_layout(fig, height=340, hovermode="x unified",
                legend=dict(orientation="h", y=1.08, x=0))
    fig.update_yaxes(title=f"{mlabel(metric)} across qubits", type="log" if log else "linear",
                     dtick=1 if log else None)
    return fig


# ----------------------------- layout ----------------------------------------
CARD = dict(background=SURFACE, border=f"1px solid {GRID}", borderRadius="10px", padding="14px 16px")
H2 = dict(margin="0 0 2px 0", fontSize="15px", color=INK, fontWeight=600)
SUB = dict(margin="0 0 8px 0", fontSize="12px", color=INK_2)
LBL = dict(fontSize="11px", color=INK_2, fontWeight=600, textTransform="uppercase",
           letterSpacing="0.04em", marginBottom="4px")


def control(label, comp, width):
    return html.Div([html.Div(label, style=LBL), comp], style=dict(width=width))


def card(title, sub, *children, style=None):
    return html.Div([html.H2(title, style=H2), html.P(sub, style=SUB), *children],
                    style={**CARD, **(style or {})})


month_marks = {i: pd.Timestamp(d).strftime("%b") for i, d in enumerate(DATES) if d.endswith("-01")}

app = Dash(__name__, title="IBM Quantum Calibration Explorer")
app.layout = html.Div(style=dict(fontFamily=FONT, background=PAGE, color=INK, minHeight="100vh",
                                 padding="20px 28px"), children=[
    html.H1("IBM Quantum Calibration Explorer", style=dict(margin="0", fontSize="22px")),
    html.P("Daily calibration of 156 qubits on three IBM Heron chips (ibm_fez, ibm_kingston, "
           "ibm_marrakesh), 31 Mar – 27 Sep 2026. Each value is the daily median of the 6-hourly "
           "calibration snapshots. Click a qubit on the chip map or a row of the heatmap to inspect it.",
           style=dict(color=INK_2, fontSize="13px", margin="4px 0 16px 0", maxWidth="1000px")),

    # --- global controls (one row) ---
    html.Div(style=dict(display="flex", gap="18px", flexWrap="wrap", alignItems="flex-end",
                        marginBottom="14px"), children=[
        control("Backend", dcc.Dropdown(BACKENDS, BACKENDS[0], id="backend", clearable=False), "190px"),
        control("Metric", dcc.Dropdown([{"label": mlabel(m), "value": m} for m in METRICS],
                                       "T1", id="metric", clearable=False), "220px"),
        control("Date range", dcc.DatePickerRange(
            id="dates", min_date_allowed=DATES[0], max_date_allowed=DATES[-1],
            start_date=DATES[0], end_date=DATES[-1], display_format="YYYY-MM-DD"), "300px"),
        control("Color / y scale", dcc.RadioItems(
            [{"label": " Linear", "value": "lin"}, {"label": " Log", "value": "log"}], "lin",
            id="scale", inline=True, inputStyle=dict(marginLeft="8px"),
            style=dict(fontSize="13px", paddingBottom="8px")), "160px"),
        html.Div(id="metric-desc", style=dict(fontSize="12px", color=INK_2, paddingBottom="8px")),
    ]),

    # --- stat tiles ---
    html.Div(id="tiles", style=dict(display="grid", gridTemplateColumns="repeat(4, 1fr)",
                                    gap="12px", marginBottom="14px")),

    # --- chip map + qubit detail ---
    html.Div(style=dict(display="grid", gridTemplateColumns="1fr 1fr", gap="14px",
                        marginBottom="14px"), children=[
        card("Chip map", "Qubits placed by their physical couplings. Color = selected metric. "
                         "Gray = no calibration that day.",
             dcc.RadioItems([{"label": " Median over date range", "value": "period"},
                             {"label": " Single day (slider)", "value": "day"}], "period",
                            id="chip-mode", inline=True, inputStyle=dict(marginLeft="10px"),
                            style=dict(fontSize="12px")),
             dcc.Graph(id="chip", config=dict(displaylogo=False)),
             html.Div(style=dict(display="flex", gap="10px", alignItems="center"), children=[
                 html.Button("▶ Play", id="play", n_clicks=0,
                             style=dict(fontSize="12px", padding="4px 10px", cursor="pointer",
                                        border=f"1px solid {GRID}", borderRadius="6px",
                                        background=PAGE)),
                 html.Div(dcc.Slider(0, len(DATES) - 1, 1, value=len(DATES) - 1, id="day",
                                     marks=month_marks, updatemode="drag", allow_direct_input=False,
                                     tooltip=dict(placement="bottom", always_visible=False)),
                          style=dict(flex="1")),
                 html.Span(id="day-label", style=dict(fontSize="12px", color=INK_2, width="80px")),
             ]),
             dcc.Interval(id="tick", interval=350, disabled=True)),
        card("Qubit detail", "Daily median of the selected qubit vs. the chip median. "
                             "Triangles = glitch events (sudden jumps detected by summarize_ibm.py).",
             html.Div([html.Span("Qubit ", style=dict(fontSize="12px", color=INK_2)),
                       dcc.Dropdown(QUBITS, DEFAULT_QUBIT, id="qubit", clearable=False,
                                    style=dict(width="90px", display="inline-block",
                                               verticalAlign="middle"))]),
             dcc.Graph(id="qubit-fig", config=dict(displaylogo=False))),
    ]),

    card("Qubit × day heatmap", "Each row is a qubit, each column a day. Horizontal streaks = "
                                "persistently bad/good qubits; vertical stripes = chip-wide events.",
         dcc.Graph(id="heatmap", config=dict(displaylogo=False)), style=dict(marginBottom="14px")),

    card("Backend comparison", "Chip-wide median per day (line) and 10th–90th percentile across "
                               "the 156 qubits (band) for the selected metric.",
         dcc.Graph(id="compare", config=dict(displaylogo=False))),

    html.P("Data: IBM Quantum backend properties, sampled every 6 h. "
           "Source files: dashboard_data/ (built by prepare_dashboard_data.py).",
           style=dict(fontSize="11px", color=MUTED, marginTop="16px")),
])


# ----------------------------- callbacks -------------------------------------
@app.callback(Output("scale", "value"), Output("metric-desc", "children"), Input("metric", "value"))
def metric_default_scale(metric):
    return ("log" if METRICS[metric]["log"] else "lin"), METRICS[metric]["desc"]


@app.callback(Output("qubit", "value"),
              Input("chip", "clickData"), Input("heatmap", "clickData"),
              prevent_initial_call=True)
def select_qubit(chip_click, heat_click):
    if ctx.triggered_id == "chip" and chip_click:
        cd = chip_click["points"][0].get("customdata")
        return int(cd) if cd is not None else no_update
    if ctx.triggered_id == "heatmap" and heat_click:
        return int(heat_click["points"][0]["y"])
    return no_update


@app.callback(Output("tick", "disabled"), Output("play", "children"), Output("chip-mode", "value"),
              Input("play", "n_clicks"), State("tick", "disabled"), prevent_initial_call=True)
def toggle_play(_, disabled):
    playing = disabled          # was stopped -> start
    return (not playing), ("❚❚ Pause" if playing else "▶ Play"), ("day" if playing else no_update)


@app.callback(Output("day", "value"), Input("tick", "n_intervals"),
              State("day", "value"), State("dates", "start_date"), State("dates", "end_date"),
              prevent_initial_call=True)
def advance(_, day, start, end):
    lo, hi = DATES.index(max(start[:10], DATES[0])), DATES.index(min(end[:10], DATES[-1]))
    return lo if day >= hi or day < lo else day + 1


@app.callback(Output("day-label", "children"), Input("day", "value"))
def day_label(i):
    return DATES[i]


@app.callback(Output("tiles", "children"),
              Input("backend", "value"), Input("dates", "start_date"), Input("dates", "end_date"))
def tiles(backend, start, end):
    d, g = filt(backend, start[:10], end[:10])
    items = [
        ("Median T1", f"{d.T1.median():.0f} µs"),
        ("Median T2", f"{d.T2.median():.0f} µs"),
        ("Median readout error", f"{d.readout_error.median() * 100:.2f} %"),
        ("Glitch events", f"{len(g):,}  ·  {g.qubit.nunique()} qubits"),
    ]
    return [html.Div([html.Div(k, style=LBL),
                      html.Div(v, style=dict(fontSize="22px", fontWeight=600, color=INK))],
                     style=CARD) for k, v in items]


@app.callback(Output("chip", "figure"),
              Input("backend", "value"), Input("metric", "value"), Input("scale", "value"),
              Input("dates", "start_date"), Input("dates", "end_date"),
              Input("chip-mode", "value"), Input("day", "value"), Input("qubit", "value"))
def update_chip(backend, metric, scale, start, end, mode, day, q):
    return fig_chip(backend, metric, scale == "log", start[:10], end[:10], mode, DATES[day], q)


@app.callback(Output("heatmap", "figure"),
              Input("backend", "value"), Input("metric", "value"), Input("scale", "value"),
              Input("dates", "start_date"), Input("dates", "end_date"), Input("qubit", "value"))
def update_heatmap(backend, metric, scale, start, end, q):
    return fig_heatmap(backend, metric, scale == "log", start[:10], end[:10], q)


@app.callback(Output("qubit-fig", "figure"),
              Input("backend", "value"), Input("qubit", "value"),
              Input("dates", "start_date"), Input("dates", "end_date"))
def update_qubit(backend, q, start, end):
    return fig_qubit(backend, q, start[:10], end[:10])


@app.callback(Output("compare", "figure"),
              Input("metric", "value"), Input("scale", "value"),
              Input("dates", "start_date"), Input("dates", "end_date"))
def update_compare(metric, scale, start, end):
    return fig_compare(metric, scale == "log", start[:10], end[:10])


if __name__ == "__main__":
    app.run(debug=False, host="127.0.0.1", port=8050)
