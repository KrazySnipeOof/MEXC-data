# MEXC Memecoin Historical Data

Historical OHLCV candles for memecoins tradable on [MEXC](https://www.mexc.com) spot, against USDT:
- **1-minute candles, last ~30 days** (the longest history MEXC's API will serve at 1-minute resolution)
- **Daily candles, last ~3 years** (or since listing, if shorter)

## How the memecoin universe is built

1. Pull all coins tagged under a set of CoinGecko meme-related categories (configurable;
   default just `meme-token`, but the fuller dataset here was built from a union of ~20
   categories — `solana-meme-coins`, `base-meme-coins`, `ai-meme-coins`, etc. — since
   CoinGecko's category buckets are siloed, not hierarchical: a coin tagged "Solana Meme"
   on its own profile page is not automatically included when querying the generic "Meme"
   bucket).
2. Pull MEXC's tradable USDT spot symbols (`/api/v3/exchangeInfo`).
3. Intersect by ticker symbol.
4. Drop any ticker that collides with a coin in the global top-150-by-market-cap
   that isn't itself a well-known large memecoin (handles cases like an obscure
   project ticker-squatting `ADA`, `XRP`, `SOL`, `W`, etc., where MEXC's listing
   is actually the established, non-meme coin).

This is a ticker-based heuristic, not a guarantee:
- See `KNOWN_LARGE_MEMECOINS` and the `--exclude` flag if you spot a false positive
  (a ticker collision that let a non-meme coin through).
- Use `--include` to force-add a ticker CoinGecko's category lists missed — this
  happens for very low-cap coins sitting right at a category list's cutoff (e.g.
  `TIT`/titcoin, ~$300k market cap, was added this way after confirming it independently
  via CoinGecko's per-coin profile data).

## Known MEXC API limitation: 1-minute candles only go back ~30 days

`/api/v3/klines` with `interval=1m` returns an **empty result** (not an error) once
`startTime` is more than ~30 days in the past, regardless of how long the symbol has
actually been listed. This is a hard server-side limit, not a bug in this script. Coarser
intervals (`60m`, `4h`, `1d`, `1M`) are not subject to this cliff and support multi-year
lookback — hence the separate daily dataset for longer-term history.

Valid `--interval` values: `1m, 5m, 15m, 30m, 60m, 4h, 1d, 1M`. Despite looking like
reasonable aliases, `1h`, `1w`, and `1mo` are all rejected by the API.

For symbols with very little real trading history, requesting a much wider date range
than the symbol's actual lifetime can also return empty or inconsistent results even for
the longer intervals — if a symbol looks suspicious (0 candles in the manifest despite
being actively traded), try narrowing `--days` to roughly match its real listing age.

## Data format

```
crypto csv data/
  <SYMBOL> data/
    <SYMBOL>_<YEAR>_minute.Last.txt   # 1-minute candles, ~last 30 days
    <SYMBOL>_<YEAR>_daily.Last.txt    # daily candles, ~last 3 years
```

Each file is semicolon-delimited, no header:

```
20260618 010600;0.010164;0.010164;0.010142;0.010142;4055.26
```

Columns: `Datetime(YYYYMMDD HHMMSS);Open;High;Low;Close;Volume`

## Usage

```
python fetch_mexc_meme_klines.py --days 30                          # 1m, capped at ~30d by MEXC
python fetch_mexc_meme_klines.py --interval 1d --days 1095          # 3y of daily candles
python fetch_mexc_meme_klines.py --days 7 --limit 5                 # smoke test
python fetch_mexc_meme_klines.py --categories meme-token,solana-meme-coins
python fetch_mexc_meme_klines.py --exclude FOO,BAR                  # extra exclusions
python fetch_mexc_meme_klines.py --include TIT                      # force-add a missed ticker
```

No third-party dependencies — standard library only. Rerun anytime to refresh
the data; a local cache (`_meme_universe_cache.json`, gitignored) avoids
re-hitting CoinGecko's rate limits on every run within `--cache-ttl-hours`
(default 24h).

`_mexc_meme_manifest.csv` (1-minute run) and `_mexc_meme_manifest_daily.csv`
(daily run) in the output folder record every symbol attempted and its candle
count or error.

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
