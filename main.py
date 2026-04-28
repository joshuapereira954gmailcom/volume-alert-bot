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

EXTRA_CHAT_IDS = ["6975027359"]

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
conversation_history = []
asia_range = {"high": None, "low": None, "recorded": False}
last_dxy = {"price": None, "time": None}
liquidity_levels = {"pdh": None, "pdl": None, "pwh": None, "pwl": None}
news_results_sent = set()

STRATEGY_CONTEXT = """You are the personal AI trading assistant for Joshua, an M1 forex trader in London using Smart Money Concepts and SMT divergence.

Joshua trades: EUR/USD, GBP/USD, XAU/USD, XAG/USD only.
Strategy: SMT divergence between EUR/USD and GBP/USD, and between Gold and Silver.
Looks for: liquidity sweeps, order blocks, FVGs, inducement, BOS/CHOCH on M1.
Top down analysis: H1 and M15 bias first, then M1 execution.
Sessions BST: Asia 00:00-07:00, Frankfurt 07:00-08:00, London 08:00-13:00, NY 14:30-21:00.
Key setups: London open Judas swing 08:00-09:00, NY sweep 14:30-15:00.
Asia range highs and lows are key liquidity targets at London open.
Previous day high/low and previous week high/low are major institutional liquidity levels.
DXY inverse relationship with EUR/USD and GBP/USD.
When retail sentiment is 65%+ one direction, smart money is likely opposite.
Never trades weekends or during high impact news.

Respond like a sharp trading partner. Direct, concise, max 5 lines unless asked for more.
Plain text only — no asterisks, no hashes, no markdown.
Always reference live prices when discussing pairs.
Reference liquidity levels and Asia range when relevant."""

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
                "source": {"type": "base64", "media_type": media_type, "data": image_base64}
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
        return "AI unavailable right now."

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
        result["H1"] = "Bullish" if h1["trend"] == "bullish" else "Bearish"
    if m15:
        result["M15"] = "Bullish" if m15["trend"] == "bullish" else "Bearish"
    if m1:
        result["M1"] = "Bullish" if m1["change"] > 0 else "Bearish"
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
            eur = get_yahoo_htf("EUR/USD", "1d", "5d")
            if eur:
                data = get_yahoo_price("EUR/USD")
                if data:
                    liquidity_levels["pdh"] = data["high"]
                    liquidity_levels["pdl"] = data["low"]
            eur_w = get_yahoo_htf("EUR/USD", "1wk", "1mo")
            if eur_w:
                liquidity_levels["pwh"] = eur_w["latest"]
                liquidity_levels["pwl"] = eur_w["prev"]
            print(f"Liquidity updated: {liquidity_levels}", flush=True)

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
        "Previous Day High": liquidity_levels["pdh"],
        "Previous Day Low": liquidity_levels["pdl"],
        "Previous Week High": liquidity_levels["pwh"],
        "Previous Week Low": liquidity_levels["pwl"],
    }
    for label, level in levels.items():
        if not level:
            continue
        distance = abs(price - level) * 10000
        if distance <= 3:
            key = f"liq-touch-{label}-{lt.strftime('%Y-%m-%d-%H-%M')}"
            if key not in last_hourly:
                last_hourly[key] = True
                direction = "above" if price >= level else "below"
                prompt = f"""EUR/USD just touched {label} at {level}. Price: {price}. Session: {session}. 3 lines plain text: sweep or test, expected reaction, M1 setup."""
                ai = ask_claude(prompt)
                msg = (
                    f"🎯 <b>LIQUIDITY LEVEL TOUCHED</b>\n\n"
                    f"<b>{label}: {level}</b>\n"
                    f"EUR/USD: {price} ({direction})\n"
                    f"🕐 {lt.strftime('%H:%M')} {suffix} | {session}\n\n"
                    f"🤖 <b>AI READ:</b>\n{ai}"
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
        prompt = f"""Asia closed. EUR/USD range: High {asia_range['high']} Low {asia_range['low']} Range {rng} pips. {get_liquidity_context()}. 3 lines plain text: which side has liquidity, which level London sweeps first at 08:00, London bias."""
        ai = ask_claude(prompt)
        msg = (
            f"🌏 <b>ASIA RANGE COMPLETE</b>\n\n"
            f"📈 High: {asia_range['high']}\n"
            f"📉 Low: {asia_range['low']}\n"
            f"📏 Range: {rng} pips\n\n"
            f"🤖 <b>LONDON BIAS:</b>\n{ai}"
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
                direction = "WEAKENING 📉" if change_pct < 0 else "STRENGTHENING 📈"
                eur_impact = "📈 Bullish" if change_pct < 0 else "📉 Bearish"
                gbp_impact = "📈 Bullish" if change_pct < 0 else "📉 Bearish"
                xau_impact = "📈 Bullish" if change_pct < 0 else "📉 Bearish"
                prompt = f"""DXY moved {change_pct:.2f}% — dollar {direction}. DXY: {current_price}. Session: {session}. 4 lines plain text: EUR/USD and GBP/USD impact, M1 reaction next 5-10 mins, long or short bias, specific M1 setup."""
                ai = ask_claude(prompt)
                msg = (
                    f"⚡ <b>DXY — DOLLAR {direction}</b>\n\n"
                    f"DXY: {current_price} | {'+' if change_pct > 0 else ''}{change_pct:.2f}%\n\n"
                    f"EUR/USD: {eur_impact}\n"
                    f"GBP/USD: {gbp_impact}\n"
                    f"XAU/USD: {xau_impact}\n\n"
                    f"🕐 {lt.strftime('%H:%M')} {suffix} | {session}\n\n"
                    f"🤖 <b>AI READ:</b>\n{ai}"
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
            asia_context = f"ASIA LOW SWEPT at {asia_range['low']}"
        elif eur_data["price"] > asia_range["high"]:
            asia_context = f"ASIA HIGH SWEPT at {asia_range['high']}"
        else:
            asia_context = f"Asia intact H:{asia_range['high']} L:{asia_range['low']}"
    if divergence >= 5 or "SWEPT" in asia_context:
        last_hourly[key] = True
        prompt = f"""London killzone: EUR/USD {eur_data['price']} ({'+' if eur_move > 0 else ''}{eur_move:.1f} pips), GBP/USD {gbp_data['price']} ({'+' if gbp_move > 0 else ''}{gbp_move:.1f} pips). Divergence: {divergence:.1f} pips. {asia_context}. 4 lines plain text: Judas forming, direction, M1 setup, confidence."""
        ai = ask_claude(prompt)
        msg = (
            f"🎯 <b>LONDON KILLZONE</b>\n"
            f"🕐 {lt.strftime('%H:%M')} {suffix}\n\n"
            f"EUR/USD: {eur_data['price']} ({'+' if eur_move > 0 else ''}{eur_move:.1f} pips)\n"
            f"GBP/USD: {gbp_data['price']} ({'+' if gbp_move > 0 else ''}{gbp_move:.1f} pips)\n"
            f"📏 Divergence: {divergence:.1f} pips\n"
            f"🌏 {asia_context}\n\n"
            f"🤖 <b>AI READ:</b>\n{ai}"
        )
        send_telegram(msg)

def send_session_summary():
    if is_weekend():
        return
    lt, suffix = london_time()
    summaries = [(13, "🇬🇧 LONDON SESSION"), (21, "🇺🇸 NY SESSION")]
    for h, label in summaries:
        key = f"summary-{lt.strftime('%Y-%m-%d')}-{h}"
        if lt.hour == h and lt.minute < 5 and key not in last_hourly:
            last_hourly[key] = True
            prices = get_all_prices()
            if not prices:
                return
            lines = "\n".join([
                f"{'📈' if prices[p]['change'] > 0 else '📉'} <b>{p}</b>: {prices[p]['price']} ({'+' if prices[p]['change'] * (100 if 'XAU' in p or 'XAG' in p else 10000) > 0 else ''}{prices[p]['change'] * (100 if 'XAU' in p or 'XAG' in p else 10000):.1f} pips)"
                for p in prices
            ])
            data_text = " | ".join([f"{p}: {prices[p]['price']}" for p in prices])
            prompt = f"""{label} closed. Prices: {data_text}. 4 lines plain text: session result, which pair moved most, liquidity swept, bias into next session."""
            ai = ask_claude(prompt)
            msg = (
                f"📊 <b>{label} SUMMARY</b>\n"
                f"🕐 {lt.strftime('%H:%M')} {suffix}\n\n"
                f"{lines}\n\n"
                f"🤖 <b>SESSION READ:</b>\n{ai}"
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
                    week_events.append(f"{et_london.strftime('%a %H:%M')} {suffix} — {e.get('currency')} {e.get('title')}")
                except:
                    pass
    except:
        week_events = []
    price_text = " | ".join([f"{p}: {prices[p]['price']}" for p in prices]) if prices else "unavailable"
    news_text = "\n".join(week_events[:10]) if week_events else "Light week"
    prompt = f"""Weekly outlook for Joshua week of {lt.strftime('%d %b %Y')}. Prices: {price_text}. Events: {news_text}. Plain text. Weekly bias EUR/USD GBP/USD, key levels, risk events, best days, main setup. Under 200 words."""
    ai = ask_claude(prompt)
    price_lines = "\n".join([f"{'📈' if prices[p]['change'] > 0 else '📉'} <b>{p}</b>: {prices[p]['price']}" for p in prices]) if prices else ""
    news_lines = "\n".join([f"🔴 {e}" for e in week_events[:8]]) if week_events else "✅ Light week"
    msg = (
        f"📅 <b>WEEKLY OUTLOOK — {lt.strftime('W/C %d %b %Y')}</b>\n\n"
        f"💰 <b>PRICES</b>\n{price_lines}\n\n"
        f"🗓 <b>KEY EVENTS</b>\n{news_lines}\n\n"
        f"🤖 <b>AI WEEKLY READ:</b>\n{ai}"
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
            except Exception as e:
                print(f"FF fetch error: {e}", flush=True)

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
        session = get_session() or "Market closed"

        for headline in unique[:20]:
            if any(kw in headline.lower() for kw in keywords) and headline not in last_ff_headlines:
                last_ff_headlines.add(headline)
                if len(last_ff_headlines) > 200:
                    last_ff_headlines = set(list(last_ff_headlines)[-100:])

                eur_data = get_yahoo_price("EUR/USD")
                xau_data = get_yahoo_price("XAU/USD")
                time.sleep(0.5)
                price_context = f"EUR/USD: {eur_data['price']} | XAU/USD: {xau_data['price']}" if eur_data and xau_data else ""

                prompt = f"""Breaking headline on ForexFactory: "{headline}"
Time: {lt.strftime('%H:%M')} {suffix} | Session: {session}
{price_context}
Liquidity: {get_liquidity_context()}
4 lines plain text: which pairs affected, likely M1 reaction next 5-15 mins, trade or avoid, specific M1 setup."""

                ai = ask_claude(prompt)
                msg = (
                    f"🔴 <b>FF BREAKING NEWS</b>\n\n"
                    f"📰 {headline}\n\n"
                    f"🕐 {lt.strftime('%H:%M')} {suffix} | {session}\n"
                    f"{price_context}\n\n"
                    f"🤖 <b>AI READ + M1 REACTION:</b>\n{ai}"
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
                prompt = f"""Red folder in {int(mins_until)} mins: {event.get('title')} for {event.get('currency')}. Forecast: {event.get('forecast', 'N/A')}, Previous: {event.get('previous', 'N/A')}. 3 lines plain text: EUR/USD GBP/USD impact, trade or avoid, beat/miss expected move."""
                ai = ask_claude(prompt)
                msg = (
                    f"📰 <b>RED FOLDER — {int(mins_until)} MINS</b>\n\n"
                    f"🏷 {event.get('title')}\n"
                    f"🌍 {event.get('currency')} 🔴\n"
                    f"🕐 {event_london.strftime('%H:%M')} {suffix}\n"
                    f"📊 Forecast: {event.get('forecast', 'N/A')} | Prev: {event.get('previous', 'N/A')}\n\n"
                    f"🤖 <b>AI READ:</b>\n{ai}"
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
                    usd_impact = "USD BULLISH — EUR/GBP likely drop" if beat else "USD BEARISH — EUR/GBP likely pump"
                elif currency == "EUR":
                    usd_impact = "EUR BULLISH — EUR/USD likely pump" if beat else "EUR BEARISH — EUR/USD likely drop"
                else:
                    usd_impact = "GBP BULLISH — GBP/USD likely pump" if beat else "GBP BEARISH — GBP/USD likely drop"
                eur_data = get_yahoo_price("EUR/USD")
                gbp_data = get_yahoo_price("GBP/USD")
                price_context = f"EUR/USD: {eur_data['price']} | GBP/USD: {gbp_data['price']}" if eur_data and gbp_data else ""
                prompt = f"""News result: {event.get('title')} for {currency}. Actual: {actual}, Forecast: {forecast}, Previous: {previous}. {result_emoji}. {usd_impact}. {price_context}. 3 lines plain text: immediate M1 reaction, trade setup now, specific entry to watch."""
                ai = ask_claude(prompt)
                msg = (
                    f"📊 <b>NEWS RESULT — {event.get('title')}</b>\n\n"
                    f"{result_emoji}\n"
                    f"Actual: <b>{actual}</b> | Forecast: {forecast} | Prev: {previous}\n"
                    f"🌍 {currency} | 🕐 {event_london.strftime('%H:%M')} {suffix}\n"
                    f"{usd_impact}\n"
                    f"{price_context}\n\n"
                    f"🤖 <b>M1 REACTION:</b>\n{ai}"
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
        direction = "📈 UP" if data["price"] > data["prev_price"] else "📉 DOWN"
        if ratio >= SPIKE_MULTIPLIER:
            correlated = "GBP/USD" if pair == "EUR/USD" else "EUR/USD" if pair == "GBP/USD" else "XAG/USD" if pair == "XAU/USD" else "XAU/USD"
            corr_data = all_data.get(correlated)
            corr_text = f"\n🔗 {correlated}: {corr_data['price']} {'📈' if corr_data['price'] > corr_data['prev_price'] else '📉'}" if corr_data else ""
            prompt = f"""Volume spike: {pair} {ratio:.1f}x normal. Price: {data['price']} {direction} {pip_move:.1f} pips. Session: {session}. Correlated {correlated}: {corr_data['price'] if corr_data else 'N/A'}. Liquidity: {get_liquidity_context()}. 4 lines plain text: spike meaning, SMT context, M1 setup, confidence."""
            ai = ask_claude(prompt)
            msg = (
                f"🚨 <b>VOLUME SPIKE — {pair}</b>\n\n"
                f"📊 {ratio:.1f}x normal\n"
                f"💰 {data['price']} {direction} | {pip_move:.1f} pips"
                f"{corr_text}\n"
                f"🕐 {london_time_str()} | {session}\n\n"
                f"🤖 {ai}"
            )
            send_telegram(msg)
        elif pip_move >= 10:
            prompt = f"""{pair} moved {pip_move:.1f} pips {direction} at {london_time_str()}. Session: {session}. Price: {data['price']}. Liquidity: {get_liquidity_context()}. 3 lines plain text: Judas or momentum, SMT context, what to watch."""
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
    price_summary = "\n".join([
        f"{'📈' if prices[p]['change'] > 0 else '📉'} <b>{p}</b>: {prices[p]['price']}"
        for p in prices
    ])
    mtf_lines = ""
    if mtf_eur:
        mtf_lines += f"EUR/USD H1:{mtf_eur.get('H1','?')} M15:{mtf_eur.get('M15','?')} M1:{mtf_eur.get('M1','?')}\n"
    if mtf_gbp:
        mtf_lines += f"GBP/USD H1:{mtf_gbp.get('H1','?')} M15:{mtf_gbp.get('M15','?')} M1:{mtf_gbp.get('M1','?')}"
    data_text = " | ".join([f"{p}: {prices[p]['price']} ({'up' if prices[p]['change'] > 0 else 'down'})" for p in prices])
    prompt = f"""Hourly bias for Joshua. {lt.strftime('%H:%M')} {suffix}. Session: {session}. Prices: {data_text}. MTF: EUR/USD H1:{mtf_eur.get('H1','?')} M15:{mtf_eur.get('M15','?')} | GBP/USD H1:{mtf_gbp.get('H1','?')} M15:{mtf_gbp.get('M15','?')}. Liquidity: {get_liquidity_context()}. 4 lines plain text: directional bias, strongest/weakest pair, SMT divergence, one thing to watch."""
    ai = ask_claude(prompt)
    msg = (
        f"🕐 <b>HOURLY BIAS — {lt.strftime('%H:00')} {suffix}</b>\n"
        f"📍 {session}\n\n"
        f"{price_summary}\n\n"
        f"📊 <b>MTF:</b>\n{mtf_lines}\n\n"
        f"🤖 {ai}\n\n"
        f"🔄 Next: {(lt.hour + 1) % 24:02d}:00 {suffix}"
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
                        today_events.append(f"{et_london.strftime('%H:%M')} {suffix} — {e.get('currency')} {e.get('title')}")
                except:
                    pass
    except:
        today_events = []
    price_text = " | ".join([f"{p}: {prices[p]['price']}" for p in prices]) if prices else "unavailable"
    news_text = "\n".join(today_events) if today_events else "No high impact news"
    mtf_context = f"EUR/USD H1:{mtf_eur.get('H1','?')} M15:{mtf_eur.get('M15','?')} | GBP/USD H1:{mtf_gbp.get('H1','?')} M15:{mtf_gbp.get('M15','?')}" if mtf_eur or mtf_gbp else ""
    prompt = f"""Morning brief for Joshua. {lt.strftime('%A %d %b %Y')} 07:00 {suffix}. Prices: {price_text}. MTF: {mtf_context}. Liquidity: {get_liquidity_context()}. News: {news_text}. Plain text. H1 M15 bias, London bias, key levels PDH/PDL, Asia range targets, 08:00 setup, news risk. Under 150 words."""
    ai = ask_claude(prompt)
    price_lines = "\n".join([f"{'📈' if prices[p]['change'] > 0 else '📉'} <b>{p}</b>: {prices[p]['price']}" for p in prices]) if prices else ""
    news_lines = "\n".join([f"🔴 {e}" for e in today_events]) if today_events else "✅ No high impact news"
    asia_line = f"🌏 Asia: {asia_range['low']} — {asia_range['high']}" if asia_range["high"] else ""
    liq_line = f"📌 PDH:{liquidity_levels['pdh']} PDL:{liquidity_levels['pdl']}" if liquidity_levels["pdh"] else ""
    mtf_lines = ""
    if mtf_eur:
        mtf_lines += f"EUR/USD H1:{mtf_eur.get('H1','?')} M15:{mtf_eur.get('M15','?')} M1:{mtf_eur.get('M1','?')}\n"
    if mtf_gbp:
        mtf_lines += f"GBP/USD H1:{mtf_gbp.get('H1','?')} M15:{mtf_gbp.get('M15','?')} M1:{mtf_gbp.get('M1','?')}"
    msg = (
        f"🌅 <b>MORNING BRIEF — {lt.strftime('%a %d %b')} 07:00 {suffix}</b>\n\n"
        f"💰 <b>PRICES</b>\n{price_lines}\n{asia_line}\n{liq_line}\n\n"
        f"📊 <b>MTF BIAS</b>\n{mtf_lines}\n\n"
        f"📅 <b>TODAY'S NEWS</b>\n{news_lines}\n\n"
        f"🤖 <b>AI READ:</b>\n{ai}"
    )
    send_telegram(msg)
    print("Morning brief sent", flush=True)

def check_session_countdown():
    if is_weekend():
        return
    lt, suffix = london_time()
    countdowns = [
        (23, 45, "🌏 ASIA OPEN", f"00:00 {suffix}"),
        (6, 45, "🇩🇪 FRANKFURT OPEN", f"07:00 {suffix}"),
        (7, 45, "🇬🇧 LONDON OPEN", f"08:00 {suffix}"),
        (14, 15, "🇺🇸 NY OPEN", f"14:30 {suffix}"),
    ]
    for h, m, label, open_time in countdowns:
        key = f"countdown-{lt.strftime('%Y-%m-%d')}-{h}-{m}"
        if lt.hour == h and lt.minute >= m and lt.minute < m + 5 and key not in last_hourly:
            last_hourly[key] = True
            prices = get_all_prices()
            price_lines = "\n".join([f"💰 <b>{p}</b>: {prices[p]['price']}" for p in prices]) if prices else ""
            price_text = " | ".join([f"{p}: {prices[p]['price']}" for p in prices]) if prices else "unavailable"
            prompt = f"""15 mins to {label} at {open_time}. Prices: {price_text}. Liquidity: {get_liquidity_context()}. 3 lines plain text: expected bias, which level swept first, M1 setup."""
            ai = ask_claude(prompt)
            msg = (
                f"⚡ <b>15 MINS TO {label}</b>\n"
                f"🕐 Opens: {open_time}\n\n"
                f"{price_lines}\n\n"
                f"🤖 {ai}"
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
            prompt = f"""SMT divergence: EUR/USD {eur_move:.1f} pips, GBP/USD {gbp_move:.1f} pips. Divergence: {divergence:.1f} pips. {weaker} weaker. Session: {session}. Liquidity: {get_liquidity_context()}. 3 lines plain text: SMT context, M1 setup, which pair and direction."""
            ai = ask_claude(prompt)
            msg = (
                f"⚠️ <b>SMT DIVERGENCE</b>\n\n"
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
    needs_prices = any(kw in text_lower for kw in [
        "gold", "silver", "eur", "gbp", "xau", "xag", "price", "doing",
        "level", "where", "bias", "long", "short", "buy", "sell",
        "market", "session", "setup", "trade", "dxy", "dollar", "h1", "m15"
    ])
    prices = None
    dxy_data = None
    mtf_context = ""
    if needs_prices:
        send_telegram("⏳ Fetching live data...", chat_id)
        prices = get_all_prices()
        dxy_data = get_yahoo_price("DXY")
        if any(kw in text_lower for kw in ["h1", "m15", "bias", "htf"]):
            mtf_eur = get_mtf_bias("EUR/USD")
            time.sleep(0.5)
            mtf_gbp = get_mtf_bias("GBP/USD")
            if mtf_eur:
                mtf_context += f"EUR/USD H1:{mtf_eur.get('H1','?')} M15:{mtf_eur.get('M15','?')} M1:{mtf_eur.get('M1','?')} | "
            if mtf_gbp:
                mtf_context += f"GBP/USD H1:{mtf_gbp.get('H1','?')} M15:{mtf_gbp.get('M15','?')} M1:{mtf_gbp.get('M1','?')}"
    lt, suffix = london_time()
    session = get_session() or ("Weekend — markets closed" if is_weekend() else "Off hours")
    price_context = ""
    if prices:
        price_context = "Live prices: " + " | ".join([
            f"{p}: {prices[p]['price']} ({'up' if prices[p]['change'] > 0 else 'down'} {abs(prices[p]['change'] * (100 if 'XAU' in p or 'XAG' in p else 10000)):.1f} pips)"
            for p in prices
        ])
        if dxy_data:
            price_context += f" | DXY: {dxy_data['price']}"
    prompt = f"""Joshua says: "{text}"
Time: {lt.strftime('%H:%M')} {suffix} | Session: {session}
{price_context}
{f'MTF: {mtf_context}' if mtf_context else ''}
Liquidity: {get_liquidity_context()}
Respond directly. Sharp and concise. Max 5 lines. Plain text."""
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
                lt, suffix = london_time()
                session = get_session() or "Off hours"
                prompt = f"""Joshua sent a chart{' with note: ' + caption if caption else ''}. Time: {lt.strftime('%H:%M')} {suffix}. Session: {session}. Liquidity: {get_liquidity_context()}.
Analyse using SMC/SMT:
1. Pair and timeframe
2. Market structure BOS/CHOCH
3. Liquidity sweeps — equal highs/lows, PDH/PDL, Asia range
4. FVGs and order blocks with price levels
5. Inducement visible
6. SMT divergence if two charts shown
7. Entry zone, stop loss, target with specific prices
8. Confidence out of 10
Plain text no markdown."""
                send_telegram("🤖 Analysing your chart...", chat_id)
                ai = ask_claude(prompt, img_b64)
                send_telegram(f"🤖 <b>CHART ANALYSIS</b>\n\n{ai}", chat_id)
            elif text:
                cmd = text.lower().strip()
                if cmd == "/prices":
                    send_telegram("⏳ Fetching live prices...", chat_id)
                    lt, suffix = london_time()
                    prices = get_all_prices()
                    dxy_data = get_yahoo_price("DXY")
                    prices_msg = f"💰 <b>LIVE PRICES</b>\n🕐 {lt.strftime('%H:%M')} {suffix}\n\n"
                    for pair in PAIRS:
                        if pair in prices:
                            d = "📈" if prices[pair]["change"] > 0 else "📉"
                            cp = prices[pair]["change"] * (100 if "XAU" in pair or "XAG" in pair else 10000)
                            prices_msg += f"{d} <b>{pair}</b>: {prices[pair]['price']} ({'+' if cp > 0 else ''}{cp:.1f} pips)\n"
                        else:
                            prices_msg += f"⚠️ <b>{pair}</b>: unavailable\n"
                    if dxy_data:
                        dc = dxy_data["change"] * 100
                        prices_msg += f"\n{'📈' if dxy_data['change'] > 0 else '📉'} <b>DXY</b>: {dxy_data['price']} ({'+' if dc > 0 else ''}{dc:.2f}%)"
                    liq = get_liquidity_context()
                    if liq:
                        prices_msg += f"\n\n📌 <b>KEY LEVELS:</b>\n{liq.replace(' | ', chr(10))}"
                    send_telegram(prices_msg, chat_id)
                elif cmd == "/bias":
                    handle_natural_message("what is the current H1 M15 M1 bias on EUR/USD and GBP/USD right now", chat_id)
                elif cmd == "/levels":
                    lt, suffix = london_time()
                    eur_data = get_yahoo_price("EUR/USD")
                    msg = f"📌 <b>KEY LIQUIDITY LEVELS</b>\n🕐 {lt.strftime('%H:%M')} {suffix}\n\n"
                    if liquidity_levels["pwh"]:
                        msg += f"PWH: {liquidity_levels['pwh']}\n"
                    if liquidity_levels["pwl"]:
                        msg += f"PWL: {liquidity_levels['pwl']}\n"
                    if liquidity_levels["pdh"]:
                        msg += f"PDH: {liquidity_levels['pdh']}\n"
                    if liquidity_levels["pdl"]:
                        msg += f"PDL: {liquidity_levels['pdl']}\n"
                    if asia_range["high"]:
                        msg += f"Asia High: {asia_range['high']}\n"
                    if asia_range["low"]:
                        msg += f"Asia Low: {asia_range['low']}\n"
                    if eur_data:
                        msg += f"\nEUR/USD now: {eur_data['price']}"
                    send_telegram(msg, chat_id)
                elif cmd == "/brief":
                    send_telegram("⏳ Generating brief...", chat_id)
                    lt, suffix = london_time()
                    last_hourly.pop(lt.strftime("%Y-%m-%d-brief"), None)
                    send_morning_brief()
                elif cmd == "/help":
                    lt, suffix = london_time()
                    weekend_note = "\n⚠️ Weekend — auto alerts resume Monday" if is_weekend() else ""
                    send_telegram(
                        f"📊 <b>FUNDAMENTALS BOT</b>{weekend_note}\n\n"
                        f"Talk naturally:\n"
                        f"'what is gold doing'\n"
                        f"'what is the H1 bias on EUR'\n"
                        f"'is there SMT on EU right now'\n"
                        f"'what is DXY doing'\n\n"
                        f"Commands:\n"
                        f"/prices — Live prices + DXY + levels\n"
                        f"/bias — H1 M15 M1 AI bias\n"
                        f"/levels — Key liquidity levels\n"
                        f"/brief — Morning brief\n"
                        f"/help — This menu\n\n"
                        f"📸 Send chart for SMC/SMT analysis\n\n"
                        f"⏰ Sessions ({suffix}):\n"
                        f"Asia 🌏 00:00-07:00\n"
                        f"Frankfurt 🇩🇪 07:00-08:00\n"
                        f"London 🇬🇧 08:00-13:00\n"
                        f"NY 🇺🇸 14:30-21:00\n\n"
                        f"Auto alerts weekdays:\n"
                        f"🔴 FF breaking news + M1 reaction\n"
                        f"📊 News result beat/miss\n"
                        f"⚡ DXY moves 0.3%+\n"
                        f"🚨 Volume spikes\n"
                        f"⚠️ SMT divergence\n"
                        f"🎯 London killzone\n"
                        f"🌏 Asia range\n"
                        f"📌 PDH/PDL/PWH/PWL touches\n"
                        f"📊 Hourly bias H1+M15+M1\n"
                        f"📅 Weekly outlook Mondays",
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
    HTTPServer(("0.0.0.0", 10000), Handler).serve_forever()

threading.Thread(target=run_server, daemon=True).start()
time.sleep(5)

lt, suffix = london_time()
weekend_note = " — Weekend mode" if is_weekend() else ""
send_telegram(
    f"📊 <b>FUNDAMENTALS BOT IS LIVE{weekend_note}</b>\n\n"
    f"Pairs: EUR/USD | GBP/USD | XAU/USD | XAG/USD\n\n"
    f"⏰ Sessions ({suffix}):\n"
    f"Asia 🌏 00:00-07:00\n"
    f"Frankfurt 🇩🇪 07:00-08:00\n"
    f"London 🇬🇧 08:00-13:00\n"
    f"NY 🇺🇸 14:30-21:00\n\n"
    f"🕐 {lt.strftime('%H:%M')} {suffix}\n"
    f"/help for all commands"
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
