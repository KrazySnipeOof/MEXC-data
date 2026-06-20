"""
Local web dashboard for browsing the MEXC memecoin OHLCV datasets.

Standard-library only -- no third-party deps, matching fetch_mexc_meme_klines.py.
Reads the semicolon-delimited candle files under "crypto csv data/" and serves
them to a single-page frontend (web/index.html) that renders candlestick +
volume charts via TradingView's lightweight-charts (loaded from a CDN).

Usage:
    python web_server.py                 # serve on http://127.0.0.1:8000
    python web_server.py --port 8080
    python web_server.py --data "crypto csv data" --host 0.0.0.0
"""

import argparse
import json
import re
import threading
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCRIPT_DIR = Path(__file__).parent
WEB_DIR = SCRIPT_DIR / "web"

# Module-level config, set in main().
DATA_DIR = SCRIPT_DIR / "crypto csv data"

_SYMBOL_CACHE_LOCK = threading.Lock()
_SYMBOL_CACHE = None


def _symbol_files():
    """Map SYMBOL -> sorted list of candle file paths for that symbol."""
    out = {}
    if not DATA_DIR.exists():
        return out
    for sub in sorted(DATA_DIR.iterdir()):
        if not sub.is_dir() or not sub.name.endswith(" data"):
            continue
        symbol = sub.name[: -len(" data")]
        files = sorted(sub.glob(f"{symbol}_*_minute.Last.txt"))
        if files:
            out[symbol] = files
    return out


def list_symbols():
    global _SYMBOL_CACHE
    with _SYMBOL_CACHE_LOCK:
        if _SYMBOL_CACHE is None:
            _SYMBOL_CACHE = sorted(_symbol_files().keys())
        return _SYMBOL_CACHE


_LINE_RE = re.compile(
    r"^(\d{8})\s+(\d{6});([^;]+);([^;]+);([^;]+);([^;]+);([^;\r\n]+)"
)


@lru_cache(maxsize=64)
def load_candles(symbol):
    """Parse all candle files for a symbol into a list of dicts.

    Datetime string YYYYMMDD HHMMSS is interpreted as UTC and returned as a
    unix-second timestamp (what lightweight-charts expects for `time`).
    """
    files = _symbol_files().get(symbol)
    if not files:
        return None

    import calendar

    candles = []
    for path in files:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                m = _LINE_RE.match(line)
                if not m:
                    continue
                ymd, hms, o, h, l, c, v = m.groups()
                ts = calendar.timegm(
                    (
                        int(ymd[0:4]), int(ymd[4:6]), int(ymd[6:8]),
                        int(hms[0:2]), int(hms[2:4]), int(hms[4:6]),
                        0, 0, 0,
                    )
                )
                candles.append(
                    {
                        "time": ts,
                        "open": float(o),
                        "high": float(h),
                        "low": float(l),
                        "close": float(c),
                        "volume": float(v),
                    }
                )
    candles.sort(key=lambda r: r["time"])
    return candles


def _downsample(candles, interval_min):
    """Aggregate 1-minute candles into `interval_min`-minute OHLCV buckets."""
    if interval_min <= 1:
        return candles
    bucket = interval_min * 60
    out = []
    cur = None
    cur_key = None
    for c in candles:
        key = c["time"] - (c["time"] % bucket)
        if key != cur_key:
            if cur is not None:
                out.append(cur)
            cur_key = key
            cur = {
                "time": key,
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "volume": c["volume"],
            }
        else:
            cur["high"] = max(cur["high"], c["high"])
            cur["low"] = min(cur["low"], c["low"])
            cur["close"] = c["close"]
            cur["volume"] += c["volume"]
    if cur is not None:
        out.append(cur)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "MexcDash/1.0"

    def log_message(self, fmt, *args):  # quieter logging
        pass

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status, obj):
        self._send(status, json.dumps(obj, separators=(",", ":")))

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/" or path == "/index.html":
                return self._serve_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            if path == "/api/symbols":
                return self._send_json(200, {"symbols": list_symbols()})
            if path == "/api/candles":
                return self._handle_candles(parse_qs(parsed.query))
            if path.startswith("/web/"):
                # Allow serving any static asset placed in web/.
                rel = path[len("/web/"):]
                return self._serve_static(rel)
            return self._send_json(404, {"error": "not found"})
        except BrokenPipeError:
            pass
        except Exception as e:  # never crash the server on a bad request
            self._send_json(500, {"error": str(e)})

    def _handle_candles(self, qs):
        symbol = (qs.get("symbol", [""])[0] or "").strip().upper()
        if not symbol:
            return self._send_json(400, {"error": "missing symbol"})
        try:
            interval = int(qs.get("interval", ["1"])[0])
        except ValueError:
            interval = 1
        interval = max(1, min(interval, 1440))

        candles = load_candles(symbol)
        if candles is None:
            return self._send_json(404, {"error": f"unknown symbol {symbol}"})
        data = _downsample(candles, interval)
        return self._send_json(
            200,
            {
                "symbol": symbol,
                "interval": interval,
                "count": len(data),
                "candles": data,
            },
        )

    _CONTENT_TYPES = {
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
    }

    def _serve_static(self, rel):
        # Prevent path traversal.
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())):
            return self._send_json(403, {"error": "forbidden"})
        ctype = self._CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        return self._serve_file(target, ctype)

    def _serve_file(self, path, content_type):
        if not path.exists() or not path.is_file():
            return self._send_json(404, {"error": "not found"})
        self._send(200, path.read_bytes(), content_type)


def main():
    global DATA_DIR
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    parser.add_argument("--data", default=str(SCRIPT_DIR / "crypto csv data"), help="Path to the candle data root")
    args = parser.parse_args()

    DATA_DIR = Path(args.data)

    n = len(list_symbols())
    print(f"Serving {n} symbols from {DATA_DIR}")
    print(f"Dashboard: http://{args.host}:{args.port}/")
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
