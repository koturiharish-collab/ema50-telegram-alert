"""
50 EMA cross alert (daily timeframe).
Alerts when price crosses the 50 EMA upside or downside.
Compares yesterday's close vs EMA50 with the latest (live) price vs EMA50.
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests
import yfinance as yf

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
EMA_LEN = 50
STATE_FILE = "ema50_state.json"
IST = timezone(timedelta(hours=5, minutes=30))


def load_symbols(path="watchlist.txt"):
    with open(path) as f:
        return [
            l.strip().upper()
            for l in f
            if l.strip() and not l.startswith("#")
        ]


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


def main():
    symbols = load_symbols()
    tickers = [s + ".NS" for s in symbols]
    data = yf.download(
        tickers, period="1y", interval="1d",
        group_by="ticker", auto_adjust=False, progress=False, threads=True,
    )

    state = load_state()
    today = datetime.now(IST).strftime("%Y-%m-%d")
    # keep only today's keys so the file stays small
    state = {k: v for k, v in state.items() if v == today}
    alerts = 0

    for sym, tk in zip(symbols, tickers):
        try:
            close = data[tk]["Close"].dropna()
            if len(close) < EMA_LEN + 2:
                continue
            ema = close.ewm(span=EMA_LEN, adjust=False).mean()
            prev_c, last_c = float(close.iloc[-2]), float(close.iloc[-1])
            prev_e, last_e = float(ema.iloc[-2]), float(ema.iloc[-1])

            direction = None
            if prev_c <= prev_e and last_c > last_e:
                direction = "UP"
            elif prev_c >= prev_e and last_c < last_e:
                direction = "DOWN"
            if not direction:
                continue

            key = f"{sym}_{direction}"
            if state.get(key) == today:
                continue  # already alerted today

            icon, word = ("🟢", "crossed ABOVE") if direction == "UP" else ("🔴", "crossed BELOW")
            send(
                f"{icon} 50 EMA CROSS (Daily)\n\n"
                f"Stock: {sym}\n"
                f"Price {word} 50 EMA\n"
                f"Price: ₹{last_c:.2f}\n"
                f"50 EMA: ₹{last_e:.2f}\n"
                f"Prev Close: ₹{prev_c:.2f}\n\n"
                f"https://in.tradingview.com/chart/?symbol=NSE%3A{sym}"
            )
            state[key] = today
            alerts += 1
        except Exception as e:
            print(f"{sym}: {e}", file=sys.stderr)

    save_state(state)
    print(f"Done. {alerts} alert(s) sent.")


if __name__ == "__main__":
    main()
