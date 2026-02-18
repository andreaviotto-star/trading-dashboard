"""
Quant Trading System Analytics Dashboard  v4.0
================================================
Usage: streamlit run app.py

Changes v4:
  - DATE FIX: Excel swaps DD/MM → MM/DD for days ≤ 12. We swap back.
  - VALIDATION: checks last cum_pnl == B5 (Total Net Profit) per system.
  - SIZING: hardcoded SIZING_OVERRIDES, NO @st.cache_data (avoids stale cache).
"""

import os
import re
import warnings
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import streamlit as st
import openpyxl

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
DATA_DIR = Path(os.path.dirname(os.path.abspath(__file__)))

COMMISSION_PER_MICRO = 1.50
COMMISSION_PER_MINI  = 15.00

MICRO_PREFIXES = {"MES", "MNQ", "MGC", "MCL"}
FALLBACK_CTYPE = {"ES": "MES", "NQ": "MNQ", "GC": "MGC", "CL": "MCL"}

COLORS = px.colors.qualitative.Plotly + px.colors.qualitative.Dark24
DARK   = "plotly_dark"

# ─────────────────────────────────────────────────────────────────────────────
# SIZING — AUTO FROM ReadMe.txt
# ─────────────────────────────────────────────────────────────────────────────
# Reads ReadMe.txt and parses allocation lines.  Handles:
#   - Multi-level sums:  "3 MES + 4 MES …" → 7 MES
#   - History after comma: "1GC, prima erano 5 MGC" → 1 GC (only before comma)
#   - No-space tokens:  "1GC" → 1, GC

def parse_readme_allocations(readme_path):
    """
    Parse ReadMe.txt → dict of {normalized_name: (total_qty, primary_ctype)}.
    Only lines with a colon and at least one <N> <CTYPE> token are considered.
    """
    alloc_re = re.compile(r'(\d+)\s*([A-Z]{2,3})')
    alloc_dict = {}

    try:
        with open(readme_path, "r", encoding="utf-8", errors="ignore") as f:
            for raw_line in f:
                line = raw_line.strip()
                if ':' not in line:
                    continue

                colon = line.index(':')
                name_part  = line[:colon].strip()
                alloc_part = line[colon + 1:].strip()

                if not name_part:
                    continue

                # Drop everything after first comma (historical notes)
                alloc_clean = alloc_part.split(',')[0]
                matches = alloc_re.findall(alloc_clean)
                if not matches:
                    continue

                # Sum same-type quantities: "3 MES + 4 MES" → 7 MES
                from collections import defaultdict
                by_type = defaultdict(int)
                for qty_s, ctype in matches:
                    by_type[ctype] += int(qty_s)

                primary_ctype = matches[0][1]
                total_qty     = by_type[primary_ctype]

                # Normalize name: lowercase, strip dashes, collapse spaces
                norm = name_part.lower().replace("-", " ")
                norm = re.sub(r"\s+", " ", norm).strip()

                alloc_dict[norm] = (total_qty, primary_ctype)
    except FileNotFoundError:
        pass  # ReadMe absent → all fallback

    return alloc_dict


# Parse once at import time
_README_PATH  = str(DATA_DIR / "ReadMe.txt")
ALLOCATIONS   = parse_readme_allocations(_README_PATH)


def get_alloc(stem):
    """
    Match filename stem to ReadMe allocation via word-set containment.
    Example:  ES_reversal_sessione_europea__long
            → words {"es","reversal","sessione","europea","long"}
            → matches ReadMe "es reversal sessione europea long" → 7 MES
    Uses longest match (most keywords) to avoid ambiguity.
    Fallback: 1 micro of the symbol's default type.
    """
    symbol = stem.split("_")[0].upper()

    # Normalize stem → set of words
    normalized = stem.lower().replace("__", " ").replace("_", " ")
    stem_words = set(normalized.split())

    # Find best match: ReadMe entry whose keywords are ALL in stem words
    best_match = None
    best_len   = 0  # prefer longest (most specific) match
    for sys_name, (qty, ctype) in ALLOCATIONS.items():
        keywords = set(sys_name.split())
        if keywords.issubset(stem_words):
            if len(keywords) > best_len:
                best_len   = len(keywords)
                best_match = (qty, ctype)

    if best_match:
        return {"symbol": symbol, "n": best_match[0],
                "ctype": best_match[1], "matched": True}

    ctype = FALLBACK_CTYPE.get(symbol, "MES")
    return {"symbol": symbol, "n": 1, "ctype": ctype, "matched": False}


# ─────────────────────────────────────────────────────────────────────────────
# DATE PARSING — FIX FOR EXCEL DD/MM vs MM/DD SWAP
# ─────────────────────────────────────────────────────────────────────────────
#
# Problem: Italian TradeStation exports dates as DD/MM/YYYY.
# When Excel auto-converts ambiguous dates (day ≤ 12), it reads DD/MM as MM/DD.
#   "03/05/2024" (May 3) → Excel stores as datetime(2024, 3, 5) = March 5  WRONG
#   "20/02/2024" (Feb 20) → day 20 > 12, can't be month → kept as string   OK
#
# Fix: for datetime objects, ALWAYS swap month ↔ day.
#      for strings, parse as DD/MM/YYYY.

def parse_ts_date(val):
    """Parse date, fixing Excel's DD/MM → MM/DD swap for datetime objects."""
    if val is None:
        return None

    # DATETIME OBJECTS: Excel swapped month ↔ day → swap them back
    if isinstance(val, (datetime, pd.Timestamp)):
        ts = pd.Timestamp(val)
        # Swap month and day back to correct DD/MM interpretation
        try:
            ts = ts.replace(month=ts.day, day=ts.month)
        except ValueError:
            pass  # keep original if swap creates invalid date
        return ts

    # STRINGS: parse as DD/MM/YYYY (correct Italian format)
    s = str(val).strip()
    if not s:
        return None
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return pd.Timestamp(datetime.strptime(s, fmt))
        except ValueError:
            continue
    # Last resort
    try:
        return pd.Timestamp(s)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# DATA EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def extract_b5_total_net_profit(ws):
    """Read cell B5 = Total Net Profit from Performance Summary."""
    try:
        val = ws["B5"].value
        return float(val) if val is not None else None
    except Exception:
        return None


def extract_trades(ws):
    records = []
    header_found = False
    current_trade = {}

    for row in ws.iter_rows(values_only=True):
        if not header_found:
            if row[0] == "#":
                header_found = True
            continue

        if (row[0] is not None
                and isinstance(row[0], (int, float))
                and float(row[0]) == int(float(row[0]))):
            current_trade = {
                "trade_id":   int(row[0]),
                "entry_date": parse_ts_date(row[2]),
                "direction":  str(row[1]) if row[1] else "",
            }
        elif row[0] is None and row[1] is not None and current_trade:
            records.append({
                "trade_id":   current_trade["trade_id"],
                "entry_date": current_trade["entry_date"],
                "exit_date":  parse_ts_date(row[2]),
                "direction":  current_trade["direction"],
                "pnl":        row[6],
                "cum_pnl":    row[7],
            })
            current_trade = {}

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    for c in ("pnl", "cum_pnl"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["exit_date"]).sort_values("exit_date").reset_index(drop=True)


def build_daily_equity(trades):
    if trades.empty:
        return pd.Series(dtype=float)
    eq = trades.set_index("exit_date")["cum_pnl"].sort_index()
    eq.index = pd.to_datetime(eq.index).normalize()
    eq = eq.groupby(eq.index).last()
    if len(eq) < 2:
        return eq
    all_days = pd.date_range(eq.index.min(), eq.index.max(), freq="B")
    return eq.reindex(all_days).ffill()


# ─────────────────────────────────────────────────────────────────────────────
# LOAD ALL SYSTEMS  (NO CACHE — avoids stale sizing from old runs)
# ─────────────────────────────────────────────────────────────────────────────

def load_all_systems(data_dir, comm_micro, comm_mini):
    """Load all .xlsx. Sizing from SIZING_OVERRIDES. No cache."""
    path = Path(data_dir)
    systems = {}
    validation = []

    for f in sorted(path.glob("*.xlsx")):
        stem = f.stem
        try:
            wb = openpyxl.load_workbook(str(f), data_only=True)

            # B5 total net profit for validation
            b5_val = None
            if "Performance Summary" in wb.sheetnames:
                b5_val = extract_b5_total_net_profit(wb["Performance Summary"])

            trades = (extract_trades(wb["Trades List"])
                      if "Trades List" in wb.sheetnames else pd.DataFrame())
            equity = build_daily_equity(trades)

            # Validate: last cum_pnl should match B5
            last_cum = None
            if not trades.empty:
                last_cum = trades["cum_pnl"].iloc[-1]

            v_ok = (b5_val is not None and last_cum is not None
                    and abs(b5_val - last_cum) < 0.1)
            validation.append({
                "file": stem,
                "b5": b5_val,
                "last_cum": last_cum,
                "match": v_ok,
            })

            # ── SIZING: direct dict lookup ──
            alloc    = get_alloc(stem)
            n_def    = alloc["n"]
            ctype    = alloc["ctype"]
            is_micro = ctype in MICRO_PREFIXES
            comm_rt  = comm_micro if is_micro else comm_mini

            systems[stem] = {
                "name":           stem.replace("__", " | ").replace("_", " "),
                "symbol":         alloc["symbol"],
                "contract_type":  ctype,
                "n_default":      n_def,
                "is_micro":       is_micro,
                "comm_per_trade": comm_rt,
                "matched":        alloc["matched"],
                "b5_total":       b5_val,
                "b5_match":       v_ok,
                "trades":         trades,
                "equity":         equity,
            }
        except Exception as exc:
            st.warning(f"Could not load {f.name}: {exc}")

    return systems, validation


# ─────────────────────────────────────────────────────────────────────────────
# METRICS & EQUITY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _apply_commissions(scaled, trades, n, comm):
    if trades.empty or n == 0:
        return scaled
    cum_comm = (trades["exit_date"].dt.normalize()
                .value_counts().sort_index()
                .reindex(scaled.index, fill_value=0)
                .cumsum()) * comm * n
    return scaled - cum_comm


def scaled_equity_with_comm(equity, trades, n, comm):
    if equity.empty:
        return pd.Series(dtype=float)
    return _apply_commissions(equity * n, trades, n, comm)


def compute_metrics(equity, trades, n, comm):
    if equity.empty or len(equity) < 5:
        return {}
    scaled  = _apply_commissions(equity * n, trades, n, comm)
    daily   = scaled.diff().dropna()
    run_max = scaled.cummax()
    max_dd  = (scaled - run_max).min()
    n_years = max((scaled.index[-1] - scaled.index[0]).days / 365.25, 0.01)
    net     = scaled.iloc[-1] - scaled.iloc[0]
    ann_r   = net / n_years
    ann_v   = daily.std() * np.sqrt(252)
    sharpe  = ann_r / ann_v       if ann_v  > 0 else 0.0
    calmar  = ann_r / abs(max_dd) if max_dd < 0 else 0.0
    wins    = daily[daily > 0].sum()
    losses  = abs(daily[daily < 0].sum())
    pf      = wins / losses       if losses > 0 else np.inf
    n_tr    = len(trades)         if not trades.empty else 0

    return {
        "Net Profit ($)":   round(net, 0),
        "Ann. Return ($)":  round(ann_r, 0),
        "Max Drawdown ($)": round(max_dd, 0),
        "Sharpe Ratio":     round(sharpe, 2),
        "Calmar Ratio":     round(calmar, 2),
        "Profit Factor":    round(pf, 2),
        "Ann. Volatility":  round(ann_v, 0),
        "# Trades":         n_tr,
        "Total Comm ($)":   round(n_tr * comm * n, 0),
    }


def combine_equity_curves(curves, lookback_years=2):
    if not curves:
        return pd.DataFrame()
    df     = pd.concat(curves.values(), axis=1, keys=curves.keys()).sort_index().ffill()
    cutoff = df.index.max() - pd.Timedelta(days=int(lookback_years * 365))
    return df[df.index >= cutoff].ffill().fillna(0)


def compute_portfolio_metrics(port_eq):
    if port_eq.empty or len(port_eq) < 5:
        return {}
    daily  = port_eq.diff().dropna()
    net    = port_eq.iloc[-1] - port_eq.iloc[0]
    n_yr   = max((port_eq.index[-1] - port_eq.index[0]).days / 365.25, 0.01)
    ann_r  = net / n_yr
    ann_v  = daily.std() * np.sqrt(252)
    sharpe = ann_r / ann_v if ann_v > 0 else 0.0
    dd     = (port_eq - port_eq.cummax()).min()
    calmar = ann_r / abs(dd) if dd < 0 else 0.0
    return {
        "Net Profit ($)":   round(net, 0),
        "Ann. Return ($)":  round(ann_r, 0),
        "Max Drawdown ($)": round(dd, 0),
        "Ann. Volatility":  round(ann_v, 0),
        "Sharpe Ratio":     round(sharpe, 2),
        "Calmar Ratio":     round(calmar, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# PORTFOLIO OPTIMISATION
# ─────────────────────────────────────────────────────────────────────────────

def recommend_portfolios(eq_df, systems):
    daily = eq_df.diff().dropna()
    if daily.empty or daily.shape[1] < 2:
        return []

    means   = daily.mean()
    stds    = daily.std().replace(0, np.nan)
    sharpes = (means / stds).fillna(0)
    corr    = daily.corr()
    recs    = []

    # 1 — Max Sharpe (de-correlated)
    top = sharpes.nlargest(min(8, len(sharpes))).index.tolist()
    sel = []
    for s in top:
        if not sel:
            sel.append(s)
        else:
            mc = max(corr.loc[s, x] for x in sel if x in corr.columns)
            if mc < 0.85:
                sel.append(s)
    recs.append({"name": "🏆 Max Sharpe",
                 "description": "Top-Sharpe, pairwise ρ < 0.85",
                 "systems": sel})

    # 2 — Max Diversification
    n    = min(6, len(corr))
    seed = sharpes.idxmax()
    grp  = [seed]
    rem  = [x for x in corr.columns if x != seed]
    while len(grp) < n and rem:
        avg_c = {r: corr.loc[r, grp].mean() for r in rem}
        nxt   = min(avg_c, key=avg_c.get)
        grp.append(nxt); rem.remove(nxt)
    recs.append({"name": "🌐 Max Diversification",
                 "description": "Min avg pairwise correlation",
                 "systems": grp})

    # 3 — Min Drawdown
    scores = {}
    for col in daily.columns:
        cum = daily[col].cumsum()
        dd  = (cum - cum.cummax()).min()
        scores[col] = means[col] / abs(dd) if dd < 0 else 0.0
    best6 = sorted(scores, key=scores.get, reverse=True)[:6]
    recs.append({"name": "🛡 Min Drawdown",
                 "description": "Best return/drawdown ratio",
                 "systems": best6})

    return recs


# ─────────────────────────────────────────────────────────────────────────────
# PLOTTING
# ─────────────────────────────────────────────────────────────────────────────

def plot_equity(eq, name, color="#00d4ff"):
    run_max = eq.cummax()
    dd      = eq - run_max
    fig     = make_subplots(rows=2, cols=1, shared_xaxes=True,
                            row_heights=[0.7, 0.3], vertical_spacing=0.04)
    fig.add_trace(go.Scatter(x=eq.index, y=eq.values, name=name,
                             line=dict(color=color, width=2),
                             hovertemplate="%{x|%Y-%m-%d}<br>$%{y:,.0f}<extra></extra>"),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=eq.index, y=run_max.values, name="Peak",
                             line=dict(color="#555", width=1, dash="dot"),
                             showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=eq.index, y=dd.values, name="Drawdown",
                             fill="tozeroy", line=dict(color="#ff4444", width=1),
                             fillcolor="rgba(255,68,68,0.25)",
                             hovertemplate="%{x|%Y-%m-%d}<br>DD: $%{y:,.0f}<extra></extra>"),
                  row=2, col=1)
    fig.update_layout(template=DARK, height=500,
                      margin=dict(l=0, r=0, t=10, b=0),
                      legend=dict(orientation="h", y=1.05),
                      yaxis_title="Cum. P&L ($)", yaxis2_title="Drawdown ($)")
    return fig


def plot_portfolio_equity(port_eq, eq_df):
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.65, 0.35], vertical_spacing=0.04)
    for i, col in enumerate(eq_df.columns):
        raw_color = COLORS[i % len(COLORS)]
        fill_color = (raw_color.replace("rgb", "rgba").replace(")", ",0.55)")
                      if raw_color.startswith("rgb") else raw_color)
        fig.add_trace(go.Scatter(
            x=eq_df.index, y=eq_df[col].values,
            name=col.replace("_", " ")[:30],
            stackgroup="one",
            line=dict(width=0.5),
            fillcolor=fill_color,
            hovertemplate=f"{col[:20]}<br>%{{x|%Y-%m-%d}}<br>${{y:,.0f}}<extra></extra>",
        ), row=1, col=1)
    fig.add_trace(go.Scatter(x=port_eq.index, y=port_eq.values,
                             name="Portfolio Total",
                             line=dict(color="white", width=2.5),
                             hovertemplate="%{x|%Y-%m-%d}<br>Total: $%{y:,.0f}<extra></extra>"),
                  row=1, col=1)
    dd = port_eq - port_eq.cummax()
    fig.add_trace(go.Scatter(x=port_eq.index, y=dd.values, name="Portfolio DD",
                             fill="tozeroy", line=dict(color="#ff4444", width=1),
                             fillcolor="rgba(255,68,68,0.3)"), row=2, col=1)
    fig.update_layout(template=DARK, height=580,
                      margin=dict(l=0, r=0, t=10, b=0),
                      yaxis_title="Cum. P&L ($)", yaxis2_title="Drawdown ($)")
    return fig


def plot_correlation(corr):
    labels     = [c.replace("_", " ")[:25] for c in corr.columns]
    colorscale = [[0.0, "#d73027"], [0.25, "#f46d43"],
                  [0.5,  "#1a1a2e"], [0.75, "#74add1"], [1.0, "#4575b4"]]
    fig = go.Figure(go.Heatmap(
        z=corr.values, x=labels, y=labels,
        text=[[f"{v:.2f}" for v in row] for row in corr.values],
        texttemplate="%{text}",
        colorscale=colorscale, zmid=0, zmin=-1, zmax=1,
        colorbar=dict(title="ρ"),
    ))
    fig.update_layout(template=DARK, height=620,
                      margin=dict(l=0, r=0, t=30, b=0),
                      title="Pairwise Daily-Return Correlation",
                      xaxis_tickangle=-45)
    return fig


def plot_monthly_heatmap(equity, name):
    monthly = equity.resample("ME").last().diff().dropna()
    df      = pd.DataFrame({"val": monthly})
    df["year"]  = df.index.year
    df["month"] = df.index.month
    pivot = df.pivot(index="year", columns="month", values="val").fillna(0)
    months = ["Jan","Feb","Mar","Apr","May","Jun",
              "Jul","Aug","Sep","Oct","Nov","Dec"]
    pivot.columns = [months[c - 1] for c in pivot.columns]
    fig = go.Figure(go.Heatmap(
        z=pivot.values, x=pivot.columns.tolist(), y=pivot.index.tolist(),
        text=[[f"${v:,.0f}" for v in row] for row in pivot.values],
        texttemplate="%{text}",
        colorscale="RdYlGn", zmid=0,
        colorbar=dict(title="P&L $"),
    ))
    fig.update_layout(template=DARK, height=260,
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

st.markdown("""
<style>
    .main { background-color: #0e1117; }
    [data-testid="stMetricValue"] { font-size: 1.3rem; font-weight: 700; }
    [data-testid="stMetricLabel"] { font-size: 0.75rem; color: #aaa; }
    div.stTabs [data-baseweb="tab"] {
        height: 36px; padding: 0 16px;
        border-radius: 6px 6px 0 0; font-weight: 600;
    }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("⚙️ Settings")
    data_dir_input = st.text_input("Data directory", value=str(DATA_DIR))
    lookback       = st.slider("Lookback (years)", 1, 10, 2)
    st.divider()
    st.caption("**Commission rates (round-trip)**")
    COMMISSION_PER_MICRO = st.number_input("Micro ($)",     value=1.50,  step=0.25)
    COMMISSION_PER_MINI  = st.number_input("Mini/Full ($)", value=15.00, step=0.50)
    st.divider()
    st.caption("v4.0 — hardcoded sizing + date fix + B5 validation")


# ─────────────────────────────────────────────────────────────────────────────
# LOAD DATA
# ─────────────────────────────────────────────────────────────────────────────

with st.spinner("Loading trading systems…"):
    systems, validation = load_all_systems(
        data_dir_input, COMMISSION_PER_MICRO, COMMISSION_PER_MINI)

if not systems:
    st.error("No .xlsx files found in the data directory.")
    st.stop()

n_sys = len(systems)
st.title(f"📊 Quant Portfolio Dashboard — {n_sys} Systems Loaded")


# ─────────────────────────────────────────────────────────────────────────────
# TABS
# ─────────────────────────────────────────────────────────────────────────────

tab1, tab2, tab3 = st.tabs([
    "🖥 Systems", "📦 Portfolio", "🔬 Correlation & Optimisation"])


# ═════════════════════════════════════════════════════════════════════════════
# TAB 1 — SYSTEMS
# ═════════════════════════════════════════════════════════════════════════════

with tab1:
    st.subheader("System Overview")

    rows = []
    for stem, si in systems.items():
        eq  = si["equity"]
        tr  = si["trades"]
        nd  = si["n_default"]
        cpt = si["comm_per_trade"]
        src = "✅" if si["matched"] else "⚠️"

        base = {
            "Src":      src,
            "System":   si["name"][:42],
            "Sym":      si["symbol"],
            "Contract": f'{nd} {si["contract_type"]}',
        }

        if eq.empty:
            rows.append({**base, "# Trades": len(tr),
                         "Net Profit": "—", "Max DD": "—",
                         "Sharpe": "—", "PF": "—", "Calmar": "—"})
            continue

        m = compute_metrics(eq, tr, nd, cpt)
        rows.append({**base,
            "# Trades":   m.get("# Trades", 0),
            "Net Profit": f'${m.get("Net Profit ($)", 0):,.0f}',
            "Max DD":     f'${m.get("Max Drawdown ($)", 0):,.0f}',
            "Sharpe":     m.get("Sharpe Ratio", 0),
            "PF":         m.get("Profit Factor", 0),
            "Calmar":     m.get("Calmar Ratio", 0),
        })

    st.caption("**Src**: ✅ SIZING_OVERRIDES  |  ⚠️ fallback")
    st.dataframe(pd.DataFrame(rows), use_container_width=True,
                 height=min(40 + 36 * n_sys, 620), hide_index=True)

    # ── Sizing debug ──
    with st.expander("🔍 Sizing & validation details"):
        debug_rows = []
        for stem, si in systems.items():
            last_cum = si["trades"]["cum_pnl"].iloc[-1] if not si["trades"].empty else None
            debug_rows.append({
                "File":       stem[:45],
                "Sizing":     "✅ OVERRIDE" if si["matched"] else "⚠️ FALLBACK",
                "Contracts":  si["n_default"],
                "Type":       si["contract_type"],
                "Micro":      "Yes" if si["is_micro"] else "No",
                "Comm/RT":    f"${si['comm_per_trade']:.2f}",
                "B5 (TNP)":   f"${si['b5_total']:,.2f}" if si["b5_total"] else "—",
                "Last Cum":   f"${last_cum:,.2f}" if last_cum is not None else "—",
                "B5 Match":   "✅" if si["b5_match"] else "❌",
            })
        st.dataframe(pd.DataFrame(debug_rows), hide_index=True, use_container_width=True)

        # Date range check
        st.markdown("**Date ranges per system:**")
        dr_rows = []
        for stem, si in systems.items():
            if not si["trades"].empty:
                dates = si["trades"]["exit_date"].dropna()
                dr_rows.append({
                    "System":     stem[:40],
                    "First date": dates.min().strftime("%Y-%m-%d") if len(dates) else "—",
                    "Last date":  dates.max().strftime("%Y-%m-%d") if len(dates) else "—",
                    "# Trades":   len(si["trades"]),
                })
        st.dataframe(pd.DataFrame(dr_rows), hide_index=True, use_container_width=True)

    st.divider()

    # ── System explorer ──
    st.subheader("System Explorer")
    col_sel, col_sz = st.columns([3, 1])
    with col_sel:
        chosen = st.selectbox("Select system", list(systems.keys()),
                              format_func=lambda x: systems[x]["name"])
    with col_sz:
        nd_def = systems[chosen]["n_default"]
        ct_lbl = systems[chosen]["contract_type"]
        n_sel  = st.number_input(f"Contracts ({ct_lbl})", value=nd_def,
                                 min_value=0, max_value=50, step=1,
                                 help=f"Default: {nd_def} {ct_lbl}")

    sys_data = systems[chosen]
    eq_raw   = sys_data["equity"]
    trades   = sys_data["trades"]

    if eq_raw.empty:
        st.warning("No trades found for this system.")
    else:
        cutoff    = eq_raw.index.max() - pd.Timedelta(days=int(lookback * 365))
        eq_trim   = eq_raw[eq_raw.index >= cutoff]
        tr_trim   = trades[trades["exit_date"] >= cutoff] if not trades.empty else trades
        eq_scaled = scaled_equity_with_comm(eq_trim, tr_trim, n_sel, sys_data["comm_per_trade"])
        eq_scaled -= eq_scaled.iloc[0]

        m  = compute_metrics(eq_trim, tr_trim, n_sel, sys_data["comm_per_trade"])
        mc = st.columns(5)
        for col, (lbl, val) in zip(mc, [
            ("Net Profit",    f'${m.get("Net Profit ($)", 0):,.0f}'),
            ("Max DD",        f'${m.get("Max Drawdown ($)", 0):,.0f}'),
            ("Sharpe",        f'{m.get("Sharpe Ratio", 0):.2f}'),
            ("Profit Factor", f'{m.get("Profit Factor", 0):.2f}'),
            ("Calmar",        f'{m.get("Calmar Ratio", 0):.2f}'),
        ]):
            col.metric(lbl, val)

        color_idx = list(systems.keys()).index(chosen)
        st.plotly_chart(
            plot_equity(eq_scaled, sys_data["name"], COLORS[color_idx % len(COLORS)]),
            use_container_width=True)
        st.plotly_chart(plot_monthly_heatmap(eq_scaled, sys_data["name"]),
                        use_container_width=True)

        if not trades.empty:
            with st.expander("📋 Trade list"):
                td = tr_trim.copy()
                td["P&L ($)"]     = td["pnl"]     * n_sel
                td["Cum P&L ($)"] = td["cum_pnl"] * n_sel
                st.dataframe(
                    td[["trade_id","entry_date","exit_date","direction",
                        "P&L ($)","Cum P&L ($)"]],
                    use_container_width=True, height=300)


# ═════════════════════════════════════════════════════════════════════════════
# TAB 2 — PORTFOLIO
# ═════════════════════════════════════════════════════════════════════════════

with tab2:
    st.subheader("Portfolio Builder")

    selected_systems = st.multiselect(
        "Select systems to include",
        list(systems.keys()),
        default=list(systems.keys()),
        format_func=lambda x: systems[x]["name"],
    )

    if not selected_systems:
        st.info("Select at least one system.")
        st.stop()

    st.markdown("**Position sizing (contracts per system):**")
    slider_cols = st.columns(3)
    sizing = {}
    for i, stem in enumerate(selected_systems):
        si = systems[stem]
        with slider_cols[i % 3]:
            n = st.slider(
                si["name"][:30], min_value=0, max_value=20,
                value=si["n_default"], step=1, key=f"sl_{stem}",
                help=f"Default: {si['n_default']} {si['contract_type']} "
                     f"({'micro' if si['is_micro'] else 'mini/full'})",
            )
            sizing[stem] = n

    st.divider()

    curves = {}
    for stem in selected_systems:
        n  = sizing.get(stem, 0)
        si = systems[stem]
        if n == 0 or si["equity"].empty:
            continue
        cutoff = si["equity"].index.max() - pd.Timedelta(days=int(lookback * 365))
        eq_t   = si["equity"][si["equity"].index >= cutoff]
        tr_t   = (si["trades"][si["trades"]["exit_date"] >= cutoff]
                  if not si["trades"].empty else si["trades"])
        eq_sc  = scaled_equity_with_comm(eq_t, tr_t, n, si["comm_per_trade"])
        if not eq_sc.empty:
            curves[stem] = eq_sc - eq_sc.iloc[0]

    if not curves:
        st.warning("No valid equity curves with current sizing.")
        st.stop()

    eq_df   = combine_equity_curves(curves, lookback_years=lookback)
    port_eq = eq_df.sum(axis=1)
    pm      = compute_portfolio_metrics(port_eq)

    mc_cols = st.columns(6)
    for col, (lbl, val) in zip(mc_cols, [
        ("Portfolio P&L",   f'${pm.get("Net Profit ($)", 0):,.0f}'),
        ("Ann. Return",     f'${pm.get("Ann. Return ($)", 0):,.0f}'),
        ("Max Drawdown",    f'${pm.get("Max Drawdown ($)", 0):,.0f}'),
        ("Ann. Volatility", f'${pm.get("Ann. Volatility", 0):,.0f}'),
        ("Sharpe",          f'{pm.get("Sharpe Ratio", 0):.2f}'),
        ("Calmar",          f'{pm.get("Calmar Ratio", 0):.2f}'),
    ]):
        col.metric(lbl, val)

    st.plotly_chart(plot_portfolio_equity(port_eq, eq_df), use_container_width=True)

    st.subheader("System Contribution to Portfolio P&L")
    contrib = {s: eq_df[s].iloc[-1] - eq_df[s].iloc[0] for s in eq_df.columns}
    cs      = pd.Series(contrib).sort_values(ascending=True)
    fig_bar = go.Figure(go.Bar(
        x=cs.values,
        y=[systems[k]["name"][:38] for k in cs.index],
        orientation="h",
        marker_color=["#ff4444" if v < 0 else "#00cc88" for v in cs.values],
        text=[f"${v:,.0f}" for v in cs.values],
        textposition="outside",
    ))
    fig_bar.update_layout(template=DARK, height=max(300, 38 * len(cs)),
                          margin=dict(l=0, r=70, t=10, b=0),
                          xaxis_title="P&L Contribution ($)")
    st.plotly_chart(fig_bar, use_container_width=True)

    st.plotly_chart(plot_monthly_heatmap(port_eq, "Portfolio"), use_container_width=True)

    with st.expander("📉 Drawdown Analysis"):
        dd     = port_eq - port_eq.cummax()
        dd_neg = dd[dd < 0]
        if not dd_neg.empty:
            d1, d2, d3 = st.columns(3)
            d1.metric("Max Drawdown",     f"${dd_neg.min():,.0f}")
            d2.metric("Avg Drawdown",     f"${dd_neg.mean():,.0f}")
            d3.metric("Time in Drawdown", f"{len(dd_neg)/len(dd)*100:.1f}%")
            fig_dd = go.Figure(go.Scatter(x=dd.index, y=dd.values,
                                          fill="tozeroy", line=dict(color="#ff4444"),
                                          fillcolor="rgba(255,68,68,0.25)"))
            fig_dd.update_layout(template=DARK, height=240,
                                  margin=dict(l=0, r=0, t=0, b=0),
                                  yaxis_title="Drawdown ($)")
            st.plotly_chart(fig_dd, use_container_width=True)


# ═════════════════════════════════════════════════════════════════════════════
# TAB 3 — CORRELATION & OPTIMISATION
# ═════════════════════════════════════════════════════════════════════════════

with tab3:
    st.subheader("Return Correlation Matrix")

    all_curves = {}
    for stem, si in systems.items():
        nd = si["n_default"]
        if nd == 0 or si["equity"].empty:
            continue
        cutoff = si["equity"].index.max() - pd.Timedelta(days=int(lookback * 365))
        eq_t   = si["equity"][si["equity"].index >= cutoff]
        tr_t   = (si["trades"][si["trades"]["exit_date"] >= cutoff]
                  if not si["trades"].empty else si["trades"])
        eq_sc  = scaled_equity_with_comm(eq_t, tr_t, nd, si["comm_per_trade"])
        if not eq_sc.empty:
            all_curves[stem] = eq_sc

    if len(all_curves) < 2:
        st.info("Need at least 2 systems with data.")
    else:
        corr_df  = combine_equity_curves(all_curves, lookback_years=lookback)
        daily_r  = corr_df.diff().dropna()
        corr_mat = daily_r.corr()

        short_names = {c: systems[c]["name"][:28]
                       for c in corr_mat.columns if c in systems}
        st.plotly_chart(
            plot_correlation(corr_mat.rename(
                index=short_names, columns=short_names)),
            use_container_width=True)

        upper = corr_mat.where(
            np.triu(np.ones(corr_mat.shape), k=1).astype(bool))
        vals  = upper.stack()
        c1, c2, c3 = st.columns(3)
        c1.metric("Avg Pairwise ρ", f"{vals.mean():.3f}")
        c2.metric("Max ρ",          f"{vals.max():.3f}")
        c3.metric("Min ρ",          f"{vals.min():.3f}")

        high_corr = vals[abs(vals) > 0.6].sort_values(ascending=False)
        if not high_corr.empty:
            with st.expander("⚠️ Highly correlated pairs (|ρ| > 0.6)"):
                hc_df = pd.DataFrame({
                    "System A":    [systems.get(i[0], {}).get("name", i[0])[:38]
                                    for i in high_corr.index],
                    "System B":    [systems.get(i[1], {}).get("name", i[1])[:38]
                                    for i in high_corr.index],
                    "Correlation": high_corr.values.round(3),
                })
                st.dataframe(hc_df, hide_index=True, use_container_width=True)

        st.divider()
        st.subheader("🎯 Recommended Portfolios")

        reccos = recommend_portfolios(corr_df, systems)
        for rec in reccos:
            with st.expander(f"{rec['name']} — {rec['description']}"):
                rec_curves = {s: all_curves[s]
                              for s in rec["systems"] if s in all_curves}
                if not rec_curves:
                    st.write("Insufficient data.")
                    continue
                rec_eq = combine_equity_curves(
                    rec_curves, lookback_years=lookback).sum(axis=1)
                rpm = compute_portfolio_metrics(rec_eq)

                r1, r2, r3, r4 = st.columns(4)
                r1.metric("Net P&L",
                          f'${rpm.get("Net Profit ($)", 0):,.0f}')
                r2.metric("Sharpe",
                          f'{rpm.get("Sharpe Ratio", 0):.2f}')
                r3.metric("Max DD",
                          f'${rpm.get("Max Drawdown ($)", 0):,.0f}')
                r4.metric("Calmar",
                          f'{rpm.get("Calmar Ratio", 0):.2f}')

                for s in rec["systems"]:
                    if s in systems:
                        si  = systems[s]
                        src = "✅" if si["matched"] else "⚠️"
                        st.write(f"  {src} {si['name']} — "
                                 f"**{si['n_default']} "
                                 f"{si['contract_type']}**")

                rec_norm = rec_eq - rec_eq.iloc[0]
                st.plotly_chart(
                    plot_equity(rec_norm, rec["name"], color="#f0c040"),
                    use_container_width=True)

        st.divider()

        # ── Scatter ──
        st.subheader("Risk / Return Scatter")

        def _safe_size(v):
            try:
                v = float(v)
                return 1.0 if (np.isnan(v) or np.isinf(v)) else max(abs(v), 1.0)
            except Exception:
                return 1.0

        scatter_rows = []
        for stem in corr_df.columns:
            col_data = corr_df[stem]
            d        = daily_r[stem]
            net      = col_data.iloc[-1] - col_data.iloc[0]
            n_yr     = max((col_data.index[-1] - col_data.index[0]).days
                           / 365.25, 0.01)
            ann_r    = net / n_yr
            ann_v    = d.std() * np.sqrt(252)
            sharpe   = ann_r / ann_v if ann_v > 0 else 0.0
            max_dd   = (col_data - col_data.cummax()).min()
            scatter_rows.append({
                "System":         systems.get(stem, {}).get("name", stem)[:32],
                "Symbol":         systems.get(stem, {}).get("symbol", ""),
                "Sharpe":         round(float(sharpe), 2),
                "Max DD ($)":     round(float(max_dd), 0),
                "Ann Return ($)": round(float(ann_r), 0),
                "_size":          _safe_size(ann_r),
            })

        sc_df = pd.DataFrame(scatter_rows)
        sc_df["_size"] = sc_df["_size"].apply(_safe_size)

        if not sc_df.empty:
            fig_sc = px.scatter(
                sc_df,
                x="Max DD ($)", y="Sharpe",
                text="System", color="Symbol",
                size="_size", size_max=42,
                template=DARK,
                title="Sharpe vs Max Drawdown  (bubble = |Ann. Return|)",
                hover_data={"Ann Return ($)": True, "_size": False},
            )
            fig_sc.update_traces(textposition="top center")
            fig_sc.update_layout(height=520,
                                 margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig_sc, use_container_width=True)