"""
Step 4.2/4.3 excursion sampling + Step 5 back-of-envelope backtest.

Everything operates on daily bars only (the data we have multi-year history for).
Trades are simulated sequentially per symbol -- a new signal is ignored while a
prior trade from the same rule is still open, so metrics aren't inflated by
pyramiding into the same move.
"""
import numpy as np
import pandas as pd

from . import config


def grind_entry_indices(daily_df):
    """GRIND_DOWN_EDGE candidate entries: local N-day-high closes (short the bounce)."""
    close = daily_df["Close"]
    roll_max = close.rolling(config.GRIND_ENTRY_WINDOW, min_periods=config.GRIND_ENTRY_WINDOW).max()
    mask = (close >= roll_max).fillna(False)
    return np.where(mask.values)[0]


def get_entry_indices(daily_df, edge_class, diag):
    if edge_class == "SPIKE_MEAN_REVERSION_EDGE":
        return np.asarray(diag["spike"]["spike_event_idx"])
    if edge_class == "GRIND_DOWN_EDGE":
        return grind_entry_indices(daily_df)
    return np.array([], dtype=int)


def path_excursions(daily_df, entry_idx, hold_days):
    """
    Step 4.2: per-trade MAE/MFE over the holding window, with no TP/SL cutoff --
    used purely to calibrate sensible TP/SL levels from realized price paths.
    MAE% = worst adverse move for a short (price rallying against it, via High).
    MFE% = best favorable move for a short (price dropping in its favor, via Low).
    """
    close = daily_df["Close"].values
    high = daily_df["High"].values
    low = daily_df["Low"].values
    n = len(close)

    mae, mfe = [], []
    for i in entry_idx:
        entry = close[i]
        if np.isnan(entry) or entry <= 0:
            continue
        j_end = min(i + hold_days, n - 1)
        if j_end <= i:
            continue
        h_window = high[i + 1:j_end + 1]
        l_window = low[i + 1:j_end + 1]
        if np.all(np.isnan(h_window)) or np.all(np.isnan(l_window)):
            continue
        worst = np.nanmax(h_window)
        best = np.nanmin(l_window)
        mae.append((worst - entry) / entry * 100.0)
        mfe.append((entry - best) / entry * 100.0)
    return np.array(mae), np.array(mfe)


def simulate_trades(daily_df, entry_idx, hold_days, sl_pct, tp_pct):
    """
    Step 5.1: sequential short simulation with TP/SL/time exit.
    Conservative tie-break: if a day's High and Low both breach SL and TP, SL is
    assumed to trigger first (daily bars can't tell us the true intraday order).
    No pyramiding: a new signal during an open trade is skipped.
    """
    close = daily_df["Close"].values
    high = daily_df["High"].values
    low = daily_df["Low"].values
    dates = daily_df.index
    n = len(close)

    trades = []
    blocked_until = -1
    for i in sorted(entry_idx):
        if i <= blocked_until:
            continue
        entry = close[i]
        if np.isnan(entry) or entry <= 0:
            continue
        sl_price = entry * (1 + sl_pct / 100.0)
        tp_price = entry * (1 - tp_pct / 100.0)

        exit_reason, exit_price, exit_i = "TIME", close[min(i + hold_days, n - 1)], min(i + hold_days, n - 1)
        for j in range(i + 1, min(i + hold_days, n - 1) + 1):
            h, l = high[j], low[j]
            if np.isnan(h) or np.isnan(l):
                continue
            sl_hit = h >= sl_price
            tp_hit = l <= tp_price
            if sl_hit:
                exit_reason, exit_price, exit_i = "SL", sl_price, j
                break
            if tp_hit:
                exit_reason, exit_price, exit_i = "TP", tp_price, j
                break

        pnl_pct = (entry - exit_price) / entry * 100.0
        trades.append({
            "entry_idx": i, "entry_date": dates[i], "entry_price": entry,
            "exit_idx": exit_i, "exit_date": dates[exit_i], "exit_reason": exit_reason,
            "exit_price": exit_price, "pnl_pct": pnl_pct,
        })
        blocked_until = exit_i

    return trades


def aggregate_metrics(trades):
    if not trades:
        return {
            "n_trades": 0, "hit_rate": np.nan, "avg_win": np.nan, "avg_loss": np.nan,
            "expectancy_pct": np.nan, "max_dd_pct": np.nan, "sharpe_like": np.nan,
            "dominance_frac": np.nan, "tp_hits": 0, "sl_hits": 0, "time_exits": 0,
        }
    pnl = np.array([t["pnl_pct"] for t in trades])
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    equity = np.cumsum(pnl)
    running_max = np.maximum.accumulate(np.concatenate([[0], equity]))[1:]
    dd = equity - running_max
    max_dd = float(dd.min()) if len(dd) else np.nan

    pos_sum = wins.sum() if len(wins) else 0.0
    dominance = float(wins.max() / pos_sum) if len(wins) and pos_sum > 0 else np.nan

    return {
        "n_trades": len(trades),
        "hit_rate": float(np.mean(pnl > 0)),
        "avg_win": float(wins.mean()) if len(wins) else np.nan,
        "avg_loss": float(losses.mean()) if len(losses) else np.nan,
        "expectancy_pct": float(pnl.mean()),
        "max_dd_pct": max_dd,
        "sharpe_like": float(pnl.mean() / pnl.std()) if pnl.std() > 0 else np.nan,
        "dominance_frac": dominance,
        "tp_hits": int(sum(t["exit_reason"] == "TP" for t in trades)),
        "sl_hits": int(sum(t["exit_reason"] == "SL" for t in trades)),
        "time_exits": int(sum(t["exit_reason"] == "TIME" for t in trades)),
    }


def downgrade_check(metrics):
    """Step 5.3: flag setups that shouldn't be trusted even if the diagnostic stage liked them."""
    reasons = []
    if metrics["n_trades"] < config.MIN_TRADES_FOR_CONFIDENCE:
        reasons.append("too_few_trades")
    if pd.notna(metrics["hit_rate"]) and metrics["hit_rate"] < config.MIN_HIT_RATE:
        reasons.append("low_hit_rate")
    if pd.notna(metrics["max_dd_pct"]) and metrics["max_dd_pct"] < config.MAX_DD_DOWNGRADE_PCT:
        reasons.append("excessive_drawdown")
    if pd.notna(metrics["dominance_frac"]) and metrics["dominance_frac"] > config.DOMINANCE_FRAC:
        reasons.append("pnl_dominated_by_one_trade")
    if pd.notna(metrics["expectancy_pct"]) and metrics["expectancy_pct"] <= 0:
        reasons.append("non_positive_expectancy")
    return reasons
