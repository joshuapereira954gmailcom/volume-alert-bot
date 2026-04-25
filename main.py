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

STRATEGY_CONTEXT = """You are the personal AI trading assistant for Joshua, an M1 forex trader in London using Smart Money Concepts and SMT divergence.

Joshua trades: EUR/USD, GBP/USD, XAU/USD, XAG/USD only.
Strategy: SMT divergence between EUR/USD and GBP/USD, and between Gold and Silver.
Looks for: liquidity sweeps, order blocks, FVGs, inducement, BOS/CHOCH on M1.
Sessions BST: Asia 00:00-07:00, Frankfurt 07:00-08:00, London 08:00-13:00, NY 14:30-21:00.
Key setups: London open Judas swing 08:00-09:00, NY sweep 14:30-15:00.
Asia range highs and lows are key liquidity targets at London open.
DXY inverse relationship with EUR/USD and GBP/USD.
Never trades weekends or during high impact news.

Respond like a sharp trading partner. Direct, concise, max 5 lines unless asked for more.
Plain text only — no asterisks, no hashes, no markdown.
Always reference live prices when discussing pairs.
Reference Asia range when relevant."""

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
        return {
            "price": price,
            "prev_price": prev_price,
            "change": price - prev_price,
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
        prompt = f"""Asia session closed. EUR/USD Asia range: High {asia_range['high']} Low {asia_range['low']} Range {rng} pips. 3 lines plain text: which side has liquidity, which level London likely sweeps first at 08:00 BST, bias for London session."""
        ai = ask_claude(prompt)
        msg = (
            f"🌏 <b>ASIA RANGE COMPLETE</b>\n\n"
            f"📈 High: {asia_range['high']} — liquidity above\n"
            f"📉 Low: {asia_range['low']} — liquidity below\n"
            f"📏 Range: {rng} pips\n\n"
            f"🤖 <b>LONDON OPEN BIAS:</b>\n{ai}"
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
                prompt = f"""DXY moved {change_pct:.2f}% — dollar {direction}. DXY at {current_price}. Session: {session}. Time: {lt.strftime('%H:%M')} {suffix}. 4 lines plain text: impact on EUR/USD and GBP/USD, likely M1 price action next 5-10 mins, long or short bias, specific setup to watch."""
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
        swept_low = eur_data["price"] < asia_range["low"]
        swept_high = eur_data["price"] > asia_range["high"]
        if swept_low:
            asia_context = f"ASIA LOW SWEPT at {asia_range['low']}"
        elif swept_high:
            asia_context = f"ASIA HIGH SWEPT at {asia_range['high']}"
        else:
            asia_context = f"Asia range intact — H:{asia_range['high']} L:{asia_range['low']}"
    if divergence >= 5 or "SWEPT" in asia_context:
        last_hourly[key] = True
        prompt = f"""London killzone: EUR/USD {eur_data['price']} ({'+' if eur_move > 0 else ''}{eur_move:.1f} pips), GBP/USD {gbp_data['price']} ({'+' if gbp_move > 0 else ''}{gbp_move:.1f} pips). Divergence: {divergence:.1f} pips. {asia_context}. Time: {lt.strftime('%H:%M')} {suffix}. 4 lines plain text: is Judas swing forming, direction, M1 setup to look for, confidence."""
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
            prompt = f"""{label} just closed. Prices: {data_text}. 4 lines plain text: session result for EUR/USD and GBP/USD, which pair moved most, what liquidity was swept, bias going into next session."""
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
    prompt = f"""Weekly outlook for Joshua week of {lt.strftime('%d %b %Y')}. Prices: {price_text}. Key events: {news_text}. Plain text no markdown. Cover: weekly bias EUR/USD and GBP/USD, key levels all week, highest risk events, best days to trade, main setup to prioritise. Under 200 words. Sharp direct style."""
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
            "ecb", "lagarde", "boe", "bailey",
            "iran", "ceasefire", "trump", "tariff", "sanctions",
            "nfp", "cpi", "inflation", "gdp",
            "gold", "dollar index", "dxy",
            "war", "attack", "crisis", "emergency", "collapse",
            "opec", "oil supply", "china trade", "us china",
            "recession", "bank failure", "debt ceiling"
        ]

        lt, suffix = london_time()
        session = get_session() or "Market closed"

        for headline in unique[:20]:
            headline_lower = headline.lower()
            is_relevant = any(kw in headline_lower for kw in keywords)
            if is_relevant and headline not in last_ff_headlines:
                last_ff_headlines.add(headline)
                if len(last_ff_headlines) > 200:
                    last_ff_headlines = set(list(last_ff_headlines)[-100:])

                eur_data = get_yahoo_price("EUR/USD")
                xau_data = get_yahoo_price("XAU/USD")
                time.sleep(0.5)
                price_context = ""
                if eur_data and xau_data:
                    price_context = f"EUR/USD: {eur_data['price']} | XAU/USD: {xau_data['price']}"
                asia_context = f"Asia range: H:{asia_range['high']} L:{asia_range['low']}" if asia_range["high"] else ""

                prompt = f"""Breaking headline on ForexFactory: "{headline}"
Time: {lt.strftime('%H:%M')} {suffix} | Session: {session}
{price_context}
{asia_context}

4 lines plain text for Joshua's EUR/USD, GBP/USD, XAU/USD, XAG/USD M1 strategy:
1. Which of Joshua's pairs are affected and dollar flow direction
2. Likely M1 reaction next 5-15 minutes on EUR/USD or GBP/USD
3. Trade or avoid — and why
4. If setup forms — what specifically to look for on M1"""

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
                prompt = f"""Red folder in {int(mins_until)} mins: {event.get('title')} for {event.get('currency')}. Forecast: {event.get('forecast', 'N/A')}, Previous: {event.get('previous', 'N/A')}. 3 lines plain text: impact on EUR/USD and GBP/USD, trade or avoid, expected move if beats or misses."""
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
            asia_text = f"\n🌏 Asia: {asia_range['low']} — {asia_range['high']}" if asia_range["high"] and pair in ["EUR/USD", "GBP/USD"] else ""
            prompt = f"""Volume spike: {pair} {ratio:.1f}x normal. Price: {data['price']} {direction} {pip_move:.1f} pips. Session: {session}. Correlated {correlated}: {corr_data['price'] if corr_data else 'N/A'}. Asia range H:{asia_range['high']} L:{asia_range['low']}. 4 lines plain text: spike meaning for SMT, divergence context, M1 setup to watch, confidence."""
            ai = ask_claude(prompt)
            msg = (
                f"🚨 <b>VOLUME SPIKE — {pair}</b>\n\n"
                f"📊 {ratio:.1f}x normal\n"
                f"💰 {data['price']} {direction} | {pip_move:.1f} pips"
                f"{corr_text}{asia_text}\n"
                f"🕐 {london_time_str()} | {session}\n\n"
                f"🤖 {ai}"
            )
            send_telegram(msg)
        elif pip_move >= 10:
            prompt = f"""{pair} moved {pip_move:.1f} pips {direction} at {london_time_str()}. Session: {session}. Price: {data['price']}. Asia H:{asia_range['high']} L:{asia_range['low']}. 3 lines plain text: Judas or momentum, SMT context, what to watch."""
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
    price_summary = "\n".join([
        f"{'📈' if prices[p]['change'] > 0 else '📉'} <b>{p}</b>: {prices[p]['price']}"
        for p in prices
    ])
    data_text = " | ".join([f"{p}: {prices[p]['price']} ({'up' if prices[p]['change'] > 0 else 'down'})" for p in prices])
    asia_context = f"Asia range H:{asia_range['high']} L:{asia_range['low']}" if asia_range["high"] else ""
    prompt = f"""Hourly bias for Joshua. {lt.strftime('%H:%M')} {suffix}. Session: {session}. Prices: {data_text}. {asia_context}. 4 lines plain text: directional bias, strongest/weakest pair, SMT divergence EUR/GBP or Gold/Silver, one thing to watch this hour."""
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
    asia_context = f"Asia range H:{asia_range['high']} L:{asia_range['low']}" if asia_range["high"] else "Asia range recording"
    news_text = "\n".join(today_events) if today_events else "No high impact news"
    prompt = f"""Morning brief for Joshua. {lt.strftime('%A %d %b %Y')} 07:00 {suffix}. Prices: {price_text}. {asia_context}. News: {news_text}. Plain text. Cover: London bias, key EUR/USD GBP/USD levels, Asia range liquidity targets, main 08:00 setup, news risk. Under 150 words. Sharp direct."""
    ai = ask_claude(prompt)
    price_lines = "\n".join([f"{'📈' if prices[p]['change'] > 0 else '📉'} <b>{p}</b>: {prices[p]['price']}" for p in prices]) if prices else ""
    news_lines = "\n".join([f"🔴 {e}" for e in today_events]) if today_events else "✅ No high impact news"
    asia_line = f"🌏 Asia range: {asia_range['low']} — {asia_range['high']}" if asia_range["high"] else "🌏 Asia range recording"
    msg = (
        f"🌅 <b>MORNING BRIEF — {lt.strftime('%a %d %b')} 07:00 {suffix}</b>\n\n"
        f"💰 <b>PRICES</b>\n{price_lines}\n{asia_line}\n\n"
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
            asia_context = f"Asia range H:{asia_range['high']} L:{asia_range['low']}" if asia_range["high"] else ""
            prompt = f"""15 mins to {label} at {open_time}. Prices: {price_text}. {asia_context}. 3 lines plain text: expected bias at open, which Asia level likely swept first, one specific M1 setup."""
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
            prompt = f"""SMT divergence: EUR/USD {eur_move:.1f} pips, GBP/USD {gbp_move:.1f} pips. Divergence: {divergence:.1f} pips. {weaker} weaker. Session: {session}. Asia H:{asia_range['high']} L:{asia_range['low']}. 3 lines plain text: meaningful SMT context, M1 setup, which pair to trade and direction."""
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
        "market", "session", "setup", "trade", "dxy", "dollar"
    ])
    prices = None
    dxy_data = None
    if needs_prices:
        send_telegram("⏳ Fetching live data...", chat_id)
        prices = get_all_prices()
        dxy_data = get_yahoo_price("DXY")
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
    asia_context = f"Asia range H:{asia_range['high']} L:{asia_range['low']}" if asia_range["high"] else ""
    prompt = f"""Joshua says: "{text}"
Time: {lt.strftime('%H:%M')} {suffix} | Session: {session}
{price_context}
{asia_context}
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
                asia_context = f"Asia range H:{asia_range['high']} L:{asia_range['low']}" if asia_range["high"] else ""
                prompt = f"""Joshua sent a chart{' with note: ' + caption if caption else ''}. Time: {lt.strftime('%H:%M')} {suffix}. Session: {session}. {asia_context}.
Analyse using SMC/SMT for EUR/USD, GBP/USD, XAU/USD, XAG/USD:
1. Pair and timeframe
2. Market structure — BOS/CHOCH
3. Liquidity sweeps — equal highs/lows, Asia range levels swept
4. FVGs and order blocks with price levels
5. Inducement visible
6. SMT divergence if two charts shown
7. Entry zone, stop loss, target with specific prices
8. Confidence out of 10
Plain text no markdown. Specific price levels only."""
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
                    if asia_range["high"]:
                        prices_msg += f"\n\n🌏 Asia range: {asia_range['low']} — {asia_range['high']}"
                    send_telegram(prices_msg, chat_id)
                elif cmd == "/bias":
                    handle_natural_message("what is the current bias on EUR/USD, GBP/USD, XAU/USD and XAG/USD right now", chat_id)
                elif cmd == "/brief":
                    send_telegram("⏳ Generating brief...", chat_id)
                    lt, suffix = london_time()
                    last_hourly.pop(lt.strftime("%Y-%m-%d-brief"), None)
                    send_morning_brief()
                elif cmd == "/help":
                    lt, suffix = london_time()
                    weekend_note = "\n⚠️ Weekend — auto alerts paused until Monday" if is_weekend() else ""
                    send_telegram(
                        f"📊 <b>FUNDAMENTALS BOT</b>{weekend_note}\n\n"
                        f"Talk naturally:\n"
                        f"'what is gold doing'\n"
                        f"'is there SMT on EU right now'\n"
                        f"'what should I watch at London open'\n"
                        f"'what is DXY doing'\n\n"
                        f"Commands:\n"
                        f"/prices — Live prices + DXY\n"
                        f"/bias — AI bias\n"
                        f"/brief — Morning brief\n"
                        f"/help — This menu\n\n"
                        f"📸 Send chart for SMC/SMT analysis\n\n"
                        f"⏰ Sessions ({suffix}):\n"
                        f"Asia 🌏 00:00-07:00\n"
                        f"Frankfurt 🇩🇪 07:00-08:00\n"
                        f"London 🇬🇧 08:00-13:00\n"
                        f"NY 🇺🇸 14:30-21:00\n\n"
                        f"Auto alerts (weekdays only):\n"
                        f"🔴 FF breaking news + M1 reaction\n"
                        f"⚡ DXY moves 0.3%+\n"
                        f"🚨 Volume spikes\n"
                        f"⚠️ SMT divergence\n"
                        f"🎯 London killzone 08:00-09:00\n"
                        f"🌏 Asia range at 07:00\n"
                        f"📊 Hourly bias London + NY only\n"
                        f"📅 Weekly outlook Monday 06:00\n"
                        f"📊 Session summaries 13:00 + 21:00",
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
weekend_note = " — Weekend mode, auto alerts resume Monday" if is_weekend() else ""
send_telegram(
    f"📊 <b>FUNDAMENTALS BOT IS LIVE{weekend_note}</b>\n\n"
    f"Pairs: EUR/USD | GBP/USD | XAU/USD | XAG/USD\n\n"
    f"⏰ Sessions ({suffix}):\n"
    f"Asia 🌏 00:00-07:00\n"
    f"Frankfurt 🇩🇪 07:00-08:00\n"
    f"London 🇬🇧 08:00-13:00\n"
    f"NY 🇺🇸 14:30-21:00\n\n"
    f"Weekday auto alerts:\n"
    f"🔴 FF breaking news + M1 reaction\n"
    f"⚡ DXY moves\n"
    f"🚨 Volume spikes\n"
    f"⚠️ SMT divergence\n"
    f"🎯 London killzone\n"
    f"🌏 Asia range\n"
    f"📊 Hourly bias\n"
    f"📰 Red folder alerts\n"
    f"📅 Weekly outlook Mondays\n\n"
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
    if not is_weekend():
        check_spikes()
        check_news()
        check_session_countdown()
        track_asia_range()
        check_london_killzone()
        send_session_summary()
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
