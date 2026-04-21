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

SESSIONS = {
    "Asia":   (22, 8),   # 10pm - 8am UTC
    "London": (7, 16),   # 7am - 4pm UTC
    "NY":     (13, 21),  # 1pm - 9pm UTC
}

BLACKOUT_WINDOWS = [
    (8, 25, 8, 35),
    (12, 25, 12, 35),
    (13, 55, 14, 5),
]

last_hourly = {}
last_news_ids = set()

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        print(f"Telegram: {r.status_code}", flush=True)
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)

def get_session():
    hour = datetime.now(timezone.utc).hour
    active = []
    if hour >= 22 or hour < 8:
        active.append("Asia 🌏")
    if 7 <= hour < 16:
        active.append("London 🇬🇧")
    if 13 <= hour < 21:
        active.append("NY 🇺🇸")
    return " + ".join(active) if active else None

def is_blackout():
    now = datetime.now(timezone.utc)
    for start_h, start_m, end_h, end_m in BLACKOUT_WINDOWS:
        start = now.replace(hour=start_h, minute=start_m, second=0)
        end = now.replace(hour=end_h, minute=end_m, second=0)
        if start <= now <= end:
            return True
    return False

def get_price(pair):
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
            return None, None, None
        candles = data["values"]
        latest_vol = float(candles[0].get("volume", 0))
        avg_vol = sum(float(c.get("volume", 0)) for c in candles[1:21]) / 20
        price = float(candles[0]["close"])
        prev_price = float(candles[5]["close"])
        return latest_vol, avg_vol, price, prev_price
    except Exception as e:
        print(f"API error {pair}: {e}", flush=True)
        return None, None, None, None

def check_spikes():
    if is_blackout():
        print("Blackout — skipping", flush=True)
        return
    session = get_session()
    if not session:
        print("Outside all sessions", flush=True)
        return
    for pair in PAIRS:
        result = get_price(pair)
        if result[0] is None:
            continue
        latest_vol, avg_vol, price, prev_price = result
        if avg_vol == 0:
            continue
        ratio = latest_vol / avg_vol
        print(f"{pair} | Vol: {latest_vol:.0f} | Avg: {avg_vol:.0f} | {ratio:.2f}x | {price}", flush=True)
        if ratio >= SPIKE_MULTIPLIER:
            direction = "📈 UP" if price > prev_price else "📉 DOWN"
            msg = (
                f"🚨 <b>VOLUME SPIKE — {pair}</b>\n\n"
                f"📊 Current Vol: {latest_vol:.0f}\n"
                f"📉 20-bar Avg: {avg_vol:.0f}\n"
                f"⚡ Spike: <b>{ratio:.1f}x normal</b>\n"
                f"💰 Price: {price} {direction}\n"
                f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"
                f"📍 Session: {session}\n\n"
                f"⚠️ Possible smart money positioning"
            )
            send_telegram(msg)
        time.sleep(1)

def check_news():
    global last_news_ids
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        r = requests.get(url, timeout=10)
        events = r.json()
        now = datetime.now(timezone.utc)
        for event in events:
            if event.get("impact") != "High":
                continue
            if event.get("currency") not in ["USD", "EUR", "GBP"]:
                continue
            event_id = event.get("id", event.get("title", "") + event.get("date", ""))
            if event_id in last_news_ids:
                continue
            try:
                event_time = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
            except:
                continue
            mins_until = (event_time - now).total_seconds() / 60
            if 0 <= mins_until <= 15:
                last_news_ids.add(event_id)
                msg = (
                    f"📰 <b>HIGH IMPACT NEWS IN {int(mins_until)} MINS</b>\n\n"
                    f"🏷 {event.get('title', 'Unknown')}\n"
                    f"🌍 Currency: {event.get('currency')}\n"
                    f"🕐 {event_time.strftime('%H:%M UTC')}\n"
                    f"📊 Forecast: {event.get('forecast', 'N/A')} | Previous: {event.get('previous', 'N/A')}\n\n"
                    f"⚠️ Consider avoiding new entries"
                )
                send_telegram(msg)
                print(f"News alert sent: {event.get('title')}", flush=True)
    except Exception as e:
        print(f"News check error: {e}", flush=True)

def check_hourly_bias():
    global last_hourly
    session = get_session()
    if not session:
        return
    now = datetime.now(timezone.utc)
    hour_key = now.strftime("%Y-%m-%d-%H")
    if hour_key in last_hourly:
        return
    last_hourly[hour_key] = True
    prices = {}
    directions = {}
    for pair in PAIRS:
        result = get_price(pair)
        if result[0] is None:
            continue
        _, _, price, prev_price = result
        prices[pair] = price
        directions[pair] = "📈" if price > prev_price else "📉"
        time.sleep(1)
    if not prices:
        return
    lines = "\n".join([f"{directions[p]} <b>{p}</b>: {prices[p]}" for p in prices])
    msg = (
        f"🕐 <b>HOURLY BIAS — {now.strftime('%H:00 UTC')}</b>\n"
        f"📍 Session: {session}\n\n"
        f"{lines}\n\n"
        f"🔄 Next update in 1 hour"
    )
    send_telegram(msg)
    print("Hourly bias sent", flush=True)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")
    def log_message(self, format, *args):
        pass

def run_server():
    server = HTTPServer(("0.0.0.0", 10000), Handler)
    print("HTTP server started", flush=True)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()
time.sleep(5)

print("Bot starting...", flush=True)
send_telegram("✅ <b>Volume Alert Bot v2 is live!</b>\n\n📍 Monitoring: EUR/USD, GBP/USD, XAU/USD, XAG/USD\n⏰ Sessions: Asia 🌏 London 🇬🇧 NY 🇺🇸\n📰 News alerts: ON\n📊 Hourly bias: ON")

cycle = 0
while True:
    check_spikes()
    check_news()
    if cycle % 6 == 0:
        check_hourly_bias()
    cycle += 1
    time.sleep(60)
