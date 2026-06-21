"""
Orchestrates Steps 1-5 per symbol and produces the Step 6 outputs:
  - a machine-usable per-symbol config table (required fields)
  - a fuller per-symbol research table (everything used to get there)
"""
import json
import os

import numpy as np
import pandas as pd

from . import backtest, config, diagnostics, ingest, risk, stats_desc

SETUP_NAMES = {
    "SPIKE_MEAN_REVERSION_EDGE": "SpikeShortDaily",
    "GRIND_DOWN_EDGE": "GrindShortDaily",
}


def _entry_condition_text(edge_class):
    if edge_class == "SPIKE_MEAN_REVERSION_EDGE":
        return (f"Close > {config.SPIKE_STD_MULT} stdev above its {config.SPIKE_ROLL_WINDOW}d mean "
                f"or > {config.SPIKE_MEDIAN_MULT}x its {config.SPIKE_ROLL_WINDOW}d median; "
                f"short at close, hold up to {config.HOLD_DAYS_SPIKE}d")
    if edge_class == "GRIND_DOWN_EDGE":
        return (f"New {config.GRIND_ENTRY_WINDOW}d closing high inside a statistically negative "
                f"drift regime; short at close, hold up to {config.HOLD_DAYS_GRIND}d")
    if edge_class == "MIXED/UNSTABLE":
        return "Diagnostic sub-tests conflict, or backtest invalidated the raw signal"
    if edge_class == "INSUFFICIENT_HISTORY":
        return f"Fewer than {config.MIN_DAILY_BARS} valid daily bars after cleaning"
    return "No statistically significant short pattern in spike/momentum/volume tests"


def analyze_symbol(symbol, symbol_dir, listing_dates=None):
    bundle = ingest.load_and_clean_symbol(symbol, symbol_dir)
    daily, minute, history = bundle["daily"], bundle["minute"], bundle["history"]

    record = {
        "symbol": symbol,
        "daily_bars": history["daily_bars"],
        "minute_rows": history["minute_rows"],
        "daily_sufficient": history["daily_sufficient"],
        "minute_sufficient": history["minute_sufficient"],
    }

    if listing_dates and symbol in listing_dates:
        record["listing_date"] = listing_dates[symbol]

    if not history["daily_sufficient"]:
        record.update({
            "edge_class": "INSUFFICIENT_HISTORY",
            "suggested_setup_name": "NoTrade",
            "entry_condition": _entry_condition_text("INSUFFICIENT_HISTORY"),
            "tp_pct": np.nan, "sl_pct": np.nan,
            "suggested_leverage_min": 1.0, "suggested_leverage_max": 1.0,
        })
        return record

    desc = stats_desc.describe_symbol(daily)
    diag = diagnostics.run_diagnostics(daily, minute, history)
    edge_class = diag["edge_class"]
    record["desc"] = desc
    record["diag_meta"] = {
        "n_spike_events": diag["spike"]["n_spike_events"] if diag["spike"] else None,
        "spike_primary": diag["spike"]["primary"] if diag["spike"] else None,
        "n_momentum_events": diag["momentum"]["n_momentum_events"] if diag["momentum"] else None,
        "momentum_primary": diag["momentum"]["primary"] if diag["momentum"] else None,
        "n_volume_events": diag["volume"]["n_volume_events"] if diag["volume"] else None,
        "volume_primary": diag["volume"]["primary"] if diag["volume"] else None,
        "overall": diag["overall"],
        "intraday": diag["intraday"],
    }

    if edge_class not in SETUP_NAMES:
        record.update({
            "edge_class": edge_class,
            "suggested_setup_name": "NoTrade",
            "entry_condition": _entry_condition_text(edge_class),
            "tp_pct": np.nan, "sl_pct": np.nan,
            "suggested_leverage_min": 1.0, "suggested_leverage_max": 1.0,
        })
        return record

    hold_days = config.HOLD_DAYS_SPIKE if edge_class == "SPIKE_MEAN_REVERSION_EDGE" else config.HOLD_DAYS_GRIND
    entry_idx = backtest.get_entry_indices(daily, edge_class, diag)
    mae_arr, mfe_arr = backtest.path_excursions(daily, entry_idx, hold_days)
    sltp = risk.calibrate_sl_tp(mae_arr, mfe_arr)

    if sltp is None:
        record.update({
            "edge_class": "MIXED/UNSTABLE",
            "suggested_setup_name": "NoTrade",
            "entry_condition": _entry_condition_text("MIXED/UNSTABLE"),
            "tp_pct": np.nan, "sl_pct": np.nan,
            "suggested_leverage_min": 1.0, "suggested_leverage_max": 1.0,
        })
        return record

    trades = backtest.simulate_trades(daily, entry_idx, hold_days, sltp["sl_pct"], sltp["tp_pct"])
    metrics = backtest.aggregate_metrics(trades)
    reasons = backtest.downgrade_check(metrics)
    record["backtest_metrics"] = metrics
    record["downgrade_reasons"] = reasons
    record["sltp_calibration"] = sltp

    ann_vol_candidates = [desc.get("realized_vol_30d_ann"), desc.get("realized_vol_30d_ann_median")]
    ann_vol_candidates = [v for v in ann_vol_candidates if pd.notna(v)]
    ann_vol_frac = max(ann_vol_candidates) if ann_vol_candidates else np.nan

    lev = risk.leverage_recommendation(
        ann_vol_frac, desc.get("max_single_day_pump_pct"), sltp["sl_pct"], desc.get("std_ret"), hold_days,
    )
    record["leverage_calc"] = lev

    severe = (
        metrics["n_trades"] < config.MIN_TRADES_FOR_CONFIDENCE
        or (pd.notna(metrics["hit_rate"]) and metrics["hit_rate"] < 0.30)
        or (pd.notna(metrics["max_dd_pct"]) and metrics["max_dd_pct"] < config.MAX_DD_DOWNGRADE_PCT)
        or "non_positive_expectancy" in reasons
    )
    mild = (not severe) and len(reasons) > 0

    if severe:
        record.update({
            "edge_class": "MIXED/UNSTABLE",
            "suggested_setup_name": "NoTrade",
            "entry_condition": _entry_condition_text("MIXED/UNSTABLE") + " (backtest failed robustness checks: " + ", ".join(reasons) + ")",
            "tp_pct": np.nan, "sl_pct": np.nan,
            "suggested_leverage_min": 1.0, "suggested_leverage_max": 1.0,
        })
        return record

    lev_max = lev["leverage_max"]
    lev_min = lev["leverage_min"]
    if mild:
        lev_max = max(config.MIN_LEVERAGE, lev_max - 1.0)
        lev_min = config.MIN_LEVERAGE

    record.update({
        "edge_class": edge_class,
        "suggested_setup_name": SETUP_NAMES[edge_class],
        "entry_condition": _entry_condition_text(edge_class) + (f" [downgraded: {', '.join(reasons)}]" if mild else ""),
        "tp_pct": -sltp["tp_pct"],
        "sl_pct": sltp["sl_pct"],
        "suggested_leverage_min": lev_min,
        "suggested_leverage_max": lev_max,
    })
    return record


def load_listing_dates(base_dir=config.BASE_DIR):
    path = os.path.join(base_dir, "_listing_dates.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def run_all(base_dir=config.BASE_DIR, limit=None):
    listing_dates = load_listing_dates(base_dir)
    dirs = ingest.find_symbol_dirs(base_dir)
    items = list(dirs.items())
    if limit:
        items = items[:limit]

    records = []
    for symbol, d in items:
        rec = analyze_symbol(symbol, d, listing_dates)
        records.append(rec)
    return records


CONFIG_COLUMNS = [
    "symbol", "edge_class", "suggested_setup_name", "entry_condition",
    "tp_pct", "sl_pct", "suggested_leverage_min", "suggested_leverage_max",
]


def to_config_df(records):
    rows = []
    for r in records:
        rows.append({c: r.get(c) for c in CONFIG_COLUMNS})
    df = pd.DataFrame(rows, columns=CONFIG_COLUMNS)
    df = df.rename(columns={"entry_condition": "entry_condition_human_readable"})
    for c in ["tp_pct", "sl_pct"]:
        df[c] = df[c].astype(float).round(2)
    for c in ["suggested_leverage_min", "suggested_leverage_max"]:
        df[c] = df[c].astype(float)
    return df


def to_full_df(records):
    rows = []
    for r in records:
        row = {
            "symbol": r["symbol"],
            "daily_bars": r["daily_bars"],
            "minute_rows": r["minute_rows"],
            "edge_class": r["edge_class"],
            "suggested_setup_name": r["suggested_setup_name"],
            "tp_pct": r["tp_pct"],
            "sl_pct": r["sl_pct"],
            "suggested_leverage_min": r["suggested_leverage_min"],
            "suggested_leverage_max": r["suggested_leverage_max"],
            "listing_date": r.get("listing_date"),
        }
        desc = r.get("desc") or {}
        for k in ["mean_ret", "std_ret", "skew_ret", "kurt_ret", "max_drawdown_pct",
                  "realized_vol_30d_ann", "realized_vol_7d_ann", "max_single_day_pump_pct",
                  "max_single_day_dump_pct", "avg_volume", "avg_close", "volume_return_corr"]:
            row[k] = desc.get(k)
        metrics = r.get("backtest_metrics") or {}
        for k in ["n_trades", "hit_rate", "expectancy_pct", "max_dd_pct", "sharpe_like", "dominance_frac"]:
            row[f"bt_{k}"] = metrics.get(k)
        diag_meta = r.get("diag_meta") or {}
        sp = diag_meta.get("spike_primary") or {}
        row["spike_n_events"] = diag_meta.get("n_spike_events")
        row["spike_h5_mean"] = sp.get("mean")
        row["spike_h5_hitrate"] = sp.get("hit_rate")
        row["spike_h5_tstat"] = sp.get("tstat")
        intraday = diag_meta.get("intraday") or {}
        row["intraday_robust"] = intraday.get("robust")
        rows.append(row)
    return pd.DataFrame(rows)
