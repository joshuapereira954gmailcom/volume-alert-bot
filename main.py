import os
import time
import threading
import requests
import base64
import yfinance as yf
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from bs4 import BeautifulSoup

TWELVE_API_KEY = os.environ.get("TWELVE_API_KEY", "").strip()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip().replace("\n", "").replace("\r", "").replace(" ", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "").strip()

PAIRS = ["EUR/USD", "GBP/USD", "XAU/USD", "XAG/USD"]
YAHOO_SYMBOLS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "XAU/USD": "GC=F",
    "XAG/USD": "SI=F"
}

SPIKE_MULTIPLIER = 3.0
last_hourly = {}
last_news_ids = set()
last_ff_headlines = set()
last_update_id = 0
conversation_history = []

STRATEGY_CONTEXT = """You are the personal AI trading assistant for Joshua, an M1 forex trader based in London who uses Smart Money Concepts (SMC) and SMT divergence strategy.

Joshua's trading style:
- Trades EUR/USD, GBP/USD, XAU/USD, XAG/USD
- M1 chart execution with top-down analysis
- Core strategy: SMT divergence — when EUR/USD and GBP/USD diverge, or Gold and Silver diverge
- Looks for: liquidity sweeps (equal highs/lows), order blocks, FVGs, inducement, BOS/CHOCH
- Sessions: Asia 00:00-07:00 BST, Frankfurt 07:00-08:00 BST, London 08:00-13:00 BST, NY 14:30-21:00 BST
- Key setup: London open Judas swing 08:00-09:00 BST, NY open sweep 14:30-15:00 BST
- Uses Asia range high/low as liquidity targets
- Avoids trading during high impact news

When Joshua messages you:
- Respond like a sharp trading partner, not a chatbot
- Be direct and concise — max 5 lines unless he asks for more
- Always fetch live prices when he asks about specific pairs
- When he asks about bias, give a clear directional read with reasoning
- When he sends a chart, analyse it deeply using SMC/SMT methodology
- Never use markdown asterisks or hashes — plain text only
- Use emojis sparingly
- If you need price data to answer, say you're fetching it"""

BLACKOUT_WINDOWS = [
    (9, 25, 9, 35),
    (13, 25, 13, 35),
    (14, 55, 15, 5),
]

def london_time():
    now_utc = datetime.now(timezone.utc)
    month = now_utc.month
    offset = 1 if 3 < month < 11 else 0
    london = now_utc + timedelta(hours=offset)
    suffix = "BST" if offset == 1 else "GMT"
    return london, suffix

def london_time_str():
    lt, suffix = london_time()
    return f"{lt.strftime('%H:%M')} {suffix}"

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

def ask_claude(prompt, image_base64=None, media_type="image/jpeg", use_history=False):
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }

    if use_history:
        messages = conversation_history[-10:] + [{"role": "user", "content": prompt}]
    else:
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
        messages = [{"role": "user", "content": content}]

    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 600,
        "system": STRATEGY_CONTEXT,
        "messages": messages
    }

    try:
        r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=30)
        data = r.json()
        response = data["content"][0]["text"]
        response = response.replace("**", "").replace("##", "").replace("# ", "")

        if use_history:
            conversation_history.append({"role": "user", "content": prompt})
            conversation_history.append({"role": "assistant", "content": response})
            if len(conversation_history) > 20:
                conversation_history.pop(0)
                conversation_history.pop(0)

        return response
    except Exception as e:
        print(f"Claude error: {e}", flush=True)
        return "Having trouble connecting to AI right now."

def get_yahoo_price(pair):
    symbol = YAHOO_SYMBOLS.get(pair)
    if not symbol:
        return None
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="2d", interval="1m")
        if hist.empty or len(hist) < 2:
            return None
        price = round(float(hist["Close"].iloc[-1]), 5)
        prev_price = round(float(hist["Close"].iloc[-2]), 5)
        open_price = round(float(hist["Open"].iloc[0]), 5)
        high = round(float(hist["High"].max()), 5)
        low = round(float(hist["Low"].min()), 5)
        return {
            "price": price,
            "prev_price": prev_price,
            "change": price - prev_price,
            "open": open_price,
            "high": high,
            "low": low
        }
    except Exception as e:
        print(f"Yahoo error {pair}: {e}", flush=True)
        return None

def get_all_prices():
    prices = {}
    for pair in PAIRS:
        data = get_yahoo_price(pair)
        if data:
            prices[pair] = data
        time.sleep(1)
    return prices

def get_twelve_volume(pair):
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
        print(f"Twelve error {pair}: {e}", flush=True)
        return None

def get_session():
    lt, suffix = london_time()
    hour = lt.hour
    minute = lt.minute
    active = []
    if 0 <= hour < 7:
        active.append("Asia 🌏")
    if hour == 7:
        active.append("Frankfurt 🇩🇪")
    if 8 <= hour < 13:
        active.append("London 🇬🇧")
    if 13 <= hour < 14 or (hour == 14 and minute < 30):
        active.append("Lunch 😴")
    if hour > 14 or (hour == 14 and minute >= 30):
        if hour < 21:
            active.append("NY 🇺🇸")
    return " + ".join(active) if active else None

def is_blackout():
    lt, suffix = london_time()
    hour = lt.hour
    minute = lt.minute
    windows = [
        (9, 25, 9, 35),
        (13, 25, 13, 35),
        (14, 55, 15, 5),
    ]
    for start_h, start_m, end_h, end_m in windows:
        if (hour == start_h and minute >= start_m) or (hour == end_h and minute <= end_m):
            return True
    return False

def check_ff_breaking_news():
    global last_ff_headlines
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = requests.get("https://www.forexfactory.com/news", headers=headers, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        headlines = []
        for item in soup.find_all("div", class_="flexposts__story-title")[:10]:
            text = item.get_text(strip=True)
            if text:
                headlines.append(text)

        if not headlines:
            for item in soup.find_all("a", class_="title")[:10]:
                text = item.get_text(strip=True)
                if text:
                    headlines.append(text)

        keywords = ["fed", "ecb", "boe", "dollar", "euro", "pound", "gold", "silver",
                   "iran", "trump", "powell", "inflation", "rate", "tariff", "ceasefire",
                   "gdp", "cpi", "nfp", "fomc", "hawkish", "dovish", "war", "oil"]

        for headline in headlines:
            headline_lower = headline.lower()
            is_relevant = any(kw in headline_lower for kw in keywords)
            if is_relevant and headline not in last_ff_headlines:
                last_ff_headlines.add(headline)
                if len(last_ff_headlines) > 100:
                    last_ff_headlines = set(list(last_ff_headlines)[-50:])

                prompt = f"""Breaking forex headline just posted on ForexFactory: "{headline}"
In 3 lines plain text: which currencies are affected, expected direction impact on EUR/USD and GBP/USD, and whether to avoid trading or look for a setup."""
                ai = ask_claude(prompt)
                lt, suffix = london_time()
                msg = (
                    f"🔴 <b>FF BREAKING NEWS</b>\n\n"
                    f"📰 {headline}\n\n"
                    f"🕐 {lt.strftime('%H:%M')} {suffix}\n\n"
                    f"🤖 <b>AI READ:</b>\n{ai}"
                )
                send_telegram(msg)
                print(f"FF headline sent: {headline}", flush=True)
                time.sleep(2)

    except Exception as e:
        print(f"FF news error: {e}", flush=True)

def check_news():
    global last_news_ids
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        r = requests.get(url, timeout=10)
        events = r.json()
        now = datetime.now(timezone.utc)
        lt, suffix = london_time()
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
                offset = 1 if suffix == "BST" else 0
                event_london = event_time + timedelta(hours=offset)
                prompt = f"""Red folder news dropping in {int(mins_until)} mins: {event.get('title')} for {event.get('currency')}. Forecast: {event.get('forecast', 'N/A')}, Previous: {event.get('previous', 'N/A')}. 3 lines plain text: expected impact on EUR/USD and GBP/USD, avoid trading or not, move to expect if beats or misses."""
                ai = ask_claude(prompt)
                msg = (
                    f"📰 <b>RED FOLDER — {int(mins_until)} MINS</b>\n\n"
                    f"🏷 {event.get('title')}\n"
                    f"🌍 {event.get('currency')} — High Impact 🔴\n"
                    f"🕐 {event_london.strftime('%H:%M')} {suffix}\n"
                    f"📊 Forecast: {event.get('forecast', 'N/A')} | Prev: {event.get('previous', 'N/A')}\n\n"
                    f"🤖 <b>AI READ:</b>\n{ai}"
                )
                send_telegram(msg)
    except Exception as e:
        print(f"News error: {e}", flush=True)

def check_spikes():
    if is_blackout():
        return
    session = get_session()
    if not session:
        return

    all_data = {}
    for pair in PAIRS:
        data = get_twelve_volume(pair)
        if data:
            all_data[pair] = data
        time.sleep(2)

    for pair, data in all_data.items():
        if data["avg_volume"] == 0:
            continue
        ratio = data["volume"] / data["avg_volume"]
        pip_move = abs(data["price"] - data["prev_price"]) * (100 if "XAU" in pair or "XAG" in pair else 10000)
        direction = "📈 UP" if data["price"] > data["prev_price"] else "📉 DOWN"

        if ratio >= SPIKE_MULTIPLIER:
            correlated = "GBP/USD" if pair == "EUR/USD" else "EUR/USD" if pair == "GBP/USD" else "XAG/USD" if pair == "XAU/USD" else "XAU/USD"
            corr_data = all_data.get(correlated)
            corr_text = ""
            if corr_data:
                corr_dir = "📈" if corr_data["price"] > corr_data["prev_price"] else "📉"
                corr_text = f"\n🔗 {correlated}: {corr_data['price']} {corr_dir}"

            prompt = f"""Volume spike alert: {pair} just spiked {ratio:.1f}x normal volume. Price: {data['price']}, moving {direction}, {pip_move:.1f} pips. Session: {session}. Correlated pair {correlated}: {corr_data['price'] if corr_data else 'unavailable'}. 4 lines plain text: what this means for Joshua's SMT strategy, is there divergence context, what to look for on M1, confidence level."""
            ai = ask_claude(prompt)
            msg = (
                f"🚨 <b>VOLUME SPIKE — {pair}</b>\n\n"
                f"📊 {ratio:.1f}x normal volume\n"
                f"💰 {data['price']} {direction} | {pip_move:.1f} pips"
                f"{corr_text}\n"
                f"🕐 {london_time_str()} | {session}\n\n"
                f"🤖 {ai}"
            )
            send_telegram(msg)

        elif pip_move >= 10:
            prompt = f"""{pair} just moved {pip_move:.1f} pips {direction} at {london_time_str()} during {session}. Price: {data['price']}. 3 lines plain text for Joshua: Judas sweep or real momentum, SMT context with correlated pair, what to watch on M1."""
            ai = ask_claude(prompt)
            msg = (
                f"💥 <b>FAST MOVE — {pair}</b>\n\n"
                f"📏 {pip_move:.1f} pips {direction}\n"
                f"💰 {data['price']}\n"
                f"🕐 {london_time_str()} | {session}\n\n"
                f"🤖 {ai}"
            )
            send_telegram(msg)

def check_hourly_bias():
    global last_hourly
    session = get_session()
    if not session:
        return
    lt, suffix = london_time()
    hour_key = lt.strftime("%Y-%m-%d-%H")
    if hour_key in last_hourly:
        return
    last_hourly[hour_key] = True

    prices = get_all_prices()
    if not prices:
        return

    price_summary = "\n".join([
        f"{'📈' if prices[p]['change'] > 0 else '📉'} <b>{p}</b>: {prices[p]['price']}"
        for p in prices
    ])
    data_text = " | ".join([f"{p}: {prices[p]['price']} ({'up' if prices[p]['change'] > 0 else 'down'})" for p in prices])
    prompt = f"""Hourly bias update for Joshua. Time: {lt.strftime('%H:%M')} {suffix}. Session: {session}. Live prices: {data_text}. 4 lines plain text: overall directional bias, which pair is strongest/weakest, any SMT divergence context between EUR/GBP or Gold/Silver, one specific thing to watch this hour."""
    ai = ask_claude(prompt)

    msg = (
        f"🕐 <b>HOURLY BIAS — {lt.strftime('%H:00')} {suffix}</b>\n"
        f"📍 {session}\n\n"
        f"{price_summary}\n\n"
        f"🤖 {ai}\n\n"
        f"🔄 Next: {(lt.hour + 1) % 24:02d}:00 {suffix}"
    )
    send_telegram(msg)

def send_morning_brief():
    lt, suffix = london_time()
    now = datetime.now(timezone.utc)
    if lt.hour != 7 or lt.minute > 5:
        return
    brief_key = lt.strftime("%Y-%m-%d-brief")
    if brief_key in last_hourly:
        return
    last_hourly[brief_key] = True

    prices = get_all_prices()

    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=10)
        events = r.json()
        today_events = []
        offset = 1 if suffix == "BST" else 0
        for e in events:
            if e.get("impact") == "High" and e.get("currency") in ["USD", "EUR", "GBP"]:
                try:
                    et = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
                    et_london = et + timedelta(hours=offset)
                    if et.date() == now.date():
                        today_events.append(f"{et_london.strftime('%H:%M')} {suffix} — {e.get('currency')} {e.get('title')}")
                except:
                    pass
    except:
        today_events = []

    news_text = "\n".join(today_events) if today_events else "No high impact news today"
    price_text = " | ".join([f"{p}: {prices[p]['price']}" for p in prices]) if prices else "unavailable"

    prompt = f"""Write Joshua's morning trading brief for {lt.strftime('%A %d %b %Y')}. Time: 07:00 {suffix} — Frankfurt opens in 1 hour, London opens in 1 hour.

Live prices: {price_text}
High impact news today: {news_text}

Write in plain text, no markdown. Cover:
1. Overall bias for the London session
2. Key levels on EUR/USD and GBP/USD to watch
3. Any SMT divergence context between pairs
4. The main setup to watch for at London open 08:00 {suffix}
5. News risk warning if any

Keep it under 150 words. Write like a sharp trading desk analyst talking to Joshua directly."""
    ai = ask_claude(prompt)

    price_lines = "\n".join([f"{'📈' if prices[p]['change'] > 0 else '📉'} <b>{p}</b>: {prices[p]['price']}" for p in prices]) if prices else ""
    news_lines = "\n".join([f"🔴 {e}" for e in today_events]) if today_events else "✅ No high impact news"

    msg = (
        f"🌅 <b>MORNING BRIEF — {lt.strftime('%a %d %b')} — 07:00 {suffix}</b>\n\n"
        f"💰 <b>PRICES</b>\n{price_lines}\n\n"
        f"📅 <b>TODAY'S NEWS</b>\n{news_lines}\n\n"
        f"🤖 <b>AI READ:</b>\n{ai}"
    )
    send_telegram(msg)
    print("Morning brief sent", flush=True)

def check_session_countdown():
    now = datetime.now(timezone.utc)
    lt, suffix = london_time()

    countdowns = [
        (23, 45, "🌏 ASIA OPEN", f"00:00 {suffix}"),
        (6, 45, "🇩🇪 FRANKFURT OPEN", f"07:00 {suffix}"),
        (7, 45, "🇬🇧 LONDON OPEN", f"08:00 {suffix}"),
        (14, 15, "🇺🇸 NY OPEN", f"14:30 {suffix}"),
    ]

    for h, m, label, open_time in countdowns:
        key = f"countdown-{lt.strftime('%Y-%m-%d')}-{h}-{m}"
        if lt.hour == h and lt.minute >= m and lt.minute < m + 5:
            if key not in last_hourly:
                last_hourly[key] = True
                prices = get_all_prices()
                price_lines = "\n".join([f"💰 <b>{p}</b>: {prices[p]['price']}" for p in prices]) if prices else ""
                price_text = " | ".join([f"{p}: {prices[p]['price']}" for p in prices]) if prices else "unavailable"
                prompt = f"""15 minutes to {label} at {open_time} for Joshua. Live prices: {price_text}. 3 lines plain text: expected bias and likely direction at open, where liquidity is sitting based on price levels, one specific level to watch for a sweep or setup."""
                ai = ask_claude(prompt)
                msg = (
                    f"⚡ <b>15 MINS TO {label}</b>\n"
                    f"🕐 Opens: {open_time}\n\n"
                    f"{price_lines}\n\n"
                    f"🤖 {ai}"
                )
                send_telegram(msg)

def check_correlation_breakdown():
    session = get_session()
    if not session:
        return
    eur_data = get_yahoo_price("EUR/USD")
    time.sleep(1)
    gbp_data = get_yahoo_price("GBP/USD")
    if not eur_data or not gbp_data:
        return

    eur_move = eur_data["change"] * 10000
    gbp_move = gbp_data["change"] * 10000
    divergence = abs(eur_move - gbp_move)

    if divergence >= 8:
        key = f"corr-{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H-%M')}"
        if key not in last_hourly:
            last_hourly[key] = True
            weaker = "EUR/USD" if eur_move < gbp_move else "GBP/USD"
            stronger = "GBP/USD" if weaker == "EUR/USD" else "EUR/USD"
            prompt = f"""SMT divergence context for Joshua: EUR/USD moved {eur_move:.1f} pips, GBP/USD moved {gbp_move:.1f} pips. Divergence: {divergence:.1f} pips. {weaker} is the weaker pair. Session: {session}. 3 lines plain text: is this meaningful SMT context, what M1 setup to look for, which pair to trade and in which direction."""
            ai = ask_claude(prompt)
            msg = (
                f"⚠️ <b>SMT DIVERGENCE CONTEXT</b>\n\n"
                f"EUR/USD: {'+' if eur_move > 0 else ''}{eur_move:.1f} pips\n"
                f"GBP/USD: {'+' if gbp_move > 0 else ''}{gbp_move:.1f} pips\n"
                f"📏 Divergence: {divergence:.1f} pips\n"
                f"💡 <b>{weaker} weaker | {stronger} stronger</b>\n"
                f"🕐 {london_time_str()} | {session}\n\n"
                f"🤖 {ai}"
            )
            send_telegram(msg)

def handle_natural_message(text, chat_id):
    text_lower = text.lower().strip()
    prices = None

    needs_prices = any(kw in text_lower for kw in [
        "gold", "silver", "eur", "gbp", "xau", "xag", "price", "doing",
        "level", "where", "bias", "long", "short", "buy", "sell",
        "market", "session", "setup", "trade"
    ])

    if needs_prices:
        send_telegram("⏳ Fetching live data...", chat_id)
        prices = get_all_prices()

    lt, suffix = london_time()
    session = get_session() or "Off hours"

    price_context = ""
    if prices:
        price_context = "Live prices: " + " | ".join([
            f"{p}: {prices[p]['price']} ({'up' if prices[p]['change'] > 0 else 'down'} {abs(prices[p]['change'] * (100 if 'XAU' in p or 'XAG' in p else 10000)):.1f} pips)"
            for p in prices
        ])

    prompt = f"""Joshua says: "{text}"

Current time: {lt.strftime('%H:%M')} {suffix}
Session: {session}
{price_context}

Respond directly to what he asked. Be sharp and concise. Max 5 lines. Plain text no markdown."""

    ai = ask_claude(prompt, use_history=True)
    send_telegram(f"🤖 {ai}", chat_id)

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

                caption = message.get("caption", "")
                prompt = f"""Joshua sent you this chart{' with note: ' + caption if caption else ''}.

Analyse using SMC/SMT methodology:
1. Pair and timeframe
2. Market structure — bullish or bearish, any BOS/CHOCH
3. Liquidity sweeps — equal highs/lows swept
4. FVGs and order blocks visible
5. Inducement levels marked
6. SMT divergence context if two charts shown
7. Entry setup — entry zone, stop loss, target
8. Confidence out of 10 and why

Plain text no markdown. Be specific with price levels you can see."""

                send_telegram("🤖 Analysing your chart...", chat_id)
                ai = ask_claude(prompt, img_b64)
                send_telegram(f"🤖 <b>CHART ANALYSIS</b>\n\n{ai}", chat_id)

            elif text:
                cmd = text.lower().strip()

                if cmd == "/prices":
                    send_telegram("⏳ Fetching live prices...", chat_id)
                    lt, suffix = london_time()
                    prices = get_all_prices()
                    prices_msg = f"💰 <b>LIVE PRICES</b>\n🕐 {lt.strftime('%H:%M')} {suffix}\n\n"
                    for pair in PAIRS:
                        if pair in prices:
                            d = "📈" if prices[pair]["change"] > 0 else "📉"
                            change_pips = prices[pair]["change"] * (100 if "XAU" in pair or "XAG" in pair else 10000)
                            prices_msg += f"{d} <b>{pair}</b>: {prices[pair]['price']} ({'+' if change_pips > 0 else ''}{change_pips:.1f} pips)\n"
                        else:
                            prices_msg += f"⚠️ <b>{pair}</b>: unavailable\n"
                    send_telegram(prices_msg, chat_id)

                elif cmd == "/bias":
                    send_telegram("⏳ Getting AI bias...", chat_id)
                    handle_natural_message("what's the current bias across all pairs and what should I be watching", chat_id)

                elif cmd == "/brief":
                    send_telegram("⏳ Generating brief...", chat_id)
                    lt, suffix = london_time()
                    last_hourly.pop(lt.strftime("%Y-%m-%d-brief"), None)
                    send_morning_brief()

                elif cmd == "/help":
                    lt, suffix = london_time()
                    send_telegram(
                        f"📊 <b>FUNDAMENTALS BOT</b>\n\n"
                        f"Just talk to me naturally:\n"
                        f"'what's gold doing'\n"
                        f"'am I long or short bias on GBP'\n"
                        f"'is there SMT on XAU/XAG'\n"
                        f"'what should I watch at London open'\n\n"
                        f"Commands:\n"
                        f"/prices — Live prices\n"
                        f"/bias — AI bias update\n"
                        f"/brief — Morning brief\n"
                        f"/help — This menu\n\n"
                        f"📸 Send any chart for AI analysis\n\n"
                        f"⏰ Sessions ({suffix}):\n"
                        f"Asia 🌏 00:00-07:00\n"
                        f"Frankfurt 🇩🇪 07:00-08:00\n"
                        f"London 🇬🇧 08:00-13:00\n"
                        f"NY 🇺🇸 14:30-21:00",
                        chat_id
                    )

                elif not cmd.startswith("/"):
                    handle_natural_message(text, chat_id)

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

lt, suffix = london_time()
print("Fundamentals Bot starting...", flush=True)
send_telegram(
    f"📊 <b>FUNDAMENTALS BOT IS LIVE</b>\n\n"
    f"Just talk to me naturally — ask me anything about the market.\n\n"
    f"⏰ Sessions ({suffix}):\n"
    f"Asia 🌏 00:00-07:00\n"
    f"Frankfurt 🇩🇪 07:00-08:00\n"
    f"London 🇬🇧 08:00-13:00\n"
    f"NY 🇺🇸 14:30-21:00\n\n"
    f"🔴 FF Breaking news: ON\n"
    f"📰 Red folder alerts: ON\n"
    f"🚨 Volume spikes: ON\n"
    f"📊 Hourly bias: ON\n"
    f"🌅 Morning brief: 07:00 {suffix}\n"
    f"📸 Chart analysis: ON\n\n"
    f"🕐 {lt.strftime('%H:%M')} {suffix}\n"
    f"/help for commands"
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
    if cycle % 3 == 0:
        check_ff_breaking_news()
    if cycle % 6 == 0:
        check_hourly_bias()
    send_morning_brief()
    if cycle % 3 == 0:
        check_correlation_breakdown()
    cycle += 1
    time.sleep(60)
