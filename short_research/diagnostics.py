"""
Step 3: short-edge diagnostics.

3.1 spike mean-reversion, 3.2 trend exhaustion / continuation, 3.3 volume-conditioned
forward returns, 3.4 optional intraday check, 3.5 edge classification.

All forward-return windows use only information available at signal time (rolling
stats are shifted by 1 day) -- no lookahead.
"""
import numpy as np
import pandas as pd

from . import config


def _forward_returns(close, event_mask, horizons):
    """For each True in event_mask, simple % forward return at each horizon."""
    arr = close.values
    n = len(arr)
    idx = np.where(event_mask.values)[0]
    out = {h: [] for h in horizons}
    for i in idx:
        if np.isnan(arr[i]) or arr[i] == 0:
            continue
        for h in horizons:
            j = i + h
            if j < n and not np.isnan(arr[j]):
                out[h].append((arr[j] - arr[i]) / arr[i] * 100.0)
            else:
                out[h].append(np.nan)
    return idx, {h: np.array(v, dtype=float) for h, v in out.items()}


def _summarize_forward(returns_pct):
    """returns_pct: array of % forward returns (positive = price up). For a short,
    negative is a 'win'. Returns n, mean, median, hit_rate (frac negative), tstat."""
    v = returns_pct[~np.isnan(returns_pct)]
    n = len(v)
    if n == 0:
        return dict(n=0, mean=np.nan, median=np.nan, hit_rate=np.nan, tstat=np.nan)
    mean = float(np.mean(v))
    std = float(np.std(v, ddof=1)) if n >= 2 else np.nan
    tstat = mean / (std / np.sqrt(n)) if std and std > 0 else np.nan
    hit_rate = float(np.mean(v < 0))
    return dict(n=int(n), mean=mean, median=float(np.median(v)), hit_rate=hit_rate, tstat=float(tstat) if pd.notna(tstat) else np.nan)


def spike_diagnostics(daily_df):
    """3.1 Spike-day mean-reversion test."""
    close = daily_df["Close"]
    roll_mean = close.rolling(config.SPIKE_ROLL_WINDOW, min_periods=10).mean().shift(1)
    roll_std = close.rolling(config.SPIKE_ROLL_WINDOW, min_periods=10).std().shift(1)
    roll_med = close.rolling(config.SPIKE_ROLL_WINDOW, min_periods=10).median().shift(1)

    cond_std = close > (roll_mean + config.SPIKE_STD_MULT * roll_std)
    cond_med = close > (config.SPIKE_MEDIAN_MULT * roll_med)
    spike_mask = (cond_std | cond_med).fillna(False)

    idx, fwd = _forward_returns(close, spike_mask, config.SPIKE_FORWARD_HORIZONS)
    by_horizon = {h: _summarize_forward(v) for h, v in fwd.items()}
    primary = by_horizon[config.SPIKE_PRIMARY_HORIZON]
    return {
        "n_spike_events": len(idx),
        "spike_event_idx": idx,
        "by_horizon": by_horizon,
        "primary": primary,
    }


def momentum_diagnostics(daily_df):
    """3.2 Trend exhaustion / continuation test on N-day momentum extremes."""
    close = daily_df["Close"]
    mom = (close / close.shift(config.MOMENTUM_WINDOW) - 1.0) * 100.0
    mom_signal = mom.shift(1)  # known as-of the entry day, no lookahead
    thresh = mom_signal.quantile(config.MOMENTUM_PCTL)
    event_mask = (mom_signal > thresh).fillna(False)

    idx, fwd = _forward_returns(close, event_mask, config.SPIKE_FORWARD_HORIZONS)
    by_horizon = {h: _summarize_forward(v) for h, v in fwd.items()}
    primary = by_horizon[config.SPIKE_PRIMARY_HORIZON]
    return {
        "n_momentum_events": len(idx),
        "threshold_pct": float(thresh) if pd.notna(thresh) else np.nan,
        "by_horizon": by_horizon,
        "primary": primary,
    }


def volume_diagnostics(daily_df):
    """3.3 High relative-volume day -> forward return test."""
    close = daily_df["Close"]
    vol = daily_df["Volume"]
    roll_vol_thresh = vol.rolling(config.SPIKE_ROLL_WINDOW, min_periods=10).quantile(config.VOLUME_PCTL).shift(1)
    event_mask = (vol > roll_vol_thresh).fillna(False)

    idx, fwd = _forward_returns(close, event_mask, config.SPIKE_FORWARD_HORIZONS)
    by_horizon = {h: _summarize_forward(v) for h, v in fwd.items()}
    primary = by_horizon[config.SPIKE_PRIMARY_HORIZON]
    return {
        "n_volume_events": len(idx),
        "by_horizon": by_horizon,
        "primary": primary,
    }


def overall_drift_diagnostics(daily_df):
    """Whole-history drift test used for GRIND_DOWN_EDGE detection."""
    close = daily_df["Close"]
    r = np.log(close / close.shift(1))
    r = r.dropna()
    n = len(r)
    if n < config.GRIND_MIN_DAILY_BARS:
        return dict(n=n, mean=np.nan, tstat=np.nan)
    mean = float(r.mean())
    std = float(r.std())
    tstat = mean / (std / np.sqrt(n)) if std > 0 else np.nan
    return dict(n=n, mean=mean, tstat=float(tstat) if pd.notna(tstat) else np.nan)


def intraday_diagnostics(minute_df):
    """
    3.4 Optional: large upside minute bar -> next M-minute forward return.
    Only meaningful, robust signals are surfaced; otherwise treated as 'no signal'
    rather than forced into a conclusion.
    """
    if minute_df is None or len(minute_df) < config.MIN_MINUTE_ROWS:
        return {"available": False}

    close = minute_df["Close"]
    r = np.log(close / close.shift(1)) * 100.0
    std = r.std()
    if not std or np.isnan(std) or std == 0:
        return {"available": False}

    event_mask = (r > config.INTRADAY_MOVE_STD_MULT * std).fillna(False)
    n_events = int(event_mask.sum())
    if n_events < config.INTRADAY_MIN_EVENTS:
        return {"available": True, "n_events": n_events, "robust": False}

    idx, fwd = _forward_returns(close, event_mask, [config.INTRADAY_FORWARD_MIN])
    summary = _summarize_forward(fwd[config.INTRADAY_FORWARD_MIN])
    robust = (
        summary["n"] >= config.INTRADAY_MIN_EVENTS
        and pd.notna(summary["tstat"])
        and summary["tstat"] <= -config.INTRADAY_SIG_TSTAT
        and summary["hit_rate"] >= config.INTRADAY_HITRATE_MIN
    )
    return {"available": True, "n_events": n_events, "summary": summary, "robust": bool(robust)}


def classify_edge(spike, momentum, volume_diag, overall, history_ok):
    """
    3.5 Map diagnostics to a qualitative edge class.

    Decision order:
      1. INSUFFICIENT_HISTORY if daily history failed Step 1's bar-count gate.
      2. SPIKE_MEAN_REVERSION_EDGE if spike-day forward returns are significantly
         negative with an adequate hit rate AND momentum/volume tests don't show a
         strongly contradicting continuation signal.
      3. GRIND_DOWN_EDGE if there's no strong spike-reversion signal but the whole-history
         drift is significantly negative (persistent decay).
      4. MIXED/UNSTABLE if sub-tests point in conflicting directions or signals are
         borderline/contradictory.
      5. NO_CLEAR_SHORT_EDGE otherwise.
    """
    if not history_ok:
        return "INSUFFICIENT_HISTORY"

    sp = spike["primary"]
    mo = momentum["primary"]
    vo = volume_diag["primary"]

    spike_significant = (
        spike["n_spike_events"] >= config.MIN_SPIKE_EVENTS
        and pd.notna(sp["tstat"]) and sp["tstat"] <= -config.SIG_TSTAT
        and pd.notna(sp["hit_rate"]) and sp["hit_rate"] >= config.SPIKE_HITRATE_MIN
    )
    momentum_continuation = (
        momentum["n_momentum_events"] >= config.MIN_MOMENTUM_EVENTS
        and pd.notna(mo["tstat"]) and mo["tstat"] >= config.SIG_TSTAT
    )
    momentum_reversion = (
        momentum["n_momentum_events"] >= config.MIN_MOMENTUM_EVENTS
        and pd.notna(mo["tstat"]) and mo["tstat"] <= -config.SIG_TSTAT
    )
    volume_continuation = (
        volume_diag["n_volume_events"] >= config.MIN_VOLUME_EVENTS
        and pd.notna(vo["tstat"]) and vo["tstat"] >= config.SIG_TSTAT
    )
    grind_significant = (
        pd.notna(overall["tstat"]) and overall["n"] >= config.GRIND_MIN_DAILY_BARS
        and overall["tstat"] <= -config.SIG_TSTAT and overall["mean"] < 0
    )

    if spike_significant and not momentum_continuation:
        return "SPIKE_MEAN_REVERSION_EDGE"
    if spike_significant and momentum_continuation:
        # spike-day reversion shows up, but the broader trend test says strength tends
        # to persist -- sub-tests disagree, don't ship a clean directional edge
        return "MIXED/UNSTABLE"
    if grind_significant and not (momentum_continuation or volume_continuation):
        return "GRIND_DOWN_EDGE"
    if (momentum_reversion or grind_significant) and (momentum_continuation or volume_continuation):
        return "MIXED/UNSTABLE"
    return "NO_CLEAR_SHORT_EDGE"


def run_diagnostics(daily_df, minute_df, history):
    """Full Step 3 bundle for one symbol."""
    history_ok = history["daily_sufficient"]
    if not history_ok:
        return {
            "history_ok": False,
            "edge_class": "INSUFFICIENT_HISTORY",
            "spike": None, "momentum": None, "volume": None, "overall": None, "intraday": {"available": False},
        }

    spike = spike_diagnostics(daily_df)
    momentum = momentum_diagnostics(daily_df)
    volume_diag = volume_diagnostics(daily_df)
    overall = overall_drift_diagnostics(daily_df)
    intraday = intraday_diagnostics(minute_df) if history.get("minute_sufficient") else {"available": False}

    edge_class = classify_edge(spike, momentum, volume_diag, overall, history_ok)

    return {
        "history_ok": True,
        "edge_class": edge_class,
        "spike": spike,
        "momentum": momentum,
        "volume": volume_diag,
        "overall": overall,
        "intraday": intraday,
    }
