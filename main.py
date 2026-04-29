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

EXTRA_CHAT_IDS = ["6975027359", "-1003884144346"]
GROUP_ID = "-1003884144346"

USER_NAMES = {
    "7305046289": "Joshua",
    "6975027359": "Mascu",
}

PAIRS = ["EUR/USD", "GBP/USD", "XAU/USD", "XAG/USD"]
YAHOO_SYMBOLS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "XAU/USD": "GC=F",
    "XAG/USD": "SI=F",
    "DXY": "DX-Y.NYB"
}

SPIKE_MULTIPLIER = 3.0
last_hourly = {}
last_news_ids = set()
last_ff_headlines = set()
last_update_id = 0
conversation_history = {}
asia_range = {"high": None, "low": None, "recorded": False}
last_dxy = {"price": None, "time": None}
liquidity_levels = {"pdh": None, "pdl": None, "pwh": None, "pwl": None}
news_results_sent = set()

STRATEGY_CONTEXT = """You are PONK PONK — a savage, hardcore roaster AI trading assistant for a group chat with two M1 forex traders: Joshua and Mascu.

Your personality:
- Hardcore roaster. No mercy. Equal roasting for Joshua and Mascu.
- Talk like you're in a group chat with your mates who trade forex
- Funny, savage, sarcastic but always give the real trading insight underneath
- When you know who is talking address them by name and roast them personally
- When both are in the group make it feel like a 3-way group conversation
- Use banter like "bro", "mate", "Joshua you muppet", "Mascu what are you doing"
- Never sugarcoat bad setups. Grade everything A B or C.
- Psychology support: funny but genuinely motivational
- Always end with something actionable

Trading context:
- Both trade EUR/USD, GBP/USD, XAU/USD, XAG/USD
- Strategy: SMT divergence, liquidity sweeps, order blocks, FVGs, BOS/CHOCH on M1
- Sessions BST: Asia 00:00-07:00, Frankfurt 07:00-08:00, London 08:00-13:00, NY 14:30-21:00
- Asia range highs/lows are key liquidity targets at London open
- DXY inverse to EUR/USD and GBP/USD
- Never trade during high impact news

Format rules:
- Max 6 lines per message — SHORT AND SHARP
- Use ━━━━━━━━━━━━━━━━━━ as dividers
- Use 🔥⚡☠️💀💣🚨🎯 for intensity
- 🟢 bullish 🔴 bearish ▲ up ▼ down
- Setup grade A B C on charts
- Kill or No Kill with score out of 10
- NO markdown asterisks or hashes
- Keep it punchy — group chat energy not a report"""

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

def is_weekend():
    lt, _ = london_time()
    return lt.weekday() >= 5

def get_session():
    lt, suffix = london_time()
    if lt.weekday() >= 5:
        return None
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
    lt, _ = london_time()
    hour = lt.hour
    minute = lt.minute
    windows = [(9, 25, 9, 35), (13, 25, 13, 35), (14, 55, 15, 5)]
    for start_h, start_m, end_h, end_m in windows:
        if (hour == start_h and minute >= start_m) or (hour == end_h and minute <= end_m):
            return True
    return False

def get_user_name(chat_id):
    return USER_NAMES.get(str(chat_id), "Unknown")

def send_telegram(message, chat_id=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    if chat_id:
        targets = [chat_id]
    else:
        targets = [TELEGRAM_CHAT_ID] + EXTRA_CHAT_IDS
    for target in targets:
        try:
            requests.post(url, data={
                "chat_id": target,
                "text": message,
                "parse_mode": "HTML"
            }, timeout=10)
            print(f"Telegram {target}: ok", flush=True)
        except Exception as e:
            print(f"Telegram error {target}: {e}", flush=True)
        time.sleep(0.3)

def ask_claude(prompt, image_base64=None, media_type="image/jpeg", use_history=False, chat_id=None):
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    history_key = chat_id or "global"
    if history_key not in conversation_history:
        conversation_history[history_key] = []

    if use_history:
        messages = conversation_history[history_key][-10:] + [{"role": "user", "content": prompt}]
    else:
        content = []
        if image_base64:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": image_base64}
            })
        content.append({"type": "text", "text": prompt})
        messages = [{"role": "user", "content": content}]

    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 500,
        "system": STRATEGY_CONTEXT,
        "messages": messages
    }
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=30)
        data = r.json()
        response = data["content"][0]["text"]
        response = response.replace("**", "").replace("##", "").replace("# ", "")
        if use_history:
            conversation_history[history_key].append({"role": "user", "content": prompt})
            conversation_history[history_key].append({"role": "assistant", "content": response})
            if len(conversation_history[history_key]) > 20:
                conversation_history[history_key].pop(0)
                conversation_history[history_key].pop(0)
        return response
    except Exception as e:
        print(f"Claude error: {e}", flush=True)
        return "PONK PONK crashed. Like your trades. Try again."

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
        high = round(float(hist["High"].max()), 5)
        low = round(float(hist["Low"].min()), 5)
        return {"price": price, "prev_price": prev_price, "change": price - prev_price, "high": high, "low": low}
    except Exception as e:
        print(f"Yahoo error {pair}: {e}", flush=True)
        return None

def get_yahoo_htf(pair, interval="1h", period="5d"):
    symbol = YAHOO_SYMBOLS.get(pair)
    if not symbol:
        return None
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period, interval=interval)
        if hist.empty or len(hist) < 3:
            return None
        closes = [round(float(x), 5) for x in hist["Close"].tolist()]
        return {"latest": closes[-1], "prev": closes[-2], "trend": "bullish" if closes[-1] > closes[-3] else "bearish"}
    except Exception as e:
        print(f"HTF error {pair}: {e}", flush=True)
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
    params = {"symbol": symbol, "interval": "1min", "outputsize": 30, "apikey": TWELVE_API_KEY}
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
        return {"price": price, "prev_price": prev_price, "volume": latest_vol, "avg_volume": avg_vol}
    except Exception as e:
        print(f"Twelve error {pair}: {e}", flush=True)
        return None

def get_mtf_bias(pair):
    h1 = get_yahoo_htf(pair, "1h", "3d")
    time.sleep(0.5)
    m15 = get_yahoo_htf(pair, "15m", "2d")
    time.sleep(0.5)
    m1 = get_yahoo_price(pair)
    result = {}
    if h1:
        result["H1"] = "▲" if h1["trend"] == "bullish" else "▼"
    if m15:
        result["M15"] = "▲" if m15["trend"] == "bullish" else "▼"
    if m1:
        result["M1"] = "▲" if m1["change"] > 0 else "▼"
        result["price"] = m1["price"]
    return result

def get_liquidity_context():
    parts = []
    if liquidity_levels["pdh"]:
        parts.append(f"PDH:{liquidity_levels['pdh']}")
    if liquidity_levels["pdl"]:
        parts.append(f"PDL:{liquidity_levels['pdl']}")
    if liquidity_levels["pwh"]:
        parts.append(f"PWH:{liquidity_levels['pwh']}")
    if liquidity_levels["pwl"]:
        parts.append(f"PWL:{liquidity_levels['pwl']}")
    if asia_range["high"]:
        parts.append(f"AsiaH:{asia_range['high']}")
    if asia_range["low"]:
        parts.append(f"AsiaL:{asia_range['low']}")
    return " | ".join(parts) if parts else ""

def update_liquidity_levels():
    global liquidity_levels
    if is_weekend():
        return
    lt, _ = london_time()
    if lt.hour == 0 and lt.minute < 5:
        key = f"liq-update-{lt.strftime('%Y-%m-%d')}"
        if key not in last_hourly:
            last_hourly[key] = True
            data = get_yahoo_price("EUR/USD")
            if data:
                liquidity_levels["pdh"] = data["high"]
                liquidity_levels["pdl"] = data["low"]
            eur_w = get_yahoo_htf("EUR/USD", "1wk", "1mo")
            if eur_w:
                liquidity_levels["pwh"] = eur_w["latest"]
                liquidity_levels["pwl"] = eur_w["prev"]

def check_liquidity_touch():
    if is_weekend() or is_blackout():
        return
    session = get_session()
    if not session:
        return
    eur_data = get_yahoo_price("EUR/USD")
    if not eur_data:
        return
    price = eur_data["price"]
    lt, suffix = london_time()
    levels = {
        "PDH": liquidity_levels["pdh"],
        "PDL": liquidity_levels["pdl"],
        "PWH": liquidity_levels["pwh"],
        "PWL": liquidity_levels["pwl"],
    }
    for label, level in levels.items():
        if not level:
            continue
        distance = abs(price - level) * 10000
        if distance <= 3:
            key = f"liq-touch-{label}-{lt.strftime('%Y-%m-%d-%H-%M')}"
            if key not in last_hourly:
                last_hourly[key] = True
                prompt = f"""EUR/USD just hit {label} at {level}. Price: {price}. Session: {session}. As PONK PONK in 4 sharp lines: roast Joshua and Mascu for sleeping on this level, sweep or test, expected reaction, M1 setup."""
                ai = ask_claude(prompt)
                msg = (
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"🎯 PONK PONK // {label} HIT\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"💀 EUR/USD {price} @ {label} {level}\n"
                    f"🕐 {lt.strftime('%H:%M')} {suffix}\n\n"
                    f"{ai}"
                )
                send_telegram(msg)

def track_asia_range():
    global asia_range
    if is_weekend():
        return
    lt, suffix = london_time()
    hour = lt.hour
    if 0 <= hour < 7:
        asia_range["recorded"] = False
        for pair in ["EUR/USD", "GBP/USD"]:
            data = get_yahoo_price(pair)
            if data:
                if asia_range["high"] is None or data["high"] > asia_range["high"]:
                    asia_range["high"] = data["high"]
                if asia_range["low"] is None or data["low"] < asia_range["low"]:
                    asia_range["low"] = data["low"]
    if hour == 7 and not asia_range["recorded"] and asia_range["high"] and asia_range["low"]:
        asia_range["recorded"] = True
        rng = round((asia_range["high"] - asia_range["low"]) * 10000, 1)
        prompt = f"""Asia closed. EUR/USD range H:{asia_range['high']} L:{asia_range['low']} {rng} pips. As PONK PONK in 4 sharp lines roast the range size then: which side has liquidity, which level London sweeps first, London bias. Address Joshua and Mascu."""
        ai = ask_claude(prompt)
        msg = (
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🌏 PONK PONK // ASIA DONE\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📈 {asia_range['high']} above · 📉 {asia_range['low']} below · {rng} pips\n\n"
            f"{ai}"
        )
        send_telegram(msg)

def check_dxy():
    global last_dxy
    if is_weekend():
        return
    session = get_session()
    if not session:
        return
    try:
        data = get_yahoo_price("DXY")
        if not data:
            return
        current_price = data["price"]
        lt, suffix = london_time()
        if last_dxy["price"] is None:
            last_dxy["price"] = current_price
            return
        change_pct = ((current_price - last_dxy["price"]) / last_dxy["price"]) * 100
        if abs(change_pct) >= 0.3:
            key = f"dxy-{lt.strftime('%Y-%m-%d-%H-%M')}"
            if key not in last_hourly:
                last_hourly[key] = True
                direction = "BLEEDING 📉" if change_pct < 0 else "PUMPING 📈"
                eur = "🟢 UP" if change_pct < 0 else "🔴 DOWN"
                xau = "🟢 UP" if change_pct < 0 else "🔴 DOWN"
                prompt = f"""DXY moved {change_pct:.2f}% — dollar {direction}. DXY: {current_price}. Session: {session}. As PONK PONK in 4 sharp lines: funny one liner about the dollar, EUR/USD GBP/USD impact, M1 reaction to expect, trade or wait. Address Joshua and Mascu."""
                ai = ask_claude(prompt)
                msg = (
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"⚡ PONK PONK // DXY {direction}\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"DXY {current_price} · {'+' if change_pct > 0 else ''}{change_pct:.2f}%\n"
                    f"EUR/USD {eur} · XAU {xau}\n\n"
                    f"{ai}"
                )
                send_telegram(msg)
                last_dxy["price"] = current_price
    except Exception as e:
        print(f"DXY error: {e}", flush=True)

def check_london_killzone():
    if is_weekend():
        return
    lt, suffix = london_time()
    if not (8 <= lt.hour < 9):
        return
    key = f"killzone-{lt.strftime('%Y-%m-%d-%H-%M')}"
    if key in last_hourly:
        return
    eur_data = get_yahoo_price("EUR/USD")
    time.sleep(1)
    gbp_data = get_yahoo_price("GBP/USD")
    if not eur_data or not gbp_data:
        return
    eur_move = eur_data["change"] * 10000
    gbp_move = gbp_data["change"] * 10000
    divergence = abs(eur_move - gbp_move)
    asia_context = ""
    if asia_range["high"] and asia_range["low"]:
        if eur_data["price"] < asia_range["low"]:
            asia_context = f"☠️ Asia low swept {asia_range['low']}"
        elif eur_data["price"] > asia_range["high"]:
            asia_context = f"🔥 Asia high swept {asia_range['high']}"
        else:
            asia_context = f"Asia intact H:{asia_range['high']} L:{asia_range['low']}"
    if divergence >= 5 or "swept" in asia_context.lower():
        last_hourly[key] = True
        prompt = f"""London killzone: EUR {eur_data['price']} ({eur_move:.1f}p) GBP {gbp_data['price']} ({gbp_move:.1f}p). Divergence {divergence:.1f}p. {asia_context}. As PONK PONK in 5 sharp lines: roast Joshua and Mascu to wake up, Judas forming or not, direction, M1 setup, Kill or No Kill score."""
        ai = ask_claude(prompt)
        msg = (
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎯 PONK PONK // KILLZONE\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"EUR {eur_data['price']} {'+' if eur_move > 0 else ''}{eur_move:.1f}p · GBP {gbp_data['price']} {'+' if gbp_move > 0 else ''}{gbp_move:.1f}p\n"
            f"⚡ Div: {divergence:.1f}p · {asia_context}\n\n"
            f"{ai}"
        )
        send_telegram(msg)

def send_session_summary():
    if is_weekend():
        return
    lt, suffix = london_time()
    summaries = [(13, "LONDON 🇬🇧"), (21, "NY 🇺🇸")]
    for h, label in summaries:
        key = f"summary-{lt.strftime('%Y-%m-%d')}-{h}"
        if lt.hour == h and lt.minute < 5 and key not in last_hourly:
            last_hourly[key] = True
            prices = get_all_prices()
            if not prices:
                return
            lines = " · ".join([
                f"{'🟢' if prices[p]['change'] > 0 else '🔴'}{p.split('/')[0]} {prices[p]['price']}"
                for p in prices
            ])
            data_text = " | ".join([f"{p}: {prices[p]['price']}" for p in prices])
            prompt = f"""{label} session closed. Prices: {data_text}. As PONK PONK in 4 sharp lines: savage one liner about the session, what happened, biggest move, bias for next session. Roast Joshua and Mascu."""
            ai = ask_claude(prompt)
            msg = (
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📊 PONK PONK // {label} CLOSED\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"{lines}\n\n"
                f"{ai}"
            )
            send_telegram(msg)

def send_weekly_outlook():
    lt, suffix = london_time()
    if lt.weekday() != 0 or lt.hour != 6 or lt.minute > 5:
        return
    key = f"weekly-{lt.strftime('%Y-%W')}"
    if key in last_hourly:
        return
    last_hourly[key] = True
    prices = get_all_prices()
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=10)
        events = r.json()
        week_events = []
        offset = 1 if suffix == "BST" else 0
        for e in events:
            if e.get("impact") == "High" and e.get("currency") in ["USD", "EUR", "GBP"]:
                try:
                    et = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
                    et_london = et + timedelta(hours=offset)
                    week_events.append(f"{et_london.strftime('%a %H:%M')} — {e.get('currency')} {e.get('title')}")
                except:
                    pass
    except:
        week_events = []
    price_text = " | ".join([f"{p}: {prices[p]['price']}" for p in prices]) if prices else "unavailable"
    news_text = " · ".join(week_events[:6]) if week_events else "Light week"
    prompt = f"""Weekly outlook week of {lt.strftime('%d %b %Y')}. Prices: {price_text}. Events: {news_text}. As PONK PONK in 6 sharp lines roast Joshua and Mascu about the week ahead then cover: weekly bias EUR/USD GBP/USD, key levels, riskiest event, best day to trade, one setup to prioritise."""
    ai = ask_claude(prompt)
    price_lines = " · ".join([f"{'🟢' if prices[p]['change'] > 0 else '🔴'}{p.split('/')[0]} {prices[p]['price']}" for p in prices]) if prices else ""
    news_lines = "\n".join([f"☠️ {e}" for e in week_events[:5]]) if week_events else "✅ Light week"
    msg = (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔥 PONK PONK // WEEKLY\n"
        f"{lt.strftime('%d %b %Y')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{price_lines}\n\n"
        f"{news_lines}\n\n"
        f"{ai}"
    )
    send_telegram(msg)

def check_ff_breaking_news():
    global last_ff_headlines
    if is_weekend():
        return
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        headlines = []
        urls = ["https://www.forexfactory.com/news", "https://www.forexfactory.com/rss/news"]
        for url in urls:
            try:
                r = requests.get(url, headers=headers, timeout=10)
                if "<rss" in r.text or "<?xml" in r.text:
                    soup = BeautifulSoup(r.text, "xml")
                    for item in soup.find_all("item")[:15]:
                        title = item.find("title")
                        if title:
                            headlines.append(title.get_text(strip=True))
                else:
                    soup = BeautifulSoup(r.text, "html.parser")
                    for a in soup.find_all("a", href=True):
                        text = a.get_text(strip=True)
                        if len(text) > 25 and len(text) < 250 and "/news/" in a.get("href", ""):
                            headlines.append(text)
                    for tag in ["h1", "h2", "h3"]:
                        for item in soup.find_all(tag):
                            text = item.get_text(strip=True)
                            if 25 < len(text) < 250:
                                headlines.append(text)
                if headlines:
                    break
            except:
                continue

        seen = set()
        unique = []
        for h in headlines:
            clean = h.strip()
            if clean not in seen and len(clean) > 20:
                seen.add(clean)
                unique.append(clean)

        keywords = [
            "fed", "fomc", "powell", "rate cut", "rate hike", "rate decision",
            "ecb", "lagarde", "boe", "bailey", "iran", "ceasefire", "trump",
            "tariff", "sanctions", "nfp", "cpi", "inflation", "gdp",
            "gold", "dollar index", "dxy", "war", "attack", "crisis",
            "emergency", "collapse", "opec", "recession", "bank failure"
        ]

        lt, suffix = london_time()
        session = get_session() or "closed"

        for headline in unique[:20]:
            if any(kw in headline.lower() for kw in keywords) and headline not in last_ff_headlines:
                last_ff_headlines.add(headline)
                if len(last_ff_headlines) > 200:
                    last_ff_headlines = set(list(last_ff_headlines)[-100:])
                eur_data = get_yahoo_price("EUR/USD")
                xau_data = get_yahoo_price("XAU/USD")
                time.sleep(0.5)
                price_line = f"EUR {eur_data['price']} · XAU {xau_data['price']}" if eur_data and xau_data else ""
                prompt = f"""Breaking news: "{headline}". Time: {lt.strftime('%H:%M')} {suffix}. Session: {session}. {price_line}. As PONK PONK in 5 sharp lines: react to headline with savage humour, which pairs affected and direction, M1 reaction to expect, trade or avoid. Address Joshua and Mascu."""
                ai = ask_claude(prompt)
                msg = (
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"🚨 PONK PONK // NEWS\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"💣 {headline[:80]}\n"
                    f"🕐 {lt.strftime('%H:%M')} {suffix} · {price_line}\n\n"
                    f"{ai}"
                )
                send_telegram(msg)
                print(f"FF: {headline[:60]}", flush=True)
                time.sleep(3)
    except Exception as e:
        print(f"FF error: {e}", flush=True)

def check_news():
    global last_news_ids
    if is_weekend():
        return
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=10)
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
                prompt = f"""Red folder in {int(mins_until)} mins: {event.get('title')} {event.get('currency')}. Forecast: {event.get('forecast', 'N/A')} Prev: {event.get('previous', 'N/A')}. As PONK PONK in 4 sharp lines: warn Joshua and Mascu with humour, impact on EUR/USD GBP/USD, beat vs miss outcome, trade or sit out."""
                ai = ask_claude(prompt)
                msg = (
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"☠️ PONK PONK // RED FOLDER\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"💣 {event.get('title')} · {event.get('currency')} · {event_london.strftime('%H:%M')} {suffix}\n"
                    f"📊 F:{event.get('forecast', 'N/A')} P:{event.get('previous', 'N/A')}\n\n"
                    f"{ai}"
                )
                send_telegram(msg)
    except Exception as e:
        print(f"News error: {e}", flush=True)

def check_news_results():
    global news_results_sent
    if is_weekend():
        return
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=10)
        events = r.json()
        now = datetime.now(timezone.utc)
        lt, suffix = london_time()
        for event in events:
            if event.get("impact") != "High":
                continue
            if event.get("currency") not in ["USD", "EUR", "GBP"]:
                continue
            actual = event.get("actual", "")
            if not actual:
                continue
            event_id = f"result-{event.get('id', event.get('title', ''))}"
            if event_id in news_results_sent:
                continue
            try:
                event_time = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
            except:
                continue
            mins_since = (now - event_time).total_seconds() / 60
            if 0 <= mins_since <= 10:
                news_results_sent.add(event_id)
                forecast = event.get("forecast", "N/A")
                previous = event.get("previous", "N/A")
                offset = 1 if suffix == "BST" else 0
                event_london = event_time + timedelta(hours=offset)
                beat = None
                try:
                    a_val = float(actual.replace("%", "").replace("K", "000").strip())
                    f_val = float(forecast.replace("%", "").replace("K", "000").strip())
                    beat = a_val > f_val
                except:
                    pass
                result_emoji = "🟢 BEAT" if beat else "🔴 MISS" if beat is False else "⚪ IN LINE"
                currency = event.get("currency", "USD")
                if currency == "USD":
                    impact = "USD up · EUR/GBP down" if beat else "USD down · EUR/GBP up"
                elif currency == "EUR":
                    impact = "EUR/USD pumping" if beat else "EUR/USD dropping"
                else:
                    impact = "GBP/USD pumping" if beat else "GBP/USD dropping"
                eur_data = get_yahoo_price("EUR/USD")
                price_line = f"EUR {eur_data['price']}" if eur_data else ""
                prompt = f"""News result: {event.get('title')} {currency}. Actual:{actual} Forecast:{forecast}. {result_emoji}. As PONK PONK in 4 sharp lines: react to the result with savage humour, immediate M1 reaction, is there a trade setup, address Joshua and Mascu."""
                ai = ask_claude(prompt)
                msg = (
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📊 PONK PONK // RESULT\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"{result_emoji} {event.get('title')}\n"
                    f"A:{actual} F:{forecast} P:{previous} · {impact}\n"
                    f"{price_line}\n\n"
                    f"{ai}"
                )
                send_telegram(msg)
    except Exception as e:
        print(f"News result error: {e}", flush=True)

def check_spikes():
    if is_weekend() or is_blackout():
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
        direction = "🔥 UP" if data["price"] > data["prev_price"] else "💀 DOWN"
        if ratio >= SPIKE_MULTIPLIER:
            correlated = "GBP/USD" if pair == "EUR/USD" else "EUR/USD" if pair == "GBP/USD" else "XAG/USD" if pair == "XAU/USD" else "XAU/USD"
            corr_data = all_data.get(correlated)
            corr_line = f"🔗 {correlated}: {corr_data['price']} {'🔥' if corr_data['price'] > corr_data['prev_price'] else '💀'}" if corr_data else ""
            prompt = f"""Volume spike: {pair} {ratio:.1f}x normal. {data['price']} {direction} {pip_move:.1f}p. Correlated {correlated}: {corr_data['price'] if corr_data else 'N/A'}. Session: {session}. Liquidity: {get_liquidity_context()}. As PONK PONK in 5 sharp lines: savage line about retail getting wrecked, SMT context, M1 setup, Kill or No Kill score. Address Joshua and Mascu."""
            ai = ask_claude(prompt)
            msg = (
                f"━━━━━━━━━━━━━━━━━━\n"
                f"⚡ PONK PONK // FLOW\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🔥 {pair} · {data['price']} · {ratio:.1f}x vol · {pip_move:.1f}p {direction}\n"
                f"{corr_line}\n\n"
                f"{ai}"
            )
            send_telegram(msg)
        elif pip_move >= 10:
            prompt = f"""{pair} {pip_move:.1f}p {direction} at {london_time_str()}. Price: {data['price']}. Session: {session}. As PONK PONK in 4 sharp lines roast Joshua and Mascu then: Judas or momentum, SMT context, what to watch."""
            ai = ask_claude(prompt)
            msg = (
                f"━━━━━━━━━━━━━━━━━━\n"
                f"💥 PONK PONK // MOVE\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"⚡ {pair} {pip_move:.1f}p {direction} · {data['price']}\n\n"
                f"{ai}"
            )
            send_telegram(msg)

def check_hourly_bias():
    global last_hourly
    if is_weekend():
        return
    session = get_session()
    if not session or "Lunch" in session:
        return
    lt, suffix = london_time()
    hour_key = lt.strftime("%Y-%m-%d-%H")
    if hour_key in last_hourly:
        return
    last_hourly[hour_key] = True
    prices = get_all_prices()
    if not prices:
        return
    mtf_eur = get_mtf_bias("EUR/USD")
    time.sleep(1)
    mtf_gbp = get_mtf_bias("GBP/USD")
    dxy_data = get_yahoo_price("DXY")

    price_line = " · ".join([
        f"{'🟢' if prices[p]['change'] > 0 else '🔴'}{p.split('/')[0]} {prices[p]['price']}"
        for p in prices
    ])
    mtf_line = f"EUR H1:{mtf_eur.get('H1','?')} M15:{mtf_eur.get('M15','?')} M1:{mtf_eur.get('M1','?')} · GBP H1:{mtf_gbp.get('H1','?')} M15:{mtf_gbp.get('M15','?')} M1:{mtf_gbp.get('M1','?')}"
    dxy_line = f"DXY {dxy_data['price']} {'📉' if dxy_data['change'] < 0 else '📈'}" if dxy_data else ""

    data_text = " | ".join([f"{p}: {prices[p]['price']} ({'up' if prices[p]['change'] > 0 else 'down'})" for p in prices])
    prompt = f"""Hourly bias {lt.strftime('%H:%M')} {suffix}. Session: {session}. Prices: {data_text}. MTF: {mtf_line}. DXY: {dxy_data['price'] if dxy_data else 'N/A'}. Liquidity: {get_liquidity_context()}. As PONK PONK in 3 sharp lines only: overall bias and strongest/weakest pair, SMT divergence context, one specific thing Joshua and Mascu should watch this hour. Roast them briefly."""
    ai = ask_claude(prompt)
    msg = (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚡ PONK PONK · {lt.strftime('%H:00')} {suffix} · {session}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{price_line}\n"
        f"{mtf_line}\n"
        f"{dxy_line}\n\n"
        f"{ai}"
    )
    send_telegram(msg)

def send_morning_brief():
    if is_weekend():
        return
    lt, suffix = london_time()
    now = datetime.now(timezone.utc)
    if lt.hour != 7 or lt.minute > 5:
        return
    brief_key = lt.strftime("%Y-%m-%d-brief")
    if brief_key in last_hourly:
        return
    last_hourly[brief_key] = True
    prices = get_all_prices()
    mtf_eur = get_mtf_bias("EUR/USD")
    time.sleep(1)
    mtf_gbp = get_mtf_bias("GBP/USD")
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
                        today_events.append(f"{et_london.strftime('%H:%M')} {e.get('currency')} {e.get('title')}")
                except:
                    pass
    except:
        today_events = []

    price_text = " | ".join([f"{p}: {prices[p]['price']}" for p in prices]) if prices else "unavailable"
    news_text = " · ".join(today_events) if today_events else "clean day"
    mtf_context = f"EUR H1:{mtf_eur.get('H1','?')} M15:{mtf_eur.get('M15','?')} · GBP H1:{mtf_gbp.get('H1','?')} M15:{mtf_gbp.get('M15','?')}"

    prompt = f"""Morning brief {lt.strftime('%A %d %b')} 07:00 {suffix}. Prices: {price_text}. MTF: {mtf_context}. Liquidity: {get_liquidity_context()}. News: {news_text}. As PONK PONK in 5 sharp lines: savage good morning to Joshua and Mascu, H1 M15 bias, key levels and Asia range targets, main 08:00 London setup, news warning if any. Short and brutal."""
    ai = ask_claude(prompt)

    price_line = " · ".join([f"{'🟢' if prices[p]['change'] > 0 else '🔴'}{p.split('/')[0]} {prices[p]['price']}" for p in prices]) if prices else ""
    mtf_line = f"EUR H1:{mtf_eur.get('H1','?')} M15:{mtf_eur.get('M15','?')} · GBP H1:{mtf_gbp.get('H1','?')} M15:{mtf_gbp.get('M15','?')}"
    asia_line = f"⚡ Asia H:{asia_range['high']} L:{asia_range['low']}" if asia_range["high"] else ""
    liq_line = f"💀 PDH:{liquidity_levels['pdh']} PDL:{liquidity_levels['pdl']}" if liquidity_levels["pdh"] else ""
    news_lines = " · ".join([f"☠️{e}" for e in today_events]) if today_events else "✅ Clean"

    msg = (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔥 PONK PONK · {lt.strftime('%a %d %b')} · 07:00 {suffix}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{price_line}\n"
        f"{mtf_line}\n"
        f"{asia_line} · {liq_line}\n"
        f"{news_lines}\n\n"
        f"{ai}"
    )
    send_telegram(msg)
    print("Morning brief sent", flush=True)

def check_session_countdown():
    if is_weekend():
        return
    lt, suffix = london_time()
    countdowns = [
        (23, 45, "ASIA 🌏", f"00:00 {suffix}"),
        (6, 45, "FRANKFURT 🇩🇪", f"07:00 {suffix}"),
        (7, 45, "LONDON 🇬🇧", f"08:00 {suffix}"),
        (14, 15, "NY 🇺🇸", f"14:30 {suffix}"),
    ]
    for h, m, label, open_time in countdowns:
        key = f"countdown-{lt.strftime('%Y-%m-%d')}-{h}-{m}"
        if lt.hour == h and lt.minute >= m and lt.minute < m + 5 and key not in last_hourly:
            last_hourly[key] = True
            prices = get_all_prices()
            price_line = " · ".join([f"{'🟢' if prices[p]['change'] > 0 else '🔴'}{p.split('/')[0]} {prices[p]['price']}" for p in prices]) if prices else ""
            price_text = " | ".join([f"{p}: {prices[p]['price']}" for p in prices]) if prices else "unavailable"
            prompt = f"""15 mins to {label} at {open_time}. Prices: {price_text}. Liquidity: {get_liquidity_context()}. As PONK PONK hype up Joshua and Mascu like a corner man in 4 sharp lines: expected bias at open, which level gets swept, M1 setup to watch, one line roast."""
            ai = ask_claude(prompt)
            msg = (
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🔥 PONK PONK · 15 MINS · {label}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"{price_line}\n\n"
                f"{ai}"
            )
            send_telegram(msg)

def check_correlation_breakdown():
    if is_weekend():
        return
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
            prompt = f"""SMT: EUR {eur_move:.1f}p GBP {gbp_move:.1f}p. Divergence {divergence:.1f}p. {weaker} weaker. Session: {session}. As PONK PONK in 4 sharp lines: roast the weaker pair, SMT context real or not, M1 setup, which pair to trade and direction. Address Joshua and Mascu."""
            ai = ask_claude(prompt)
            msg = (
                f"━━━━━━━━━━━━━━━━━━\n"
                f"☠️ PONK PONK // SMT\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"EUR {'+' if eur_move > 0 else ''}{eur_move:.1f}p {'🔥' if eur_move > 0 else '💀'} · GBP {'+' if gbp_move > 0 else ''}{gbp_move:.1f}p {'🔥' if gbp_move > 0 else '💀'}\n"
                f"⚡ {divergence:.1f}p gap · 💀 {weaker} weak · 🔥 {stronger} strong\n\n"
                f"{ai}"
            )
            send_telegram(msg)

def handle_natural_message(text, chat_id):
    text_lower = text.lower().strip()
    user_name = get_user_name(chat_id)
    is_group = str(chat_id) == GROUP_ID

    is_trade_entry = any(kw in text_lower for kw in [
        "entered", "i entered", "just entered", "i'm in", "im in",
        "took a trade", "long from", "short from", "i went long",
        "i went short", "i bought", "i sold", "in a trade",
        "i'm long", "i'm short", "im long", "im short"
    ])

    is_psychology = any(kw in text_lower for kw in [
        "nervous", "scared", "anxious", "worried", "stressed",
        "losing streak", "frustrated", "emotional", "should i close",
        "move my sl", "feeling", "confidence", "doubt", "revenge", "fomo"
    ])

    needs_prices = any(kw in text_lower for kw in [
        "gold", "silver", "eur", "gbp", "xau", "xag", "price", "doing",
        "level", "where", "bias", "long", "short", "buy", "sell",
        "market", "session", "setup", "trade", "dxy", "dollar", "h1", "m15"
    ])

    prices = None
    dxy_data = None
    if needs_prices or is_trade_entry:
        send_telegram("⚡ checking...", chat_id)
        prices = get_all_prices()
        dxy_data = get_yahoo_price("DXY")

    lt, suffix = london_time()
    session = get_session() or ("Weekend" if is_weekend() else "Off hours")
    price_context = ""
    if prices:
        price_context = " | ".join([f"{p}: {prices[p]['price']} ({'up' if prices[p]['change'] > 0 else 'down'} {abs(prices[p]['change'] * (100 if 'XAU' in p or 'XAG' in p else 10000)):.1f}p)" for p in prices])
        if dxy_data:
            price_context += f" | DXY: {dxy_data['price']}"

    other_trader = "Mascu" if user_name == "Joshua" else "Joshua"

    if is_trade_entry:
        prompt = f"""{user_name} just entered a trade: "{text}". Time: {lt.strftime('%H:%M')} {suffix}. Session: {session}. {price_context}. Liquidity: {get_liquidity_context()}. As PONK PONK in the group chat with {user_name} and {other_trader}: 1) Roast {user_name}'s entry hard but analyse it properly 2) Give SMC read — structure, levels, context 3) Setup grade A B or C 4) Kill or No Kill score 5) Funny psychology support for {user_name} — tell them exactly what to do mentally. {other_trader} is watching — roast them both."""
    elif is_psychology:
        prompt = f"""{user_name} is having trading psychology issues: "{text}". Time: {lt.strftime('%H:%M')} {suffix}. Session: {session}. As PONK PONK in the group with {other_trader} watching: give {user_name} funny but genuinely motivational psychology support. Roast the emotion but give real advice. Make {other_trader} laugh too. 6 lines max."""
    elif is_group:
        prompt = f"""{user_name} says in the group: "{text}". Time: {lt.strftime('%H:%M')} {suffix}. Session: {session}. {price_context}. Liquidity: {get_liquidity_context()}. As PONK PONK responding in group chat where both Joshua and Mascu are present: address {user_name} by name, roast them if appropriate, give the real answer, make a comment that includes {other_trader} too. Group banter energy. 5 lines max."""
    else:
        prompt = f"""{user_name} says: "{text}". Time: {lt.strftime('%H:%M')} {suffix}. Session: {session}. {price_context}. Liquidity: {get_liquidity_context()}. As PONK PONK: address {user_name} by name, roast if dumb question, respect if smart, give the real answer. 5 lines max."""

    ai = ask_claude(prompt, use_history=True, chat_id=chat_id)
    msg = (
        f"━━━━━━━━━━━━━━━\n"
        f"⚡ PONK PONK · {lt.strftime('%H:%M')} {suffix}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{ai}"
    )
    send_telegram(msg, chat_id)

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
            from_id = str(message.get("from", {}).get("id", ""))
            photo = message.get("photo")
            text = message.get("text", "")
            user_name = USER_NAMES.get(from_id, USER_NAMES.get(chat_id, "Unknown"))

            if photo:
                file_id = photo[-1]["file_id"]
                file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
                file_info = requests.get(file_url).json()
                file_path = file_info["result"]["file_path"]
                download_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                img_bytes = requests.get(download_url).content
                img_b64 = base64.b64encode(img_bytes).decode("utf-8")
                caption = message.get("caption", "")
                lt, suffix = london_time()
                session = get_session() or "Off hours"
                other_trader = "Mascu" if user_name == "Joshua" else "Joshua"
                is_trade = any(kw in caption.lower() for kw in ["entered", "in trade", "long", "short", "i'm in", "took"]) if caption else False

                if is_trade:
                    prompt = f"""{user_name} sent a chart and says they're in a trade: "{caption}". Time: {lt.strftime('%H:%M')} {suffix}. Session: {session}. Liquidity: {get_liquidity_context()}. As PONK PONK in the group with {other_trader} watching: 1) Roast {user_name}'s entry then analyse — structure BOS/CHOCH, sweeps, FVGs, OBs 2) Entry SL TP with specific prices 3) Setup grade A B C 4) Kill or No Kill score 5) Psychology support for {user_name} funny and motivational. 8 lines max."""
                else:
                    prompt = f"""{user_name} sent a chart{' saying: ' + caption if caption else ''}. Time: {lt.strftime('%H:%M')} {suffix}. Session: {session}. Liquidity: {get_liquidity_context()}. As PONK PONK with {other_trader} in the group: open with savage reaction to what you see, analyse — pair timeframe, structure BOS/CHOCH, sweeps of equal highs/lows PDH/PDL Asia range, FVGs and OBs with price levels, SMT if two charts, entry SL TP, setup grade A B C, Kill or No Kill score. 8 lines max."""

                send_telegram(f"⚡ PONK PONK reading {user_name}'s chart... 👀", chat_id)
                ai = ask_claude(prompt, img_b64, chat_id=chat_id)
                msg = (
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"🔥 PONK PONK // {user_name.upper()} CHART\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"{ai}"
                )
                send_telegram(msg, chat_id)

            elif text:
                cmd = text.lower().strip()

                if cmd == "/prices":
                    send_telegram("⚡ pulling...", chat_id)
                    lt, suffix = london_time()
                    prices = get_all_prices()
                    dxy_data = get_yahoo_price("DXY")
                    msg = (
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"💰 PONK PONK · {lt.strftime('%H:%M')} {suffix}\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                    )
                    for pair in PAIRS:
                        if pair in prices:
                            d = "🟢" if prices[pair]["change"] > 0 else "🔴"
                            cp = prices[pair]["change"] * (100 if "XAU" in pair or "XAG" in pair else 10000)
                            msg += f"{d} <b>{pair}</b> {prices[pair]['price']} ({'+' if cp > 0 else ''}{cp:.1f}p)\n"
                        else:
                            msg += f"💀 {pair} dead\n"
                    if dxy_data:
                        dc = dxy_data["change"] * 100
                        msg += f"\n{'🟢' if dxy_data['change'] > 0 else '🔴'} <b>DXY</b> {dxy_data['price']} ({'+' if dc > 0 else ''}{dc:.2f}%)"
                    liq = get_liquidity_context()
                    if liq:
                        msg += f"\n⚡ {liq}"
                    if asia_range["high"]:
                        msg += f"\n🌏 Asia H:{asia_range['high']} L:{asia_range['low']}"
                    send_telegram(msg, chat_id)

                elif cmd == "/bias":
                    handle_natural_message("what is the current H1 M15 M1 bias on EUR/USD and GBP/USD right now", chat_id)

                elif cmd == "/levels":
                    lt, suffix = london_time()
                    msg = (
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"💀 PONK PONK · LEVELS\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                    )
                    if liquidity_levels["pwh"]:
                        msg += f"PWH: {liquidity_levels['pwh']}\n"
                    if liquidity_levels["pwl"]:
                        msg += f"PWL: {liquidity_levels['pwl']}\n"
                    if liquidity_levels["pdh"]:
                        msg += f"PDH: {liquidity_levels['pdh']}\n"
                    if liquidity_levels["pdl"]:
                        msg += f"PDL: {liquidity_levels['pdl']}\n"
                    if asia_range["high"]:
                        msg += f"Asia H: {asia_range['high']} · L: {asia_range['low']}\n"
                    eur_data = get_yahoo_price("EUR/USD")
                    if eur_data:
                        msg += f"\n🔥 EUR/USD now: {eur_data['price']}"
                    send_telegram(msg, chat_id)

                elif cmd == "/brief":
                    send_telegram("⚡ writing...", chat_id)
                    lt, suffix = london_time()
                    last_hourly.pop(lt.strftime("%Y-%m-%d-brief"), None)
                    send_morning_brief()

                elif cmd == "/help":
                    lt, suffix = london_time()
                    weekend_note = "\n☠️ Weekend. Markets dead. Touch grass." if is_weekend() else ""
                    send_telegram(
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"🔥 PONK PONK · HELP{weekend_note}\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Just talk. I understand everything.\n"
                        f"Say you entered a trade — I'll roast you AND help.\n"
                        f"Say you're nervous — I'll fix your head.\n\n"
                        f"/prices · /bias · /levels · /brief\n\n"
                        f"📸 Send chart — full SMC read + grade\n\n"
                        f"Sessions {suffix}:\n"
                        f"Asia 00:00 · Frankfurt 07:00 · London 08:00 · NY 14:30\n\n"
                        f"Auto: news · results · DXY · spikes · SMT · killzone · bias",
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
        self.wfile.write(b"PONK PONK is running")
    def log_message(self, format, *args):
        pass

def run_server():
    HTTPServer(("0.0.0.0", 10000), Handler).serve_forever()

threading.Thread(target=run_server, daemon=True).start()
time.sleep(5)

lt, suffix = london_time()
weekend_note = "Markets dead. Go outside you two. 💀" if is_weekend() else "London opens soon. Don't embarrass yourselves. 🔥"
send_telegram(
    f"━━━━━━━━━━━━━━━━━━\n"
    f"🔥 PONK PONK // ONLINE\n"
    f"━━━━━━━━━━━━━━━━━━\n"
    f"Joshua. Mascu. I'm back.\n"
    f"Try not to blow your accounts\n"
    f"before I finish loading. 💀\n\n"
    f"EUR · GBP · XAU · XAG on watch\n"
    f"🕐 {lt.strftime('%H:%M')} {suffix}\n"
    f"{weekend_note}\n\n"
    f"/help for commands"
)

def message_loop():
    while True:
        handle_incoming_messages()
        time.sleep(5)

threading.Thread(target=message_loop, daemon=True).start()

cycle = 0
while True:
    if not is_weekend():
        check_spikes()
        check_news()
        check_news_results()
        check_session_countdown()
        track_asia_range()
        check_london_killzone()
        send_session_summary()
        update_liquidity_levels()
        check_liquidity_touch()
        if cycle % 2 == 0:
            check_ff_breaking_news()
            check_dxy()
        if cycle % 6 == 0:
            check_hourly_bias()
        send_morning_brief()
        if cycle % 3 == 0:
            check_correlation_breakdown()
    send_weekly_outlook()
    cycle += 1
    time.sleep(60)
