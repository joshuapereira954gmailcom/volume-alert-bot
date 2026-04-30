import os
import time
import threading
import requests
import base64
import yfinance as yf
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

TWELVE_API_KEY = os.environ.get("TWELVE_API_KEY", "").strip()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip().replace("\n", "").replace("\r", "").replace(" ", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

EXTRA_CHAT_IDS = ["6975027359", "-1003884144346"]
GROUP_ID = "-1003884144346"

USER_NAMES = {
    "7305046289": "Joshua",
    "6975027359": "Mascu",
}

YAHOO_SYMBOLS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "XAU/USD": "GC=F",
    "XAG/USD": "SI=F",
    "US500": "ES=F",
    "US100": "NQ=F",
    "DXY": "DX-Y.NYB"
}

SMT_PAIRS = [
    ("EUR/USD", "GBP/USD"),
    ("XAU/USD", "XAG/USD"),
    ("US500", "US100")
]

last_hourly = {}
last_news_ids = set()
last_update_id = 0
conversation_history = {}
last_dxy = {"price": None}
liquidity_levels = {"pdh": None, "pdl": None, "pwh": None, "pwl": None}
news_results_sent = set()
news_surprise_history = {"USD": [], "EUR": [], "GBP": []}
smt_alerts_sent = set()

STRATEGY_CONTEXT = """You are PONK PONK — a sharp, funny trading assistant texting Joshua and Mascu. Both trade the Divergence Kill strategy — SMT divergence on EUR/USD vs GBP/USD, Gold vs Silver, US500 vs US100 on M1.

How you text:
- Short. Lowercase. WhatsApp style. Max 4 lines for chat.
- Genuinely funny and a dry roaster. One good line beats three average ones.
- Vary roasts. Never repeat the same joke.
- When market is moving be serious and direct.
- Address people by name always.
- Never use the word muppet. No caps for emphasis.
- Always give the real answer. Never sacrifice usefulness for banter.
- Psychology: funny but actually helpful. Roast the emotion, fix the problem.

Divergence Kill strategy:
- Watch EUR/USD vs GBP/USD, Gold vs Silver, US500 vs US100
- One pair sweeps major swing high or low, other fails to confirm = divergence
- Trade the lagging instrument that failed to follow
- Drop to M1, find FVG or OB from the sweep
- Wait for BOS candle to close, enter at close
- SL beyond OB or inducement zone
- TP at opposite major swing
- Min 3:1 RR. Only London and NY killzones.
- Never trade during high impact news

Format:
- Chat: plain text, no dividers, just talk
- Alerts: one divider at top, max 5 lines after
- No asterisks, no hashes, no markdown
- Pair names uppercase: EUR/USD, 1.1748
- Setup grade A/B/C and kill score /10 on charts"""

def london_time():
    now_utc = datetime.now(timezone.utc)
    offset = 1 if 3 < now_utc.month < 11 else 0
    london = now_utc + timedelta(hours=offset)
    return london, "BST" if offset == 1 else "GMT"

def is_weekend():
    lt, _ = london_time()
    return lt.weekday() >= 5

def get_session():
    lt, _ = london_time()
    if lt.weekday() >= 5:
        return None
    hour, minute = lt.hour, lt.minute
    if 8 <= hour < 13:
        return "London"
    if hour > 14 or (hour == 14 and minute >= 30):
        if hour < 21:
            return "NY"
    if 0 <= hour < 7:
        return "Asia"
    if hour == 7:
        return "Frankfurt"
    return None

def is_killzone():
    lt, _ = london_time()
    hour, minute = lt.hour, lt.minute
    if hour == 8 and minute <= 30:
        return True
    if hour == 14 and 30 <= minute <= 59:
        return True
    if hour == 15 and minute <= 30:
        return True
    return False

def is_blackout():
    lt, _ = london_time()
    hour, minute = lt.hour, lt.minute
    windows = [(9, 25, 9, 35), (13, 25, 13, 35), (14, 55, 15, 5)]
    for sh, sm, eh, em in windows:
        if (hour == sh and minute >= sm) or (hour == eh and minute <= em):
            return True
    return False

def get_user_name(from_id, chat_id):
    if from_id and str(from_id) in USER_NAMES:
        return USER_NAMES[str(from_id)]
    if chat_id and str(chat_id) in USER_NAMES:
        return USER_NAMES[str(chat_id)]
    return None

def get_tradingview_link(pair):
    symbols = {
        "EUR/USD": "EURUSD", "GBP/USD": "GBPUSD",
        "XAU/USD": "XAUUSD", "XAG/USD": "XAGUSD",
        "US500": "SPX500USD", "US100": "NAS100USD"
    }
    symbol = symbols.get(pair, "EURUSD")
    return f"https://www.tradingview.com/chart/?symbol=OANDA%3A{symbol}&interval=1"

def get_ff_calendar_url():
    lt, _ = london_time()
    return f"https://www.forexfactory.com/calendar?day={lt.strftime('%Y-%m-%d')}"

def send_telegram(message, chat_id=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    targets = [chat_id] if chat_id else [TELEGRAM_CHAT_ID] + EXTRA_CHAT_IDS
    for target in targets:
        try:
            requests.post(url, data={
                "chat_id": target,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true"
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
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_base64}})
        content.append({"type": "text", "text": prompt})
        messages = [{"role": "user", "content": content}]

    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 400,
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
        hist = ticker.history(period="5d", interval="1h")
        if hist.empty or len(hist) < 3:
            return None
        price = round(float(hist["Close"].iloc[-1]), 5)
        prev = round(float(hist["Close"].iloc[-2]), 5)
        high = round(float(hist["High"].iloc[-24:].max()), 5)
        low = round(float(hist["Low"].iloc[-24:].min()), 5)
        prev_day_high = round(float(hist["High"].iloc[-48:-24].max()), 5)
        prev_day_low = round(float(hist["Low"].iloc[-48:-24].min()), 5)
        return {
            "price": price,
            "change": price - prev,
            "high": high,
            "low": low,
            "pdh": prev_day_high,
            "pdl": prev_day_low
        }
    except Exception as e:
        print(f"Yahoo error {pair}: {e}", flush=True)
        return None

def get_yahoo_htf(pair, interval="1h", period="3d"):
    symbol = YAHOO_SYMBOLS.get(pair)
    if not symbol:
        return None
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period, interval=interval)
        if hist.empty or len(hist) < 3:
            return None
        closes = [round(float(x), 5) for x in hist["Close"].tolist()]
        return "bullish" if closes[-1] > closes[-3] else "bearish"
    except Exception as e:
        print(f"HTF error {pair}: {e}", flush=True)
        return None

def get_twelve_volume(pair):
    symbol_map = {
        "EUR/USD": "EUR/USD", "GBP/USD": "GBP/USD",
        "XAU/USD": "XAU/USD", "XAG/USD": "XAG/USD"
    }
    symbol = symbol_map.get(pair, "").replace("/", "")
    if not symbol:
        return None
    try:
        params = {"symbol": symbol, "interval": "1min", "outputsize": 20, "apikey": TWELVE_API_KEY}
        r = requests.get("https://api.twelvedata.com/time_series", params=params, timeout=10)
        data = r.json()
        if "values" not in data:
            return None
        candles = data["values"]
        latest_vol = float(candles[0].get("volume", 0))
        avg_vol = sum(float(c.get("volume", 0)) for c in candles[1:]) / (len(candles) - 1)
        return {"volume": latest_vol, "avg": avg_vol, "ratio": round(latest_vol / avg_vol, 1) if avg_vol > 0 else 0}
    except Exception as e:
        print(f"Twelve error {pair}: {e}", flush=True)
        return None

def supabase_request(method, endpoint, data=None):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    try:
        if method == "POST":
            r = requests.post(url, headers=headers, json=data, timeout=10)
        elif method == "GET":
            r = requests.get(url, headers=headers, timeout=10)
        elif method == "PATCH":
            r = requests.patch(url, headers=headers, json=data, timeout=10)
        return r.json()
    except Exception as e:
        print(f"Supabase error: {e}", flush=True)
        return None

def log_trade_from_chart(user_name, pair, direction, session, day, emotion, notes):
    lt, _ = london_time()
    trade = {
        "user_name": user_name,
        "pair": pair,
        "direction": direction,
        "entry": None,
        "sl": None,
        "tp": None,
        "sl_pips": None,
        "tp_pips": None,
        "rr": None,
        "result": "open",
        "session": session,
        "day_of_week": day,
        "notes": f"{emotion} — {notes}" if emotion else notes
    }
    result = supabase_request("POST", "trades", trade)
    if result and len(result) > 0:
        return result[0].get("id")
    return None

def close_trade_result(trade_id, result, pips=None):
    lt, _ = london_time()
    r_gained = round(pips / 15, 2) if pips else 0
    supabase_request("PATCH", f"trades?id=eq.{trade_id}", {
        "result": result,
        "pips_gained": pips,
        "r_gained": r_gained,
        "closed_at": lt.isoformat()
    })

def get_open_trades(user_name=None):
    endpoint = "trades?result=eq.open&order=created_at.desc"
    if user_name:
        endpoint += f"&user_name=eq.{user_name}"
    return supabase_request("GET", endpoint) or []

def get_all_trades(user_name=None, limit=100):
    endpoint = f"trades?order=created_at.desc&limit={limit}"
    if user_name:
        endpoint += f"&user_name=eq.{user_name}"
    return supabase_request("GET", endpoint) or []

def show_stats(chat_id, from_id):
    user_name = get_user_name(from_id, chat_id)
    trades = get_all_trades(user_name)
    closed = [t for t in trades if t["result"] in ["win", "loss"]]

    if len(closed) < 3:
        send_telegram(f"need at least 3 closed trades for stats. you have {len(closed)} so far.", chat_id)
        return

    wins = [t for t in closed if t["result"] == "win"]
    losses = [t for t in closed if t["result"] == "loss"]
    win_rate = round(len(wins) / len(closed) * 100, 1)
    total_pips = sum(t.get("pips_gained", 0) or 0 for t in closed)
    total_r = sum(t.get("r_gained", 0) or 0 for t in closed)

    day_stats = {}
    for t in closed:
        day = t.get("day_of_week", "Unknown")
        if day not in day_stats:
            day_stats[day] = {"wins": 0, "total": 0}
        day_stats[day]["total"] += 1
        if t["result"] == "win":
            day_stats[day]["wins"] += 1

    pair_stats = {}
    for t in closed:
        p = t.get("pair", "unknown")
        if p not in pair_stats:
            pair_stats[p] = {"wins": 0, "total": 0}
        pair_stats[p]["total"] += 1
        if t["result"] == "win":
            pair_stats[p]["wins"] += 1

    emotion_stats = {}
    for t in closed:
        notes = (t.get("notes") or "").lower()
        for emotion in ["confident", "fomo", "anxious", "nervous", "calm", "revenge", "unsure"]:
            if emotion in notes:
                if emotion not in emotion_stats:
                    emotion_stats[emotion] = {"wins": 0, "total": 0}
                emotion_stats[emotion]["total"] += 1
                if t["result"] == "win":
                    emotion_stats[emotion]["wins"] += 1

    best_day = max(day_stats.items(), key=lambda x: x[1]["wins"] / x[1]["total"] if x[1]["total"] >= 2 else 0)
    worst_day = min(day_stats.items(), key=lambda x: x[1]["wins"] / x[1]["total"] if x[1]["total"] >= 2 else 1)
    pair_breakdown = " · ".join([f"{p}: {v['wins']}/{v['total']}" for p, v in pair_stats.items()])
    emotion_breakdown = " · ".join([f"{e}: {v['wins']}/{v['total']}" for e, v in emotion_stats.items()])

    prompt = f"""Stats for {user_name}. {len(closed)} trades. Win rate {win_rate}%. {len(wins)}W {len(losses)}L. Pips: {total_pips:.0f}. R: {total_r:.1f}. Best day: {best_day[0]}. Worst: {worst_day[0]}. Pairs: {pair_breakdown}. Emotions: {emotion_breakdown}. As PONK PONK — 5 lines: what the stats reveal about their trading, what their edge is, emotion patterns, what to stop doing, one specific improvement. Honest, useful, light roast."""
    ai = ask_claude(prompt)

    msg = f"stats · {user_name} · {len(closed)} trades\n\n"
    msg += f"win rate: {win_rate}% · {len(wins)}W {len(losses)}L\n"
    msg += f"pips: {'+' if total_pips > 0 else ''}{total_pips:.0f} · R: {'+' if total_r > 0 else ''}{total_r:.1f}R\n"
    msg += f"best day: {best_day[0]} · worst: {worst_day[0]}\n"
    msg += f"pairs: {pair_breakdown}\n"
    if emotion_breakdown:
        msg += f"emotions: {emotion_breakdown}\n"
    msg += f"\n{ai}"
    send_telegram(msg, chat_id)

def show_psychology(chat_id, from_id):
    user_name = get_user_name(from_id, chat_id)
    trades = get_all_trades(user_name)
    closed = [t for t in trades if t["result"] in ["win", "loss"]]

    if len(closed) < 5:
        send_telegram(f"need at least 5 closed trades. you have {len(closed)}.", chat_id)
        return

    consecutive_losses = 0
    max_streak = 0
    revenge_count = 0
    closed_reversed = list(reversed(closed))

    for i, t in enumerate(closed_reversed):
        if t["result"] == "loss":
            consecutive_losses += 1
            max_streak = max(max_streak, consecutive_losses)
            if i > 0:
                prev = closed_reversed[i - 1]
                if prev["result"] == "loss":
                    try:
                        prev_time = datetime.fromisoformat(prev["created_at"].replace("Z", "+00:00"))
                        curr_time = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))
                        if (curr_time - prev_time).seconds < 1800:
                            revenge_count += 1
                    except:
                        pass
        else:
            consecutive_losses = 0

    emotion_win_rates = {}
    for t in closed:
        notes = (t.get("notes") or "").lower()
        for emotion in ["confident", "fomo", "anxious", "nervous", "calm", "revenge", "unsure"]:
            if emotion in notes:
                if emotion not in emotion_win_rates:
                    emotion_win_rates[emotion] = {"wins": 0, "total": 0}
                emotion_win_rates[emotion]["total"] += 1
                if t["result"] == "win":
                    emotion_win_rates[emotion]["wins"] += 1

    friday_trades = [t for t in closed if t.get("day_of_week") == "Friday"]
    friday_wins = len([t for t in friday_trades if t["result"] == "win"])
    friday_rate = round(friday_wins / len(friday_trades) * 100) if friday_trades else 0

    emotion_text = " · ".join([
        f"{e}: {round(v['wins']/v['total']*100)}% win rate ({v['total']} trades)"
        for e, v in emotion_win_rates.items() if v["total"] >= 2
    ])

    prompt = f"""Psychology deep dive for {user_name}. {len(closed)} trades. Max losing streak: {max_streak}. Possible revenge trades: {revenge_count}. Friday win rate: {friday_rate}% ({len(friday_trades)} trades). Emotion patterns: {emotion_text if emotion_text else 'not enough emotion data yet'}. As PONK PONK — 6 lines: honest deep psychology insight, what mental patterns are costing them money, what emotional state they trade best in, what to do when on a losing streak, one rule to follow. Be real and useful."""
    ai = ask_claude(prompt)

    msg = f"psychology · {user_name}\n\n"
    msg += f"max losing streak: {max_streak}\n"
    msg += f"possible revenge trades: {revenge_count}\n"
    msg += f"friday win rate: {friday_rate}%\n"
    if emotion_text:
        msg += f"\nemotion breakdown:\n"
        for e, v in emotion_win_rates.items():
            if v["total"] >= 2:
                rate = round(v["wins"] / v["total"] * 100)
                msg += f"{e}: {rate}% win rate · {v['total']} trades\n"
    msg += f"\n{ai}"
    send_telegram(msg, chat_id)

def get_confluence_read(chat_id, from_id, pair_focus=None):
    user_name = get_user_name(from_id, chat_id)
    send_telegram("checking everything now...", chat_id)

    lt, suffix = london_time()
    session = get_session() or "off hours"

    all_data = {}
    for pair in ["EUR/USD", "GBP/USD", "XAU/USD", "XAG/USD", "US500", "US100", "DXY"]:
        data = get_yahoo_price(pair)
        if data:
            all_data[pair] = data
        time.sleep(0.5)

    htf_data = {}
    for pair in ["EUR/USD", "GBP/USD", "XAU/USD", "XAG/USD"]:
        h1 = get_yahoo_htf(pair, "1h", "3d")
        m15 = get_yahoo_htf(pair, "15m", "2d")
        htf_data[pair] = {"H1": h1 or "unknown", "M15": m15 or "unknown"}
        time.sleep(0.5)

    vol_data = {}
    for pair in ["EUR/USD", "GBP/USD", "XAU/USD", "XAG/USD"]:
        vol = get_twelve_volume(pair)
        if vol:
            vol_data[pair] = vol
        time.sleep(1)

    dxy = all_data.get("DXY", {})
    dxy_bias = "dropping" if dxy.get("change", 0) < 0 else "rising"

    smt_findings = []
    for p1, p2 in SMT_PAIRS:
        d1 = all_data.get(p1)
        d2 = all_data.get(p2)
        if not d1 or not d2:
            continue
        move1 = d1["change"]
        move2 = d2["change"]
        pdh1 = d1.get("pdh")
        pdl1 = d1.get("pdl")
        price1 = d1["price"]
        price2 = d2["price"]

        swept_high = pdh1 and price1 > pdh1
        swept_low = pdl1 and price1 < pdl1
        p2_move_same = (move1 > 0 and move2 > 0) or (move1 < 0 and move2 < 0)
        divergence = abs(move1 - move2) > (move1 * 0.3 if move1 != 0 else 0.001)

        if swept_high and move2 < move1 * 0.5:
            smt_findings.append({
                "pair1": p1, "pair2": p2,
                "type": "bearish",
                "detail": f"{p1} swept PDH {pdh1} · {p2} failed to confirm",
                "trade": f"short {p2}"
            })
        elif swept_low and move2 > move1 * 0.5:
            smt_findings.append({
                "pair1": p1, "pair2": p2,
                "type": "bullish",
                "detail": f"{p1} swept PDL {pdl1} · {p2} failed to confirm",
                "trade": f"long {p2}"
            })
        elif divergence and not p2_move_same:
            weaker = p2 if abs(move2) < abs(move1) else p1
            direction = "long" if move1 > 0 else "short"
            smt_findings.append({
                "pair1": p1, "pair2": p2,
                "type": "divergence",
                "detail": f"{p1} and {p2} diverging · {weaker} lagging",
                "trade": f"{direction} {weaker}"
            })

    confluence_score = 0
    confluence_notes = []

    if smt_findings:
        confluence_score += 2
        confluence_notes.append(f"SMT confirmed on {smt_findings[0]['pair1']} vs {smt_findings[0]['pair2']}")

    if session in ["London", "NY"]:
        confluence_score += 1
        confluence_notes.append(f"active session: {session}")

    if is_killzone():
        confluence_score += 1
        confluence_notes.append("inside killzone window")

    for pair, vol in vol_data.items():
        if vol["ratio"] >= 2.0:
            confluence_score += 1
            confluence_notes.append(f"volume spike on {pair} — {vol['ratio']}x normal")
            break

    focus_pair = pair_focus or (smt_findings[0]["pair2"] if smt_findings else "EUR/USD")
    focus_data = all_data.get(focus_pair, {})
    focus_h1 = htf_data.get(focus_pair, {}).get("H1", "unknown")
    focus_m15 = htf_data.get(focus_pair, {}).get("M15", "unknown")

    if focus_h1 == focus_m15 and focus_h1 != "unknown":
        confluence_score += 1
        confluence_notes.append(f"H1 and M15 aligned {focus_h1} on {focus_pair}")

    now = datetime.now(timezone.utc)
    news_warning = ""
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=10)
        events = r.json()
        for event in events:
            if event.get("impact") != "High":
                continue
            try:
                et = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
                mins = (et - now).total_seconds() / 60
                if 0 <= mins <= 30:
                    news_warning = f"⚠️ {event.get('title')} in {int(mins)} mins — stay out"
                    confluence_score -= 2
                    confluence_notes.append("news incoming — reduces confidence")
                    break
            except:
                pass
    except:
        pass

    price_summary = " | ".join([f"{p}: {all_data[p]['price']}" for p in ["EUR/USD", "GBP/USD", "XAU/USD"] if p in all_data])
    htf_summary = " · ".join([f"{p} H1:{htf_data[p]['H1']} M15:{htf_data[p]['M15']}" for p in htf_data])
    vol_summary = " · ".join([f"{p} {v['ratio']}x vol" for p, v in vol_data.items()])
    smt_summary = " | ".join([s["detail"] for s in smt_findings]) if smt_findings else "no clear SMT divergence yet"
    trade_idea = smt_findings[0]["trade"] if smt_findings else "no clear trade yet"

    prompt = f"""Market confluence read for {user_name or 'Joshua and Mascu'}. Time: {lt.strftime('%H:%M')} {suffix}. Session: {session}.

Prices: {price_summary}
DXY: {dxy.get('price', 'N/A')} — {dxy_bias}
HTF bias: {htf_summary}
Volume: {vol_summary if vol_summary else 'no significant volume'}
SMT: {smt_summary}
Confluence score: {confluence_score}/6
Trade idea: {trade_idea}
{news_warning}

As PONK PONK — give a specific confluence read. Tell them:
1. What SMT divergence is forming if any and which pair to trade
2. What the volume and order flow is saying
3. What HTF bias confirms or denies
4. What to look for on M1 to confirm entry
5. Confidence level and any warnings

Plain text. 6 lines max. Address {user_name if user_name else 'them'} by name. Be specific and useful."""

    ai = ask_claude(prompt, use_history=True, chat_id=chat_id)

    tv1 = get_tradingview_link(focus_pair)
    corr = "GBP/USD" if focus_pair == "EUR/USD" else "EUR/USD" if focus_pair == "GBP/USD" else "XAG/USD" if focus_pair == "XAU/USD" else "XAU/USD"
    tv2 = get_tradingview_link(corr)

    price_line = " · ".join([f"{'🟢' if all_data[p]['change'] > 0 else '🔴'} {p.split('/')[0]} {all_data[p]['price']}" for p in ["EUR/USD", "GBP/USD", "XAU/USD"] if p in all_data])

    msg = f"{price_line}\n"
    msg += f"DXY {dxy.get('price', 'N/A')} {dxy_bias} · session: {session}\n"
    msg += f"SMT: {smt_summary[:80]}\n"
    msg += f"confluence: {confluence_score}/6\n"
    if news_warning:
        msg += f"{news_warning}\n"
    msg += f"\n{ai}\n\n"
    msg += f"<a href='{tv1}'>{focus_pair} chart</a> · <a href='{tv2}'>{corr} chart</a>"
    send_telegram(msg, chat_id)

def check_smt_divergence_scanner():
    if is_weekend() or is_blackout():
        return
    session = get_session()
    if session not in ["London", "NY"]:
        return

    lt, suffix = london_time()

    for p1, p2 in SMT_PAIRS:
        d1 = get_yahoo_price(p1)
        time.sleep(0.5)
        d2 = get_yahoo_price(p2)
        if not d1 or not d2:
            continue

        pdh1 = d1.get("pdh")
        pdl1 = d1.get("pdl")
        price1 = d1["price"]
        move1 = d1["change"]
        move2 = d2["change"]

        alert_key = None
        alert_msg = None

        if pdh1 and price1 > pdh1 and move2 < move1 * 0.4:
            alert_key = f"smt-bearish-{p1}-{p2}-{lt.strftime('%Y-%m-%d-%H')}"
            if alert_key not in smt_alerts_sent:
                vol = get_twelve_volume(p1)
                vol_line = f"volume {vol['ratio']}x normal at sweep" if vol and vol['ratio'] >= 1.5 else ""
                h1_p2 = get_yahoo_htf(p2, "1h", "3d")
                htf_line = f"{p2} H1 {h1_p2}" if h1_p2 else ""
                prompt = f"""Divergence Kill setup forming. {p1} swept PDH {pdh1}. {p2} failed to confirm. Bearish divergence. Session: {session}. {vol_line}. {htf_line}. Trade: short {p2}. As PONK PONK — 4 lines: confirm the divergence kill setup, what to look for on M1, where SL and TP go, kill score /10."""
                ai = ask_claude(prompt)
                tv1 = get_tradingview_link(p1)
                tv2 = get_tradingview_link(p2)
                alert_msg = (
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"divergence kill · {p1} vs {p2} · {lt.strftime('%H:%M')} {suffix}\n"
                    f"{p1} swept PDH {pdh1} ✅\n"
                    f"{p2} failed to confirm ❌ · trade: short {p2}\n"
                    f"{vol_line}\n"
                    f"{ai}\n\n"
                    f"<a href='{tv1}'>{p1} chart</a> · <a href='{tv2}'>{p2} chart</a>"
                )

        elif pdl1 and price1 < pdl1 and move2 > move1 * 0.4:
            alert_key = f"smt-bullish-{p1}-{p2}-{lt.strftime('%Y-%m-%d-%H')}"
            if alert_key not in smt_alerts_sent:
                vol = get_twelve_volume(p1)
                vol_line = f"volume {vol['ratio']}x normal at sweep" if vol and vol['ratio'] >= 1.5 else ""
                h1_p2 = get_yahoo_htf(p2, "1h", "3d")
                htf_line = f"{p2} H1 {h1_p2}" if h1_p2 else ""
                prompt = f"""Divergence Kill setup forming. {p1} swept PDL {pdl1}. {p2} failed to confirm. Bullish divergence. Session: {session}. {vol_line}. {htf_line}. Trade: long {p2}. As PONK PONK — 4 lines: confirm the divergence kill setup, what to look for on M1, where SL and TP go, kill score /10."""
                ai = ask_claude(prompt)
                tv1 = get_tradingview_link(p1)
                tv2 = get_tradingview_link(p2)
                alert_msg = (
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"divergence kill · {p1} vs {p2} · {lt.strftime('%H:%M')} {suffix}\n"
                    f"{p1} swept PDL {pdl1} ✅\n"
                    f"{p2} failed to confirm ❌ · trade: long {p2}\n"
                    f"{vol_line}\n"
                    f"{ai}\n\n"
                    f"<a href='{tv1}'>{p1} chart</a> · <a href='{tv2}'>{p2} chart</a>"
                )

        if alert_key and alert_msg and alert_key not in smt_alerts_sent:
            smt_alerts_sent.add(alert_key)
            send_telegram(alert_msg)

        time.sleep(1)

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
                currency = event.get("currency", "USD")

                beat = None
                try:
                    a_val = float(actual.replace("%", "").replace("K", "000").strip())
                    f_val = float(forecast.replace("%", "").replace("K", "000").strip())
                    beat = a_val > f_val
                except:
                    pass

                result_word = "beat" if beat else "miss" if beat is False else "in line"
                if currency == "USD":
                    impact = "dollar up · EUR/GBP dropping" if beat else "dollar down · EUR/GBP pumping"
                elif currency == "EUR":
                    impact = "EUR/USD bullish" if beat else "EUR/USD bearish"
                else:
                    impact = "GBP/USD bullish" if beat else "GBP/USD bearish"

                news_surprise_history[currency].append("beat" if beat else "miss" if beat is False else "inline")
                if len(news_surprise_history[currency]) > 10:
                    news_surprise_history[currency].pop(0)

                prompt = f"""News result: {event.get('title')} for {currency}. actual {actual} vs forecast {forecast}. {result_word}. {impact}. 3 lines plain text: what happened, M1 reaction to expect, trade now or wait. Address Joshua and Mascu."""
                ai = ask_claude(prompt)
                result_icon = "🟢" if beat else "🔴" if beat is False else "⚪"
                tv_eur = get_tradingview_link("EUR/USD")
                tv_gbp = get_tradingview_link("GBP/USD")
                msg = (
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"{result_icon} {event.get('title')} · {currency} · {result_word}\n"
                    f"actual {actual} · forecast {forecast} · prev {previous}\n"
                    f"{impact}\n"
                    f"{ai}\n"
                    f"<a href='{tv_eur}'>EUR chart</a> · <a href='{tv_gbp}'>GBP chart</a>"
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
    eur_data = get_yahoo_price("EUR/USD")
    gbp_data = get_yahoo_price("GBP/USD")
    price_line = f"EUR/USD {eur_data['price']} · GBP/USD {gbp_data['price']}" if eur_data and gbp_data else ""
    prompt = f"""30 mins after {title} for {currency}. Expected: {impact}. Current: {price_line}. 3 lines: did price follow expected direction or reverse, where it is now, still a setup or done. Address Joshua and Mascu."""
    ai = ask_claude(prompt)
    msg = f"━━━━━━━━━━━━━━━━━━\n{title} · 30 mins later\n{price_line}\n{ai}"
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
                currency = event.get("currency", "USD")
                if currency == "USD":
                    impact = "beat = dollar up · miss = dollar down"
                elif currency == "EUR":
                    impact = "beat = EUR pumps · miss = EUR drops"
                else:
                    impact = "beat = GBP pumps · miss = GBP drops"
                msg = (
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"⚠️ {event.get('title')} · {currency} · {event_london.strftime('%H:%M')} {suffix}\n"
                    f"F:{event.get('forecast', 'N/A')} P:{event.get('previous', 'N/A')}\n"
                    f"{impact}\n"
                    f"stay out until it clears\n"
                    f"<a href='{get_ff_calendar_url()}'>full calendar</a>"
                )
                send_telegram(msg)
    except Exception as e:
        print(f"News error: {e}", flush=True)

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
                        today_events.append({
                            "time": et_london.strftime("%H:%M"),
                            "currency": e.get("currency"),
                            "title": e.get("title"),
                            "forecast": e.get("forecast", "N/A"),
                            "previous": e.get("previous", "N/A")
                        })
                except:
                    pass
    except:
        today_events = []

    eur_data = get_yahoo_price("EUR/USD")
    gbp_data = get_yahoo_price("GBP/USD")
    xau_data = get_yahoo_price("XAU/USD")
    price_line = ""
    if eur_data and gbp_data and xau_data:
        price_line = f"EUR {eur_data['price']} · GBP {gbp_data['price']} · XAU {xau_data['price']}"

    news_lines = ""
    if today_events:
        for e in today_events:
            news_lines += f"🔴 {e['time']} · {e['currency']} {e['title']} · F:{e['forecast']} P:{e['previous']}\n"
    else:
        news_lines = "✅ nothing major today\n"

    calendar_url = get_ff_calendar_url()
    tv_eur = get_tradingview_link("EUR/USD")
    tv_gbp = get_tradingview_link("GBP/USD")

    greeting = f"morning Joshua and Mascu. {lt.strftime('%a %d %b')}." if not today_events else f"morning Joshua and Mascu. {lt.strftime('%a %d %b')}. {len(today_events)} red folder{'s' if len(today_events) > 1 else ''} today — plan around them."

    msg = (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"07:00 {suffix} · {lt.strftime('%a %d %b')}\n"
        f"{price_line}\n\n"
        f"{greeting}\n\n"
        f"today's news:\n{news_lines}\n"
        f"📅 <a href='{calendar_url}'>full calendar</a>\n"
        f"📊 <a href='{tv_eur}'>EUR chart</a> · <a href='{tv_gbp}'>GBP chart</a>"
    )
    send_telegram(msg)
    print("Morning brief sent", flush=True)

def handle_chart_photo(img_b64, caption, chat_id, from_id):
    user_name = get_user_name(from_id, chat_id) or "Unknown"
    lt, suffix = london_time()
    session = get_session() or "off hours"
    day = lt.strftime("%A")

    caption_lower = (caption or "").lower()
    direction = None
    if any(kw in caption_lower for kw in ["long", "buy", "bought"]):
        direction = "long"
    elif any(kw in caption_lower for kw in ["short", "sell", "sold"]):
        direction = "short"

    emotion = None
    for e in ["confident", "fomo", "anxious", "nervous", "calm", "revenge", "unsure", "excited", "scared"]:
        if e in caption_lower:
            emotion = e
            break

    prompt = f"""This is {user_name}. They sent a chart with caption: "{caption or 'no caption'}". Session: {session}. Day: {day}. Time: {lt.strftime('%H:%M')} {suffix}.

Analyse the chart as PONK PONK — no dividers, 7 lines max:
1. What pair and timeframe is this
2. Is there SMT divergence visible — if two charts shown, compare them
3. Structure — BOS/CHOCH, sweeps of PDH/PDL/equal highs/lows
4. FVGs and Order Blocks with price levels
5. Is this a valid Divergence Kill setup — grade A/B/C
6. Entry, SL, TP levels if visible
7. Kill score /10 and one line of psychology for {user_name}

If direction is {direction or 'unclear from chart'}, factor that in.
Emotion noted: {emotion or 'none stated'}.
Be specific. Be funny but useful."""

    send_telegram("looking at this...", chat_id)
    ai = ask_claude(prompt, img_b64, chat_id=chat_id)

    pair_detected = "EUR/USD"
    for p in ["EUR/USD", "GBP/USD", "XAU/USD", "XAG/USD", "US500", "US100"]:
        if p.lower().replace("/", "") in (caption or "").lower().replace("/", ""):
            pair_detected = p
            break

    if direction:
        trade_id = log_trade_from_chart(user_name, pair_detected, direction, session, day, emotion, caption or "")
        if trade_id:
            send_telegram(f"{ai}\n\nlogged as #{trade_id} · {direction} {pair_detected} · {session} · {day}", chat_id)
        else:
            send_telegram(ai, chat_id)
    else:
        send_telegram(ai, chat_id)

def handle_natural_message(text, chat_id, from_id=None):
    text_lower = text.lower().strip()
    user_name = get_user_name(from_id, chat_id)
    is_group = str(chat_id) == GROUP_ID
    other_trader = "Mascu" if user_name == "Joshua" else "Joshua" if user_name == "Mascu" else None

    is_close = any(kw in text_lower for kw in [
        "win", "won", "loss", "lost", "stopped out", "sl hit", "hit tp", "took profit"
    ])

    is_psychology = any(kw in text_lower for kw in [
        "nervous", "scared", "anxious", "worried", "stressed", "losing streak",
        "frustrated", "emotional", "should i close", "move my sl", "fomo", "revenge"
    ])

    is_read_request = any(kw in text_lower for kw in [
        "what should i do", "what do i do", "what should i watch", "what do i watch",
        "give me a read", "market read", "what's the play", "whats the play",
        "should i take", "confluence", "what are you seeing", "check the market",
        "is there a setup", "anything setting up", "what do you see"
    ])

    if is_close:
        import re
        numbers = re.findall(r'\d+', text)
        trade_id = int(numbers[0]) if numbers and int(numbers[0]) > 10 else None
        pips = float(numbers[1]) if len(numbers) > 1 else None
        result = "win" if any(kw in text_lower for kw in ["win", "won", "hit tp", "took profit"]) else "loss"

        if trade_id:
            close_trade_result(trade_id, result, pips)
            send_telegram(f"closed #{trade_id} as {result}. logged {user_name}.", chat_id)
        else:
            open_trades = get_open_trades(user_name)
            if open_trades:
                latest = open_trades[0]
                close_trade_result(latest["id"], result, pips)
                send_telegram(f"closed #{latest['id']} {latest['pair']} as {result}. logged.", chat_id)
            else:
                send_telegram("no open trades found. which trade number?", chat_id)
        return

    if is_read_request:
        get_confluence_read(chat_id, from_id)
        return

    if is_psychology:
        prompt = f"""talking to {user_name or 'a trader'}. they said: "{text}". respond like a funny trading mate — no dividers, 3 lines: roast the emotion lightly then give honest real advice about what to do right now."""
        ai = ask_claude(prompt, use_history=True, chat_id=chat_id)
        send_telegram(ai, chat_id)
        return

    lt, suffix = london_time()
    session = get_session() or ("weekend" if is_weekend() else "off hours")
    group_context = f"group chat — {other_trader} is also here." if is_group and other_trader else ""
    name_context = f"talking to {user_name}." if user_name else ""

    prompt = f"""{name_context} {group_context} they say: "{text}". time: {lt.strftime('%H:%M')} {suffix}. session: {session}. respond like a funny trading mate — no dividers, 2-3 lines. use their name. roast if appropriate. answer directly and usefully."""
    ai = ask_claude(prompt, use_history=True, chat_id=chat_id)
    send_telegram(ai, chat_id)

def send_guide(chat_id):
    guide = (
        "divergence kill — how to use this bot\n\n"
        "auto alerts:\n"
        "07:00 — morning brief with today's news\n"
        "15 mins before red folder — warning\n"
        "when news drops — result + impact\n"
        "30 mins after — debrief\n"
        "during london/NY — divergence kill scanner fires when conditions form\n\n"
        "send a chart:\n"
        "photo + caption with 'long' or 'short'\n"
        "add how you felt — 'confident' 'fomo' 'nervous' etc\n"
        "bot reads it, analyses the setup, logs the trade\n\n"
        "close a trade:\n"
        "type 'win' or 'loss' → closes latest\n"
        "type 'win #5' → closes trade 5\n\n"
        "commands:\n"
        "/stats · /psychology · /trades · /guide\n\n"
        "ask for a read:\n"
        "type 'give me a read' or 'is there a setup'\n"
        "bot checks everything — SMT, volume, HTF, news — and reports back"
    )
    send_telegram(guide, chat_id)

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

            if photo:
                file_id = photo[-1]["file_id"]
                file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
                file_info = requests.get(file_url).json()
                file_path = file_info["result"]["file_path"]
                download_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                img_bytes = requests.get(download_url).content
                img_b64 = base64.b64encode(img_bytes).decode("utf-8")
                caption = message.get("caption", "")
                handle_chart_photo(img_b64, caption, chat_id, from_id)

            elif text:
                cmd = text.lower().strip()

                if cmd == "/stats":
                    show_stats(chat_id, from_id)
                elif cmd == "/psychology":
                    show_psychology(chat_id, from_id)
                elif cmd == "/trades":
                    open_trades = get_open_trades(get_user_name(from_id, chat_id))
                    if not open_trades:
                        send_telegram("no open trades right now", chat_id)
                    else:
                        msg = f"open trades · {len(open_trades)}\n\n"
                        for t in open_trades:
                            msg += f"#{t['id']} {t['direction']} {t['pair']} · {t['session']} · {t['day_of_week']}\n"
                            if t.get("notes"):
                                msg += f"notes: {t['notes']}\n"
                            msg += "\n"
                        send_telegram(msg, chat_id)
                elif cmd == "/guide":
                    send_guide(chat_id)
                elif cmd == "/help":
                    lt, suffix = london_time()
                    send_telegram(
                        f"ponk ponk · divergence kill bot\n\n"
                        f"/stats · /psychology · /trades · /guide\n\n"
                        f"send a chart photo to log a trade\n"
                        f"type 'win' or 'loss' to close it\n"
                        f"type 'give me a read' for full confluence check\n\n"
                        f"auto alerts: morning brief · news warnings · news results · divergence kill setups\n\n"
                        f"sessions {suffix}: asia 00:00 · london 08:00 · NY 14:30",
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
send_telegram(
    f"ponk ponk online · divergence kill edition\n"
    f"joshua · mascu · let's get it\n\n"
    f"watching: EUR/GBP · Gold/Silver · US500/US100\n"
    f"{lt.strftime('%H:%M')} {suffix}\n\n"
    f"/guide to see how it works"
)

def message_loop():
    while True:
        handle_incoming_messages()
        time.sleep(5)

threading.Thread(target=message_loop, daemon=True).start()

cycle = 0
while True:
    if not is_weekend():
        check_news()
        check_news_results()
        send_morning_brief()
        if cycle % 3 == 0:
            check_smt_divergence_scanner()
    cycle += 1
    time.sleep(60)
