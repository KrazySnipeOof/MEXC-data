"""
Step 1: data ingestion and cleaning.

Scans `crypto csv data/<SYMBOL> data/` for the per-year daily and minute
`.Last.txt` files, concatenates them per symbol, parses timestamps to UTC,
drops/flags bad rows, and tags each symbol with a history-sufficiency verdict.
"""
import glob
import os
import re

import numpy as np
import pandas as pd

from . import config

COLS = ["Open", "High", "Low", "Close", "Volume"]
_SYMBOL_DIR_RE = re.compile(r"^(?P<symbol>.+) data$")


def find_symbol_dirs(base_dir=config.BASE_DIR):
    """Return {symbol: dirpath} for every '<SYMBOL> data' folder under base_dir."""
    out = {}
    for d in sorted(glob.glob(os.path.join(base_dir, "*"))):
        if not os.path.isdir(d):
            continue
        m = _SYMBOL_DIR_RE.match(os.path.basename(d))
        if m:
            out[m.group("symbol")] = d
    return out


def _read_one(path):
    df = pd.read_csv(
        path,
        sep=";",
        header=None,
        names=["Datetime"] + COLS,
        dtype={"Datetime": str},
    )
    df["Datetime"] = pd.to_datetime(df["Datetime"], format="%Y%m%d %H%M%S", utc=True)
    return df


def load_concat(symbol_dir, pattern):
    """Load and concat every file matching pattern (e.g. '*_daily.Last.txt')."""
    files = sorted(glob.glob(os.path.join(symbol_dir, pattern)))
    if not files:
        return None
    frames = [_read_one(f) for f in files]
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset="Datetime").sort_values("Datetime")
    df = df.set_index("Datetime")
    for c in COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def load_daily(symbol_dir):
    return load_concat(symbol_dir, "*_daily.Last.txt")


def load_minute(symbol_dir):
    return load_concat(symbol_dir, "*_minute.Last.txt")


def _flag_unconfirmed_wicks(df):
    """
    Flag single-bar price spikes that look like bad ticks rather than real moves:
    Close deviates from the local rolling median by > WICK_RATIO_MULT, on volume
    far below the local rolling median (no participation), AND the very next bar
    reverts most of the way back. Real pumps/dumps keep volume up and don't fully
    revert one bar later; a lone unconfirmed wick does.
    """
    n = len(df)
    if n < config.ROLLING_WINDOW:
        return pd.Series(False, index=df.index)

    roll_med_price = df["Close"].rolling(config.ROLLING_WINDOW, center=True, min_periods=5).median()
    roll_med_vol = df["Volume"].rolling(config.ROLLING_WINDOW, center=True, min_periods=5).median()

    ratio = df["Close"] / roll_med_price
    extreme = (ratio > config.WICK_RATIO_MULT) | (ratio < 1.0 / config.WICK_RATIO_MULT)
    unconfirmed_vol = df["Volume"] < (config.WICK_VOL_CONFIRM_FRAC * roll_med_vol)

    next_close = df["Close"].shift(-1)
    next_ratio = next_close / roll_med_price
    move_size = (ratio - 1.0).abs()
    next_move_size = (next_ratio - 1.0).abs()
    reverted = next_move_size <= (1.0 - config.WICK_REVERT_FRAC) * move_size

    flagged = extreme & unconfirmed_vol & reverted & roll_med_price.notna()
    return flagged.fillna(False)


def clean_ohlcv(df):
    """Drop non-positive prices and unconfirmed single-bar wicks. Returns (clean_df, report)."""
    report = {"raw_rows": len(df)}
    if df is None or df.empty:
        report.update(nonpositive_dropped=0, wicks_dropped=0, clean_rows=0)
        return df, report

    nonpositive = (df[["Open", "High", "Low", "Close"]] <= 0).any(axis=1)
    df1 = df.loc[~nonpositive].copy()
    report["nonpositive_dropped"] = int(nonpositive.sum())

    wick_flag = _flag_unconfirmed_wicks(df1)
    df2 = df1.loc[~wick_flag].copy()
    report["wicks_dropped"] = int(wick_flag.sum())
    report["clean_rows"] = len(df2)
    return df2, report


def assess_history(daily_clean, minute_clean):
    """Return dict of sufficiency flags used to gate a symbol into/out of active recs."""
    daily_bars = 0 if daily_clean is None else daily_clean["Close"].notna().sum()
    daily_sufficient = daily_bars >= config.MIN_DAILY_BARS

    minute_days = 0
    minute_rows = 0 if minute_clean is None else len(minute_clean)
    if minute_clean is not None and not minute_clean.empty:
        minute_days = pd.Series(minute_clean.index.date).nunique()
    minute_sufficient = (minute_days >= config.MIN_MINUTE_DAYS) and (minute_rows >= config.MIN_MINUTE_ROWS)

    return {
        "daily_bars": int(daily_bars),
        "daily_sufficient": bool(daily_sufficient),
        "minute_days": int(minute_days),
        "minute_rows": int(minute_rows),
        "minute_sufficient": bool(minute_sufficient),
    }


def load_and_clean_symbol(symbol, symbol_dir):
    """Full Step 1 pipeline for a single symbol. Returns a dict bundle."""
    raw_daily = load_daily(symbol_dir)
    raw_minute = load_minute(symbol_dir)

    daily_clean, daily_report = clean_ohlcv(raw_daily) if raw_daily is not None else (None, {"raw_rows": 0, "nonpositive_dropped": 0, "wicks_dropped": 0, "clean_rows": 0})
    minute_clean, minute_report = clean_ohlcv(raw_minute) if raw_minute is not None else (None, {"raw_rows": 0, "nonpositive_dropped": 0, "wicks_dropped": 0, "clean_rows": 0})

    # Daily: reindex to a full calendar grid so gaps are explicit NaNs, never forward-filled.
    if daily_clean is not None and not daily_clean.empty:
        full_idx = pd.date_range(daily_clean.index.min().normalize(), daily_clean.index.max().normalize(), freq="D", tz="UTC")
        daily_clean = daily_clean.reindex(full_idx)

    history = assess_history(daily_clean, minute_clean)

    return {
        "symbol": symbol,
        "daily": daily_clean,
        "minute": minute_clean,
        "daily_clean_report": daily_report,
        "minute_clean_report": minute_report,
        "history": history,
    }


def iter_symbols(base_dir=config.BASE_DIR, limit=None):
    dirs = find_symbol_dirs(base_dir)
    items = list(dirs.items())
    if limit:
        items = items[:limit]
    for symbol, d in items:
        yield load_and_clean_symbol(symbol, d)
