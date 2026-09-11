"""Results explorer — the first gexbot UI (monitoring spec §14.3 seed).

Reads backtest bundles (days.parquet / trades.parquet / gates.json /
summary.md) and renders: headline stats, equity curve, per-day P&L, the
trade log, and gate status. Read-only by design (§15.1): it renders
evidence; it changes nothing.

Run via:  python -m gexbot explore    (serves on GEX_DASHBOARD_PORT, localhost)
"""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import streamlit as st

st.set_page_config(page_title="gexbot — results", layout="wide")

# ── locate bundles ───────────────────────────────────────────────────
from gexbot.config import settings  # noqa: E402

results_root = Path(settings.gex_data_root) / "results"
bundles = sorted([p for p in results_root.iterdir() if p.is_dir()],
                 reverse=True) if results_root.exists() else []

st.title("gexbot — simulated trades & P&L")
if not bundles:
    st.info("No results bundles yet. Run:  python -m gexbot backtest --start ... --end ...")
    st.stop()

labels = [b.name for b in bundles]
choice = st.sidebar.selectbox("Results bundle", labels, index=0)
bundle = bundles[labels.index(choice)]
st.sidebar.caption(f"{len(bundles)} bundle(s) in {results_root}")

days = pl.read_parquet(bundle / "days.parquet") if (bundle / "days.parquet").exists() else None
trades = pl.read_parquet(bundle / "trades.parquet") if (bundle / "trades.parquet").exists() else None
_gj = json.loads((bundle / "gates.json").read_text()) if (bundle / "gates.json").exists() else {}
# Bundles written before the --max-dd flag are flat {gate: {...}}; newer ones
# nest under "gates" and carry the thresholds that judged the run.
gates = _gj.get("gates", _gj)
gate_params = _gj.get("gate_params", {})

# ── headline stats ───────────────────────────────────────────────────
if days is not None and days.height:
    net = float(days["pnl"].sum())
    n_trades = int(days["trades"].sum())
    costs = float(days["costs"].sum())
    halt_days = int(days["halted"].sum())
    win_rate = None
    pf = None
    if trades is not None and trades.height:
        wins = trades.filter(pl.col("pnl") > 0)
        losses = trades.filter(pl.col("pnl") < 0)
        win_rate = wins.height / trades.height
        lw, ll = float(wins["pnl"].sum()), -float(losses["pnl"].sum())
        pf = float("inf") if ll == 0 else lw / ll

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Net P&L", f"${net:+,.0f}")
    c2.metric("Trades", f"{n_trades}")
    c3.metric("Win rate", f"{win_rate:.0%}" if win_rate is not None else "—")
    c4.metric("Profit factor", f"{pf:.2f}" if pf not in (None, float('inf')) else ("∞" if pf else "—"))
    c5.metric("Costs paid", f"${costs:,.0f}")
    c6.metric("Halt days", f"{halt_days}")

    # ── equity curve + daily P&L ─────────────────────────────────────
    left, right = st.columns(2)
    dd = days.sort("day").with_columns(pl.col("pnl").cum_sum().alias("equity"))
    with left:
        st.subheader("Equity curve (cumulative P&L)")
        st.line_chart(dd.select(["day", "equity"]).to_pandas().set_index("day"))
    with right:
        st.subheader("Daily P&L")
        st.bar_chart(dd.select(["day", "pnl"]).to_pandas().set_index("day"))

# ── trade log ────────────────────────────────────────────────────────
st.subheader("Trade log")
if trades is not None and trades.height:
    def _hhmm(m: int) -> str:
        return f"{m // 60:02d}:{m % 60:02d}"

    view = trades.with_columns(
        pl.col("entry_minute").map_elements(_hhmm, return_dtype=pl.Utf8).alias("entry"),
        pl.col("exit_minute").map_elements(_hhmm, return_dtype=pl.Utf8).alias("exit"),
        pl.when(pl.col("direction") > 0).then(pl.lit("CALL"))
          .otherwise(pl.lit("PUT")).alias("side"),
        pl.col("exit_reason").str.replace("ExitReason.", "").alias("why"),
        pl.col("pnl").round(0),
        pl.col("entry_fill").round(2),
        pl.col("exit_fill").round(2),
    ).select(["day", "entry", "exit", "side", "kind", "contracts",
              "entry_fill", "exit_fill", "why", "pnl"]).sort(["day", "entry"])
    st.dataframe(view.to_pandas(), use_container_width=True, hide_index=True)

    st.subheader("P&L by exit reason")
    by_reason = (trades.with_columns(
        pl.col("exit_reason").str.replace("ExitReason.", "").alias("why"))
        .group_by("why").agg(pl.len().alias("count"), pl.col("pnl").sum().round(0))
        .sort("pnl", descending=True))
    st.dataframe(by_reason.to_pandas(), hide_index=True)
else:
    st.caption("No trades in this bundle — check blocked-signal logs and data coverage.")

# ── gates ────────────────────────────────────────────────────────────
st.subheader("§10 Gates")
if gate_params:
    st.caption(
        f"Judged at: OOS maxDD <= {gate_params.get('max_drawdown_frac', 0):.1%} · "
        f"OOS PF >= {gate_params.get('min_profit_factor_oos', '?')} · "
        f"IS/OOS trades >= {gate_params.get('min_trades_is', '?')}/"
        f"{gate_params.get('min_trades_oos', '?')}")
else:
    st.caption("Bundle predates threshold recording — judged at the GateParams "
               "defaults of its day (OOS maxDD 12%).")
for name, g in gates.items():
    icon = "✅" if g.get("passed") else "❌"
    st.write(f"{icon} **{name}** — {g.get('detail','')}")

st.caption("Read-only by design: this screen renders evidence and changes nothing. "
           "Marks on days without downloaded quotes are model-estimated (BS fallback) "
           "— treat P&L on those days as approximate until the REST mark fetcher lands.")
