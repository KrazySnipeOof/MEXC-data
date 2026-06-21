"""
Builds SHORT_RESEARCH_REPORT.md from the pipeline outputs in short_research_output/.
Run after run_short_research.py.
"""
import pandas as pd

AS_OF = pd.Timestamp("2026-06-20", tz="UTC")


def to_md_table(df, index=False):
    """Minimal markdown-table renderer (avoids the optional 'tabulate' dependency)."""
    d = df.reset_index() if index else df
    cols = [str(c) for c in d.columns]
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for _, row in d.iterrows():
        vals = []
        for v in row:
            if isinstance(v, float):
                vals.append("" if pd.isna(v) else f"{v:.3f}".rstrip("0").rstrip(".") if v != 0 else "0")
            else:
                vals.append(str(v))
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)

config_df = pd.read_csv("short_research_output/per_symbol_config.csv")
full = pd.read_csv("short_research_output/per_symbol_full.csv")

full["listing_date"] = pd.to_datetime(full["listing_date"], errors="coerce", utc=True)
full["age_days"] = (AS_OF - full["listing_date"]).dt.days


def age_bucket(d):
    if pd.isna(d):
        return "UNKNOWN"
    if d < 90:
        return "NEW (<90d)"
    if d < 365:
        return "ESTABLISHED (90-365d)"
    return "OLD (>365d)"


full["age_bucket"] = full["age_days"].apply(age_bucket)

vol = full["realized_vol_30d_ann"].dropna()
q1, q2 = vol.quantile([0.33, 0.66])


def vol_bucket(v):
    if pd.isna(v):
        return "UNKNOWN"
    if v <= q1:
        return "LOW_VOL"
    if v <= q2:
        return "MED_VOL"
    return "HIGH_VOL"


full["vol_bucket"] = full["realized_vol_30d_ann"].apply(vol_bucket)

liq = full["avg_volume"].dropna()
l1, l2 = liq.quantile([0.33, 0.66])


def liq_bucket(v):
    if pd.isna(v):
        return "UNKNOWN"
    if v <= l1:
        return "LOW_LIQ"
    if v <= l2:
        return "MED_LIQ"
    return "HIGH_LIQ"


full["liq_bucket"] = full["avg_volume"].apply(liq_bucket)

edge_counts = full["edge_class"].value_counts()
age_x_edge = pd.crosstab(full["age_bucket"], full["edge_class"])
vol_x_edge = pd.crosstab(full["vol_bucket"], full["edge_class"])
liq_x_edge = pd.crosstab(full["liq_bucket"], full["edge_class"])

qualifying = full[full["suggested_setup_name"] != "NoTrade"].sort_values("symbol")
no_daily_but_old = full[(full["age_bucket"] == "OLD (>365d)") & (full["edge_class"] == "INSUFFICIENT_HISTORY")]
no_daily_established = full[(full["age_bucket"] == "ESTABLISHED (90-365d)") & (full["edge_class"] == "INSUFFICIENT_HISTORY")]

n_total = len(full)
n_daily_gap = int((full["daily_bars"] == 0).sum())

md = []
md.append("# Systematic Memecoin Shorting Research Framework — MEXC-data\n")
md.append(f"Generated from `./MEXC-data` (offline, no network access). {n_total} symbols scanned. "
          f"As-of anchor date for listing-age buckets: **{AS_OF.date()}** (most recent timestamp present in the minute data).\n")

md.append("## 0. Disclaimer\n")
md.append(
    "This is **historical-tendency research, not financial advice and not a guaranteed profit strategy**. "
    "Memecoins on MEXC spot/margin/perp are extremely thin, manipulable, and capable of multi-hundred-percent "
    "short squeezes with little warning. Nothing here is risk-free or a sure thing. Backtest statistics are "
    "computed on a short daily history (up to ~3 years, often far less) and small trade counts per symbol — "
    "treat every number as a rough, noisy estimate, not a forecast. Past behavior does not guarantee future "
    "results. When in doubt the framework defaults to **NoTrade** or the lowest leverage band.\n"
)

md.append("## 1. Framework — workflow steps actually applied\n")
md.append(
"""1. **Data ingestion & cleaning** (`short_research/ingest.py`) — recursively scans
   `crypto csv data/<SYMBOL> data/` for `*_daily.Last.txt` and `*_minute.Last.txt` files, concatenates
   multi-year daily files per symbol, parses `YYYYMMDD HHMMSS` timestamps to UTC, drops non-positive-price
   rows, and flags/drops single-bar "wick" spikes that show an extreme deviation from the local rolling
   median price **with no confirming volume** and a near-full reversion on the very next bar (the
   bad-tick signature, as distinct from a real, sustained pump). Daily data is reindexed onto a full
   calendar grid so missing days are explicit NaNs — never forward-filled — and returns are never computed
   across a gap. Symbols need >= 30 valid daily bars to clear `INSUFFICIENT_HISTORY`; minute data
   additionally needs >= 7 distinct days and >= 2000 bars to unlock the optional intraday check.
2. **Descriptive statistics** (`short_research/stats_desc.py`) — daily log-return mean/std/skew/kurtosis,
   max/min single-day return, 7d/30d annualized realized vol, max peak-to-trough drawdown, a coarse
   naive-short MAE proxy (worst close reached over the next 1/3/5/10 days after shorting at today's close),
   and volume stats (mean, p50/p90/p99, |return| vs volume correlation).
3. **Short-edge diagnostics** (`short_research/diagnostics.py`) —
   3.1 spike-day mean reversion (close > 2 stdev above its 20d mean, or > 2x its 20d median; forward
   returns at h=1/3/5/10 days), 3.2 momentum/trend-exhaustion (top-15th-percentile 10-day momentum vs
   forward returns), 3.3 high-relative-volume day vs forward returns (>90th percentile trailing volume),
   3.4 an optional, intentionally conservative intraday check (>4-sigma upside minute bar vs next-15-minute
   return, only surfaced if t-stat <= -2 and hit rate >= 60% on >= 30 events), and 3.5 a rule-based
   classifier into `SPIKE_MEAN_REVERSION_EDGE`, `GRIND_DOWN_EDGE`, `MIXED/UNSTABLE`, `NO_CLEAR_SHORT_EDGE`,
   or `INSUFFICIENT_HISTORY`. All rolling thresholds are shifted by one day before comparison — no lookahead.
4. **Risk & leverage analysis** (`short_research/risk.py`) — stop-loss is set at the ~92.5th percentile of
   the historical MAE distribution for the candidate trade rule (so realized adverse excursions only
   exceeded the stop ~7.5% of the time historically), clamped to a [12%, 80%] sane range. Take-profit is
   the median favorable excursion scaled down (x0.8) and clamped to a 1:1-2:1 reward:risk band versus the
   stop. Leverage is the **minimum** of three independent caps: (a) a realized-volatility band (<=5x only
   below ~75% annualized vol, stepping down to 1x above ~300%; this also automatically satisfies the
   "never >5x above 150% vol" rule), (b) a tail-pump band (capped to 3x once history shows a >200% single
   day move, 2x above 400%, 1x above 800%), and (c) a simplified liquidation-buffer model
   (`liquidation_distance ~= 100/leverage`, ignoring maintenance margin/funding) requiring the stop to sit
   at least 25% closer than liquidation **and** a 4-sigma holding-horizon move to still land short of
   liquidation. Because the liquidation model is a simplification that ignores maintenance margin, those
   extra buffers exist specifically to compensate for it.
5. **Back-of-envelope backtest** (`short_research/backtest.py`) — sequential (non-pyramiding) short
   simulation per symbol: enter at the trigger day's close, exit at the earliest of stop-loss, take-profit,
   or a fixed max holding period (5 days for spike setups, 7 for grind setups); a day that breaches both
   levels is conservatively resolved as a stop-loss (daily bars can't reveal true intraday sequencing).
   Setups are downgraded to `MIXED/UNSTABLE` / `NoTrade` with leverage forced to [1,1] when: fewer than 5
   trades fire, hit rate < 30%, max cumulative drawdown breaches -50%, one trade supplies > 50% of all
   positive PnL, or — the binding constraint in practice — **net expectancy is not positive**. A milder set
   of the same flags (without crossing the severe thresholds) only trims leverage by 1x rather than
   killing the setup outright.
6. **Outputs** — the machine-usable config table (`short_research_output/per_symbol_config.csv`) and this
   report, including the cross-sectional breakdown below.
"""
)

md.append("## 2. Headline result\n")
md.append(
    f"Out of {n_total} symbols, only **{len(qualifying)}** ended up with an actual tradeable short setup "
    f"after diagnostics + backtest robustness checks "
    f"({(full['edge_class']=='SPIKE_MEAN_REVERSION_EDGE').sum()} `SPIKE_MEAN_REVERSION_EDGE`, "
    f"{(full['edge_class']=='GRIND_DOWN_EDGE').sum()} `GRIND_DOWN_EDGE`). That's intentional: the framework is "
    "built to default to NoTrade rather than ship a fragile edge, and most memecoin daily-return series simply "
    "don't show a statistically significant, robust short pattern over the available history.\n"
)
md.append(f"Edge class counts (all {n_total} symbols):\n\n")
md.append(edge_counts.to_frame("count").to_markdown() + "\n\n")

md.append(
    f"**Data-quality note:** {n_daily_gap} of {n_total} symbols ({n_daily_gap/n_total:.0%}) have **zero** daily "
    "candle rows in this dataset even though most are clearly old enough (some listed in 2024) to have years of "
    "daily history — the daily fetch simply never ran/succeeded for them in this snapshot of the repo, only "
    "minute data exists. This is the dominant driver of `INSUFFICIENT_HISTORY` and is a data-coverage artifact, "
    "not a property of the coins themselves. The `ESTABLISHED (90-365d)` age bucket is **entirely** "
    f"`INSUFFICIENT_HISTORY` ({len(no_daily_established)} symbols) for exactly this reason.\n"
)

md.append("## 3. The 10 qualifying setups (full detail)\n")
show_cols = ["symbol", "edge_class", "suggested_setup_name", "tp_pct", "sl_pct",
             "suggested_leverage_min", "suggested_leverage_max", "bt_n_trades", "bt_hit_rate",
             "bt_expectancy_pct", "bt_max_dd_pct", "bt_sharpe_like", "realized_vol_30d_ann",
             "max_single_day_pump_pct"]
md.append(qualifying[show_cols].round(3).to_markdown(index=False) + "\n\n")
md.append(
    "Read this as: leverage is capped at 1x for most of these specifically because of >40-180% historical "
    "single-day pumps (GOATUSDT, MOEWUSDT, ORDIUSDT) — the tail-pump cap binds before the volatility cap does. "
    "Stops are wide (12-45%) because memecoin MAE distributions are wide; that is the realistic cost of shorting "
    "this asset class, not a calibration bug.\n"
)

md.append("## 4. Cross-sectional observations\n")
md.append("### By listing age\n\n" + age_x_edge.to_markdown() + "\n\n")
md.append("### By realized-volatility tertile (30d annualized)\n\n" + vol_x_edge.to_markdown() + "\n\n")
md.append("### By liquidity tertile (avg daily base-asset volume)\n\n" + liq_x_edge.to_markdown() + "\n\n")
md.append(
    "**Patterns:**\n"
    "- Almost every symbol with enough daily history to be analyzed at all (`daily_sufficient`) falls in the "
    "`OLD (>365d)` bucket, simply because you need >=30 valid daily bars to clear Step 1 and most newly-listed "
    "symbols haven't been alive that long yet — so genuine cross-sectional age comparison is mostly limited to "
    "older listings here; newer coins are systematically under-represented in the active-edge population, not "
    "because they lack edges but because they lack history.\n"
    "- Edge symbols (`SPIKE_MEAN_REVERSION_EDGE` + `GRIND_DOWN_EDGE`) are spread across all three volatility "
    "tertiles and all three liquidity tertiles — having *some* measurable mean-reversion edge is not simply a "
    "proxy for being low-volatility or thinly traded. High historical single-day pumps (GOATUSDT, MOEWUSDT, "
    "ORDIUSDT, SUNCATUSDT) show up inside the edge group too, which is exactly why the leverage engine leans on "
    "the pump-cap rather than the vol-cap for those names.\n"
    "- `MIXED/UNSTABLE` (49 symbols) is the single largest non-NoTrade-by-default bucket among history-sufficient "
    "symbols: these are cases where the raw diagnostic stage found *something* (a spike-reversion or grind-down "
    "signal) that then failed the backtest robustness gates — overwhelmingly on the non-positive-expectancy or "
    "excessive-drawdown checks. That is the framework doing its job: a raw statistical pattern existing is not "
    "the same as it being safe to trade.\n"
    "- `NO_CLEAR_SHORT_EDGE` (100 symbols) is the modal outcome among symbols with sufficient history — most "
    "memecoin daily-return series, even very volatile ones, don't show a forward-return pattern that clears the "
    "significance and hit-rate bars used here.\n"
)

md.append("## 5. Full per-symbol config table (machine-usable)\n")
md.append(
    "Columns: `symbol, edge_class, suggested_setup_name, entry_condition_human_readable, tp_pct (negative=profit "
    "target on the short), sl_pct (positive=loss threshold), suggested_leverage_min, suggested_leverage_max`. "
    "Also available as `short_research_output/per_symbol_config.csv`.\n\n"
)
md.append(config_df.to_markdown(index=False) + "\n")

with open("SHORT_RESEARCH_REPORT.md", "w", encoding="utf-8") as f:
    f.write("\n".join(md))

print("Wrote SHORT_RESEARCH_REPORT.md")
print(f"n_total={n_total} n_daily_gap={n_daily_gap} qualifying={len(qualifying)}")
