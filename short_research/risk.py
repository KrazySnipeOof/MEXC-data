"""
Step 4: risk and leverage analysis.

Turns the empirical MAE/MFE distribution (from backtest.path_excursions) into a
SL/TP pair, then turns realized volatility + tail-pump history + a simplified
liquidation-distance model into a leverage interval.
"""
import numpy as np
import pandas as pd

from . import config

VOL_LEVERAGE_BANDS = [
    (0.75, 5.0),
    (1.50, 3.0),
    (3.00, 2.0),
    (float("inf"), 1.0),
]

PUMP_LEVERAGE_BANDS = [
    (2.00, config.MAX_LEVERAGE_HARD_CAP),  # no extra cap below 200% single-day pump
    (4.00, 3.0),
    (8.00, 2.0),
    (float("inf"), 1.0),
]


def calibrate_sl_tp(mae_arr, mfe_arr):
    """Step 4.2/4.3: SL from the MAE tail, TP bounded to a sane R:R vs typical MFE."""
    mae_valid = mae_arr[~np.isnan(mae_arr)] if len(mae_arr) else np.array([])
    if len(mae_valid) == 0:
        return None

    raw_sl = float(np.percentile(mae_valid, config.MAE_SL_PERCENTILE * 100))
    sl_pct = float(np.clip(raw_sl, config.SL_FLOOR_PCT, config.SL_CAP_PCT))
    outlier_capped = raw_sl > config.SL_CAP_PCT

    mfe_valid = mfe_arr[~np.isnan(mfe_arr)] if len(mfe_arr) else np.array([])
    median_mfe = float(np.median(mfe_valid)) if len(mfe_valid) else sl_pct

    tp_raw = 0.8 * median_mfe
    rr = tp_raw / sl_pct if sl_pct > 0 else config.TP_RR_MIN
    rr_clamped = float(np.clip(rr, config.TP_RR_MIN, config.TP_RR_MAX))
    tp_pct = sl_pct * rr_clamped

    return {
        "sl_pct": round(sl_pct, 2),
        "tp_pct": round(tp_pct, 2),
        "raw_sl_pct": round(raw_sl, 2),
        "sl_outlier_capped": bool(outlier_capped),
        "median_mfe_pct": round(median_mfe, 2),
        "rr": round(rr_clamped, 2),
        "n_mae_samples": int(len(mae_valid)),
    }


def _band_cap(value, bands):
    if value is None or pd.isna(value):
        return config.MIN_LEVERAGE
    for threshold, cap in bands:
        if value <= threshold:
            return cap
    return config.MIN_LEVERAGE


def leverage_recommendation(ann_vol_frac, max_single_day_pump_pct, sl_pct, daily_std_ret, hold_days):
    """
    Step 4.4/4.5: combine a volatility-based cap, a tail-pump cap, and a liquidation-
    buffer cap (approximate isolated-margin distance ~= 100/leverage) into one number.

    Liquidation model is a simplified linear approximation (ignores maintenance margin
    and funding) -- intentionally conservative extra buffers (25% beyond SL, clearing a
    4-sigma move) are applied specifically to compensate for that simplification.
    """
    vol_cap = _band_cap(ann_vol_frac, VOL_LEVERAGE_BANDS)

    pump_frac = (max_single_day_pump_pct / 100.0) if pd.notna(max_single_day_pump_pct) else None
    pump_cap = _band_cap(pump_frac, PUMP_LEVERAGE_BANDS)

    if pd.isna(daily_std_ret) or daily_std_ret is None:
        tail_move_pct = config.SL_CAP_PCT
    else:
        tail_move_pct = config.LIQ_TAIL_SIGMA * daily_std_ret * 100.0 * np.sqrt(hold_days)

    liq_move_required = max(sl_pct * config.LIQ_SL_BUFFER_MULT, tail_move_pct)
    liq_cap = (100.0 / liq_move_required) if liq_move_required > 0 else config.MIN_LEVERAGE

    leverage_max = min(vol_cap, pump_cap, liq_cap, config.MAX_LEVERAGE_HARD_CAP)
    leverage_max = max(config.MIN_LEVERAGE, np.floor(leverage_max * 2) / 2.0)  # round down to nearest 0.5x

    if leverage_max > 1.5:
        leverage_min = max(config.MIN_LEVERAGE, np.floor((leverage_max / 2.0) * 2) / 2.0)
    else:
        leverage_min = config.MIN_LEVERAGE

    return {
        "leverage_min": float(leverage_min),
        "leverage_max": float(leverage_max),
        "vol_cap": float(vol_cap),
        "pump_cap": float(pump_cap),
        "liq_cap": round(float(liq_cap), 2),
        "liq_move_required_pct": round(float(liq_move_required), 2),
        "tail_move_pct": round(float(tail_move_pct), 2),
    }
