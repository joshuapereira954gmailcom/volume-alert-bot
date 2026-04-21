import os
import time
import threading
import requests
import base64
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

TWELVE_API_KEY = os.environ.get("TWELVE_API_KEY", "").strip()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip().replace("\n", "").replace("\r", "").replace(" ", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "").strip()

PAIRS = ["EUR/USD", "GBP/USD", "XAU/USD", "XAG/USD"]
SPIKE_MULTIPLIER = 3.0
last_hourly = {}
last_news_ids = set()
last_update_id = 0

BLACKOUT_WINDOWS = [
    (8, 25, 8, 35),
    (12, 25, 12, 35),
    (13, 55, 14, 5),
]

def send_telegram(message, chat_id=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": chat_id or TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        print(f"Telegram: {r.status_code}", flush=True)
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)

def ask_claude(prompt, image_base64=None, media_type="image/jpeg"):
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    content = []
    if image_base64:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": image_base64
            }
        })
    content.append({"type": "text", "text": prompt})
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 500,
        "messages": [{"role": "user", "content": content}]
    }
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=30)
        data = r.json()
        return data["content"][0]["text"]
    except Exception as e:
        print(f"Claude error: {e}", flush=True)
        return "AI analysis unavailable"

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

def get_price_data(pair):
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
            return None
        candles = data["values"]
        latest_vol = float(candles[0].get("volume", 0))
        avg_vol = sum(float(c.get("volume", 0)) for c in candles[1:21]) / 20
        price = float(candles[0]["close"])
        prev_price = float(candles[5]["close"])
        return {
            "price": price,
            "prev_price": prev_price,
            "volume": latest_vol,
            "avg_volume": avg_vol,
        }
    except Exception as e:
        print(f"API error {pair}: {e}", flush=True)
        return None

def check_spikes():
    if is_blackout():
        return
    session = get_session()
    if not session:
        return

    all_data = {}
    for pair in PAIRS:
        data = get_price_data(pair)
        if data:
            all_data[pair] = data
        time.sleep(1)

    for pair, data in all_data.items():
        if data["avg_volume"] == 0:
            continue
        ratio = data["volume"] / data["avg_volume"]
        pip_move = abs(data["price"] - data["prev_price"]) * (100 if "XAU" in pair or "XAG" in pair else 10000)
        direction = "📈 UP" if data["price"] > data["prev_price"] else "📉 DOWN"

        print(f"{pair} | {ratio:.2f}x | {data['price']} | {pip_move:.1f} pips", flush=True)

        if ratio >= SPIKE_MULTIPLIER:
            correlated = "GBP/USD" if pair == "EUR/USD" else "EUR/USD" if pair == "GBP/USD" else "XAG/USD" if pair == "XAU/USD" else "XAU/USD"
            corr_data = all_data.get(correlated)
            corr_text = ""
            if corr_data:
                corr_dir = "📈" if corr_data["price"] > corr_data["prev_price"] else "📉"
                corr_text = f"\n🔗 {correlated}: {corr_data['price']} {corr_dir}"

            prompt = f"""You are an institutional forex trader analysing M1 price action using SMT divergence strategy.
Data: Pair: {pair}, Price: {data['price']}, Volume spike: {ratio:.1f}x, Direction: {direction}, Pips: {pip_move:.1f}, Session: {session}, Correlated pair ({correlated}): {corr_data['price'] if corr_data else 'unavailable'}.
In 4 lines max: what this volume spike likely means, SMT divergence context, what to look for on M1, confidence level HIGH/MEDIUM/LOW."""

            ai = ask_claude(prompt)
            msg = (
                f"🚨 <b>VOLUME SPIKE — {pair}</b>\n\n"
                f"📊 Vol: {data['volume']:.0f} | Avg: {data['avg_volume']:.0f}\n"
                f"⚡ Spike: <b>{ratio:.1f}x normal</b>\n"
                f"💰 Price: {data['price']} {direction}\n"
                f"📏 Move: {pip_move:.1f} pips"
                f"{corr_text}\n"
                f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"
                f"📍 {session}\n\n"
                f"🤖 <b>AI READ:</b>\n{ai}"
            )
            send_telegram(msg)

        elif pip_move >= 10:
            prompt = f"""M1 trader alert. {pair} moved {pip_move:.1f} pips {direction} at {datetime.now(timezone.utc).strftime('%H:%M')} UTC during {session}. Price: {data['price']}. In 3 lines: Judas sweep or real momentum? What to watch for."""
            ai = ask_claude(prompt)
            msg = (
                f"💥 <b>FAST MOVE — {pair}</b>\n\n"
                f"📏 {pip_move:.1f} pips in 1 candle\n"
                f"💰 Price: {data['price']} {direction}\n"
                f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"
                f"📍 {session}\n\n"
                f"🤖 <b>AI READ:</b>\n{ai}"
            )
            send_telegram(msg)

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
            event_id = str(event.get("id", event.get("title", "") + event.get("date", "")))
            if event_id in last_news_ids:
                continue
            try:
                event_time = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
            except:
                continue
            mins_until = (event_time - now).total_seconds() / 60
            if 0 <= mins_until <= 15:
                last_news_ids.add(event_id)
                prompt = f"""News in {int(mins_until)} mins: {event.get('title')} for {event.get('currency')}. Forecast: {event.get('forecast', 'N/A')}, Previous: {event.get('previous', 'N/A')}. In 3 lines: market impact on EUR/USD and GBP/USD, avoid trading or not, expected move if beats/misses."""
                ai = ask_claude(prompt)
                msg = (
                    f"📰 <b>RED FOLDER — {int(mins_until)} MINS</b>\n\n"
                    f"🏷 {event.get('title')}\n"
                    f"🌍 {event.get('currency')} — High Impact 🔴\n"
                    f"🕐 {event_time.strftime('%H:%M UTC')}\n"
                    f"📊 Forecast: {event.get('forecast', 'N/A')} | Prev: {event.get('previous', 'N/A')}\n\n"
                    f"🤖 <b>AI READ:</b>\n{ai}"
                )
                send_telegram(msg)
    except Exception as e:
        print(f"News error: {e}", flush=True)

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
    for pair in PAIRS:
        data = get_price_data(pair)
        if data:
            prices[pair] = data
        time.sleep(1)

    if not prices:
        return

    price_summary = "\n".join([
        f"{'📈' if prices[p]['price'] > prices[p]['prev_price'] else '📉'} <b>{p}</b>: {prices[p]['price']}"
        for p in prices
    ])

    data_text = " | ".join([f"{p}: {prices[p]['price']} ({'up' if prices[p]['price'] > prices[p]['prev_price'] else 'down'})" for p in prices])
    prompt = f"""Hourly bias for M1 SMT trader. Time: {now.strftime('%H:%M UTC')}. Session: {session}. Prices: {data_text}. In 4 lines: overall bias, strongest/weakest pairs, SMT divergence context EUR/GBP or Gold/Silver, one key thing to watch this hour."""
    ai = ask_claude(prompt)

    msg = (
        f"🕐 <b>HOURLY BIAS — {now.strftime('%H:00 UTC')}</b>\n"
        f"📍 {session}\n\n"
        f"{price_summary}\n\n"
        f"🤖 <b>AI READ:</b>\n{ai}\n\n"
        f"🔄 Next: {(now.hour + 1) % 24:02d}:00 UTC"
    )
    send_telegram(msg)

def send_morning_brief():
    now = datetime.now(timezone.utc)
    if now.hour != 6 or now.minute > 5:
        return
    brief_key = now.strftime("%Y-%m-%d-brief")
    if brief_key in last_hourly:
        return
    last_hourly[brief_key] = True

    prices = {}
    for pair in PAIRS:
        data = get_price_data(pair)
        if data:
            prices[pair] = data
        time.sleep(1)

    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=10)
        events = r.json()
        today_events = []
        for e in events:
            if e.get("impact") == "High" and e.get("currency") in ["USD", "EUR", "GBP"]:
                try:
                    et = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
                    if et.date() == now.date():
                        today_events.append(f"{et.strftime('%H:%M')} UTC — {e.get('currency')} {e.get('title')}")
                except:
                    pass
    except:
        today_events = []

    news_text = "\n".join(today_events) if today_events else "No high impact news today"
    price_text = " | ".join([f"{p}: {prices[p]['price']}" for p in prices]) if prices else "unavailable"

    prompt = f"""You are a professional trading desk analyst writing a pre-session brief for an M1 SMT divergence forex trader in London.
Date: {now.strftime('%A %d %b %Y')}. Prices: {price_text}. News today: {news_text}.
Write a concise brief covering: London session bias, key EUR/USD and GBP/USD levels, main risks, one specific setup at London open, risk warning if any. Under 150 words. Direct like a trading desk analyst."""

    ai = ask_claude(prompt)
    price_lines = "\n".join([f"{'📈' if prices[p]['price'] > prices[p]['prev_price'] else '📉'} <b>{p}</b>: {prices[p]['price']}" for p in prices]) if prices else ""
    news_lines = "\n".join([f"🔴 {e}" for e in today_events]) if today_events else "✅ No high impact news"

    msg = (
        f"🌅 <b>MORNING BRIEF — {now.strftime('%a %d %b')}</b>\n"
        f"Powered by Fundamentals AI 📊\n\n"
        f"💰 <b>PRICES</b>\n{price_lines}\n\n"
        f"📅 <b>TODAY'S NEWS</b>\n{news_lines}\n\n"
        f"🤖 <b>AI PRE-SESSION READ:</b>\n{ai}"
    )
    send_telegram(msg)
    print("Morning brief sent", flush=True)

def check_session_countdown():
    now = datetime.now(timezone.utc)
    countdowns = [
        (6, 45, "🇬🇧 LONDON OPEN", "07:00 UTC"),
        (12, 45, "🇺🇸 NY OPEN", "13:00 UTC"),
    ]
    for h, m, label, open_time in countdowns:
        key = f"countdown-{now.strftime('%Y-%m-%d')}-{h}-{m}"
        if now.hour == h and now.minute >= m and now.minute < m + 5:
            if key not in last_hourly:
                last_hourly[key] = True
                prices = {}
                for pair in PAIRS:
                    data = get_price_data(pair)
                    if data:
                        prices[pair] = data
                    time.sleep(1)
                price_lines = "\n".join([f"💰 <b>{p}</b>: {prices[p]['price']}" for p in prices]) if prices else ""
                prompt = f"""15 minutes to {label} at {open_time}. Prices: {', '.join([f'{p}: {prices[p]["price"]}' for p in prices])}. In 3 lines: expected bias at open, where liquidity sits, one key level to watch for a sweep."""
                ai = ask_claude(prompt)
                msg = (
                    f"⚡ <b>15 MINS TO {label}</b>\n"
                    f"🕐 Opens: {open_time}\n\n"
                    f"{price_lines}\n\n"
                    f"🤖 <b>AI READ:</b>\n{ai}"
                )
                send_telegram(msg)

def check_correlation_breakdown():
    session = get_session()
    if not session:
        return
    eur_data = get_price_data("EUR/USD")
    time.sleep(1)
    gbp_data = get_price_data("GBP/USD")
    if not eur_data or not gbp_data:
        return

    eur_move = (eur_data["price"] - eur_data["prev_price"]) * 10000
    gbp_move = (gbp_data["price"] - gbp_data["prev_price"]) * 10000
    divergence = abs(eur_move - gbp_move)

    if divergence >= 8:
        key = f"corr-{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H-%M')}"
        if key not in last_hourly:
            last_hourly[key] = True
            weaker = "EUR/USD" if eur_move < gbp_move else "GBP/USD"
            prompt = f"""SMT context detected. EUR/USD moved {eur_move:.1f} pips, GBP/USD moved {gbp_move:.1f} pips. Divergence: {divergence:.1f} pips. Session: {session}. In 3 lines: is this meaningful SMT context, which pair is weaker, what M1 setup to look for."""
            ai = ask_claude(prompt)
            msg = (
                f"⚠️ <b>CORRELATION BREAKDOWN</b>\n\n"
                f"EUR/USD: {'+' if eur_move > 0 else ''}{eur_move:.1f} pips\n"
                f"GBP/USD: {'+' if gbp_move > 0 else ''}{gbp_move:.1f} pips\n"
                f"📏 Divergence: {divergence:.1f} pips\n\n"
                f"💡 <b>{weaker} is weaker</b>\n"
                f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"
                f"📍 {session}\n\n"
                f"🤖 <b>AI READ:</b>\n{ai}"
            )
            send_telegram(msg)

def handle_incoming_messages():
    global last_update_id
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        params = {"offset": last_update_id + 1, "timeout": 3}
        r = requests.get(url, params=params, timeout=8)
        updates = r.json().get("result", [])
        for update in updates:
            last_update_id = update["update_id"]
            message = update.get("message", {})
            chat_id = str(message.get("chat", {}).get("id", ""))
            photo = message.get("photo")
            text = message.get("text", "")

            if photo:
                file_id = photo[-1]["file_id"]
                file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
                file_info = requests.get(file_url).json()
                file_path = file_info["result"]["file_path"]
                download_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                img_bytes = requests.get(download_url).content
                img_b64 = base64.b64encode(img_bytes).decode("utf-8")

                prompt = """You are an expert SMT divergence forex trader analysing an M1 chart.
Analyse and provide:
1. Pair and timeframe
2. Market structure bullish/bearish
3. Liquidity sweeps visible
4. FVGs or order blocks
5. SMT divergence context if visible
6. Entry setup if present — entry, stop, target
7. Confidence level out of 10
Be direct. Max 150 words."""

                send_telegram("🤖 Analysing your chart...", chat_id)
                ai = ask_claude(prompt, img_b64)
                send_telegram(f"🤖 <b>CHART ANALYSIS</b>\n\n{ai}", chat_id)

            elif text:
                cmd = text.lower().strip()

                if cmd == "/prices":
                    send_telegram("⏳ Fetching live prices...", chat_id)
                    prices_msg = "💰 <b>LIVE PRICES</b>\n\n"
                    for pair in PAIRS:
                        data = get_price_data(pair)
                        if data:
                            d = "📈" if data["price"] > data["prev_price"] else "📉"
                            prices_msg += f"{d} <b>{pair}</b>: {data['price']}\n"
                        time.sleep(0.5)
                    send_telegram(prices_msg, chat_id)

                elif cmd == "/bias":
                    send_telegram("⏳ Generating AI bias...", chat_id)
                    now = datetime.now(timezone.utc)
                    hour_key = now.strftime("%Y-%m-%d-%H-manual")
                    last_hourly.pop(hour_key, None)
                    prices = {}
                    for pair in PAIRS:
                        data = get_price_data(pair)
                        if data:
                            prices[pair] = data
                        time.sleep(0.5)
                    session = get_session() or "Off hours"
                    price_summary = "\n".join([f"{'📈' if prices[p]['price'] > prices[p]['prev_price'] else '📉'} <b>{p}</b>: {prices[p]['price']}" for p in prices])
                    data_text = " | ".join([f"{p}: {prices[p]['price']} ({'up' if prices[p]['price'] > prices[p]['prev_price'] else 'down'})" for p in prices])
                    prompt = f"""Instant bias for M1 SMT trader. Time: {now.strftime('%H:%M UTC')}. Session: {session}. Prices: {data_text}. In 4 lines: overall bias, strongest/weakest pairs, SMT divergence context, one key thing to watch."""
                    ai = ask_claude(prompt)
                    msg = (
                        f"🕐 <b>INSTANT BIAS — {now.strftime('%H:%M UTC')}</b>\n"
                        f"📍 {session}\n\n"
                        f"{price_summary}\n\n"
                        f"🤖 <b>AI READ:</b>\n{ai}"
                    )
                    send_telegram(msg, chat_id)

                elif cmd == "/brief":
                    send_telegram("⏳ Generating morning brief...", chat_id)
                    last_hourly.pop(datetime.now(timezone.utc).strftime("%Y-%m-%d-brief"), None)
                    send_morning_brief()

                elif cmd == "/help":
                    send_telegram(
                        "📊 <b>FUNDAMENTALS BOT COMMANDS</b>\n\n"
                        "/prices — Live prices all pairs\n"
                        "/bias — Instant AI bias update\n"
                        "/brief — Morning brief on demand\n"
                        "/help — Show commands\n\n"
                        "📸 Send any chart image for instant AI analysis\n\n"
                        "🤖 Powered by Fundamentals AI",
                        chat_id
                    )

    except Exception as e:
        print(f"Message handler error: {e}", flush=True)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Fundamentals Bot is running")
    def log_message(self, format, *args):
        pass

def run_server():
    server = HTTPServer(("0.0.0.0", 10000), Handler)
    print("HTTP server started", flush=True)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()
time.sleep(5)

print("Fundamentals Bot starting...", flush=True)
send_telegram(
    "📊 <b>FUNDAMENTALS BOT IS LIVE</b>\n\n"
    "📍 Monitoring: EUR/USD, GBP/USD, XAU/USD, XAG/USD\n"
    "⏰ Sessions: Asia 🌏 London 🇬🇧 NY 🇺🇸\n"
    "🤖 AI Analysis: ON\n"
    "📰 News Alerts: ON\n"
    "📊 Hourly Bias: ON\n"
    "🌅 Morning Brief: 06:30 UTC daily\n"
    "📸 Chart Analysis: Send any image\n\n"
    "Commands: /prices /bias /brief /help"
)

def message_loop():
    while True:
        handle_incoming_messages()
        time.sleep(5)

threading.Thread(target=message_loop, daemon=True).start()

cycle = 0
while True:
    check_spikes()
    check_news()
    check_session_countdown()
    if cycle % 6 == 0:
        check_hourly_bias()
    send_morning_brief()
    if cycle % 3 == 0:
        check_correlation_breakdown()
    cycle += 1
    time.sleep(60)
