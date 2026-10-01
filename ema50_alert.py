"""
50 EMA cross alert (daily timeframe) for Indian (NSE) stocks.

- Alerts when price crosses the 50 EMA upside (green) or downside (red).
- Compares yesterday's close vs EMA50 with the latest (live) price vs EMA50.
- Runs only on weekdays between 09:15 and 15:45 IST (set FORCE=true to test).
- Universe (env UNIVERSE): all | nifty500 | watchlist
- Each stock alerts at most once per direction per day.
"""
import io
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import pandas as pd
import requests
import yfinance as yf

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
UNIVERSE = os.environ.get("UNIVERSE", "all").strip().lower()
MIN_AVG_VOL = int(os.environ.get("MIN_AVG_VOL", "100000") or 0)
FORCE = os.environ.get("FORCE", "false").strip().lower() == "true"

EMA_LEN = 50
BATCH = 150
STATE_FILE = "ema50_state.json"
IST = timezone(timedelta(hours=5, minutes=30))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/csv,*/*",
}
ALL_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
N500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"


def market_is_open(now):
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= minutes <= (15 * 60 + 45)


def read_csv_url(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = [c.strip() for c in df.columns]
    return df


def load_watchlist(path="watchlist.txt"):
    with open(path) as f:
        return [l.strip().upper() for l in f if l.strip() and not l.startswith("#")]


def load_symbols():
    if UNIVERSE == "all":
        try:
            df = read_csv_url(ALL_URL)
            df = df[df["SERIES"].astype(str).str.strip() == "EQ"]
            syms = sorted(set(df["SYMBOL"].astype(str).str.strip().str.upper()))
            if len(syms) > 500:
                print(f"Universe: all NSE EQ stocks ({len(syms)})")
                return syms
        except Exception as e:
            print(f"Could not load full NSE list ({e}); trying NIFTY 500", file=sys.stderr)
    if UNIVERSE in ("all", "nifty500"):
        try:
            df = read_csv_url(N500_URL)
            syms = sorted(set(df["Symbol"].astype(str).str.strip().str.upper()))
            if len(syms) > 100:
                print(f"Universe: NIFTY 500 ({len(syms)})")
                return syms
        except Exception as e:
            print(f"Could not load NIFTY 500 list ({e}); using watchlist.txt", file=sys.stderr)
    syms = load_watchlist()
    print(f"Universe: watchlist.txt ({len(syms)})")
    return syms


def fetch_prices(tickers):
    """Download daily data in batches. Returns {ticker: DataFrame[Close, Volume]}."""
    out = {}
    for i in range(0, len(tickers), BATCH):
        chunk = tickers[i:i + BATCH]
        try:
            d = yf.download(
                chunk, period="1y", interval="1d", group_by="ticker",
                auto_adjust=False, progress=False, threads=True,
            )
        except Exception as e:
            print(f"Batch {i // BATCH + 1} failed: {e}", file=sys.stderr)
            continue
        if d is None or d.empty:
            continue
        if not isinstance(d.columns, pd.MultiIndex):
            d = pd.concat({chunk[0]: d}, axis=1)
        for tk in chunk:
            try:
                df = d[tk][["Close", "Volume"]].dropna(subset=["Close"])
            except KeyError:
                continue
            if len(df) >= EMA_LEN + 2:
                out[tk] = df
        time.sleep(1)
    return out


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def send(msg):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": msg, "disable_web_page_preview": True},
        timeout=20,
    )
    r.raise_for_status()


def send_in_chunks(lines):
    head = "📊 50 EMA CROSS (Daily)\n\n"
    msg = head
    for ln in lines:
        if len(msg) + len(ln) + 1 > 3800:
            send(msg)
            msg = head
            time.sleep(1)
        msg += ln + "\n"
    if msg != head:
        send(msg)


def main():
    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    print(f"IST time now: {now:%Y-%m-%d %H:%M}")

    if not FORCE and not market_is_open(now):
        print("Market closed (weekday 09:15-15:45 IST only). Nothing to do.")
        save_state(load_state())
        return

    symbols = load_symbols()
    tickers = [s + ".NS" for s in symbols]
    prices = fetch_prices(tickers)
    print(f"Got usable data for {len(prices)} of {len(tickers)} stocks")
    if not prices:
        print("No price data received.", file=sys.stderr)
        save_state(load_state())
        return

    latest_date = max(df.index[-1].date() for df in prices.values())
    if not FORCE and latest_date.strftime("%Y-%m-%d") != today:
        print(f"Latest candle is {latest_date}, not today. Market holiday? Skipping.")
        save_state(load_state())
        return

    # keep only today's alert keys so the file stays small
    state = {k: v for k, v in load_state().items() if v == today}
    ups, downs, new_keys = [], [], []

    for tk, df in prices.items():
        sym = tk[:-3]
        try:
            if df.index[-1].date() != latest_date:
                continue  # stale / halted stock
            close = df["Close"].astype(float)
            ema = close.ewm(span=EMA_LEN, adjust=False).mean()
            prev_c, last_c = float(close.iloc[-2]), float(close.iloc[-1])
            prev_e, last_e = float(ema.iloc[-2]), float(ema.iloc[-1])

            if prev_c <= prev_e and last_c > last_e:
                direction = "UP"
            elif prev_c >= prev_e and last_c < last_e:
                direction = "DOWN"
            else:
                continue

            key = f"{sym}_{direction}"
            if state.get(key) == today:
                continue

            if MIN_AVG_VOL and float(df["Volume"].tail(20).mean()) < MIN_AVG_VOL:
                continue  # skip illiquid stocks

            link = f"https://in.tradingview.com/chart/?symbol=NSE%3A{quote(sym, safe='')}"
            if direction == "UP":
                ups.append(f"🟢 {sym}  ₹{last_c:.2f} crossed ABOVE 50 EMA (₹{last_e:.2f})\n{link}\n")
            else:
                downs.append(f"🔴 {sym}  ₹{last_c:.2f} crossed BELOW 50 EMA (₹{last_e:.2f})\n{link}\n")
            new_keys.append(key)
        except Exception as e:
            print(f"{sym}: {e}", file=sys.stderr)

    lines = sorted(ups) + sorted(downs)
    if lines:
        send_in_chunks(lines)  # raises on failure, so state is not saved and next run retries
        for k in new_keys:
            state[k] = today
    save_state(state)
    print(f"Done. {len(lines)} alert(s): {len(ups)} up, {len(downs)} down.")


if __name__ == "__main__":
    main()
