"""
Resolve each coin's true listing date on MEXC (the date of its earliest
available candle) and cache the result to `_listing_dates.json` next to the
data, so the web dashboard can show a real "created" date per coin.

MEXC's /api/v3/klines caps responses at 500 candles and, given a huge range,
returns the most *recent* ones -- so we page backward via `endTime` until a
batch comes back shorter than the cap, meaning we've reached the first candle.

By default it resolves every symbol found under the local data directory
(so the dates line up with what the dashboard serves).

Usage:
    python fetch_listing_dates.py
    python fetch_listing_dates.py --symbols TRUMPUSDT,PEPEUSDT   # just these
    python fetch_listing_dates.py --refresh                      # ignore cache
    python fetch_listing_dates.py --data "crypto csv data"
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

MEXC_BASE = "https://api.mexc.com"
USER_AGENT = "Mozilla/5.0 (compatible; mexc-listing-dates/1.0)"
SCRIPT_DIR = Path(__file__).parent
KLINE_CAP = 500

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


DAY_MS = 86400 * 1000
WINDOW_MS = (KLINE_CAP - 1) * DAY_MS  # MEXC requires startTime..endTime to span <= ~500 intervals


def earliest_kline_ms(symbol, max_windows=120):
    """Find a symbol's first daily candle (its listing date).

    MEXC's klines endpoint ignores `endTime` on its own and rejects ranges wider
    than ~500 intervals, so we step backward one <=499-day window at a time
    (passing both startTime and endTime) until we find the window that contains
    the very first candle, or hit an empty window (listing is just after it).
    """
    end_ms = int(time.time() * 1000)
    earliest = None
    for _ in range(max_windows):
        start_ms = end_ms - WINDOW_MS
        batch = http_get_json(
            f"{MEXC_BASE}/api/v3/klines",
            params={
                "symbol": symbol,
                "interval": "1d",
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": KLINE_CAP,
            },
        )
        if not batch:
            break  # coin didn't exist this far back; earliest is from a later window
        first = batch[0][0]
        earliest = first if earliest is None else min(earliest, first)
        # If the window's first candle sits after its start, there is nothing
        # before it -> that's the listing candle.
        if first > start_ms + DAY_MS:
            break
        end_ms = start_ms - 1
        time.sleep(0.1)
    return earliest


def symbols_from_data(data_dir):
    out = []
    if data_dir.exists():
        for sub in sorted(data_dir.iterdir()):
            if sub.is_dir() and sub.name.endswith(" data"):
                out.append(sub.name[: -len(" data")])
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default=str(SCRIPT_DIR / "crypto csv data"), help="Data root used to enumerate symbols")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols to resolve instead of scanning --data")
    parser.add_argument("--out", default="", help="Output JSON path (default: <data>/_listing_dates.json)")
    parser.add_argument("--refresh", action="store_true", help="Re-resolve even symbols already in the cache")
    args = parser.parse_args()

    data_dir = Path(args.data)
    out_path = Path(args.out) if args.out else (data_dir / "_listing_dates.json")

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = symbols_from_data(data_dir)
    if not symbols:
        print("No symbols to resolve.", file=sys.stderr)
        return

    cache = {}
    if out_path.exists():
        try:
            cache = json.loads(out_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}

    todo = symbols if args.refresh else [s for s in symbols if s not in cache]
    print(f"Resolving listing dates for {len(todo)} symbol(s) ({len(symbols) - len(todo)} cached)")

    resolved = 0
    for i, symbol in enumerate(todo, 1):
        try:
            ms = earliest_kline_ms(symbol)
            if ms is None:
                print(f"[{i}/{len(todo)}] {symbol}: no candles", file=sys.stderr)
                continue
            date = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            cache[symbol] = date
            resolved += 1
            print(f"[{i}/{len(todo)}] {symbol}: {date}")
        except Exception as e:
            print(f"[{i}/{len(todo)}] {symbol}: FAILED {e}", file=sys.stderr)
        if i % 10 == 0:
            out_path.write_text(json.dumps(cache, indent=0, sort_keys=True), encoding="utf-8")

    out_path.write_text(json.dumps(cache, indent=0, sort_keys=True), encoding="utf-8")
    print(f"\nDone: resolved {resolved}, total cached {len(cache)} -> {out_path}")


if __name__ == "__main__":
    main()
