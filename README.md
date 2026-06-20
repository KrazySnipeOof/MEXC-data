# MEXC Memecoin Historical Data

Historical 1-minute OHLCV candles for every memecoin tradable on [MEXC](https://www.mexc.com) spot, against USDT.

## How the memecoin universe is built

1. Pull all coins tagged under CoinGecko's `meme-token` category (configurable).
2. Pull MEXC's tradable USDT spot symbols (`/api/v3/exchangeInfo`).
3. Intersect by ticker symbol.
4. Drop any ticker that collides with a coin in the global top-150-by-market-cap
   that isn't itself a well-known large memecoin (handles cases like an obscure
   project ticker-squatting `ADA`, `XRP`, `SOL`, `W`, etc., where MEXC's listing
   is actually the established, non-meme coin).

This is a ticker-based heuristic, not a guarantee — see `KNOWN_LARGE_MEMECOINS`
and the `--exclude` flag in the script if you spot a stray false positive.

## Data format

```
crypto csv data/
  <SYMBOL> data/
    <SYMBOL>_<YEAR>_minute.Last.txt
```

Each file is semicolon-delimited, no header:

```
20260618 010600;0.010164;0.010164;0.010142;0.010142;4055.26
```

Columns: `Datetime(YYYYMMDD HHMMSS);Open;High;Low;Close;Volume`

## Usage

```
python fetch_mexc_meme_klines.py --days 30
python fetch_mexc_meme_klines.py --days 7 --limit 5            # smoke test
python fetch_mexc_meme_klines.py --categories meme-token,solana-meme-coins
python fetch_mexc_meme_klines.py --exclude FOO,BAR              # extra exclusions
```

No third-party dependencies — standard library only. Rerun anytime to refresh
the data; a local cache (`_meme_universe_cache.json`, gitignored) avoids
re-hitting CoinGecko's rate limits on every run within `--cache-ttl-hours`
(default 24h).

`_mexc_meme_manifest.csv` in the output folder records every symbol attempted
and its candle count or error.

## Web dashboard

A local, dependency-free web dashboard for browsing the datasets — candlestick
+ volume charts, symbol search, and interval switching (1m/5m/15m/1h/4h/1d).

```
python web_server.py                 # http://127.0.0.1:8000
python web_server.py --port 8765     # pick a different port
python web_server.py --data "crypto csv data"
```

Then open the printed URL in a browser. Charts are rendered with TradingView's
lightweight-charts (loaded from a CDN, so the page needs internet access; the
candle data itself is served entirely from your local files).

The server (standard library only) exposes two JSON endpoints:

- `GET /api/symbols` — available symbols, each with its `created` date (the
  coin's MEXC listing date when known, otherwise the first candle in the local
  data) and a `listing` flag indicating which of the two it is
- `GET /api/candles?symbol=TRUMPUSDT&interval=5` — OHLCV candles for a symbol,
  optionally aggregated to an N-minute `interval`

### Coin listing ("created") dates

The dashboard shows each coin's creation date next to its symbol. By default
this is the first candle present in the local data, but you can resolve the
real MEXC listing date for every coin with:

```
python fetch_listing_dates.py            # all symbols found in the data dir
python fetch_listing_dates.py --symbols PEPEUSDT,TRUMPUSDT
python fetch_listing_dates.py --refresh  # re-resolve, ignoring the cache
```

This walks each symbol's daily klines backward to find its first candle and
writes `crypto csv data/_listing_dates.json` (`SYMBOL -> YYYY-MM-DD`). The web
server picks that file up automatically and prefers it over the
first-candle-in-data fallback.
