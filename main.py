import os
import time
import requests
from datetime import datetime, timezone

TWELVE_API_KEY = os.environ.get("TWELVE_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

PAIRS = ["EUR/USD", "GBP/USD", "XAU/USD", "XAG/USD"]

SPIKE_MULTIPLIER = 3.0

BLACKOUT_WINDOWS = [
    (8, 25, 8, 35),
    (12, 25, 12, 35),
    (13, 55, 14, 5),
]

volume_history = {pair: [] for pair in PAIRS}

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"})

def is_blackout():
    now = datetime.now(timezone.utc)
    for start_h, start_m, end_h, end_m in BLACKOUT_WINDOWS:
        start = now.replace(hour=start_h, minute=start_m, second=0)
        end = now.replace(hour=end_h, minute=end_m, second=0)
        if start <= now <= end:
            return True
    return False

def is_trading_session():
    now = datetime.now(timezone.utc)
    hour = now.hour
    return 7 <= hour <= 21

def get_volume(pair):
    symbol = pair.replace("/", "")
    url = f"https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": "1min",
        "outputsize": 30,
        "apikey": TWELVE_API_KEY
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if "values" not in data:
            return None, None, None
        candles = data["values"]
        latest = candles[0]
        latest_vol = float(latest.get("volume", 0))
        avg_vol = sum(float(c.get("volume", 0)) for c in candles[1:21]) / 20
        price = float(latest["close"])
        return latest_vol, avg_vol, price
    except:
        return None, None, None

def check_spikes():
    if not is_trading_session():
        return
    if is_blackout():
        print("Blackout period — skipping")
        return

    for pair in PAIRS:
        latest_vol, avg_vol, price = get_volume(pair)
        if latest_vol is None:
            continue
        if avg_vol == 0:
            continue

        ratio = latest_vol / avg_vol
        print(f"{pair} | Vol: {latest_vol:.0f} | Avg: {avg_vol:.0f} | Ratio: {ratio:.2f}x | Price: {price}")

        if ratio >= SPIKE_MULTIPLIER:
            msg = (
                f"🚨 <b>VOLUME SPIKE — {pair}</b>\n\n"
                f"📊 Current Vol: {latest_vol:.0f}\n"
                f"📉 20-bar Avg: {avg_vol:.0f}\n"
                f"⚡ Spike: <b>{ratio:.1f}x normal</b>\n"
                f"💰 Price: {price}\n"
                f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n\n"
                f"⚠️ Possible smart money positioning"
            )
            send_telegram(msg)
            print(f"ALERT SENT: {pair} {ratio:.1f}x spike")

        time.sleep(1)

if __name__ == "__main__":
    send_telegram("✅ Volume Alert Bot is live — watching EUR/USD, GBP/USD, XAU/USD, XAG/USD")
    while True:
        check_spikes()
        time.sleep(60)
