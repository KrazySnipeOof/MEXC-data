"""
Step 2: descriptive statistics per symbol, computed on the daily close series
(plus basic volume stats). Everything here is read-only diagnostics consumed by
the edge classifier (Step 3) and the risk engine (Step 4).
"""
import numpy as np
import pandas as pd
from scipy import stats as sstats

from . import config


def log_returns(close):
    """1-day log returns, NaN across any gap (missing day) rather than spanning it."""
    valid = close.notna()
    r = np.log(close / close.shift(1))
    # a return is only meaningful if both this bar and the prior bar are real (non-gap) bars
    r[~(valid & valid.shift(1))] = np.nan
    return r


def max_drawdown(close):
    s = close.dropna()
    if s.empty:
        return np.nan
    running_max = s.cummax()
    dd = (s - running_max) / running_max
    return float(dd.min()) * 100.0  # %, negative


def naive_short_mae(close, horizons=config.MAE_HORIZONS):
    """
    Coarse MAE proxy for a naive 'short at close' position: for each day, the worst
    (highest) close reached over the next H days, expressed as % adverse move.
    Daily-close based (we don't have intraday highs beyond the daily bar here).
    """
    out = {}
    fwd_max = {}
    n = len(close)
    arr = close.values
    for h in horizons:
        worst = np.full(n, np.nan)
        for i in range(n - 1):
            j = min(i + h, n - 1)
            window = arr[i + 1:j + 1]
            if len(window) and not np.all(np.isnan(window)):
                worst[i] = np.nanmax(window)
        mae_pct = (worst - arr) / arr * 100.0
        out[h] = mae_pct
    return out


def realized_vol(returns, window):
    return returns.rolling(window, min_periods=max(3, window // 2)).std() * np.sqrt(config.ANNUALIZATION)


def volume_stats(daily_df):
    vol = daily_df["Volume"].dropna()
    if vol.empty:
        return {"avg_volume": np.nan, "vol_p50": np.nan, "vol_p90": np.nan, "vol_p99": np.nan,
                "volume_return_corr": np.nan}
    ret = log_returns(daily_df["Close"])
    aligned = pd.concat([vol, ret.abs()], axis=1, keys=["v", "r"]).dropna()
    corr = aligned["v"].corr(aligned["r"]) if len(aligned) >= 10 else np.nan
    return {
        "avg_volume": float(vol.mean()),
        "vol_p50": float(vol.quantile(0.50)),
        "vol_p90": float(vol.quantile(0.90)),
        "vol_p99": float(vol.quantile(0.99)),
        "volume_return_corr": float(corr) if pd.notna(corr) else np.nan,
    }


def describe_symbol(daily_df):
    """Step 2 bundle for one symbol's daily series. daily_df may be None / insufficient."""
    if daily_df is None or daily_df["Close"].notna().sum() < config.MIN_DAILY_BARS:
        return None

    close = daily_df["Close"]
    r = log_returns(close)
    r_valid = r.dropna()

    desc = {
        "n_days": int(close.notna().sum()),
        "avg_close": float(close.mean()),
        "mean_ret": float(r_valid.mean()) if len(r_valid) else np.nan,
        "std_ret": float(r_valid.std()) if len(r_valid) else np.nan,
        "skew_ret": float(sstats.skew(r_valid)) if len(r_valid) >= 8 else np.nan,
        "kurt_ret": float(sstats.kurtosis(r_valid)) if len(r_valid) >= 8 else np.nan,
        "max_ret": float(r_valid.max()) if len(r_valid) else np.nan,
        "min_ret": float(r_valid.min()) if len(r_valid) else np.nan,
        "max_single_day_pump_pct": float((np.exp(r_valid.max()) - 1) * 100) if len(r_valid) else np.nan,
        "max_single_day_dump_pct": float((np.exp(r_valid.min()) - 1) * 100) if len(r_valid) else np.nan,
        "max_drawdown_pct": max_drawdown(close),
    }

    for w in config.REALIZED_VOL_WINDOWS:
        rv = realized_vol(r, w)
        desc[f"realized_vol_{w}d_ann"] = float(rv.iloc[-1]) if len(rv.dropna()) else np.nan
        desc[f"realized_vol_{w}d_ann_median"] = float(rv.median()) if len(rv.dropna()) else np.nan

    mae = naive_short_mae(close)
    for h, arr in mae.items():
        arr_valid = arr[~np.isnan(arr)]
        desc[f"mae_h{h}_p50"] = float(np.percentile(arr_valid, 50)) if len(arr_valid) else np.nan
        desc[f"mae_h{h}_p90"] = float(np.percentile(arr_valid, 90)) if len(arr_valid) else np.nan

    desc.update(volume_stats(daily_df))

    n = len(r_valid)
    if n >= 2 and desc["std_ret"] and desc["std_ret"] > 0:
        desc["mean_ret_tstat"] = desc["mean_ret"] / (desc["std_ret"] / np.sqrt(n))
    else:
        desc["mean_ret_tstat"] = np.nan

    return desc
