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
last_update_id = 0
conversation_history = {}
asia_range = {"high": None, "low": None, "recorded": False}
last_dxy = {"price": None, "time": None}
liquidity_levels = {"pdh": None, "pdl": None, "pwh": None, "pwl": None}
news_results_sent = set()
news_surprise_history = {"USD": [], "EUR": [], "GBP": []}
accumulation_tracking = {}

STRATEGY_CONTEXT = """You are PONK PONK — a trading assistant texting Joshua and Mascu in a group chat. Both are M1 forex traders using SMT divergence and SMC concepts.

How you text:
- Like a real person sending a WhatsApp message. Short. Lowercase mostly. No drama.
- Max 4 lines for any response. Never write paragraphs.
- No capital letters unless it's a pair name or level number.
- No excessive emojis. One or two max if it adds something.
- No hype words. No screaming. No exclamation marks everywhere.
- If someone says hello just say hey back and ask what they're watching or make one observation about the market.
- Get to the point immediately. No intro sentences.
- Roast lightly when appropriate. One dry line beats three loud ones.
- When market is moving be serious and direct. Save chat for quiet times.
- Always address people by name when you know who's talking.

Trading knowledge:
- Pairs: EUR/USD, GBP/USD, XAU/USD, XAG/USD
- Strategy: SMT divergence, liquidity sweeps, OBs, FVGs, BOS/CHOCH on M1
- Sessions BST: Asia 00:00-07:00, Frankfurt 07:00-08:00, London 08:00-13:00, NY 14:30-21:00
- Asia range highs/lows are key London targets. DXY inverse to EUR/GBP.
- Never trade high impact news.

Format:
- Chat replies: plain text. no dividers. no bold. just talk.
- Auto alerts: one short divider line at top. max 4 lines after it.
- Pair names and price levels can be uppercase: EUR/USD, 1.1748
- No asterisks. No hashes. No markdown.
- Setup grade A/B/C and kill score /10 on charts only."""

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
        active.append("Asia")
    if hour == 7:
        active.append("Frankfurt")
    if 8 <= hour < 13:
        active.append("London")
    if 13 <= hour < 14 or (hour == 14 and minute < 30):
        active.append("lunch")
    if hour > 14 or (hour == 14 and minute >= 30):
        if hour < 21:
            active.append("NY")
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

def get_user_name(from_id, chat_id):
    if from_id and str(from_id) in USER_NAMES:
        return USER_NAMES[str(from_id)]
    if chat_id and str(chat_id) in USER_NAMES:
        return USER_NAMES[str(chat_id)]
    return None

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
        except Exception as e:
            print(f"Telegram error {target}: {e}", flush=True)
        time.sleep(0.3)

def ask_claude(prompt, image_base64=None, media_type="image/jpeg", use_history=False, chat_id=None):
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    history_key = str(chat_id) if chat_id else "global"
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
        "max_tokens": 300,
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
        return "something broke, try again"

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

def get_surprise_history_text(currency):
    history = news_surprise_history.get(currency, [])
    if not history:
        return ""
    symbols = []
    for result in history[-5:]:
        if result == "beat":
            symbols.append("✅")
        elif result == "miss":
            symbols.append("❌")
        else:
            symbols.append("⚪")
    beats = history[-5:].count("beat")
    trend = "been strong lately" if beats >= 3 else "been weak lately" if beats <= 1 else "mixed recently"
    return f"{currency} data {''.join(symbols)} — {trend}"

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

def check_smart_money_narrative():
    if is_weekend():
        return
    session = get_session()
    if not session or "lunch" in session:
        return
    lt, suffix = london_time()
    hour_key = f"narrative-{lt.strftime('%Y-%m-%d-%H')}"
    if hour_key in last_hourly:
        return
    last_hourly[hour_key] = True

    prices = get_all_prices()
    dxy_data = get_yahoo_price("DXY")
    if not prices or not dxy_data:
        return

    mtf_eur = get_mtf_bias("EUR/USD")
    time.sleep(0.5)
    mtf_gbp = get_mtf_bias("GBP/USD")

    price_text = " | ".join([f"{p}: {prices[p]['price']}" for p in prices])
    dxy_trend = "dropping" if dxy_data["change"] < 0 else "rising"

    prompt = f"""Smart money flow context. Time: {lt.strftime('%H:%M')} {suffix}. Session: {session}. Prices: {price_text}. DXY {dxy_data['price']} {dxy_trend}. EUR H1:{mtf_eur.get('H1','?')} M15:{mtf_eur.get('M15','?')} GBP H1:{mtf_gbp.get('H1','?')} M15:{mtf_gbp.get('M15','?')}. Liquidity: {get_liquidity_context()}. 3 lines plain text like a text message. What institutional money is likely doing. What the narrative is. What to watch. No hype. Address Joshua and Mascu."""

    ai = ask_claude(prompt)
    msg = f"━━━━━━━━━━━━━━━━━━\nsmarts · {lt.strftime('%H:%M')} {suffix}\n{ai}"
    send_telegram(msg)

def check_confluence_zones():
    if is_weekend() or is_blackout():
        return
    session = get_session()
    if not session or "lunch" in session or "Asia" in session:
        return

    eur_data = get_yahoo_price("EUR/USD")
    if not eur_data:
        return

    price = eur_data["price"]
    lt, suffix = london_time()

    all_levels = {}
    if liquidity_levels["pdh"]:
        all_levels["PDH"] = liquidity_levels["pdh"]
    if liquidity_levels["pdl"]:
        all_levels["PDL"] = liquidity_levels["pdl"]
    if liquidity_levels["pwh"]:
        all_levels["PWH"] = liquidity_levels["pwh"]
    if liquidity_levels["pwl"]:
        all_levels["PWL"] = liquidity_levels["pwl"]
    if asia_range["high"]:
        all_levels["Asia H"] = asia_range["high"]
    if asia_range["low"]:
        all_levels["Asia L"] = asia_range["low"]

    nearby = {k: v for k, v in all_levels.items() if abs(price - v) * 10000 <= 8}

    if len(nearby) >= 2:
        key = f"confluence-{lt.strftime('%Y-%m-%d-%H-%M')}"
        if key not in last_hourly:
            last_hourly[key] = True
            levels_text = " · ".join([f"{k} {v}" for k, v in nearby.items()])
            prompt = f"""EUR/USD at {price} near confluence of levels: {levels_text}. Session: {session}. 3 lines plain text: what this zone means, what institutions likely do here, what M1 signal to wait for. No hype. Address Joshua and Mascu."""
            ai = ask_claude(prompt)
            levels_display = " · ".join([f"{k} {v}" for k, v in nearby.items()])
            msg = f"━━━━━━━━━━━━━━━━━━\nconfluence · EUR/USD {price}\n{levels_display}\n{ai}"
            send_telegram(msg)

def check_london_ny_handoff():
    if is_weekend():
        return
    lt, suffix = london_time()
    if lt.hour != 14 or lt.minute > 5:
        return
    key = f"handoff-{lt.strftime('%Y-%m-%d')}"
    if key in last_hourly:
        return
    last_hourly[key] = True

    prices = get_all_prices()
    dxy_data = get_yahoo_price("DXY")
    if not prices:
        return

    price_text = " | ".join([f"{p}: {prices[p]['price']}" for p in prices])
    asia_text = f"Asia H:{asia_range['high']} L:{asia_range['low']}" if asia_range["high"] else ""

    prompt = f"""London just closed, NY opens in 30 mins. Prices: {price_text}. DXY: {dxy_data['price'] if dxy_data else 'N/A'}. {asia_text}. Liquidity: {get_liquidity_context()}. 4 lines plain text: what London did, what liquidity is left, what NY is likely to target, one setup to watch at 14:30. Address Joshua and Mascu."""

    ai = ask_claude(prompt)
    price_line = " · ".join([f"{'🟢' if prices[p]['change'] > 0 else '🔴'} {p.split('/')[0]} {prices[p]['price']}" for p in prices])
    msg = f"━━━━━━━━━━━━━━━━━━\nLondon done · NY in 30\n{price_line}\n{ai}"
    send_telegram(msg)

def check_cot_report():
    lt, suffix = london_time()
    if lt.weekday() != 4 or lt.hour != 15 or lt.minute > 5:
        return
    key = f"cot-{lt.strftime('%Y-%W')}"
    if key in last_hourly:
        return
    last_hourly[key] = True

    surprise_eur = get_surprise_history_text("EUR")
    surprise_gbp = get_surprise_history_text("GBP")
    surprise_usd = get_surprise_history_text("USD")
    prices = get_all_prices()
    price_text = " | ".join([f"{p}: {prices[p]['price']}" for p in prices]) if prices else "unavailable"

    prompt = f"""Friday flow summary for Joshua and Mascu. Prices: {price_text}. Data trends — {surprise_usd} · {surprise_eur} · {surprise_gbp}. 4 lines plain text: institutional bias based on recent data, which pairs to favour next week, key thing to watch Monday."""

    ai = ask_claude(prompt)
    lines = [l for l in [surprise_usd, surprise_eur, surprise_gbp] if l]
    msg = f"━━━━━━━━━━━━━━━━━━\nfriday flow · {lt.strftime('%d %b')}\n" + "\n".join(lines) + f"\n{ai}"
    send_telegram(msg)

def check_order_flow_context():
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

    lt, suffix = london_time()

    for pair, data in all_data.items():
        if data["avg_volume"] == 0:
            continue
        ratio = data["volume"] / data["avg_volume"]
        pip_move = abs(data["price"] - data["prev_price"]) * (100 if "XAU" in pair or "XAG" in pair else 10000)
        direction = "up" if data["price"] > data["prev_price"] else "down"

        if ratio >= SPIKE_MULTIPLIER:
            price = data["price"]
            liq = get_liquidity_context()

            above_pdh = liquidity_levels["pdh"] and price >= liquidity_levels["pdh"]
            below_pdl = liquidity_levels["pdl"] and price <= liquidity_levels["pdl"]
            above_asia = asia_range["high"] and price >= asia_range["high"]
            below_asia = asia_range["low"] and price <= asia_range["low"]

            if above_pdh or above_asia:
                location = "premium — likely distribution"
            elif below_pdl or below_asia:
                location = "discount — likely accumulation"
            else:
                location = "mid range"

            correlated = "GBP/USD" if pair == "EUR/USD" else "EUR/USD" if pair == "GBP/USD" else "XAG/USD" if pair == "XAU/USD" else "XAU/USD"
            corr_data = all_data.get(correlated)

            prompt = f"""Volume spike on {pair}. Price {price} moving {direction} {pip_move:.1f} pips. Volume {ratio:.1f}x normal. Location: {location}. Session: {session}. {correlated}: {corr_data['price'] if corr_data else 'N/A'}. Liquidity: {liq}. 3 lines plain text: who is likely behind this, SMT context with {correlated}, M1 setup and kill score. Address Joshua and Mascu."""

            ai = ask_claude(prompt)
            corr_line = f"{correlated} {corr_data['price']}" if corr_data else ""
            msg = (
                f"━━━━━━━━━━━━━━━━━━\n"
                f"{pair} · {ratio:.1f}x vol · {pip_move:.1f}p {direction} · {location}\n"
                f"{corr_line}\n"
                f"{ai}"
            )
            send_telegram(msg)

        elif pip_move >= 10:
            prompt = f"""{pair} moved {pip_move:.1f}p {direction} at {lt.strftime('%H:%M')} {suffix}. Price: {data['price']}. Session: {session}. Liquidity: {get_liquidity_context()}. 2 lines plain text: Judas or momentum, what to watch on M1."""
            ai = ask_claude(prompt)
            msg = f"━━━━━━━━━━━━━━━━━━\n{pair} · {pip_move:.1f}p {direction} · {data['price']}\n{ai}"
            send_telegram(msg)

def check_economic_surprise(currency, beat):
    result = "beat" if beat else "miss" if beat is False else "inline"
    if currency in news_surprise_history:
        news_surprise_history[currency].append(result)
        if len(news_surprise_history[currency]) > 10:
            news_surprise_history[currency].pop(0)

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

                beat = None
                try:
                    a_val = float(actual.replace("%", "").replace("K", "000").strip())
                    f_val = float(forecast.replace("%", "").replace("K", "000").strip())
                    beat = a_val > f_val
                except:
                    pass

                currency = event.get("currency", "USD")
                check_economic_surprise(currency, beat)

                result_word = "beat" if beat else "miss" if beat is False else "in line"
                if currency == "USD":
                    impact = "dollar up · EUR/GBP dropping" if beat else "dollar down · EUR/GBP pumping"
                elif currency == "EUR":
                    impact = "EUR/USD bullish" if beat else "EUR/USD bearish"
                else:
                    impact = "GBP/USD bullish" if beat else "GBP/USD bearish"

                surprise_context = get_surprise_history_text(currency)
                eur_data = get_yahoo_price("EUR/USD")
                gbp_data = get_yahoo_price("GBP/USD")
                price_line = f"EUR/USD {eur_data['price']} · GBP/USD {gbp_data['price']}" if eur_data and gbp_data else ""

                prompt = f"""News result: {event.get('title')} for {currency}. actual {actual} vs forecast {forecast}. {result_word}. {impact}. {surprise_context}. {price_line}. Liquidity: {get_liquidity_context()}. 3 lines plain text: what happened, M1 reaction to expect, trade now or wait. Address Joshua and Mascu."""

                ai = ask_claude(prompt)
                result_icon = "🟢" if beat else "🔴" if beat is False else "⚪"
                msg = (
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"{result_icon} {event.get('title')} · {currency} · {result_word}\n"
                    f"actual {actual} · forecast {forecast} · prev {previous}\n"
                    f"{impact}\n"
                    f"{price_line}\n"
                    f"{ai}"
                )
                send_telegram(msg)

                threading.Thread(
                    target=send_news_debrief,
                    args=(event.get("title"), currency, beat, impact),
                    daemon=True
                ).start()

    except Exception as e:
        print(f"News result error: {e}", flush=True)

def send_news_debrief(title, currency, beat, impact):
    time.sleep(1800)
    if is_weekend():
        return
    lt, suffix = london_time()
    eur_data = get_yahoo_price("EUR/USD")
    gbp_data = get_yahoo_price("GBP/USD")
    price_line = f"EUR/USD {eur_data['price']} · GBP/USD {gbp_data['price']}" if eur_data and gbp_data else ""

    prompt = f"""30 mins after {title} for {currency}. Expected: {impact}. Current: {price_line}. Liquidity: {get_liquidity_context()}. 3 lines plain text: did price follow expected direction or reverse, where it is now relative to key levels, still a setup or done. Address Joshua and Mascu."""

    ai = ask_claude(prompt)
    msg = f"━━━━━━━━━━━━━━━━━━\n{title} · 30 mins later\n{price_line}\n{ai}"
    send_telegram(msg)

def check_accumulation():
    if is_weekend() or is_blackout():
        return
    session = get_session()
    if not session or "Asia" in session or "lunch" in session:
        return

    lt, suffix = london_time()

    for pair in ["EUR/USD", "GBP/USD"]:
        data = get_yahoo_price(pair)
        if not data:
            continue

        price = data["price"]

        if pair not in accumulation_tracking:
            accumulation_tracking[pair] = {"prices": [], "start_time": lt, "alerted": False}

        tracker = accumulation_tracking[pair]
        tracker["prices"].append(price)
        if len(tracker["prices"]) > 20:
            tracker["prices"].pop(0)

        if len(tracker["prices"]) >= 15:
            price_range = (max(tracker["prices"]) - min(tracker["prices"])) * 10000
            time_elapsed = (lt - tracker["start_time"]).seconds / 60

            if price_range <= 5 and time_elapsed >= 15 and not tracker["alerted"]:
                tracker["alerted"] = True
                liq = get_liquidity_context()
                location = "discount" if asia_range["low"] and price <= asia_range["low"] + 0.0010 else "premium" if asia_range["high"] and price >= asia_range["high"] - 0.0010 else "mid range"

                prompt = f"""{pair} ranging {price_range:.1f} pips for {int(time_elapsed)} mins. Price: {price}. Location: {location}. Session: {session}. Liquidity: {liq}. 3 lines plain text: looks like accumulation, which direction breakout more likely, what M1 signal to wait for. Address Joshua and Mascu."""

                ai = ask_claude(prompt)
                msg = f"━━━━━━━━━━━━━━━━━━\n{pair} tight · {price_range:.1f}p range · {int(time_elapsed)}mins · {location}\n{ai}"
                send_telegram(msg)
        else:
            tracker["alerted"] = False

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
        prompt = f"""Asia just closed. EUR/USD range high {asia_range['high']} low {asia_range['low']} — {rng} pips. Liquidity: {get_liquidity_context()}. 3 lines plain text: which side has more liquidity, which level London likely sweeps first at 08:00, London bias. Address Joshua and Mascu."""
        ai = ask_claude(prompt)
        msg = f"━━━━━━━━━━━━━━━━━━\nAsia done · H:{asia_range['high']} L:{asia_range['low']} · {rng}p\n{ai}"
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
                direction = "dropping" if change_pct < 0 else "rising"
                impact = "EUR/GBP bullish · gold up" if change_pct < 0 else "EUR/GBP bearish · gold down"
                prompt = f"""DXY moved {change_pct:.2f}% — dollar {direction}. DXY: {current_price}. Session: {session}. 2 lines plain text: impact on EUR/USD GBP/USD, M1 reaction to expect. Address Joshua and Mascu."""
                ai = ask_claude(prompt)
                msg = f"━━━━━━━━━━━━━━━━━━\nDXY {current_price} · {'+' if change_pct > 0 else ''}{change_pct:.2f}% · dollar {direction}\n{impact}\n{ai}"
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
    asia_ctx = ""
    if asia_range["high"] and asia_range["low"]:
        if eur_data["price"] < asia_range["low"]:
            asia_ctx = f"asia low swept {asia_range['low']}"
        elif eur_data["price"] > asia_range["high"]:
            asia_ctx = f"asia high swept {asia_range['high']}"
        else:
            asia_ctx = "asia range intact"
    if divergence >= 5 or "swept" in asia_ctx:
        last_hourly[key] = True
        prompt = f"""London killzone. EUR {eur_data['price']} moved {eur_move:.1f}p. GBP {gbp_data['price']} moved {gbp_move:.1f}p. Divergence {divergence:.1f}p. {asia_ctx}. Liquidity: {get_liquidity_context()}. 3 lines plain text: Judas forming or not, direction, M1 setup and kill score. Address Joshua and Mascu."""
        ai = ask_claude(prompt)
        msg = (
            f"━━━━━━━━━━━━━━━━━━\n"
            f"killzone · {lt.strftime('%H:%M')} {suffix}\n"
            f"EUR {eur_data['price']} {'+' if eur_move > 0 else ''}{eur_move:.1f}p · GBP {gbp_data['price']} {'+' if gbp_move > 0 else ''}{gbp_move:.1f}p · {asia_ctx}\n"
            f"{ai}"
        )
        send_telegram(msg)

def send_session_summary():
    if is_weekend():
        return
    lt, suffix = london_time()
    summaries = [(13, "London"), (21, "NY")]
    for h, label in summaries:
        key = f"summary-{lt.strftime('%Y-%m-%d')}-{h}"
        if lt.hour == h and lt.minute < 5 and key not in last_hourly:
            last_hourly[key] = True
            prices = get_all_prices()
            if not prices:
                return
            price_line = " · ".join([f"{'🟢' if prices[p]['change'] > 0 else '🔴'} {p.split('/')[0]} {prices[p]['price']}" for p in prices])
            data_text = " | ".join([f"{p}: {prices[p]['price']}" for p in prices])
            prompt = f"""{label} session closed. Prices: {data_text}. Liquidity: {get_liquidity_context()}. 3 lines plain text: how session went, biggest mover, bias for next session. Address Joshua and Mascu."""
            ai = ask_claude(prompt)
            msg = f"━━━━━━━━━━━━━━━━━━\n{label} done · {lt.strftime('%H:%M')} {suffix}\n{price_line}\n{ai}"
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
                    week_events.append(f"{et_london.strftime('%a %d %H:%M')} · {e.get('currency')} {e.get('title')}")
                except:
                    pass
    except:
        week_events = []
    price_text = " | ".join([f"{p}: {prices[p]['price']}" for p in prices]) if prices else "unavailable"
    news_text = "\n".join(week_events[:10]) if week_events else "nothing major"
    prompt = f"""Weekly outlook {lt.strftime('%d %b %Y')}. Prices: {price_text}. Key events: {news_text}. 4 lines plain text: weekly bias EUR/USD GBP/USD, key levels, most important event, main setup to watch. Address Joshua and Mascu."""
    ai = ask_claude(prompt)
    price_line = " · ".join([f"{'🟢' if prices[p]['change'] > 0 else '🔴'} {p.split('/')[0]} {prices[p]['price']}" for p in prices]) if prices else ""
    news_lines = "\n".join([f"🔴 {e}" for e in week_events]) if week_events else "nothing major this week"
    msg = (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"week of {lt.strftime('%d %b %Y')}\n"
        f"{price_line}\n\n"
        f"red folders:\n{news_lines}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{ai}"
    )
    send_telegram(msg)

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
                surprise_ctx = get_surprise_history_text(event.get("currency", "USD"))
                prompt = f"""Red folder in {int(mins_until)} mins: {event.get('title')} for {event.get('currency')}. forecast {event.get('forecast', 'N/A')} prev {event.get('previous', 'N/A')}. {surprise_ctx}. 3 lines plain text: what this does to EUR/USD GBP/USD, beat vs miss outcome, trade or sit out. Address Joshua and Mascu."""
                ai = ask_claude(prompt)
                msg = (
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"⚠️ {event.get('title')} · {event.get('currency')} · {event_london.strftime('%H:%M')} {suffix}\n"
                    f"F:{event.get('forecast', 'N/A')} P:{event.get('previous', 'N/A')}\n"
                    f"{surprise_ctx}\n"
                    f"{ai}"
                )
                send_telegram(msg)
    except Exception as e:
        print(f"News error: {e}", flush=True)

def check_hourly_bias():
    global last_hourly
    if is_weekend():
        return
    session = get_session()
    if not session or "lunch" in session:
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

    price_line = " · ".join([f"{'🟢' if prices[p]['change'] > 0 else '🔴'} {p.split('/')[0]} {prices[p]['price']}" for p in prices])
    mtf_line = f"EUR H1:{mtf_eur.get('H1','?')} M15:{mtf_eur.get('M15','?')} · GBP H1:{mtf_gbp.get('H1','?')} M15:{mtf_gbp.get('M15','?')}"
    dxy_line = f"DXY {dxy_data['price']} {'📉' if dxy_data['change'] < 0 else '📈'}" if dxy_data else ""
    data_text = " | ".join([f"{p}: {prices[p]['price']} ({'up' if prices[p]['change'] > 0 else 'down'})" for p in prices])

    prompt = f"""Hourly bias {lt.strftime('%H:%M')} {suffix}. Session: {session}. Prices: {data_text}. MTF: {mtf_line}. DXY: {dxy_data['price'] if dxy_data else 'N/A'}. Liquidity: {get_liquidity_context()}. 3 lines plain text: overall bias and strongest/weakest pair, SMT context if any, one thing to watch this hour. Address Joshua and Mascu."""
    ai = ask_claude(prompt)

    msg = (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{lt.strftime('%H:00')} {suffix} · {session}\n"
        f"{price_line}\n"
        f"{mtf_line} · {dxy_line}\n"
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
                        today_events.append(f"{et_london.strftime('%H:%M')} · {e.get('currency')} {e.get('title')} · F:{e.get('forecast', 'N/A')}")
                except:
                    pass
    except:
        today_events = []

    price_text = " | ".join([f"{p}: {prices[p]['price']}" for p in prices]) if prices else "unavailable"
    news_text = " · ".join(today_events) if today_events else "nothing major"
    mtf_context = f"EUR H1:{mtf_eur.get('H1','?')} M15:{mtf_eur.get('M15','?')} · GBP H1:{mtf_gbp.get('H1','?')} M15:{mtf_gbp.get('M15','?')}"

    prompt = f"""Morning brief {lt.strftime('%A %d %b')} 07:00 {suffix}. Prices: {price_text}. MTF: {mtf_context}. Liquidity: {get_liquidity_context()}. News today: {news_text}. 4 lines plain text: good morning to Joshua and Mascu, H1 M15 bias, key levels and Asia range, main London 08:00 setup, news warning if any."""
    ai = ask_claude(prompt)

    price_line = " · ".join([f"{'🟢' if prices[p]['change'] > 0 else '🔴'} {p.split('/')[0]} {prices[p]['price']}" for p in prices]) if prices else ""
    mtf_line = f"EUR H1:{mtf_eur.get('H1','?')} M15:{mtf_eur.get('M15','?')} · GBP H1:{mtf_gbp.get('H1','?')} M15:{mtf_gbp.get('M15','?')}"
    asia_line = f"Asia H:{asia_range['high']} L:{asia_range['low']}" if asia_range["high"] else ""
    liq_line = f"PDH:{liquidity_levels['pdh']} PDL:{liquidity_levels['pdl']}" if liquidity_levels["pdh"] else ""
    news_lines = "\n".join([f"🔴 {e}" for e in today_events]) if today_events else "✅ nothing major today"

    msg = (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{lt.strftime('%a %d %b')} · 07:00 {suffix}\n"
        f"{price_line}\n"
        f"{mtf_line}\n"
        f"{asia_line} · {liq_line}\n\n"
        f"today:\n{news_lines}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{ai}"
    )
    send_telegram(msg)

def check_session_countdown():
    if is_weekend():
        return
    lt, suffix = london_time()
    countdowns = [
        (23, 45, "Asia", f"00:00 {suffix}"),
        (6, 45, "Frankfurt", f"07:00 {suffix}"),
        (7, 45, "London", f"08:00 {suffix}"),
        (14, 15, "NY", f"14:30 {suffix}"),
    ]
    for h, m, label, open_time in countdowns:
        key = f"countdown-{lt.strftime('%Y-%m-%d')}-{h}-{m}"
        if lt.hour == h and lt.minute >= m and lt.minute < m + 5 and key not in last_hourly:
            last_hourly[key] = True
            prices = get_all_prices()
            price_line = " · ".join([f"{'🟢' if prices[p]['change'] > 0 else '🔴'} {p.split('/')[0]} {prices[p]['price']}" for p in prices]) if prices else ""
            price_text = " | ".join([f"{p}: {prices[p]['price']}" for p in prices]) if prices else "unavailable"
            prompt = f"""15 mins to {label} open at {open_time}. Prices: {price_text}. Liquidity: {get_liquidity_context()}. 3 lines plain text: expected bias, which level likely gets swept, M1 setup. Address Joshua and Mascu."""
            ai = ask_claude(prompt)
            msg = f"━━━━━━━━━━━━━━━━━━\n15 mins to {label} · {open_time}\n{price_line}\n{ai}"
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
            prompt = f"""SMT divergence: EUR/USD {eur_move:.1f}p · GBP/USD {gbp_move:.1f}p. Gap {divergence:.1f}p. {weaker} weaker. Session: {session}. Liquidity: {get_liquidity_context()}. 3 lines plain text: meaningful or noise, M1 setup if valid, which pair and direction. Address Joshua and Mascu."""
            ai = ask_claude(prompt)
            msg = (
                f"━━━━━━━━━━━━━━━━━━\n"
                f"SMT · EUR/USD {'+' if eur_move > 0 else ''}{eur_move:.1f}p · GBP/USD {'+' if gbp_move > 0 else ''}{gbp_move:.1f}p · {divergence:.1f}p gap\n"
                f"{weaker} weak · {stronger} strong\n"
                f"{ai}"
            )
            send_telegram(msg)

def send_guide(chat_id):
    guide = (
        "how to read news results\n\n"
        "🟢 beat — actual better than forecast\n"
        "🔴 miss — actual worse than forecast\n"
        "⚪ in line — matched forecast\n\n"
        "USD news:\n"
        "beat = dollar stronger → EUR/GBP drops\n"
        "miss = dollar weaker → EUR/GBP pumps\n\n"
        "EUR/GBP news:\n"
        "beat = that pair pumps\n"
        "miss = that pair drops\n\n"
        "reading the numbers:\n"
        "F = forecast (what was expected)\n"
        "P = previous (last time it printed)\n"
        "actual > forecast = beat 🟢\n"
        "actual < forecast = miss 🔴\n\n"
        "surprise history ✅❌⚪:\n"
        "tracks last 5 releases for that currency\n"
        "lots of ❌ = data trend weakening\n"
        "lots of ✅ = data trend strengthening\n\n"
        "don't trade the spike\n"
        "wait 5-15 mins for it to exhaust\n"
        "ponk ponk sends a debrief 30 mins later"
    )
    send_telegram(guide, chat_id)

def handle_natural_message(text, chat_id, from_id=None):
    text_lower = text.lower().strip()
    user_name = get_user_name(from_id, chat_id)
    is_group = str(chat_id) == GROUP_ID
    other_trader = "Mascu" if user_name == "Joshua" else "Joshua" if user_name == "Mascu" else None

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
        prices = get_all_prices()
        dxy_data = get_yahoo_price("DXY")

    lt, suffix = london_time()
    session = get_session() or ("weekend" if is_weekend() else "off hours")
    price_context = ""
    if prices:
        price_context = " | ".join([
            f"{p}: {prices[p]['price']} ({'up' if prices[p]['change'] > 0 else 'down'} {abs(prices[p]['change'] * (100 if 'XAU' in p or 'XAG' in p else 10000)):.1f}p)"
            for p in prices
        ])
        if dxy_data:
            price_context += f" | DXY: {dxy_data['price']}"

    name_context = f"talking to {user_name}." if user_name else "unknown trader."
    group_context = f"group chat — {other_trader} is also here." if is_group and other_trader else ""

    if is_trade_entry:
        prompt = f"""{name_context} {group_context} they just said: "{text}". time: {lt.strftime('%H:%M')} {suffix}. session: {session}. {price_context}. liquidity: {get_liquidity_context()}. respond like a trading mate texting — no dividers, 4 lines max: react to the entry, quick SMC read, setup grade A/B/C, kill score /10, one line real psychology."""
    elif is_psychology:
        prompt = f"""{name_context} {group_context} they said: "{text}". respond like a trading mate texting — no dividers, 3 lines: honest support, real advice, keep it human."""
    else:
        prompt = f"""{name_context} {group_context} they say: "{text}". time: {lt.strftime('%H:%M')} {suffix}. session: {session}. {price_context}. liquidity: {get_liquidity_context()}. respond like a trading mate texting — no dividers, 2-3 lines max. use their name. answer directly. if it's a greeting just say hey back and ask what they're watching."""

    ai = ask_claude(prompt, use_history=True, chat_id=chat_id)
    send_telegram(ai, chat_id)

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
            user_name = get_user_name(from_id, chat_id)
            is_group = chat_id == GROUP_ID
            other_trader = "Mascu" if user_name == "Joshua" else "Joshua" if user_name == "Mascu" else None

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
                session = get_session() or "off hours"
                is_trade = any(kw in caption.lower() for kw in ["entered", "in trade", "long", "short", "i'm in", "took"]) if caption else False
                name_ctx = f"this is {user_name}." if user_name else ""
                group_ctx = f"group chat — {other_trader} is watching." if is_group and other_trader else ""

                if is_trade:
                    prompt = f"""{name_ctx} {group_ctx} they sent a chart and said: "{caption}". time: {lt.strftime('%H:%M')} {suffix}. session: {session}. liquidity: {get_liquidity_context()}. respond like a trading mate texting — no dividers, 6 lines max: react to entry, SMC analysis — structure sweeps FVGs OBs SMT if two charts, entry SL TP with prices, setup grade A/B/C, kill score /10, one line psychology."""
                else:
                    prompt = f"""{name_ctx} {group_ctx} they sent a chart{' saying: ' + caption if caption else ''}. time: {lt.strftime('%H:%M')} {suffix}. session: {session}. liquidity: {get_liquidity_context()}. respond like a trading mate texting — no dividers, 6 lines max: quick reaction, pair and timeframe, structure BOS/CHOCH, sweeps of equal highs/lows PDH/PDL Asia range, FVGs and OBs with prices, SMT if two charts, entry SL TP, setup grade A/B/C, kill score /10."""

                send_telegram("looking at this...", chat_id)
                ai = ask_claude(prompt, img_b64, chat_id=chat_id)
                send_telegram(ai, chat_id)

            elif text:
                cmd = text.lower().strip()

                if cmd == "/prices":
                    lt, suffix = london_time()
                    prices = get_all_prices()
                    dxy_data = get_yahoo_price("DXY")
                    msg = f"prices · {lt.strftime('%H:%M')} {suffix}\n\n"
                    for pair in PAIRS:
                        if pair in prices:
                            d = "🟢" if prices[pair]["change"] > 0 else "🔴"
                            cp = prices[pair]["change"] * (100 if "XAU" in pair or "XAG" in pair else 10000)
                            msg += f"{d} {pair} {prices[pair]['price']} ({'+' if cp > 0 else ''}{cp:.1f}p)\n"
                        else:
                            msg += f"· {pair} unavailable\n"
                    if dxy_data:
                        dc = dxy_data["change"] * 100
                        msg += f"\n{'🟢' if dxy_data['change'] > 0 else '🔴'} DXY {dxy_data['price']} ({'+' if dc > 0 else ''}{dc:.2f}%)"
                    liq = get_liquidity_context()
                    if liq:
                        msg += f"\n\n{liq}"
                    if asia_range["high"]:
                        msg += f"\nAsia H:{asia_range['high']} L:{asia_range['low']}"
                    send_telegram(msg, chat_id)

                elif cmd == "/bias":
                    handle_natural_message("what is the H1 M15 M1 bias on EUR and GBP right now", chat_id, from_id)

                elif cmd == "/levels":
                    lt, suffix = london_time()
                    msg = f"levels · {lt.strftime('%H:%M')} {suffix}\n\n"
                    if liquidity_levels["pwh"]:
                        msg += f"PWH: {liquidity_levels['pwh']}\n"
                    if liquidity_levels["pwl"]:
                        msg += f"PWL: {liquidity_levels['pwl']}\n"
                    if liquidity_levels["pdh"]:
                        msg += f"PDH: {liquidity_levels['pdh']}\n"
                    if liquidity_levels["pdl"]:
                        msg += f"PDL: {liquidity_levels['pdl']}\n"
                    if asia_range["high"]:
                        msg += f"Asia H:{asia_range['high']} L:{asia_range['low']}\n"
                    eur_data = get_yahoo_price("EUR/USD")
                    if eur_data:
                        msg += f"\nEUR/USD now: {eur_data['price']}"
                    send_telegram(msg, chat_id)

                elif cmd == "/news":
                    lt, suffix = london_time()
                    now = datetime.now(timezone.utc)
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
                                        today_events.append(f"🔴 {et_london.strftime('%H:%M')} · {e.get('currency')} {e.get('title')} · F:{e.get('forecast','N/A')}")
                                except:
                                    pass
                        msg = "today's red folders:\n\n" + "\n".join(today_events) if today_events else "nothing major today"
                        send_telegram(msg, chat_id)
                    except:
                        send_telegram("can't fetch news right now", chat_id)

                elif cmd == "/guide":
                    send_guide(chat_id)

                elif cmd == "/brief":
                    lt, suffix = london_time()
                    last_hourly.pop(lt.strftime("%Y-%m-%d-brief"), None)
                    send_morning_brief()

                elif cmd == "/help":
                    lt, suffix = london_time()
                    send_telegram(
                        f"ponk ponk\n\n"
                        f"/prices · /bias · /levels · /news · /brief · /guide\n\n"
                        f"just talk normally\n"
                        f"send a chart for SMC read\n"
                        f"say you entered and i'll analyse it\n"
                        f"say you're nervous and i'll help\n\n"
                        f"sessions {suffix}:\n"
                        f"asia 00:00 · frankfurt 07:00 · london 08:00 · NY 14:30",
                        chat_id
                    )

                elif not cmd.startswith("/"):
                    handle_natural_message(text, chat_id, from_id)

    except Exception as e:
        print(f"Message handler error: {e}", flush=True)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"PONK PONK running")
    def log_message(self, format, *args):
        pass

def run_server():
    HTTPServer(("0.0.0.0", 10000), Handler).serve_forever()

threading.Thread(target=run_server, daemon=True).start()
time.sleep(5)

lt, suffix = london_time()
weekend_note = "markets closed. rest up." if is_weekend() else "london opens soon."
send_telegram(
    f"ponk ponk online\n"
    f"joshua · mascu · let's go\n\n"
    f"EUR · GBP · XAU · XAG\n"
    f"{lt.strftime('%H:%M')} {suffix} · {weekend_note}\n\n"
    f"/help for commands · /guide for news reading"
)

def message_loop():
    while True:
        handle_incoming_messages()
        time.sleep(5)

threading.Thread(target=message_loop, daemon=True).start()

cycle = 0
while True:
    if not is_weekend():
        check_order_flow_context()
        check_news()
        check_news_results()
        check_session_countdown()
        track_asia_range()
        check_london_killzone()
        send_session_summary()
        update_liquidity_levels()
        check_confluence_zones()
        check_accumulation()
        check_london_ny_handoff()
        if cycle % 2 == 0:
            check_dxy()
        if cycle % 6 == 0:
            check_hourly_bias()
            check_smart_money_narrative()
        send_morning_brief()
        if cycle % 3 == 0:
            check_correlation_breakdown()
        check_cot_report()
    send_weekly_outlook()
    cycle += 1
    time.sleep(60)
