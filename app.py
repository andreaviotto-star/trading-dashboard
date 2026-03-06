"""
Quant Trading System Analytics Dashboard  v11.0
    st.plotly_chart(fig_bar, use_container_width=True, key="chart_1")

    st.plotly_chart(plot_monthly_heatmap(port_eq, "Portfolio"), use_container_width=True)

    # ── Drawdown Decomposition ────────────────────────────────────────────────
    st.subheader("🔍 Drawdown Decomposition")
    st.caption("For each major portfolio drawdown, which system caused it?")
    dd_episodes = decompose_drawdown(eq_df, port_eq, top_n=3)
    if dd_episodes:
        for ep_i, ep in enumerate(dd_episodes):
            title = (f"DD #{ep_i+1}:  {ep['peak_date'].strftime('%Y-%m-%d')} → "
                     f"{ep['trough_date'].strftime('%Y-%m-%d')}  "
                     f"(Loss: ${ep['dd_abs']:,.0f})")
            with st.expander(title, expanded=(ep_i == 0)):
                blame_s = sorted(ep["blame_pct"].items(),
                                  key=lambda x: x[1], reverse=True)
                blame_s = [(k, v) for k, v in blame_s if v > 0][:8]
                if blame_s:
                    bkeys = [systems[k]["display_name"][:30] if k in systems else k[:30]
                             for k, _ in blame_s]
                    bvals = [v for _, v in blame_s]
                    fig_b = go.Figure(go.Bar(
                        y=bkeys, x=bvals, orientation="h",
                        marker_color=[C["red"] if v > 15 else C["amber"] for v in bvals],
                        text=[f"{v:.1f}%" for v in bvals],
                        textposition="outside"))
                    fig_b.update_layout(template=THEME,
                                         height=max(200, 35 * len(bkeys)),
                                         margin=dict(l=0, r=60, t=10, b=0),
                                         xaxis_title="Blame (%)")
                    st.plotly_chart(fig_b, use_container_width=True, key="chart_2")
    else:
        st.info("No significant drawdown episodes in this lookback window.")

    with st.expander("📉 Overall Drawdown Analysis"):
        dd     = port_eq - port_eq.cummax()
        dd_neg = dd[dd < 0]
        if not dd_neg.empty:
            d1, d2, d3 = st.columns(3)
            d1.metric("Max Drawdown",     f"${dd_neg.min():,.0f}")
            d2.metric("Avg Drawdown",     f"${dd_neg.mean():,.0f}")
            d3.metric("Time in Drawdown", f"{len(dd_neg)/len(dd)*100:.1f}%")
            fig_dd = go.Figure(go.Scatter(
                x=dd.index, y=dd.values, fill="tozeroy",
                line=dict(color=C["dd_line"]), fillcolor=C["drawdown"]))
            fig_dd.update_layout(template=THEME, height=240,
                                  margin=dict(l=0, r=0, t=0, b=0),
                                  yaxis_title="Drawdown ($)")
            st.plotly_chart(fig_dd, use_container_width=True, key="chart_3")


# ═════════════════════════════════════════════════════════════════════════════
# TAB 3 — CORRELATION & OPTIMISATION
# ═════════════════════════════════════════════════════════════════════════════

with tab3:
    all_curves = {}
    for stem in _active_stems:
        si = systems[stem]
        eq = get_net_equity_trimmed(si, lookback, _cur_ratio(stem))
        if not eq.empty:
            all_curves[stem] = eq

    if len(all_curves) < 2:
        st.info("Need ≥ 2 systems with data.")
        st.stop()

    corr_df  = combine_equity_curves(all_curves, lookback_years=lookback)
    daily_r  = corr_df.diff().dropna()
    corr_mat = daily_r.corr()

    # ── Clustered Correlation ─────────────────────────────────────────────────
    st.subheader("Clustered Correlation Heatmap")
    st.caption("Ward-linkage hierarchical clustering. 🔴 boxes = over-exposed pairs.")

    over_exp_thresh = st.slider("Over-exposed threshold (ρ)", 0.5, 0.95, 0.70, 0.05)
    cluster_score   = compute_cluster_risk_score(corr_mat)
    upper           = corr_mat.where(np.triu(np.ones(corr_mat.shape), k=1).astype(bool))
    corr_vals       = upper.stack()
    n_overexp       = (corr_vals > over_exp_thresh).sum()

    c1, c2, c3 = st.columns(3)
    c1.metric("Cluster Risk Score", f"{cluster_score:.0f}/100",
              help="0 = uncorrelated, 100 = all move together")
    c2.metric("Avg Pairwise ρ",     f"{corr_vals.mean():.3f}")
    c3.metric(f"Pairs ρ>{over_exp_thresh}", f"{n_overexp}",
              delta="⚠️ Concentration!" if n_overexp > 3 else "OK",
              delta_color="inverse")

    st.plotly_chart(
        plot_clustered_correlation(corr_mat, systems, over_exp_thresh),
        use_container_width=True)

    high_corr = corr_vals[corr_vals > over_exp_thresh].sort_values(ascending=False)
    if not high_corr.empty:
        with st.expander(f"⚠️ Over-exposed pairs (ρ > {over_exp_thresh})"):
            hc_df = pd.DataFrame({
                "System A":    [systems.get(i[0], {}).get("display_name", i[0])[:38]
                                for i in high_corr.index],
                "System B":    [systems.get(i[1], {}).get("display_name", i[1])[:38]
                                for i in high_corr.index],
                "Correlation": high_corr.values.round(3),
                "Risk":        ["🔴 High" if v > 0.85 else "🟡 Watch"
                                for v in high_corr.values],
            })
            st.dataframe(hc_df, hide_index=True, use_container_width=True)

    st.divider()

    # ── Risk Parity ───────────────────────────────────────────────────────────
    st.subheader("⚖️ Risk Parity Sizing (Advisory)")
    st.caption(
        f"Inverse-volatility weighting targeting **${rp_target:,.0f}/day** per system. "
        "These are advisory — use the Tab 1 editor to apply them.")

    rp_rows = []
    for stem in _active_stems:
        si = systems[stem]
        d  = _rp_detail.get(stem, {})
        rp_rows.append({
            "System":        si["display_name"][:35],
            "Symbol":        si["symbol"],
            "Contract":      si["contract_label"],
            "Daily Vol ($)": f"${d.get('vol_1ct', 0):,.0f}",
            "Raw N":         f'{d.get("raw_n", si["default_n"]):.1f}',
            "RP Advisory N": _rp_sizing.get(stem, si["default_n"]),
            "Current N":     st.session_state["sizing"].get(stem, si["default_n"]),
        })
    st.dataframe(pd.DataFrame(rp_rows), hide_index=True, use_container_width=True)

    st.divider()

    # ── Recommended Sub-Portfolios ────────────────────────────────────────────
    st.subheader("🎯 Recommended Sub-Portfolios")
    st.caption("Subsets selected by different risk/return criteria.")

    reccos = recommend_portfolios(corr_df, systems)
    for rec in reccos:
        with st.expander(f"{rec['name']} — {rec['description']}"):
            rec_stems = [s for s in rec["systems"] if s in all_curves]
            if not rec_stems:
                st.write("Insufficient data.")
                continue
            ratios_rec = {s: _cur_ratio(s) for s in rec_stems}
            rec_eq     = build_portfolio_equity(systems, rec_stems, lookback, ratios_rec)
            rec_m      = compute_portfolio_metrics(rec_eq)
            mc4 = st.columns(4)
            for col, (lbl, key, fmt) in zip(mc4, [
                ("Net P&L", "Net Profit ($)", "$"), ("Sharpe", "Sharpe Ratio", "f"),
                ("Max DD",  "Max Drawdown ($)", "$"), ("Calmar", "Calmar Ratio", "f"),
            ]):
                v = rec_m.get(key, 0)
                col.metric(lbl, f"${v:,.0f}" if fmt == "$" else f"{v:.2f}")
            if not rec_eq.empty:
                norm = rec_eq - rec_eq.iloc[0]
                fig_r = go.Figure(go.Scatter(x=norm.index, y=norm.values,
                                              line=dict(color=C["blue"], width=2)))
                fig_r.update_layout(template=THEME, height=320,
                                     margin=dict(l=0, r=0, t=10, b=0),
                                     yaxis_title="Cum. P&L ($)")
                st.plotly_chart(fig_r, use_container_width=True, key="chart_4")

    st.divider()

    # ── Risk / Return scatter ─────────────────────────────────────────────────
    st.subheader("Risk / Return Scatter")
    scatter_rows = []
    for stem in corr_df.columns:
        d   = daily_r[stem]
        col = corr_df[stem]
        net   = col.iloc[-1] - col.iloc[0]
        n_yr  = max((col.index[-1] - col.index[0]).days / 365.25, 0.01)
        ann_r = net / n_yr
        ann_v = d.std() * np.sqrt(252)
        sharpe_v = ann_r / ann_v if ann_v > 0 else 0
        max_dd_v = (col - col.cummax()).min()
        scatter_rows.append({
            "System":         systems.get(stem, {}).get("display_name", stem)[:32],
            "Symbol":         systems.get(stem, {}).get("symbol", ""),
            "Sharpe":         round(float(sharpe_v), 2),
            "Max DD ($)":     round(float(max_dd_v), 0),
            "Ann Return ($)": round(float(ann_r), 0),
            "_sz":            max(abs(float(ann_r)), 1.0),
        })
    if scatter_rows:
        sc_df  = pd.DataFrame(scatter_rows)
        fig_sc = px.scatter(sc_df, x="Max DD ($)", y="Sharpe",
                            text="System", color="Symbol",
                            size="_sz", size_max=42, template=THEME,
                            title="Sharpe vs Max Drawdown  (bubble = |Ann. Return|)",
                            hover_data={"Ann Return ($)": True, "_sz": False})
        fig_sc.update_traces(textposition="top center")
        fig_sc.update_layout(height=520, margin=dict(l=0, r=0, t=40, b=0))
        st.plotly_chart(fig_sc, use_container_width=True, key="chart_5")


# ═════════════════════════════════════════════════════════════════════════════
# TAB 4 — FORWARD ANALYSIS
# ═════════════════════════════════════════════════════════════════════════════

with tab4:

    # ── System Health Monitor ─────────────────────────────────────────────────
    st.subheader("🏥 System Health Monitor")
    st.caption("Rolling metrics using ReadMe default sizing (independent of Tab 1 overrides).")

    hw_choice = st.radio("Rolling window", ["3 months (63d)", "6 months (126d)"],
                          horizontal=True, key="hw")
    hw_days   = 63 if "3" in hw_choice else 126

    health_rows   = []
    health_charts = {}
    for stem in _active_stems:
        si  = systems[stem]
        hdf = compute_rolling_health(si["trades"], si["comm_per_trade"], hw_days)
        if hdf.empty:
            health_rows.append({
                "System": si["display_name"][:35], "Status": "⚪",
                "Sharpe": "—", "PF": "—", "Win %": "—", "Period P&L": "—"})
            continue
        health_charts[stem] = hdf
        lt = hdf.iloc[-1]
        tl = health_traffic_light(lt["Sharpe"], lt["Profit Factor"], lt["Win Rate"])
        health_rows.append({
            "System":     si["display_name"][:35],
            "Status":     tl,
            "Sharpe":     f'{lt["Sharpe"]:.2f}',
            "PF":         f'{lt["Profit Factor"]:.2f}',
            "Win %":      f'{lt["Win Rate"]*100:.1f}%',
            "Period P&L": f'${lt["Period P&L"]:,.0f}',
        })

    st.dataframe(pd.DataFrame(health_rows), hide_index=True,
                 use_container_width=True,
                 height=min(42 + 36 * len(health_rows), 520))

    if health_charts:
        with st.expander("📈 Rolling Sharpe over time"):
            fig_rsh = go.Figure()
            for i, (stem, hdf) in enumerate(health_charts.items()):
                fig_rsh.add_trace(go.Scatter(
                    x=hdf.index, y=hdf["Sharpe"].values,
                    name=systems[stem]["display_name"][:25],
                    line=dict(color=COLORS[i % len(COLORS)], width=1.5),
                    hovertemplate="%{x|%Y-%m-%d}<br>Sharpe:%{y:.2f}<extra></extra>"))
            fig_rsh.add_hline(y=0,   line_dash="dot",  line_color=C["zero_line"])
            fig_rsh.add_hline(y=0.8, line_dash="dash", line_color=C["green"],
                              opacity=0.5, annotation_text="Good (0.8)")
            fig_rsh.update_layout(template=THEME, height=420,
                                   margin=dict(l=0, r=0, t=10, b=0),
                                   yaxis_title="Rolling Sharpe",
                                   legend=dict(font=dict(size=10)))
            st.plotly_chart(fig_rsh, use_container_width=True, key="chart_6")

        with st.expander("📈 Rolling Profit Factor over time"):
            fig_rpf = go.Figure()
            for i, (stem, hdf) in enumerate(health_charts.items()):
                if "Profit Factor" in hdf.columns:
                    fig_rpf.add_trace(go.Scatter(
                        x=hdf.index, y=hdf["Profit Factor"].values,
                        name=systems[stem]["display_name"][:25],
                        line=dict(color=COLORS[i % len(COLORS)], width=1.5)))
            fig_rpf.add_hline(y=1.0, line_dash="dot", line_color=C["red"],
                              annotation_text="Break-even (1.0)")
            fig_rpf.update_layout(template=THEME, height=420,
                                   margin=dict(l=0, r=0, t=10, b=0),
                                   yaxis_title="Rolling PF",
                                   legend=dict(font=dict(size=10)))
            st.plotly_chart(fig_rpf, use_container_width=True, key="chart_7")

    st.divider()

    # ── Monte Carlo ───────────────────────────────────────────────────────────
    st.subheader("🎲 Monte Carlo Forward Projection")
    st.caption(
        "Bootstrap resampling of historical daily net returns → confidence bands. "
        "Uses current sizing from Tab 1.")

    mc_c1, mc_c2 = st.columns(2)
    with mc_c1:
        mc_sims = st.selectbox("Simulations", [500, 1000, 2000, 5000], index=1)
    with mc_c2:
        mc_days_opt = st.selectbox(
            "Horizon", [63, 126, 252],
            format_func=lambda x: {63: "3 months", 126: "6 months", 252: "12 months"}[x],
            index=1)

    if st.button("🎲 Run Monte Carlo", type="primary", key="run_mc"):
        with st.spinner(f"Running {mc_sims:,} simulations…"):
            ratios_mc = {s: _cur_ratio(s) for s in _active_stems}
            mc_res = monte_carlo_simulation(
                systems, _active_stems, lookback,
                n_sims=mc_sims, forward_days=mc_days_opt,
                sizing_ratios=ratios_mc)

        if mc_res is None:
            st.warning("Not enough data.")
        else:
            pct     = mc_res["percentiles"]
            days_x  = list(range(len(pct)))
            hor_lbl = {63: "3 months", 126: "6 months",
                       252: "12 months"}.get(mc_days_opt, "")

            fig_mc = go.Figure()
            fig_mc.add_trace(go.Scatter(x=days_x, y=pct["95th"].values,
                                         mode="lines", line=dict(width=0),
                                         showlegend=False))
            fig_mc.add_trace(go.Scatter(x=days_x, y=pct["5th"].values,
                                         mode="lines", line=dict(width=0),
                                         fill="tonexty",
                                         fillcolor="rgba(38,139,210,0.12)",
                                         name="5-95th pct"))
            fig_mc.add_trace(go.Scatter(x=days_x, y=pct["75th"].values,
                                         mode="lines", line=dict(width=0),
                                         showlegend=False))
            fig_mc.add_trace(go.Scatter(x=days_x, y=pct["25th"].values,
                                         mode="lines", line=dict(width=0),
                                         fill="tonexty",
                                         fillcolor="rgba(38,139,210,0.22)",
                                         name="25-75th pct"))
            fig_mc.add_trace(go.Scatter(x=days_x, y=pct["50th"].values,
                                         name="Median",
                                         line=dict(color=C["blue"], width=2.5)))
            fig_mc.add_hline(y=0, line_dash="dot", line_color=C["zero_line"])
            fig_mc.update_layout(template=THEME, height=450,
                                  margin=dict(l=0, r=0, t=30, b=0),
                                  title=f"Monte Carlo: {mc_sims:,} × {hor_lbl}",
                                  xaxis_title="Trading Days Forward",
                                  yaxis_title="Projected P&L ($)")
            st.plotly_chart(fig_mc, use_container_width=True, key="chart_8")

            final = mc_res["paths"].iloc[-1]
            ec    = st.columns(5)
            ec[0].metric("Median",  f"${final.median():,.0f}")
            ec[1].metric("Mean",    f"${final.mean():,.0f}")
            ec[2].metric("5th pct", f"${final.quantile(0.05):,.0f}")
            ec[3].metric("95th",    f"${final.quantile(0.95):,.0f}")
            ec[4].metric("P(loss)", f"{(final < 0).mean():.1%}")

            dd_rows = [{"DD Threshold": f"${t:,.0f}",
                        "Probability":  f"{p:.1%}",
                        "Bar": "█" * int(p * 30)}
                       for t, p in sorted(mc_res["dd_probs"].items())]
            st.dataframe(pd.DataFrame(dd_rows), hide_index=True,
                         use_container_width=True)

    st.divider()

    # ── Sizing Recommendation Summary ─────────────────────────────────────────
    st.subheader("📋 Sizing Recommendation Summary")
    st.caption("Based on rolling health metrics (3-month window). Not financial advice.")

    rec_rows = []
    for stem in _active_stems:
        si  = systems[stem]
        nd  = si["default_n"]
        hdf = compute_rolling_health(si["trades"], si["comm_per_trade"], 63)
        cur_n = st.session_state["sizing"].get(stem, nd)

        if hdf.empty:
            rec_rows.append({
                "System":   si["display_name"][:35],
                "Contract": si["contract_label"],
                "Current N": cur_n,
                "Health":   "⚪ No data",
                "Suggestion": "Insufficient data"})
            continue

        lt  = hdf.iloc[-1]
        sh, pf_v, wr = lt["Sharpe"], lt["Profit Factor"], lt["Win Rate"]
        tl  = health_traffic_light(sh, pf_v, wr)
        if tl == "🟢":
            sugg = ("Scale up — strong momentum" if sh > 1.5 and pf_v > 1.5
                    else "Maintain — performing well")
        elif tl == "🟡":
            sugg = "Monitor — consider reducing if trend continues"
        else:
            sugg = "Consider reducing or pausing" if sh < -0.5 else "Consider reducing"

        rec_rows.append({
            "System":     si["display_name"][:35],
            "Contract":   si["contract_label"],
            "Default N":  nd,
            "Current N":  cur_n,
            "Health":     f"{tl} Sh={sh:.1f} PF={pf_v:.1f}",
            "Suggestion": sugg,
        })

    st.dataframe(pd.DataFrame(rec_rows), hide_index=True,
                 use_container_width=True,
                 height=min(42 + 36 * len(rec_rows), 520))
    st.info("⚠️ Suggestions based on rolling historical metrics. "
            "Not financial advice. Past performance ≠ future results.")

    st.divider()

    # ── Regime Analysis ───────────────────────────────────────────────────────
    st.subheader("🌡️ Regime Analysis")
    st.caption("Classifies market conditions via portfolio realized volatility.")

    reg_c1, reg_c2, reg_c3 = st.columns(3)
    with reg_c1:
        reg_vw = st.selectbox("Vol window (days)", [10, 15, 20, 30, 40], index=2, key="r_vw")
    with reg_c2:
        reg_lp = st.slider("Low-vol pct", 10, 45, 33, key="r_lp")
    with reg_c3:
        reg_hp = st.slider("High-vol pct", 55, 90, 66, key="r_hp")

    _reg_curves = {}
    for stem in _active_stems:
        si = systems[stem]
        eq = get_net_equity_trimmed(si, lookback, _cur_ratio(stem))
        if not eq.empty:
            _reg_curves[stem] = eq

    if len(_reg_curves) < 2:
        st.info("Need ≥ 2 active systems.")
    else:
        regime_df, regime_thresholds = compute_regime_series(
            _reg_curves, vol_window=reg_vw, low_pct=reg_lp, high_pct=reg_hp)

        if regime_df.empty:
            st.warning("Not enough data for regime classification.")
        else:
            cur_regime = regime_df["regime"].iloc[-1]
            reg_emoji  = {"Low Vol": "😴", "Medium Vol": "😐", "High Vol": "🔥"}
            st.markdown(
                f"### Current Regime: {reg_emoji.get(cur_regime, '')} **{cur_regime}**")

            rm1, rm2, rm3, rm4 = st.columns(4)
            rm1.metric("Current Ann. Vol", f"${regime_df['vol'].iloc[-1]:,.0f}")
            rm2.metric("Thresholds",
                       f"${regime_thresholds['low']:,.0f} / ${regime_thresholds['high']:,.0f}")
            rc  = regime_df["regime"].value_counts()
            tot = len(regime_df)
            rm3.metric("% Low Vol",  f"{rc.get('Low Vol',  0)/tot*100:.0f}%")
            rm4.metric("% High Vol", f"{rc.get('High Vol', 0)/tot*100:.0f}%")

            rcmap = {"Low Vol": C["green"], "Medium Vol": C["amber"], "High Vol": C["red"]}
            fig_reg = go.Figure()
            for label, color in rcmap.items():
                mask = regime_df["regime"] == label
                sub  = regime_df[mask]
                if not sub.empty:
                    fig_reg.add_trace(go.Scatter(
                        x=sub.index, y=sub["vol"].values, mode="markers",
                        marker=dict(color=color, size=3, opacity=0.6), name=label))
            fig_reg.add_hline(y=regime_thresholds["low"],  line_dash="dash",
                              line_color=C["green"], opacity=0.5)
            fig_reg.add_hline(y=regime_thresholds["high"], line_dash="dash",
                              line_color=C["red"], opacity=0.5)
            fig_reg.update_layout(template=THEME, height=300,
                                   margin=dict(l=0, r=0, t=10, b=0),
                                   yaxis_title="Ann. Vol ($)",
                                   legend=dict(orientation="h", y=1.05))
            st.plotly_chart(fig_reg, use_container_width=True, key="chart_9")

            with st.expander("🔄 Regime Transition Probabilities"):
                trans_df = compute_regime_transitions(regime_df)
                if not trans_df.empty:
                    st.caption("P(next = column | today = row)")
                    fig_t = go.Figure(go.Heatmap(
                        z=trans_df.values,
                        x=trans_df.columns.tolist(), y=trans_df.index.tolist(),
                        text=[[f"{v:.1%}" for v in row] for row in trans_df.values],
                        texttemplate="%{text}",
                        colorscale="Blues", zmin=0, zmax=1,
                        colorbar=dict(title="P")))
                    fig_t.update_layout(template=THEME, height=280,
                                         margin=dict(l=0, r=0, t=10, b=0))
                    st.plotly_chart(fig_t, use_container_width=True, key="chart_10")
                    persist = {r: trans_df.loc[r, r] for r in trans_df.index}
                    st.markdown(
                        f"**Persistence:** Low Vol stays {persist.get('Low Vol',0):.0%} · "
                        f"Medium stays {persist.get('Medium Vol',0):.0%} · "
                        f"High Vol stays {persist.get('High Vol',0):.0%}")

            st.markdown("##### System Performance by Regime")
            rp_table = []
            for stem in _active_stems:
                si   = systems[stem]
                cutoff = (si["equity"].index.max() - pd.Timedelta(days=int(lookback * 365))
                          if not si["equity"].empty else pd.Timestamp("2000-01-01"))
                tr_t = (si["trades"][si["trades"]["exit_date"] >= cutoff]
                        if not si["trades"].empty else si["trades"])
                rp = compute_system_regime_performance(
                    tr_t, regime_df, si["comm_per_trade"])
                for rl in ["Low Vol", "Medium Vol", "High Vol"]:
                    m = rp.get(rl, {})
                    if m.get("n_trades", 0) == 0:
                        continue
                    rp_table.append({
                        "System":    si["display_name"][:32],
                        "Regime":    rl,
                        "# Trades":  m["n_trades"],
                        "Total P&L": f'${m["total_pnl"]:,.0f}',
                        "Avg P&L":   f'${m["avg_pnl"]:,.0f}',
                        "Win Rate":  f'{m["win_rate"]:.0%}',
                        "PF":        f'{m["profit_factor"]:.2f}',
                    })
            if rp_table:
                st.dataframe(pd.DataFrame(rp_table), hide_index=True,
                             use_container_width=True,
                             height=min(42 + 36 * len(rp_table), 520))
            st.info("💡 Systems robust across all regimes are the strongest core holdings.")