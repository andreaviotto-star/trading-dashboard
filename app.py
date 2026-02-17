"""
Quant Trading System Analytics Dashboard
=========================================
Streamlit app for multi-system futures portfolio analysis.
Usage: streamlit run app.py
"""

import os
import re
import warnings
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import streamlit as st
import openpyxl

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — edit these at will
# ─────────────────────────────────────────────────────────────────────────────
DATA_DIR = Path(os.path.dirname(__file__))   # same folder as app.py

COMMISSION_PER_MICRO = 1.50    # USD round-trip per micro contract
COMMISSION_PER_MINI  = 15.00   # USD round-trip per full/mini contract

# Micro vs non-micro classification
MICRO_PREFIXES = {"MES", "MNQ", "MGC", "MCL"}

# Default allocations parsed from ReadMe.txt
# Format: filename_stem → (n_contracts, contract_type)
DEFAULT_ALLOC = {
    "CL_donchian__long_e_short":              (3,  "MCL"),
    "ES_breakout_sessione_regolare__long":     (1,  "MES"),
    "ES_breakout_sessione_regolare__short":    (3,  "MES"),
    "ES_donchian__short":                      (6,  "MES"),
    "ES_reversal_sessione_europea__long":      (7,  "MES"),   # 3+4 levels
    "ES_reversal_sessione_europea__short":     (7,  "MES"),   # 3+4 levels
    "ES_trend_stocastico__long":               (1,  "MES"),
    "ES_trend_stocastico__short":              (3,  "MES"),
    "NQ_breakout_sessione_continua__long_e_short": (1, "NQ"),
    "NQ_breakout_sessione_regolare__short":    (1,  "MNQ"),   # not in ReadMe, default 1 MNQ
    "NQ_trend_stocastico__long":              (1,  "MNQ"),
    "NQ_trend_stocastico__short":             (3,  "MNQ"),
    "NQ_opening_range_breakout_custom__long_e_short": (3, "MNQ"),
    "GC_breakout__long_e_short":              (3,  "MGC"),
    "GC_donchian__long_e_short":              (1,  "GC"),
    "GC_sessione_asiatica__long":             (2,  "MGC"),
    "GC_weekend_bias__long":                  (5,  "MGC"),
    "ES_opening_range_breakout_custom__long_e_short": (3, "MES"),
}

COLORS = px.colors.qualitative.Plotly + px.colors.qualitative.Dark24

# ─────────────────────────────────────────────────────────────────────────────
# DATE PARSING
# ─────────────────────────────────────────────────────────────────────────────

def parse_ts_date(val) -> pd.Timestamp | None:
    """Parse TradeStation date — handles both datetime objects and DD/MM/YYYY strings."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return pd.Timestamp(val)
    if isinstance(val, pd.Timestamp):
        return val
    s = str(val).strip()
    if not s:
        return None
    # Try DD/MM/YYYY HH:MM or DD/MM/YYYY
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            return pd.Timestamp(datetime.strptime(s, fmt))
        except ValueError:
            continue
    try:
        return pd.Timestamp(s)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# DATA EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def extract_performance_summary(ws) -> dict:
    """Parse Performance Summary sheet into a dict of metrics."""
    metrics = {}
    for row in ws.iter_rows(values_only=True):
        if row[0] is None:
            continue
        key = str(row[0]).strip()
        val = row[1] if len(row) > 1 else None
        if val is not None and val != "n/a":
            try:
                metrics[key] = float(val)
            except (TypeError, ValueError):
                metrics[key] = val
    return metrics


def extract_trades(ws) -> pd.DataFrame:
    """
    Parse Trades List sheet.
    Returns DataFrame with columns: trade_id, entry_date, exit_date, pnl, cum_pnl
    """
    records = []
    header_found = False
    current_trade = {}

    for row in ws.iter_rows(values_only=True):
        # Skip until we find the header
        if not header_found:
            if row[0] == "#":
                header_found = True
            continue

        if row[0] is not None and str(row[0]).isdigit() or (
            row[0] is not None and isinstance(row[0], (int, float)) and row[0] == int(row[0])
        ):
            # Entry row
            current_trade = {
                "trade_id":   int(row[0]),
                "entry_date": parse_ts_date(row[2]),
                "direction":  str(row[1]) if row[1] else "",
                "entry_price": row[4],
                "pnl":        row[7],   # Net Profit column on entry row
            }
        elif row[0] is None and row[1] is not None:
            # Exit row
            exit_date = parse_ts_date(row[2])
            cum_pnl   = row[7]
            pnl       = row[6]
            if current_trade:
                records.append({
                    "trade_id":   current_trade.get("trade_id"),
                    "entry_date": current_trade.get("entry_date"),
                    "exit_date":  exit_date,
                    "direction":  current_trade.get("direction", ""),
                    "pnl":        pnl if pnl is not None else current_trade.get("pnl"),
                    "cum_pnl":    cum_pnl,
                })
                current_trade = {}

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    # Ensure numeric
    for c in ["pnl", "cum_pnl"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # Sort by exit_date
    df = df.dropna(subset=["exit_date"]).sort_values("exit_date").reset_index(drop=True)
    return df


def build_daily_equity(trades: pd.DataFrame) -> pd.Series:
    """
    Build a daily equity series from cumulative P&L at each trade exit.
    Returns a Series indexed by date (date only), values = cumulative P&L.
    """
    if trades.empty:
        return pd.Series(dtype=float)

    # Use exit_date → cum_pnl
    eq = trades.set_index("exit_date")["cum_pnl"].sort_index()
    eq.index = pd.to_datetime(eq.index).normalize()   # date only

    # Keep last value per day
    eq = eq.groupby(eq.index).last()

    # Forward-fill to a complete daily series
    if len(eq) < 2:
        return eq
    all_days = pd.date_range(eq.index.min(), eq.index.max(), freq="B")
    eq = eq.reindex(all_days).ffill()
    return eq


# ─────────────────────────────────────────────────────────────────────────────
# LOAD ALL SYSTEMS
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def load_all_systems(data_dir: str) -> dict:
    """Load all .xlsx files and return a dict of system_name → data."""
    systems = {}
    path = Path(data_dir)

    for f in sorted(path.glob("*.xlsx")):
        stem = f.stem
        try:
            wb = openpyxl.load_workbook(str(f), data_only=True)

            # Performance Summary
            perf = {}
            if "Performance Summary" in wb.sheetnames:
                perf = extract_performance_summary(wb["Performance Summary"])

            # Trades
            trades = pd.DataFrame()
            if "Trades List" in wb.sheetnames:
                trades = extract_trades(wb["Trades List"])

            # Build equity curve
            equity = build_daily_equity(trades)

            # Contract info
            alloc = DEFAULT_ALLOC.get(stem, (1, "MES"))
            n_default, ctype = alloc
            is_micro = ctype in MICRO_PREFIXES
            comm_per_trade = COMMISSION_PER_MICRO if is_micro else COMMISSION_PER_MINI

            # Symbol from filename
            symbol = stem.split("_")[0]   # CL, ES, GC, NQ

            systems[stem] = {
                "name":          stem.replace("_", " ").replace("  ", " | "),
                "symbol":        symbol,
                "contract_type": ctype,
                "n_default":     n_default,
                "is_micro":      is_micro,
                "comm_per_trade": comm_per_trade,
                "perf":          perf,
                "trades":        trades,
                "equity":        equity,   # cum P&L for 1 contract, NO commissions
            }
        except Exception as e:
            st.warning(f"Could not load {f.name}: {e}")

    return systems


# ─────────────────────────────────────────────────────────────────────────────
# METRICS
# ─────────────────────────────────────────────────────────────────────────────

def compute_metrics(equity: pd.Series, trades: pd.DataFrame,
                    n_contracts: float, comm_per_trade: float,
                    label: str = "") -> dict:
    """Compute quant metrics for a scaled equity series."""
    if equity.empty or len(equity) < 5:
        return {}

    # Scale P&L
    scaled = equity * n_contracts

    # Total commissions
    n_trades = len(trades) if not trades.empty else 0
    total_comm = n_trades * comm_per_trade * n_contracts
    scaled_net = scaled - total_comm   # subtract commissions linearly? 
    # Better: reduce final equity only (TradeStation doesn't include commissions)
    # We'll apply commissions as a running subtraction proportional to trade count per day
    if not trades.empty and n_trades > 0:
        comm_per_day = total_comm / n_trades
        # Build cumulative commission per day
        trade_dates = trades["exit_date"].dt.normalize()
        trades_per_day = trade_dates.value_counts().sort_index()
        cum_comm = trades_per_day.reindex(scaled.index, fill_value=0).cumsum() * comm_per_trade * n_contracts
        scaled_net = scaled - cum_comm
    else:
        scaled_net = scaled.copy()

    # Daily returns
    daily_ret = scaled_net.diff().dropna()

    # Drawdown
    running_max = scaled_net.cummax()
    dd = scaled_net - running_max
    max_dd = dd.min()

    # Annualised metrics
    n_years = max((scaled_net.index[-1] - scaled_net.index[0]).days / 365.25, 0.01)
    net_profit = scaled_net.iloc[-1] - scaled_net.iloc[0]
    ann_return = net_profit / n_years

    daily_std = daily_ret.std()
    ann_vol = daily_std * np.sqrt(252)

    sharpe = (ann_return / ann_vol) if ann_vol > 0 else 0
    calmar = (ann_return / abs(max_dd)) if max_dd < 0 else 0

    # Profit factor
    wins   = daily_ret[daily_ret > 0].sum()
    losses = abs(daily_ret[daily_ret < 0].sum())
    pf     = (wins / losses) if losses > 0 else np.inf

    # Recovery factor
    rec_factor = (net_profit / abs(max_dd)) if max_dd < 0 else 0

    return {
        "Net Profit ($)":   round(net_profit, 0),
        "Ann. Return ($)":  round(ann_return, 0),
        "Max Drawdown ($)": round(max_dd, 0),
        "Sharpe Ratio":     round(sharpe, 2),
        "Calmar Ratio":     round(calmar, 2),
        "Profit Factor":    round(pf, 2),
        "Ann. Volatility":  round(ann_vol, 0),
        "Recovery Factor":  round(rec_factor, 2),
        "# Trades":         n_trades,
        "Total Comm ($)":   round(total_comm, 0),
    }


def scaled_equity_with_comm(equity: pd.Series, trades: pd.DataFrame,
                             n_contracts: float, comm_per_trade: float) -> pd.Series:
    """Return equity scaled & net of commissions."""
    if equity.empty:
        return pd.Series(dtype=float)
    scaled = equity * n_contracts
    if not trades.empty and len(trades) > 0:
        total_comm = len(trades) * comm_per_trade * n_contracts
        trade_dates = trades["exit_date"].dt.normalize()
        tpd = trade_dates.value_counts().sort_index()
        cum_comm = tpd.reindex(scaled.index, fill_value=0).cumsum() * comm_per_trade * n_contracts
        return scaled - cum_comm
    return scaled


# ─────────────────────────────────────────────────────────────────────────────
# PORTFOLIO HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def combine_equity_curves(curves: dict[str, pd.Series],
                           lookback_years: float = 2) -> pd.DataFrame:
    """Align multiple equity curves on a common daily index, last N years."""
    if not curves:
        return pd.DataFrame()

    df = pd.concat(curves.values(), axis=1, keys=curves.keys())
    df = df.sort_index().ffill()

    # Trim to lookback
    cutoff = df.index.max() - pd.Timedelta(days=int(lookback_years * 365))
    df = df[df.index >= cutoff]
    df = df.ffill().fillna(0)
    return df


def portfolio_daily_pnl(eq_df: pd.DataFrame) -> pd.Series:
    """Daily P&L of the combined portfolio."""
    return eq_df.sum(axis=1).diff().fillna(0)


def correlation_matrix(eq_df: pd.DataFrame) -> pd.DataFrame:
    """Pearson correlation of daily returns."""
    daily = eq_df.diff().dropna()
    return daily.corr()


def compute_portfolio_metrics(port_eq: pd.Series) -> dict:
    """High-level portfolio metrics."""
    if port_eq.empty or len(port_eq) < 5:
        return {}
    daily = port_eq.diff().dropna()
    net   = port_eq.iloc[-1] - port_eq.iloc[0]
    n_yr  = max((port_eq.index[-1] - port_eq.index[0]).days / 365.25, 0.01)
    ann_r = net / n_yr
    ann_v = daily.std() * np.sqrt(252)
    sharpe= ann_r / ann_v if ann_v > 0 else 0
    run_mx= port_eq.cummax()
    dd    = (port_eq - run_mx).min()
    calmar= ann_r / abs(dd) if dd < 0 else 0
    return {
        "Net Profit ($)":   round(net, 0),
        "Ann. Return ($)":  round(ann_r, 0),
        "Max Drawdown ($)": round(dd, 0),
        "Ann. Volatility":  round(ann_v, 0),
        "Sharpe Ratio":     round(sharpe, 2),
        "Calmar Ratio":     round(calmar, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# PORTFOLIO OPTIMISATION (simple)
# ─────────────────────────────────────────────────────────────────────────────

def recommend_portfolios(eq_df: pd.DataFrame,
                          systems: dict,
                          sizing: dict) -> list[dict]:
    """Return 3 recommended portfolio configurations."""
    daily = eq_df.diff().dropna()
    if daily.empty or daily.shape[1] < 2:
        return []

    means = daily.mean()
    stds  = daily.std()
    sharpes = (means / stds).replace([np.inf, -np.inf], 0)
    corr  = daily.corr()

    recommendations = []

    # 1 — Max Sharpe (top half by Sharpe, exclude highly correlated pairs)
    top = sharpes.nlargest(min(8, len(sharpes))).index.tolist()
    # Remove one from any pair with corr > 0.85
    selected = []
    for s in top:
        if not selected:
            selected.append(s)
        else:
            max_c = max(corr.loc[s, x] for x in selected if x in corr.columns)
            if max_c < 0.85:
                selected.append(s)
    recommendations.append({
        "name":        "🏆 Max Sharpe",
        "description": "Top-Sharpe systems, low mutual correlation (<0.85)",
        "systems":     selected,
    })

    # 2 — Low Correlation (maximise diversification via min avg pairwise corr)
    n = min(6, len(corr))
    names = corr.columns.tolist()
    best_set, best_score = None, np.inf
    # Greedy: start with highest-Sharpe, add lowest-avg-corr
    seed = sharpes.idxmax()
    grp  = [seed]
    remaining = [x for x in names if x != seed]
    while len(grp) < n and remaining:
        avg_corrs = {r: corr.loc[r, grp].mean() for r in remaining}
        nxt = min(avg_corrs, key=avg_corrs.get)
        grp.append(nxt)
        remaining.remove(nxt)
    recommendations.append({
        "name":        "🌐 Max Diversification",
        "description": "Minimise average pairwise correlation across systems",
        "systems":     grp,
    })

    # 3 — Low Drawdown (negative Sharpe filtered, then rank by Calmar-proxy)
    dd_scores = {}
    for col in daily.columns:
        cum = daily[col].cumsum()
        dd  = (cum - cum.cummax()).min()
        mean= daily[col].mean()
        dd_scores[col] = mean / abs(dd) if dd < 0 else 0
    best_calmar = sorted(dd_scores, key=dd_scores.get, reverse=True)[:6]
    recommendations.append({
        "name":        "🛡 Min Drawdown",
        "description": "Best mean-return / max-drawdown ratio per system",
        "systems":     best_calmar,
    })

    return recommendations


# ─────────────────────────────────────────────────────────────────────────────
# PLOTTING HELPERS
# ─────────────────────────────────────────────────────────────────────────────

DARK_TEMPLATE = "plotly_dark"

def plot_equity(eq: pd.Series, name: str, color: str = "#00d4ff") -> go.Figure:
    """Single equity curve with drawdown subplot."""
    run_max = eq.cummax()
    dd = (eq - run_max)

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.7, 0.3], vertical_spacing=0.04)

    fig.add_trace(go.Scatter(x=eq.index, y=eq.values, name=name,
                             line=dict(color=color, width=2),
                             hovertemplate="%{x|%Y-%m-%d}<br>Equity: $%{y:,.0f}<extra></extra>"),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=eq.index, y=run_max.values, name="Peak",
                             line=dict(color="#666", width=1, dash="dot"), showlegend=False),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=eq.index, y=dd.values, name="Drawdown",
                             fill="tozeroy", line=dict(color="#ff4444", width=1),
                             fillcolor="rgba(255,68,68,0.25)",
                             hovertemplate="%{x|%Y-%m-%d}<br>DD: $%{y:,.0f}<extra></extra>"),
                  row=2, col=1)

    fig.update_layout(template=DARK_TEMPLATE, height=500,
                      margin=dict(l=0, r=0, t=10, b=0),
                      legend=dict(orientation="h", y=1.05),
                      yaxis_title="Cum. P&L ($)",
                      yaxis2_title="Drawdown ($)")
    return fig


def plot_portfolio_equity(port_eq: pd.Series, eq_df: pd.DataFrame) -> go.Figure:
    """Portfolio equity with individual system contribution bands."""
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.65, 0.35], vertical_spacing=0.04)

    # Stacked area for each system
    for i, col in enumerate(eq_df.columns):
        fig.add_trace(go.Scatter(
            x=eq_df.index, y=eq_df[col].values,
            name=col.replace("_", " ")[:30],
            stackgroup="one",
            line=dict(width=0.5),
            fillcolor=COLORS[i % len(COLORS)].replace("rgb", "rgba").replace(")", ",0.6)"),
            hovertemplate=f"{col[:20]}<br>%{{x|%Y-%m-%d}}<br>${{y:,.0f}}<extra></extra>",
        ), row=1, col=1)

    # Portfolio total line
    fig.add_trace(go.Scatter(x=port_eq.index, y=port_eq.values,
                             name="Portfolio Total",
                             line=dict(color="white", width=2.5),
                             hovertemplate="%{x|%Y-%m-%d}<br>Total: $%{y:,.0f}<extra></extra>"),
                  row=1, col=1)

    # Drawdown
    pk = port_eq.cummax()
    dd = port_eq - pk
    fig.add_trace(go.Scatter(x=port_eq.index, y=dd.values, name="Portfolio DD",
                             fill="tozeroy", line=dict(color="#ff4444", width=1),
                             fillcolor="rgba(255,68,68,0.3)",
                             showlegend=True), row=2, col=1)

    fig.update_layout(template=DARK_TEMPLATE, height=580,
                      margin=dict(l=0, r=0, t=10, b=0),
                      yaxis_title="Cum. P&L ($)",
                      yaxis2_title="Drawdown ($)")
    return fig


def plot_correlation(corr: pd.DataFrame) -> go.Figure:
    """Annotated correlation heatmap."""
    labels = [c.replace("_", " ")[:25] for c in corr.columns]
    z = corr.values

    colorscale = [
        [0.0,  "#d73027"],
        [0.25, "#f46d43"],
        [0.5,  "#1a1a2e"],
        [0.75, "#74add1"],
        [1.0,  "#4575b4"],
    ]

    text = [[f"{v:.2f}" for v in row] for row in z]
    fig = go.Figure(go.Heatmap(
        z=z, x=labels, y=labels,
        text=text, texttemplate="%{text}",
        colorscale=colorscale,
        zmid=0, zmin=-1, zmax=1,
        colorbar=dict(title="ρ"),
    ))
    fig.update_layout(template=DARK_TEMPLATE, height=600,
                      margin=dict(l=0, r=0, t=30, b=0),
                      title="Pairwise Return Correlation",
                      xaxis_tickangle=-45)
    return fig


def plot_monthly_heatmap(equity: pd.Series, name: str) -> go.Figure:
    """Monthly returns heatmap."""
    monthly = equity.resample("ME").last().diff()
    df = pd.DataFrame({"val": monthly})
    df["year"]  = df.index.year
    df["month"] = df.index.month
    pivot = df.pivot(index="year", columns="month", values="val").fillna(0)
    pivot.columns = ["Jan","Feb","Mar","Apr","May","Jun",
                     "Jul","Aug","Sep","Oct","Nov","Dec"][:len(pivot.columns)]

    text = [[f"${v:,.0f}" for v in row] for row in pivot.values]
    fig = go.Figure(go.Heatmap(
        z=pivot.values, x=pivot.columns.tolist(), y=pivot.index.tolist(),
        text=text, texttemplate="%{text}",
        colorscale="RdYlGn", zmid=0,
        colorbar=dict(title="P&L $"),
    ))
    fig.update_layout(template=DARK_TEMPLATE, height=250,
                      margin=dict(l=0, r=0, t=30, b=0),
                      title=f"Monthly P&L — {name}")
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Quant Portfolio Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Inject custom CSS
st.markdown("""
<style>
    .main { background-color: #0e1117; }
    [data-testid="stMetricValue"] { font-size: 1.3rem; font-weight: 700; }
    [data-testid="stMetricLabel"] { font-size: 0.75rem; color: #aaa; }
    div.stTabs [data-baseweb="tab-list"] { gap: 6px; }
    div.stTabs [data-baseweb="tab"] { 
        height: 36px; padding: 0 16px;
        border-radius: 6px 6px 0 0;
        font-weight: 600;
    }
    .metric-card {
        background: #1c1f26; border-radius: 10px;
        padding: 14px 18px; margin: 4px 0;
    }
    h1, h2, h3 { color: #e8eaf6; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.image("https://img.icons8.com/fluency/48/stock-market.png", width=42)
    st.title("⚙️ Settings")

    data_dir_input = st.text_input("Data directory", value=str(DATA_DIR))
    lookback = st.slider("Lookback (years)", 1, 10, 2)

    st.divider()
    st.caption("**Commission rates**")
    COMMISSION_PER_MICRO = st.number_input("Micro RT ($)", value=1.50, step=0.25)
    COMMISSION_PER_MINI  = st.number_input("Mini/Full RT ($)", value=15.00, step=0.50)

    st.divider()
    st.caption("v1.0 — Quant Dashboard")


# ─────────────────────────────────────────────────────────────────────────────
# LOAD DATA
# ─────────────────────────────────────────────────────────────────────────────

with st.spinner("Loading trading systems…"):
    systems = load_all_systems(data_dir_input)

if not systems:
    st.error("No .xlsx files found in the specified directory.")
    st.stop()

n_sys = len(systems)
st.title(f"📊 Quant Portfolio Dashboard — {n_sys} Systems")

# ─────────────────────────────────────────────────────────────────────────────
# TABS
# ─────────────────────────────────────────────────────────────────────────────

tab1, tab2, tab3 = st.tabs(["🖥 Systems", "📦 Portfolio", "🔬 Correlation & Optimisation"])

# ═════════════════════════════════════════════════════════════════════════════
# TAB 1 — SYSTEMS
# ═════════════════════════════════════════════════════════════════════════════

with tab1:
    # ── Summary table ──────────────────────────────────────────────────────
    st.subheader("System Overview")

    rows = []
    for stem, sys in systems.items():
        eq = sys["equity"]
        trades = sys["trades"]
        nd = sys["n_default"]
        cpt = sys["comm_per_trade"]

        if eq.empty:
            rows.append({
                "System": sys["name"][:40],
                "Symbol": sys["symbol"],
                "Contract": f'{nd} {sys["contract_type"]}',
                "# Trades": len(trades),
                "Net Profit": "—",
                "Max DD": "—",
                "Sharpe": "—",
                "PF": "—",
                "Calmar": "—",
            })
            continue

        m = compute_metrics(eq, trades, nd, cpt)
        rows.append({
            "System": sys["name"][:40],
            "Symbol": sys["symbol"],
            "Contract": f'{nd} {sys["contract_type"]}',
            "# Trades":   m.get("# Trades", 0),
            "Net Profit": f'${m.get("Net Profit ($)", 0):,.0f}',
            "Max DD":     f'${m.get("Max Drawdown ($)", 0):,.0f}',
            "Sharpe":     m.get("Sharpe Ratio", 0),
            "PF":         m.get("Profit Factor", 0),
            "Calmar":     m.get("Calmar Ratio", 0),
        })

    summary_df = pd.DataFrame(rows)
    st.dataframe(
        summary_df,
        use_container_width=True,
        height=min(40 + 36 * n_sys, 600),
        hide_index=True,
    )

    st.divider()

    # ── Individual system explorer ──────────────────────────────────────────
    st.subheader("System Explorer")

    col_sel, col_size = st.columns([3, 1])
    with col_sel:
        chosen = st.selectbox("Select system", list(systems.keys()),
                              format_func=lambda x: systems[x]["name"])
    with col_size:
        nd_default = systems[chosen]["n_default"]
        n_sel = st.number_input("Contracts (override)", value=nd_default,
                                min_value=0, max_value=50, step=1)

    sys_data = systems[chosen]
    eq_raw   = sys_data["equity"]
    trades   = sys_data["trades"]
    cpt      = sys_data["comm_per_trade"]

    if eq_raw.empty:
        st.warning("No trades found for this system.")
    else:
        # Trim to lookback
        cutoff = eq_raw.index.max() - pd.Timedelta(days=int(lookback * 365))
        eq_trim = eq_raw[eq_raw.index >= cutoff]
        trades_trim = trades[trades["exit_date"] >= cutoff] if not trades.empty else trades

        # Scale
        eq_scaled = scaled_equity_with_comm(eq_trim, trades_trim, n_sel, cpt)
        eq_scaled -= eq_scaled.iloc[0]   # start at 0

        # Key metrics
        m = compute_metrics(eq_trim, trades_trim, n_sel, cpt)

        # Metric row
        mc = st.columns(5)
        metric_items = [
            ("Net Profit", f'${m.get("Net Profit ($)", 0):,.0f}'),
            ("Max DD",     f'${m.get("Max Drawdown ($)", 0):,.0f}'),
            ("Sharpe",     f'{m.get("Sharpe Ratio", 0):.2f}'),
            ("Profit Factor", f'{m.get("Profit Factor", 0):.2f}'),
            ("Calmar",     f'{m.get("Calmar Ratio", 0):.2f}'),
        ]
        for col, (lbl, val) in zip(mc, metric_items):
            col.metric(lbl, val)

        # Equity chart
        color_idx = list(systems.keys()).index(chosen)
        fig = plot_equity(eq_scaled, sys_data["name"],
                          color=COLORS[color_idx % len(COLORS)])
        st.plotly_chart(fig, use_container_width=True)

        # Monthly heatmap
        st.plotly_chart(plot_monthly_heatmap(eq_scaled, sys_data["name"]),
                        use_container_width=True)

        # Trades table
        if not trades.empty:
            with st.expander("📋 Trade list"):
                show_trades = trades_trim.copy()
                show_trades["pnl_scaled"] = show_trades["pnl"] * n_sel
                show_trades["cum_scaled"] = show_trades["cum_pnl"] * n_sel
                st.dataframe(
                    show_trades[["trade_id","entry_date","exit_date",
                                 "direction","pnl_scaled","cum_scaled"]]
                    .rename(columns={"pnl_scaled":"P&L ($)", "cum_scaled":"Cum P&L ($)"}),
                    use_container_width=True,
                    height=300,
                )


# ═════════════════════════════════════════════════════════════════════════════
# TAB 2 — PORTFOLIO
# ═════════════════════════════════════════════════════════════════════════════

with tab2:
    st.subheader("Portfolio Builder")

    # System selector
    all_names = list(systems.keys())
    selected_systems = st.multiselect(
        "Select systems to include",
        all_names,
        default=all_names,
        format_func=lambda x: systems[x]["name"],
    )

    if not selected_systems:
        st.info("Select at least one system to build the portfolio.")
        st.stop()

    # Sizing sliders — compact 3-column layout
    st.markdown("**Position sizing** (number of contracts per system):")
    n_cols = 3
    slider_cols = st.columns(n_cols)
    sizing = {}
    for i, stem in enumerate(selected_systems):
        sys_info = systems[stem]
        with slider_cols[i % n_cols]:
            n = st.slider(
                f"{sys_info['name'][:28]}",
                min_value=0,
                max_value=20,
                value=sys_info["n_default"],
                step=1,
                key=f"slider_{stem}",
                help=f"Default: {sys_info['n_default']} {sys_info['contract_type']}",
            )
            sizing[stem] = n

    st.divider()

    # Build scaled equity curves
    curves = {}
    for stem in selected_systems:
        n = sizing[stem]
        if n == 0:
            continue
        sys_info = systems[stem]
        eq_raw = sys_info["equity"]
        trades = sys_info["trades"]
        if eq_raw.empty:
            continue
        # Trim
        cutoff = eq_raw.index.max() - pd.Timedelta(days=int(lookback * 365))
        eq_t = eq_raw[eq_raw.index >= cutoff]
        tr_t = trades[trades["exit_date"] >= cutoff] if not trades.empty else trades
        eq_sc = scaled_equity_with_comm(eq_t, tr_t, n, sys_info["comm_per_trade"])
        # Normalise each to start at 0
        if not eq_sc.empty:
            eq_sc = eq_sc - eq_sc.iloc[0]
        curves[stem] = eq_sc

    if not curves:
        st.warning("No valid equity curves with the current sizing.")
        st.stop()

    # Align
    eq_df = combine_equity_curves(curves, lookback_years=lookback)
    port_eq = eq_df.sum(axis=1)

    # Portfolio metrics
    pm = compute_portfolio_metrics(port_eq)

    m_cols = st.columns(6)
    pm_items = [
        ("Portfolio P&L",    f'${pm.get("Net Profit ($)", 0):,.0f}'),
        ("Ann. Return",      f'${pm.get("Ann. Return ($)", 0):,.0f}'),
        ("Max Drawdown",     f'${pm.get("Max Drawdown ($)", 0):,.0f}'),
        ("Ann. Volatility",  f'${pm.get("Ann. Volatility", 0):,.0f}'),
        ("Sharpe",           f'{pm.get("Sharpe Ratio", 0):.2f}'),
        ("Calmar",           f'{pm.get("Calmar Ratio", 0):.2f}'),
    ]
    for col, (lbl, val) in zip(m_cols, pm_items):
        col.metric(lbl, val)

    # Portfolio chart
    st.plotly_chart(plot_portfolio_equity(port_eq, eq_df), use_container_width=True)

    # Per-system contribution bar
    st.subheader("System Contribution to Portfolio P&L")
    contrib = {stem: eq_df[stem].iloc[-1] - eq_df[stem].iloc[0]
               for stem in eq_df.columns}
    contrib_s = pd.Series(contrib).sort_values(ascending=True)
    bar_colors = ["#ff4444" if v < 0 else "#00cc88" for v in contrib_s.values]
    fig_bar = go.Figure(go.Bar(
        x=contrib_s.values,
        y=[systems[k]["name"][:35] for k in contrib_s.index],
        orientation="h",
        marker_color=bar_colors,
        text=[f"${v:,.0f}" for v in contrib_s.values],
        textposition="outside",
    ))
    fig_bar.update_layout(template=DARK_TEMPLATE, height=max(300, 35 * len(contrib_s)),
                          margin=dict(l=0, r=60, t=10, b=0),
                          xaxis_title="P&L Contribution ($)")
    st.plotly_chart(fig_bar, use_container_width=True)

    # Monthly portfolio heatmap
    st.plotly_chart(plot_monthly_heatmap(port_eq, "Portfolio"), use_container_width=True)

    # Drawdown analysis
    with st.expander("📉 Drawdown Analysis"):
        pk = port_eq.cummax()
        dd = port_eq - pk
        dd_series = dd[dd < 0]
        if not dd_series.empty:
            st.write(f"**Max Drawdown:** ${dd_series.min():,.0f}")
            st.write(f"**Avg Drawdown:** ${dd_series.mean():,.0f}")
            st.write(f"**Time in Drawdown:** {(len(dd_series)/len(dd)*100):.1f}%")
            fig_dd = go.Figure(go.Scatter(x=dd.index, y=dd.values,
                                          fill="tozeroy", line=dict(color="#ff4444"),
                                          fillcolor="rgba(255,68,68,0.25)"))
            fig_dd.update_layout(template=DARK_TEMPLATE, height=250,
                                  margin=dict(l=0, r=0, t=0, b=0),
                                  yaxis_title="Drawdown ($)")
            st.plotly_chart(fig_dd, use_container_width=True)


# ═════════════════════════════════════════════════════════════════════════════
# TAB 3 — CORRELATION & OPTIMISATION
# ═════════════════════════════════════════════════════════════════════════════

with tab3:
    st.subheader("Return Correlation Matrix")

    # Build full curves for all systems (default sizing)
    all_curves = {}
    for stem, sys_info in systems.items():
        eq_raw = sys_info["equity"]
        trades = sys_info["trades"]
        nd = sys_info["n_default"]
        cpt = sys_info["comm_per_trade"]
        if eq_raw.empty or nd == 0:
            continue
        cutoff = eq_raw.index.max() - pd.Timedelta(days=int(lookback * 365))
        eq_t = eq_raw[eq_raw.index >= cutoff]
        tr_t = trades[trades["exit_date"] >= cutoff] if not trades.empty else trades
        eq_sc = scaled_equity_with_comm(eq_t, tr_t, nd, cpt)
        if not eq_sc.empty:
            all_curves[stem] = eq_sc

    if len(all_curves) < 2:
        st.info("Need at least 2 systems with data for correlation analysis.")
    else:
        corr_df = combine_equity_curves(all_curves, lookback_years=lookback)
        corr_mat = correlation_matrix(corr_df)

        # Rename for display
        short_names = {c: systems[c]["name"][:30] for c in corr_mat.columns if c in systems}
        corr_display = corr_mat.rename(index=short_names, columns=short_names)

        st.plotly_chart(plot_correlation(corr_display), use_container_width=True)

        # Correlation stats
        upper = corr_mat.where(np.triu(np.ones(corr_mat.shape), k=1).astype(bool))
        vals  = upper.stack()
        c1, c2, c3 = st.columns(3)
        c1.metric("Avg. Pairwise Corr.", f"{vals.mean():.3f}")
        c2.metric("Max Corr.",           f"{vals.max():.3f}")
        c3.metric("Min Corr.",           f"{vals.min():.3f}")

        # Highly correlated pairs
        high_corr = vals[abs(vals) > 0.6].sort_values(ascending=False)
        if not high_corr.empty:
            with st.expander("⚠️ Highly correlated pairs (|ρ| > 0.6)"):
                hc_df = pd.DataFrame({
                    "System A": [systems.get(i[0], {}).get("name", i[0])[:35]
                                 for i in high_corr.index],
                    "System B": [systems.get(i[1], {}).get("name", i[1])[:35]
                                 for i in high_corr.index],
                    "Correlation": high_corr.values.round(3),
                })
                st.dataframe(hc_df, hide_index=True, use_container_width=True)

        st.divider()

        # ── Recommended Portfolios ──────────────────────────────────────────
        st.subheader("🎯 Recommended Portfolios")
        st.caption("Algorithmically selected sub-portfolios based on risk/return characteristics.")

        reccos = recommend_portfolios(corr_df, systems, sizing={s: systems[s]["n_default"]
                                                                 for s in systems})

        for rec in reccos:
            with st.expander(f"{rec['name']} — {rec['description']}"):
                rec_stems = rec["systems"]
                rec_curves = {s: all_curves[s] for s in rec_stems if s in all_curves}
                rec_eq_df  = combine_equity_curves(rec_curves, lookback_years=lookback)
                rec_port   = rec_eq_df.sum(axis=1)
                rpm = compute_portfolio_metrics(rec_port)

                # Metrics
                rm1, rm2, rm3, rm4 = st.columns(4)
                rm1.metric("Net P&L",   f'${rpm.get("Net Profit ($)", 0):,.0f}')
                rm2.metric("Sharpe",    f'{rpm.get("Sharpe Ratio", 0):.2f}')
                rm3.metric("Max DD",    f'${rpm.get("Max Drawdown ($)", 0):,.0f}')
                rm4.metric("Calmar",    f'{rpm.get("Calmar Ratio", 0):.2f}')

                # Systems list
                st.write("**Systems:**")
                for s in rec_stems:
                    if s in systems:
                        si = systems[s]
                        st.write(f"  • {si['name']} — {si['n_default']} {si['contract_type']}")

                # Mini equity chart
                rec_port_norm = rec_port - rec_port.iloc[0]
                fig_rec = plot_equity(rec_port_norm, rec["name"], color="#f0c040")
                st.plotly_chart(fig_rec, use_container_width=True)

        st.divider()

        # ── Scatter: Sharpe vs Max DD ──────────────────────────────────────
        st.subheader("Risk/Return Scatter")
        scatter_data = []
        daily = corr_df.diff().dropna()
        for stem in corr_df.columns:
            col_data = corr_df[stem]
            d = daily[stem]
            net = col_data.iloc[-1] - col_data.iloc[0]
            n_yr = max((col_data.index[-1] - col_data.index[0]).days / 365.25, 0.01)
            ann_r = net / n_yr
            ann_v = d.std() * np.sqrt(252)
            sh = ann_r / ann_v if ann_v > 0 else 0
            pk2 = col_data.cummax()
            dd2 = (col_data - pk2).min()
            scatter_data.append({
                "System": systems.get(stem, {}).get("name", stem)[:30],
                "Symbol": systems.get(stem, {}).get("symbol", ""),
                "Sharpe": round(sh, 2),
                "Max DD ($)": round(dd2, 0),
                "Ann. Return ($)": round(ann_r, 0),
            })
        sc_df = pd.DataFrame(scatter_data)
        if not sc_df.empty:
            fig_sc = px.scatter(
                sc_df, x="Max DD ($)", y="Sharpe",
                text="System", color="Symbol",
                size="Ann. Return ($)",
                size_max=35,
                template=DARK_TEMPLATE,
                title="Sharpe vs Max Drawdown (bubble = Ann. Return)",
                hover_data=["Ann. Return ($)"],
            )
            fig_sc.update_traces(textposition="top center")
            fig_sc.update_layout(height=500, margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig_sc, use_container_width=True)
