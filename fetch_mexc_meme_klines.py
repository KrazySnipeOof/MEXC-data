"""
Fetch 1-minute (or other interval) historical klines for memecoins listed on MEXC spot.

Memecoin universe = CoinGecko category coins (default category: meme-token),
intersected with MEXC's tradable spot symbols for the given quote asset (default USDT).
Ticker-symbol matching is an approximation -- a handful of MEXC listings may be
false positives/negatives if a ticker is reused across unrelated projects.

Output format: crypto csv data/{SYMBOL} data/{SYMBOL}_{YEAR}_minute.Last.txt,
semicolon-delimited, no header, columns:
    Datetime(YYYYMMDD HHMMSS);Open;High;Low;Close;Volume
split into one file per calendar year, so it drops straight into
load_1m_data()-style loaders (pandas.read_csv(sep=";", header=None, ...)).

Usage:
    python fetch_mexc_meme_klines.py --days 30
    python fetch_mexc_meme_klines.py --days 7 --interval 1m --limit 5   # smoke test
    python fetch_mexc_meme_klines.py --categories meme-token,solana-meme-coins --quote USDT
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

MEXC_BASE = "https://api.mexc.com"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
USER_AGENT = "Mozilla/5.0 (compatible; mexc-meme-fetcher/1.0)"
SCRIPT_DIR = Path(__file__).parent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def http_get_json(url, params=None, retries=6):
    full_url = f"{url}?{urlencode(params)}" if params else url
    req = urllib.request.Request(full_url, headers={"User-Agent": USER_AGENT})
    delay = 2.0
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            code = getattr(e, "code", None)
            if attempt < retries - 1 and (code in (429, 418, 503) or code is None):
                wait = delay
                retry_after = getattr(e, "headers", None) and e.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = float(retry_after) + 1
                    except ValueError:
                        pass
                print(f"  (rate limited, waiting {wait:.0f}s...)", file=sys.stderr)
                time.sleep(wait)
                delay *= 1.7
                continue
            raise
    raise RuntimeError(f"Failed to GET {full_url}")


def _coingecko_markets_tickers(params_overrides, max_pages=10):
    tickers = set()
    page = 1
    while page <= max_pages:
        data = http_get_json(
            f"{COINGECKO_BASE}/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 250,
                "page": page,
                "sparkline": "false",
                **params_overrides,
            },
        )
        if not data:
            break
        for coin in data:
            sym = coin.get("symbol", "").upper()
            if sym:
                tickers.add(sym)
        if len(data) < 250:
            break
        page += 1
        time.sleep(1.5)
    return tickers


def _fetch_meme_tickers(categories):
    tickers = set()
    for category in categories:
        tickers |= _coingecko_markets_tickers({"category": category}, max_pages=100)
    return tickers


def _fetch_top_market_cap_tickers(n):
    return _coingecko_markets_tickers({}, max_pages=-(-n // 250))


def cached_fetch(key, fetch_fn, cache_path, cache_ttl_hours, use_cache=True):
    if use_cache and cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            entry = cache.get(key)
            if entry and (time.time() - entry["fetched_at"]) < cache_ttl_hours * 3600:
                age_h = (time.time() - entry["fetched_at"]) / 3600
                print(f"Using cached '{key}' list (age {age_h:.1f}h)")
                return set(entry["tickers"])
        except (json.JSONDecodeError, KeyError, OSError):
            pass

    tickers = fetch_fn()

    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}
    cache[key] = {"fetched_at": time.time(), "tickers": sorted(tickers)}
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    return tickers


# Large-cap projects whose ticker is sometimes ticker-squatted by an unrelated,
# much smaller project that CoinGecko tags under a meme category. We treat any
# ticker among the global top-N-by-market-cap as "not a memecoin" unless it's
# also a well-known large memecoin (whitelisted below), since MEXC's listing
# for a shared ticker is essentially always the established, high-cap project.
KNOWN_LARGE_MEMECOINS = {
    "DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI", "BOME", "TRUMP", "MEME",
    "MOG", "BRETT", "POPCAT", "TURBO", "NEIRO", "DOGS", "HMSTR", "CATI",
    "PNUT", "GOAT", "FARTCOIN", "ACT", "AIDOGE", "BABYDOGE",
}


def get_non_meme_blue_chip_tickers(top_n, cache_path, cache_ttl_hours, use_cache=True):
    top_tickers = cached_fetch(
        f"top_market_cap_{top_n}",
        lambda: _fetch_top_market_cap_tickers(top_n),
        cache_path, cache_ttl_hours, use_cache,
    )
    return top_tickers - KNOWN_LARGE_MEMECOINS


def get_mexc_symbols(quote):
    info = http_get_json(f"{MEXC_BASE}/api/v3/exchangeInfo")
    out = {}
    for s in info.get("symbols", []):
        if s.get("quoteAsset") == quote and s.get("isSpotTradingAllowed"):
            out[s["baseAsset"].upper()] = s["symbol"]
    return out


def fetch_klines(symbol, interval, start_ms, end_ms, limit=500):
    candles = []
    cursor = start_ms
    while cursor < end_ms:
        batch = http_get_json(
            f"{MEXC_BASE}/api/v3/klines",
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": limit,
            },
        )
        if not batch:
            break
        candles.extend(batch)
        next_cursor = batch[-1][0] + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(0.1)
    return candles


def write_year_files(out_dir, symbol, candles):
    by_year = defaultdict(list)
    for c in candles:
        dt = datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc)
        by_year[dt.year].append((dt, c))

    symbol_dir = out_dir / f"{symbol} data"
    symbol_dir.mkdir(parents=True, exist_ok=True)

    for year, rows in sorted(by_year.items()):
        rows.sort(key=lambda r: r[0])
        path = symbol_dir / f"{symbol}_{year}_minute.Last.txt"
        with open(path, "w", encoding="utf-8") as f:
            for dt, c in rows:
                ts = dt.strftime("%Y%m%d %H%M%S")
                f.write(f"{ts};{c[1]};{c[2]};{c[3]};{c[4]};{c[5]}\n")
        print(f"  -> {len(rows)} bars -> {path}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--days", type=float, default=30, help="Days of history to fetch (default: 30)")
    parser.add_argument("--interval", default="1m", help="Kline interval: 1m,5m,15m,30m,1h,4h,1d,1w,1mo (default: 1m)")
    parser.add_argument("--quote", default="USDT", help="Quote asset (default: USDT)")
    parser.add_argument(
        "--categories", default="meme-token",
        help="Comma-separated CoinGecko category ids unioned as the memecoin universe (default: meme-token)",
    )
    parser.add_argument(
        "--out", default=str(SCRIPT_DIR / "crypto csv data"),
        help="Output root directory (default: ./crypto csv data next to this script)",
    )
    parser.add_argument("--limit", type=int, default=0, help="Cap number of symbols fetched, 0 = no cap (useful for smoke testing)")
    parser.add_argument("--cache-ttl-hours", type=float, default=24, help="Reuse cached CoinGecko lists if younger than this (default: 24)")
    parser.add_argument("--no-cache", action="store_true", help="Force fresh CoinGecko lookups, ignoring any cache")
    parser.add_argument(
        "--no-blue-chip-filter", action="store_true",
        help="Don't exclude tickers that collide with a global top-market-cap (non-meme) coin",
    )
    parser.add_argument(
        "--exclude", default="",
        help="Comma-separated extra tickers to exclude (e.g. for ticker collisions you've spotted)",
    )
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    cache_path = SCRIPT_DIR / "_meme_universe_cache.json"

    print(f"Fetching memecoin universe from CoinGecko categories: {categories}")
    meme_tickers = cached_fetch(
        f"meme:{','.join(sorted(categories))}",
        lambda: _fetch_meme_tickers(categories),
        cache_path, args.cache_ttl_hours, use_cache=not args.no_cache,
    )
    print(f"Found {len(meme_tickers)} memecoin tickers on CoinGecko")

    if not args.no_blue_chip_filter:
        print("Fetching global top-market-cap tickers to filter out ticker-squatted blue chips...")
        blue_chip_tickers = get_non_meme_blue_chip_tickers(150, cache_path, args.cache_ttl_hours, use_cache=not args.no_cache)
        collisions = sorted(meme_tickers & blue_chip_tickers)
        if collisions:
            print(f"Excluding {len(collisions)} ticker(s) that collide with major non-meme coins: {collisions}")
        meme_tickers -= blue_chip_tickers

    manual_exclude = {t.strip().upper() for t in args.exclude.split(",") if t.strip()}
    meme_tickers -= manual_exclude

    print("Fetching MEXC tradable symbols...")
    mexc_symbols = get_mexc_symbols(args.quote)
    print(f"MEXC has {len(mexc_symbols)} {args.quote} spot pairs")

    matched = sorted((b, mexc_symbols[b]) for b in meme_tickers if b in mexc_symbols)
    print(f"Matched {len(matched)} memecoins tradable on MEXC against {args.quote}\n")

    if args.limit:
        matched = matched[: args.limit]

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(args.days * 86400 * 1000)

    manifest_path = out_dir / "_mexc_meme_manifest.csv"
    ok, failed = 0, 0
    with open(manifest_path, "w", encoding="utf-8") as manifest:
        manifest.write("base_asset,mexc_symbol,status\n")
        for base, symbol in matched:
            try:
                print(f"Fetching {symbol} ({args.interval}, last {args.days}d)...")
                candles = fetch_klines(symbol, args.interval, start_ms, end_ms)
                if candles:
                    write_year_files(out_dir, symbol, candles)
                manifest.write(f"{base},{symbol},ok:{len(candles)} candles\n")
                ok += 1
            except Exception as e:
                print(f"  -> FAILED: {e}", file=sys.stderr)
                manifest.write(f"{base},{symbol},error:{e}\n")
                failed += 1

    print(f"\nDone: {ok} ok, {failed} failed. Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
