import os
import time
import threading
import requests
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

TWELVE_API_KEY = os.environ.get("TWELVE_API_KEY", "").strip()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip().replace("\n", "").replace("\r", "").replace(" ", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

PAIRS = ["EUR/USD", "GBP/USD", "XAU/USD", "XAG/USD"]
SPIKE_MULTIPLIER = 3.0
BLACKOUT_WINDOWS = [
    (8, 25, 8, 35),
    (12, 25, 12, 35),
    (13, 55, 14, 5),
]

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")
    def log_message(self, format, *args):
        pass

def run_server():
    server = HTTPServer(("0.0.0.0", 10000), Handler)
    print("HTTP server started on port 10000", flush=True)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()
time.sleep(5)

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    print(f"Sending to Telegram... token length: {len(TELEGRAM_TOKEN)}", flush=True)
    try:
        r = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        print(f"Telegram response: {r.status_code} — {r.text}", flush=True)
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)

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
    return 7 <= now.hour <= 21

def get_volume(pair):
    symbol = pair.replace("/", "")
    params = {
        "symbol": symbol,
        "interval": "1min",
        "outputsize": 30,
        "apikey": TWELVE_API_KEY
    }
    try:
        r = requests.get("https://api.twelvedata.com/time_series", params=params, timeout=10)
        data = r.json()
        if "values" not in data:
            print(f"{pair} API error: {data}", flush=True)
            return None, None, None
        candles = data["values"]
        latest_vol = float(candles[0].get("volume", 0))
        avg_vol = sum(float(c.get("volume", 0)) for c in candles[1:21]) / 20
        price = float(candles[0]["close"])
        return latest_vol, avg_vol, price
    except Exception as e:
        print(f"API error {pair}: {e}", flush=True)
        return None, None, None

def check_spikes():
    if not is_trading_session():
        print("Outside trading hours", flush=True)
        return
    if is_blackout():
        print("Blackout period — skipping", flush=True)
        return
    for pair in PAIRS:
        latest_vol, avg_vol, price = get_volume(pair)
        if latest_vol is None or avg_vol == 0:
            continue
        ratio = latest_vol / avg_vol
        print(f"{pair} | Vol: {latest_vol:.0f} | Avg: {avg_vol:.0f} | Ratio: {ratio:.2f}x | Price: {price}", flush=True)
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
        time.sleep(1)

print("Bot starting...", flush=True)
send_telegram("✅ Volume Alert Bot is live — watching EUR/USD, GBP/USD, XAU/USD, XAG/USD")

while True:
    check_spikes()
    time.sleep(60)
