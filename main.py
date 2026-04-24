import os
import time
import threading
import requests
import base64
import hashlib
import sqlite3
import re
import yfinance as yf
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────

# ENV VARS

# ─────────────────────────────────────────────

TWELVE_API_KEY  = os.environ.get(“TWELVE_API_KEY”,  “”).strip()
TELEGRAM_TOKEN  = os.environ.get(“TELEGRAM_TOKEN”,  “”).strip().replace(”\n”,””).replace(”\r”,””).replace(” “,””)
TELEGRAM_CHAT_ID= os.environ.get(“TELEGRAM_CHAT_ID”,””).strip()
CLAUDE_API_KEY  = os.environ.get(“CLAUDE_API_KEY”,  “”).strip()

# ─────────────────────────────────────────────

# CONSTANTS

# ─────────────────────────────────────────────

PAIRS = [“EUR/USD”, “GBP/USD”, “XAU/USD”, “XAG/USD”]
YAHOO_SYMBOLS = {
“EUR/USD”: “EURUSD=X”,
“GBP/USD”: “GBPUSD=X”,
“XAU/USD”: “GC=F”,
“XAG/USD”: “SI=F”
}
SPIKE_MULTIPLIER  = 3.0
last_hourly       = {}
last_ff_headlines = set()
last_update_id    = 0
conversation_history = []

STRATEGY_CONTEXT = “”“You are the personal AI trading assistant for Joshua, an M1 forex trader based in London who uses Smart Money Concepts (SMC) and SMT divergence strategy.

Joshua’s trading style:

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
- If you need price data to answer, say you’re fetching it”””

# ─────────────────────────────────────────────

# DATABASE — SQLite journal + P&L tracker

# ─────────────────────────────────────────────

DB_PATH = “trades.db”

def init_db():
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
# Trade journal
c.execute(”””
CREATE TABLE IF NOT EXISTS trades (
id          INTEGER PRIMARY KEY AUTOINCREMENT,
timestamp   TEXT,
pair        TEXT,
direction   TEXT,
entry       REAL,
sl          REAL,
tp          REAL,
risk_r      REAL,
status      TEXT DEFAULT ‘open’,
result      TEXT,
pnl_r       REAL,
session     TEXT,
notes       TEXT,
chart_analysis TEXT
)
“””)
# Chart log — screenshots sent to bot
c.execute(”””
CREATE TABLE IF NOT EXISTS chart_logs (
id          INTEGER PRIMARY KEY AUTOINCREMENT,
timestamp   TEXT,
pair        TEXT,
timeframe   TEXT,
analysis    TEXT,
trade_id    INTEGER
)
“””)
conn.commit()
conn.close()
print(“DB initialised”, flush=True)

def db_log_trade(pair, direction, entry, sl, tp, session, notes=””):
risk_pips = abs(entry - sl)
reward_pips = abs(tp - entry)
risk_r = round(reward_pips / risk_pips, 2) if risk_pips > 0 else 0
lt, suffix = london_time()
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute(”””
INSERT INTO trades (timestamp, pair, direction, entry, sl, tp, risk_r, status, session, notes)
VALUES (?, ?, ?, ?, ?, ?, ?, ‘open’, ?, ?)
“””, (lt.strftime(”%Y-%m-%d %H:%M”), pair, direction.upper(), entry, sl, tp, risk_r, session or “Unknown”, notes))
trade_id = c.lastrowid
conn.commit()
conn.close()
return trade_id, risk_r

def db_close_trade(trade_id, result):
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute(“SELECT entry, sl, tp, direction FROM trades WHERE id=?”, (trade_id,))
row = c.fetchone()
if not row:
conn.close()
return None
entry, sl, tp, direction = row
risk_pips = abs(entry - sl)
if result.upper() == “WIN”:
pnl_r = round(abs(tp - entry) / risk_pips, 2) if risk_pips > 0 else 0
status = “closed_win”
elif result.upper() == “LOSS”:
pnl_r = -1.0
status = “closed_loss”
else:
pnl_r = 0
status = “closed_be”
lt, _ = london_time()
c.execute(”””
UPDATE trades SET status=?, result=?, pnl_r=? WHERE id=?
“””, (status, result.upper(), pnl_r, trade_id))
conn.commit()
conn.close()
return pnl_r

def db_log_chart(pair, timeframe, analysis, trade_id=None):
lt, _ = london_time()
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute(”””
INSERT INTO chart_logs (timestamp, pair, timeframe, analysis, trade_id)
VALUES (?, ?, ?, ?, ?)
“””, (lt.strftime(”%Y-%m-%d %H:%M”), pair or “Unknown”, timeframe or “M1”, analysis, trade_id))
conn.commit()
conn.close()

def db_get_stats():
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute(“SELECT COUNT(*) FROM trades WHERE status != ‘open’”)
total = c.fetchone()[0]
c.execute(“SELECT COUNT(*) FROM trades WHERE status=‘closed_win’”)
wins = c.fetchone()[0]
c.execute(“SELECT SUM(pnl_r) FROM trades WHERE status != ‘open’”)
total_r = c.fetchone()[0] or 0
c.execute(“SELECT * FROM trades WHERE status=‘open’”)
open_trades = c.fetchall()
conn.close()
wr = round((wins / total * 100), 1) if total > 0 else 0
return {“total”: total, “wins”: wins, “wr”: wr, “total_r”: round(total_r, 2), “open”: open_trades}

def db_get_open_trades():
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute(“SELECT id, pair, direction, entry, sl, tp, risk_r, timestamp FROM trades WHERE status=‘open’”)
rows = c.fetchall()
conn.close()
return rows

# ─────────────────────────────────────────────

# TIME HELPERS

# ─────────────────────────────────────────────

def london_time():
now_utc = datetime.now(timezone.utc)
offset  = 1 if 3 < now_utc.month < 11 else 0
london  = now_utc + timedelta(hours=offset)
suffix  = “BST” if offset == 1 else “GMT”
return london, suffix

def london_time_str():
lt, suffix = london_time()
return f”{lt.strftime(’%H:%M’)} {suffix}”

# ─────────────────────────────────────────────

# TELEGRAM

# ─────────────────────────────────────────────

def send_telegram(message, chat_id=None):
url = f”https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage”
try:
r = requests.post(url, data={
“chat_id”:    chat_id or TELEGRAM_CHAT_ID,
“text”:       message,
“parse_mode”: “HTML”
}, timeout=10)
print(f”Telegram: {r.status_code}”, flush=True)
except Exception as e:
print(f”Telegram error: {e}”, flush=True)

# ─────────────────────────────────────────────

# CLAUDE

# ─────────────────────────────────────────────

def ask_claude(prompt, image_base64=None, media_type=“image/jpeg”, use_history=False):
headers = {
“x-api-key”:         CLAUDE_API_KEY,
“anthropic-version”: “2023-06-01”,
“content-type”:      “application/json”
}
if use_history:
messages = conversation_history[-10:] + [{“role”: “user”, “content”: prompt}]
else:
content = []
if image_base64:
content.append({“type”: “image”, “source”: {“type”: “base64”, “media_type”: media_type, “data”: image_base64}})
content.append({“type”: “text”, “text”: prompt})
messages = [{“role”: “user”, “content”: content}]

```
body = {
    "model":      "claude-haiku-4-5-20251001",
    "max_tokens": 600,
    "system":     STRATEGY_CONTEXT,
    "messages":   messages
}
try:
    r    = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=30)
    data = r.json()
    resp = data["content"][0]["text"]
    resp = resp.replace("**","").replace("##","").replace("# ","")
    if use_history:
        conversation_history.append({"role": "user",      "content": prompt})
        conversation_history.append({"role": "assistant", "content": resp})
        if len(conversation_history) > 20:
            conversation_history.pop(0)
            conversation_history.pop(0)
    return resp
except Exception as e:
    print(f"Claude error: {e}", flush=True)
    return "Having trouble connecting to AI right now."
```

# ─────────────────────────────────────────────

# PRICES

# ─────────────────────────────────────────────

def get_yahoo_price(pair):
symbol = YAHOO_SYMBOLS.get(pair)
if not symbol:
return None
try:
ticker = yf.Ticker(symbol)
hist   = ticker.history(period=“2d”, interval=“1m”)
if hist.empty or len(hist) < 2:
return None
return {
“price”:      round(float(hist[“Close”].iloc[-1]), 5),
“prev_price”: round(float(hist[“Close”].iloc[-2]), 5),
“change”:     round(float(hist[“Close”].iloc[-1]) - float(hist[“Close”].iloc[-2]), 5),
“open”:       round(float(hist[“Open”].iloc[0]),  5),
“high”:       round(float(hist[“High”].max()),    5),
“low”:        round(float(hist[“Low”].min()),     5)
}
except Exception as e:
print(f”Yahoo error {pair}: {e}”, flush=True)
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
symbol = pair.replace(”/”, “”)
params = {“symbol”: symbol, “interval”: “1min”, “outputsize”: 30, “apikey”: TWELVE_API_KEY}
try:
r    = requests.get(“https://api.twelvedata.com/time_series”, params=params, timeout=10)
data = r.json()
if “values” not in data:
return None
candles    = data[“values”]
latest_vol = float(candles[0].get(“volume”, 0))
avg_vol    = sum(float(c.get(“volume”, 0)) for c in candles[1:21]) / 20
return {
“price”:      float(candles[0][“close”]),
“prev_price”: float(candles[5][“close”]),
“volume”:     latest_vol,
“avg_volume”: avg_vol,
}
except Exception as e:
print(f”Twelve error {pair}: {e}”, flush=True)
return None

# ─────────────────────────────────────────────

# SESSION

# ─────────────────────────────────────────────

def get_session():
lt, suffix = london_time()
h, m = lt.hour, lt.minute
active = []
if 0 <= h < 7:                          active.append(“Asia 🌏”)
if h == 7:                              active.append(“Frankfurt 🇩🇪”)
if 8 <= h < 13:                         active.append(“London 🇬🇧”)
if 13 <= h < 14 or (h == 14 and m < 30):active.append(“Lunch 😴”)
if h > 14 or (h == 14 and m >= 30):
if h < 21:                          active.append(“NY 🇺🇸”)
return “ + “.join(active) if active else None

def is_blackout():
lt, _ = london_time()
h, m  = lt.hour, lt.minute
for sh, sm, eh, em in [(9,25,9,35),(13,25,13,35),(14,55,15,5)]:
if (h == sh and m >= sm) or (h == eh and m <= em):
return True
return False

def is_active_session():
lt, _ = london_time()
h = lt.hour
return (7 <= h < 13) or (14 <= h < 21)

# ─────────────────────────────────────────────

# FF BREAKING NEWS — fixed scraper

# ─────────────────────────────────────────────

def _send_ff_alert(headline, url=””):
headers_api = {
“x-api-key”:         CLAUDE_API_KEY,
“anthropic-version”: “2023-06-01”,
“content-type”:      “application/json”
}
prompt = f””“Breaking headline on ForexFactory: “{headline}”

You are Joshua’s M1 SMC/SMT trading assistant in London. Respond in plain text, no markdown, 5 lines:

Line 1 - PAIRS: Which of EUR/USD, GBP/USD, XAU/USD, XAG/USD are directly affected and how
Line 2 - FLOW: Dollar direction — risk-on or risk-off, institutional bias
Line 3 - M1 REACTION: Expected sweep direction on M1, likely displacement, which side liquidity gets taken
Line 4 - SETUP: Trade or avoid. If trade — which pair, long or short, what to wait for before entry
Line 5 - CONTEXT: Session timing relevance and urgency level”””

```
body = {
    "model":    "claude-haiku-4-5-20251001",
    "max_tokens": 400,
    "system":   STRATEGY_CONTEXT,
    "messages": [{"role": "user", "content": prompt}]
}
try:
    r  = requests.post("https://api.anthropic.com/v1/messages", headers=headers_api, json=body, timeout=30)
    ai = r.json()["content"][0]["text"]
    ai = ai.replace("**","").replace("##","").replace("# ","")
except Exception as e:
    print(f"Claude FF error: {e}", flush=True)
    ai = "AI analysis unavailable."

lt, suffix = london_time()
session    = get_session() or "Off hours"
msg = f"🔴 <b>FF BREAKING NEWS</b>\n\n📰 <b>{headline}</b>\n"
if url:
    msg += f"🔗 {url}\n"
msg += f"\n🕐 {lt.strftime('%H:%M')} {suffix} | {session}\n\n🤖 <b>M1 REACTION ANALYSIS:</b>\n\n{ai}"
send_telegram(msg)
print(f"[FF] Alert sent: {headline}", flush=True)
```

def check_ff_breaking_news():
global last_ff_headlines
try:
headers = {
“User-Agent”:      “Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36”,
“Accept-Language”: “en-US,en;q=0.9”,
“Accept”:          “text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8”,
“Referer”:         “https://www.forexfactory.com/”,
}
r    = requests.get(“https://www.forexfactory.com/news”, headers=headers, timeout=10)
soup = BeautifulSoup(r.text, “html.parser”)

```
    articles = (
        soup.select("div.flexposts__story")
        or soup.select("div.ff-news-item")
        or soup.select("article.story")
        or soup.select("[class*='story']")
    )

    seen_this_run = set()

    if not articles:
        print("[FF] No containers found — falling back to link scrape", flush=True)
        links = soup.select("a[href*='/news/']")
        for link in links[:15]:
            headline = link.get_text(strip=True)
            if not headline or len(headline) < 10:
                continue
            item_id = hashlib.md5(headline.encode()).hexdigest()
            if item_id in last_ff_headlines or item_id in seen_this_run:
                continue
            seen_this_run.add(item_id)
            last_ff_headlines.add(item_id)
            if len(last_ff_headlines) > 200:
                last_ff_headlines = set(list(last_ff_headlines)[-100:])
            href = link.get("href", "")
            url  = href if href.startswith("http") else f"https://www.forexfactory.com{href}"
            _send_ff_alert(headline, url)
            time.sleep(2)
        return

    for article in articles[:15]:
        title_el = (
            article.select_one(".flexposts__story-title")
            or article.select_one(".ff-news__title")
            or article.select_one("h3")
            or article.select_one("h2")
            or article.select_one("a")
        )
        if not title_el:
            continue
        headline = title_el.get_text(strip=True)
        if not headline or len(headline) < 10:
            continue
        item_id = hashlib.md5(headline.encode()).hexdigest()
        if item_id in last_ff_headlines or item_id in seen_this_run:
            continue
        seen_this_run.add(item_id)
        last_ff_headlines.add(item_id)
        if len(last_ff_headlines) > 200:
            last_ff_headlines = set(list(last_ff_headlines)[-100:])
        link_el = article.select_one("a[href]")
        href    = link_el["href"] if link_el else ""
        url     = href if href.startswith("http") else f"https://www.forexfactory.com{href}"
        _send_ff_alert(headline, url)
        time.sleep(2)

except Exception as e:
    print(f"FF news error: {e}", flush=True)
```

# ─────────────────────────────────────────────

# RED FOLDER NEWS

# ─────────────────────────────────────────────

def check_news():
try:
url    = “https://nfs.faireconomy.media/ff_calendar_thisweek.json”
r      = requests.get(url, timeout=10)
events = r.json()
now    = datetime.now(timezone.utc)
lt, suffix = london_time()
for event in events:
if event.get(“impact”) != “High”:
continue
if event.get(“currency”) not in [“USD”,“EUR”,“GBP”]:
continue
event_id = str(event.get(“id”, event.get(“title”,””) + event.get(“date”,””)))
if event_id in last_hourly:
continue
try:
event_time = datetime.fromisoformat(event[“date”].replace(“Z”,”+00:00”))
except:
continue
mins_until = (event_time - now).total_seconds() / 60
if 0 <= mins_until <= 15:
last_hourly[event_id] = True
offset       = 1 if suffix == “BST” else 0
event_london = event_time + timedelta(hours=offset)
prompt = f””“Red folder news dropping in {int(mins_until)} mins: {event.get(‘title’)} for {event.get(‘currency’)}. Forecast: {event.get(‘forecast’,‘N/A’)}, Previous: {event.get(‘previous’,‘N/A’)}. 3 lines plain text: expected impact on EUR/USD and GBP/USD, avoid trading or not, move to expect if beats or misses.”””
ai  = ask_claude(prompt)
msg = (
f”📰 <b>RED FOLDER — {int(mins_until)} MINS</b>\n\n”
f”🏷 {event.get(‘title’)}\n”
f”🌍 {event.get(‘currency’)} — High Impact 🔴\n”
f”🕐 {event_london.strftime(’%H:%M’)} {suffix}\n”
f”📊 Forecast: {event.get(‘forecast’,‘N/A’)} | Prev: {event.get(‘previous’,‘N/A’)}\n\n”
f”🤖 <b>AI READ:</b>\n{ai}”
)
send_telegram(msg)
except Exception as e:
print(f”News error: {e}”, flush=True)

# ─────────────────────────────────────────────

# VOLUME SPIKES

# ─────────────────────────────────────────────

def check_spikes():
if is_blackout() or not get_session():
return
session  = get_session()
all_data = {}
for pair in PAIRS:
data = get_twelve_volume(pair)
if data:
all_data[pair] = data
time.sleep(2)

```
for pair, data in all_data.items():
    if data["avg_volume"] == 0:
        continue
    ratio     = data["volume"] / data["avg_volume"]
    pip_move  = abs(data["price"] - data["prev_price"]) * (100 if "XAU" in pair or "XAG" in pair else 10000)
    direction = "📈 UP" if data["price"] > data["prev_price"] else "📉 DOWN"

    if ratio >= SPIKE_MULTIPLIER:
        correlated = "GBP/USD" if pair=="EUR/USD" else "EUR/USD" if pair=="GBP/USD" else "XAG/USD" if pair=="XAU/USD" else "XAU/USD"
        corr_data  = all_data.get(correlated)
        corr_text  = ""
        if corr_data:
            corr_dir  = "📈" if corr_data["price"] > corr_data["prev_price"] else "📉"
            corr_text = f"\n🔗 {correlated}: {corr_data['price']} {corr_dir}"
        prompt = f"""Volume spike alert: {pair} just spiked {ratio:.1f}x normal volume. Price: {data['price']}, moving {direction}, {pip_move:.1f} pips. Session: {session}. Correlated pair {correlated}: {corr_data['price'] if corr_data else 'unavailable'}. 4 lines plain text: what this means for Joshua's SMT strategy, is there divergence context, what to look for on M1, confidence level."""
        ai  = ask_claude(prompt)
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
        ai  = ask_claude(prompt)
        msg = (
            f"💥 <b>FAST MOVE — {pair}</b>\n\n"
            f"📏 {pip_move:.1f} pips {direction}\n"
            f"💰 {data['price']}\n"
            f"🕐 {london_time_str()} | {session}\n\n"
            f"🤖 {ai}"
        )
        send_telegram(msg)
```

# ─────────────────────────────────────────────

# HOURLY BIAS — active sessions only

# ─────────────────────────────────────────────

def check_hourly_bias():
if not is_active_session():
return
lt, suffix = london_time()
hour_key   = lt.strftime(”%Y-%m-%d-%H”)
if hour_key in last_hourly:
return
last_hourly[hour_key] = True

```
prices = get_all_prices()
if not prices:
    return

price_summary = "\n".join([
    f"{'📈' if prices[p]['change']>0 else '📉'} <b>{p}</b>: {prices[p]['price']}"
    for p in prices
])
data_text = " | ".join([f"{p}: {prices[p]['price']} ({'up' if prices[p]['change']>0 else 'down'})" for p in prices])
prompt    = f"""Hourly bias update for Joshua. Time: {lt.strftime('%H:%M')} {suffix}. Session: {get_session()}. Live prices: {data_text}. 4 lines plain text: overall directional bias, which pair is strongest/weakest, any SMT divergence context between EUR/GBP or Gold/Silver, one specific thing to watch this hour."""
ai        = ask_claude(prompt)
msg       = (
    f"🕐 <b>HOURLY BIAS — {lt.strftime('%H:00')} {suffix}</b>\n"
    f"📍 {get_session()}\n\n"
    f"{price_summary}\n\n"
    f"🤖 {ai}\n\n"
    f"🔄 Next: {(lt.hour+1)%24:02d}:00 {suffix}"
)
send_telegram(msg)
```

# ─────────────────────────────────────────────

# MORNING BRIEF

# ─────────────────────────────────────────────

def send_morning_brief():
lt, suffix = london_time()
if lt.hour != 7 or lt.minute > 5:
return
brief_key = lt.strftime(”%Y-%m-%d-brief”)
if brief_key in last_hourly:
return
last_hourly[brief_key] = True

```
prices = get_all_prices()
now    = datetime.now(timezone.utc)
try:
    r      = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=10)
    events = r.json()
    offset = 1 if suffix=="BST" else 0
    today_events = []
    for e in events:
        if e.get("impact")=="High" and e.get("currency") in ["USD","EUR","GBP"]:
            try:
                et        = datetime.fromisoformat(e["date"].replace("Z","+00:00"))
                et_london = et + timedelta(hours=offset)
                if et.date() == now.date():
                    today_events.append(f"{et_london.strftime('%H:%M')} {suffix} — {e.get('currency')} {e.get('title')}")
            except:
                pass
except:
    today_events = []

news_text  = "\n".join(today_events) if today_events else "No high impact news today"
price_text = " | ".join([f"{p}: {prices[p]['price']}" for p in prices]) if prices else "unavailable"
prompt     = f"""Write Joshua's morning trading brief for {lt.strftime('%A %d %b %Y')}. Time: 07:00 {suffix} — Frankfurt opens in 1 hour, London opens in 1 hour.
```

Live prices: {price_text}
High impact news today: {news_text}

Plain text, no markdown. Cover:

1. Overall bias for the London session
1. Key levels on EUR/USD and GBP/USD to watch
1. Any SMT divergence context between pairs
1. The main setup to watch for at London open 08:00 {suffix}
1. News risk warning if any
   Under 150 words. Write like a sharp trading desk analyst talking to Joshua directly.”””
   ai = ask_claude(prompt)
   
   price_lines = “\n”.join([f”{‘📈’ if prices[p][‘change’]>0 else ‘📉’} <b>{p}</b>: {prices[p][‘price’]}” for p in prices]) if prices else “”
   news_lines  = “\n”.join([f”🔴 {e}” for e in today_events]) if today_events else “✅ No high impact news”
   msg         = (
   f”🌅 <b>MORNING BRIEF — {lt.strftime(’%a %d %b’)} — 07:00 {suffix}</b>\n\n”
   f”💰 <b>PRICES</b>\n{price_lines}\n\n”
   f”📅 <b>TODAY’S NEWS</b>\n{news_lines}\n\n”
   f”🤖 <b>AI READ:</b>\n{ai}”
   )
   send_telegram(msg)
   print(“Morning brief sent”, flush=True)

# ─────────────────────────────────────────────

# SESSION COUNTDOWN

# ─────────────────────────────────────────────

def check_session_countdown():
lt, suffix = london_time()
countdowns = [
(23,45,“🌏 ASIA OPEN”,     f”00:00 {suffix}”),
(6, 45,“🇩🇪 FRANKFURT OPEN”,f”07:00 {suffix}”),
(7, 45,“🇬🇧 LONDON OPEN”,   f”08:00 {suffix}”),
(14,15,“🇺🇸 NY OPEN”,       f”14:30 {suffix}”),
]
for h, m, label, open_time in countdowns:
key = f”countdown-{lt.strftime(’%Y-%m-%d’)}-{h}-{m}”
if lt.hour==h and lt.minute>=m and lt.minute<m+5:
if key not in last_hourly:
last_hourly[key] = True
prices      = get_all_prices()
price_lines = “\n”.join([f”💰 <b>{p}</b>: {prices[p][‘price’]}” for p in prices]) if prices else “”
price_text  = “ | “.join([f”{p}: {prices[p][‘price’]}” for p in prices]) if prices else “unavailable”
prompt = f””“15 minutes to {label} at {open_time} for Joshua. Live prices: {price_text}. 3 lines plain text: expected bias and likely direction at open, where liquidity is sitting based on price levels, one specific level to watch for a sweep or setup.”””
ai  = ask_claude(prompt)
msg = (
f”⚡ <b>15 MINS TO {label}</b>\n”
f”🕐 Opens: {open_time}\n\n”
f”{price_lines}\n\n”
f”🤖 {ai}”
)
send_telegram(msg)

# ─────────────────────────────────────────────

# SMT CORRELATION BREAKDOWN — session-aware thresholds

# ─────────────────────────────────────────────

def check_correlation_breakdown():
session = get_session()
if not session:
return

```
# Session-aware divergence thresholds
lt, _ = london_time()
h = lt.hour
if 0 <= h < 7:    threshold = 8
elif h == 7:      threshold = 12
elif 8 <= h < 13: threshold = 15
else:             threshold = 20

eur_data = get_yahoo_price("EUR/USD")
time.sleep(1)
gbp_data = get_yahoo_price("GBP/USD")
if not eur_data or not gbp_data:
    return

eur_move   = eur_data["change"] * 10000
gbp_move   = gbp_data["change"] * 10000
divergence = abs(eur_move - gbp_move)

if divergence >= threshold:
    key = f"corr-{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H-%M')}"
    if key not in last_hourly:
        last_hourly[key] = True
        weaker   = "EUR/USD" if eur_move < gbp_move else "GBP/USD"
        stronger = "GBP/USD" if weaker=="EUR/USD" else "EUR/USD"
        prompt   = f"""SMT divergence context for Joshua: EUR/USD moved {eur_move:.1f} pips, GBP/USD moved {gbp_move:.1f} pips. Divergence: {divergence:.1f} pips. {weaker} is the weaker pair. Session: {session}. 3 lines plain text: is this meaningful SMT context, what M1 setup to look for, which pair to trade and in which direction."""
        ai  = ask_claude(prompt)
        msg = (
            f"⚠️ <b>SMT DIVERGENCE CONTEXT</b>\n\n"
            f"EUR/USD: {'+' if eur_move>0 else ''}{eur_move:.1f} pips\n"
            f"GBP/USD: {'+' if gbp_move>0 else ''}{gbp_move:.1f} pips\n"
            f"📏 Divergence: {divergence:.1f} pips (threshold: {threshold})\n"
            f"💡 <b>{weaker} weaker | {stronger} stronger</b>\n"
            f"🕐 {london_time_str()} | {session}\n\n"
            f"🤖 {ai}"
        )
        send_telegram(msg)
```

# ─────────────────────────────────────────────

# TIER 3A — P&L TRACKER: parse natural language trade

# ─────────────────────────────────────────────

PAIR_ALIASES = {
“eurusd”: “EUR/USD”, “eu”: “EUR/USD”, “eur”: “EUR/USD”,
“gbpusd”: “GBP/USD”, “gu”: “GBP/USD”, “gbp”: “GBP/USD”,
“xauusd”: “XAU/USD”, “gold”: “XAU/USD”, “xau”: “XAU/USD”,
“xagusd”: “XAG/USD”, “silver”: “XAG/USD”, “xag”: “XAG/USD”,
}

def parse_trade(text):
“””
Parse natural language like:
‘long eurusd 1.1720 sl 1.1700 tp 1.1760’
‘short gold 3200 sl 3210 tp 3180’
Returns dict or None.
“””
text_lower = text.lower().strip()

```
direction = None
if "long" in text_lower:   direction = "LONG"
elif "short" in text_lower: direction = "SHORT"
if not direction:
    return None

pair = None
for alias, canonical in PAIR_ALIASES.items():
    if alias in text_lower:
        pair = canonical
        break
if not pair:
    return None

numbers = re.findall(r"\d+\.?\d*", text_lower)
floats  = [float(n) for n in numbers]
if len(floats) < 3:
    return None

# Try to extract labelled SL/TP
sl_match = re.search(r"sl\s*([\d.]+)", text_lower)
tp_match = re.search(r"tp\s*([\d.]+)", text_lower)

if sl_match and tp_match:
    entry = floats[0]
    sl    = float(sl_match.group(1))
    tp    = float(tp_match.group(1))
elif len(floats) >= 3:
    entry, sl, tp = floats[0], floats[1], floats[2]
else:
    return None

return {"pair": pair, "direction": direction, "entry": entry, "sl": sl, "tp": tp}
```

def handle_trade_command(text, chat_id):
“”“Handle natural language trade logging.”””
trade = parse_trade(text)
if not trade:
send_telegram(“Couldn’t parse that trade. Try: long EURUSD 1.1720 sl 1.1700 tp 1.1760”, chat_id)
return

```
session    = get_session() or "Off hours"
trade_id, risk_r = db_log_trade(
    trade["pair"], trade["direction"],
    trade["entry"], trade["sl"], trade["tp"], session
)

risk_pips   = abs(trade["entry"] - trade["sl"])
reward_pips = abs(trade["tp"] - trade["entry"])
pip_mult    = 100 if "XAU" in trade["pair"] or "XAG" in trade["pair"] else 10000

msg = (
    f"✅ <b>TRADE LOGGED — #{trade_id}</b>\n\n"
    f"📊 {trade['pair']} | {trade['direction']}\n"
    f"🎯 Entry: {trade['entry']}\n"
    f"🛑 SL: {trade['sl']} ({risk_pips*pip_mult:.1f} pips)\n"
    f"🏁 TP: {trade['tp']} ({reward_pips*pip_mult:.1f} pips)\n"
    f"📐 RR: {risk_r}R\n"
    f"🕐 {london_time_str()} | {session}\n\n"
    f"To close: 'win #{trade_id}' or 'loss #{trade_id}' or 'be #{trade_id}'"
)
send_telegram(msg, chat_id)
```

def handle_close_trade(text, chat_id):
“”“Handle ‘win #3’, ‘loss #3’, ‘be #3’.”””
text_lower = text.lower().strip()
match = re.search(r”#?(\d+)”, text)
if not match:
send_telegram(“Specify trade ID. Example: win #3”, chat_id)
return

```
trade_id = int(match.group(1))
if "win" in text_lower:     result = "WIN"
elif "loss" in text_lower:  result = "LOSS"
elif " be" in text_lower or text_lower.startswith("be"): result = "BE"
else:
    send_telegram("Use: win #ID / loss #ID / be #ID", chat_id)
    return

pnl_r = db_close_trade(trade_id, result)
if pnl_r is None:
    send_telegram(f"Trade #{trade_id} not found.", chat_id)
    return

emoji = "🟢" if result=="WIN" else "🔴" if result=="LOSS" else "⚪"
msg   = (
    f"{emoji} <b>TRADE #{trade_id} CLOSED — {result}</b>\n\n"
    f"📐 P&L: {'+' if pnl_r>=0 else ''}{pnl_r}R\n"
    f"🕐 {london_time_str()}\n\n"
    f"Type /stats for session summary."
)
send_telegram(msg, chat_id)
```

def handle_stats(chat_id):
stats      = db_get_stats()
open_trades= db_get_open_trades()
open_lines = “”
if open_trades:
open_lines = “\n\n<b>OPEN TRADES:</b>\n”
for t in open_trades:
tid, pair, direction, entry, sl, tp, rr, ts = t
open_lines += f”#{tid} {pair} {direction} @ {entry} | {rr}R | {ts}\n”

```
msg = (
    f"📊 <b>TRADE STATS</b>\n\n"
    f"Total closed: {stats['total']}\n"
    f"Wins: {stats['wins']}\n"
    f"Win rate: {stats['wr']}%\n"
    f"Total R: {'+' if stats['total_r']>=0 else ''}{stats['total_r']}R"
    f"{open_lines}"
)
send_telegram(msg, chat_id)
```

def handle_open_trades(chat_id):
trades = db_get_open_trades()
if not trades:
send_telegram(“No open trades.”, chat_id)
return
lines = “<b>OPEN TRADES:</b>\n\n”
for t in trades:
tid, pair, direction, entry, sl, tp, rr, ts = t
lines += f”#{tid} {pair} {direction} @ {entry}\nSL: {sl} | TP: {tp} | {rr}R\n{ts}\n\n”
send_telegram(lines, chat_id)

# ─────────────────────────────────────────────

# TIER 3B — CHART TO JOURNAL PIPELINE

# ─────────────────────────────────────────────

def handle_chart_analysis(img_b64, caption, chat_id):
“”“Analyse chart, log to DB, optionally link to open trade.”””
prompt = f””“Joshua sent you this chart{’ with note: ’ + caption if caption else ‘’}.

Analyse using SMC/SMT methodology:

1. Pair and timeframe visible
1. Market structure — bullish or bearish, any BOS/CHOCH
1. Liquidity sweeps — equal highs/lows swept or pending
1. FVGs and order blocks visible
1. Inducement levels marked
1. SMT divergence context if two charts shown
1. Entry setup — entry zone, stop loss, target with price levels
1. Confidence out of 10 and why

Plain text no markdown. Be specific with price levels you can see.”””

```
send_telegram("🤖 Analysing chart...", chat_id)
ai = ask_claude(prompt, img_b64)

# Extract pair from analysis for logging
pair      = None
tf        = "M1"
ai_lower  = ai.lower()
for alias, canonical in PAIR_ALIASES.items():
    if alias in ai_lower or canonical.lower().replace("/","") in ai_lower:
        pair = canonical
        break
for tf_label in ["m1","m5","m15","h1","h4","d1"]:
    if tf_label in ai_lower:
        tf = tf_label.upper()
        break

# Log to DB
db_log_chart(pair, tf, ai)

# Check if there's an open trade to link
open_trades = db_get_open_trades()
link_msg    = ""
if open_trades and pair:
    matching = [t for t in open_trades if t[1] == pair]
    if matching:
        t = matching[-1]
        tid = t[0]
        db_conn = sqlite3.connect(DB_PATH)
        db_conn.execute("UPDATE chart_logs SET trade_id=? WHERE trade_id IS NULL ORDER BY id DESC LIMIT 1", (tid,))
        db_conn.execute("UPDATE trades SET chart_analysis=? WHERE id=?", (ai[:500], tid))
        db_conn.commit()
        db_conn.close()
        link_msg = f"\n\n📎 Linked to trade #{tid} ({pair})"

send_telegram(
    f"🤖 <b>CHART ANALYSIS</b>\n\n{ai}{link_msg}\n\n"
    f"📁 Logged to journal — {london_time_str()}",
    chat_id
)
```

# ─────────────────────────────────────────────

# NATURAL LANGUAGE HANDLER

# ─────────────────────────────────────────────

def handle_natural_message(text, chat_id):
text_lower = text.lower().strip()
prices     = None

```
needs_prices = any(kw in text_lower for kw in [
    "gold","silver","eur","gbp","xau","xag","price","doing",
    "level","where","bias","long","short","buy","sell",
    "market","session","setup","trade"
])
if needs_prices:
    send_telegram("⏳ Fetching live data...", chat_id)
    prices = get_all_prices()

lt, suffix   = london_time()
session      = get_session() or "Off hours"
price_context= ""
if prices:
    price_context = "Live prices: " + " | ".join([
        f"{p}: {prices[p]['price']} ({'up' if prices[p]['change']>0 else 'down'} {abs(prices[p]['change']*(100 if 'XAU' in p or 'XAG' in p else 10000)):.1f} pips)"
        for p in prices
    ])

prompt = f"""Joshua says: "{text}"
```

Current time: {lt.strftime(’%H:%M’)} {suffix}
Session: {session}
{price_context}

Respond directly to what he asked. Be sharp and concise. Max 5 lines. Plain text no markdown.”””

```
ai = ask_claude(prompt, use_history=True)
send_telegram(f"🤖 {ai}", chat_id)
```

# ─────────────────────────────────────────────

# TELEGRAM MESSAGE HANDLER

# ─────────────────────────────────────────────

def handle_incoming_messages():
global last_update_id
try:
url    = f”https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates”
params = {“offset”: last_update_id+1, “timeout”: 3}
r      = requests.get(url, params=params, timeout=8)
updates= r.json().get(“result”, [])

```
    for update in updates:
        last_update_id = update["update_id"]
        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        photo   = message.get("photo")
        text    = message.get("text", "")

        # ── PHOTO: chart analysis + auto journal ──
        if photo:
            file_id   = photo[-1]["file_id"]
            file_info = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}").json()
            file_path = file_info["result"]["file_path"]
            img_bytes = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}").content
            img_b64   = base64.b64encode(img_bytes).decode("utf-8")
            caption   = message.get("caption", "")
            handle_chart_analysis(img_b64, caption, chat_id)

        elif text:
            cmd       = text.lower().strip()
            text_lower= cmd

            # ── COMMANDS ──
            if cmd == "/prices":
                send_telegram("⏳ Fetching live prices...", chat_id)
                lt, suffix = london_time()
                prices     = get_all_prices()
                msg        = f"💰 <b>LIVE PRICES</b>\n🕐 {lt.strftime('%H:%M')} {suffix}\n\n"
                for pair in PAIRS:
                    if pair in prices:
                        d           = "📈" if prices[pair]["change"]>0 else "📉"
                        change_pips = prices[pair]["change"]*(100 if "XAU" in pair or "XAG" in pair else 10000)
                        msg        += f"{d} <b>{pair}</b>: {prices[pair]['price']} ({'+' if change_pips>0 else ''}{change_pips:.1f} pips)\n"
                    else:
                        msg += f"⚠️ <b>{pair}</b>: unavailable\n"
                send_telegram(msg, chat_id)

            elif cmd == "/bias":
                send_telegram("⏳ Getting AI bias...", chat_id)
                handle_natural_message("what's the current bias across all pairs and what should I be watching", chat_id)

            elif cmd == "/brief":
                send_telegram("⏳ Generating brief...", chat_id)
                lt, suffix = london_time()
                last_hourly.pop(lt.strftime("%Y-%m-%d-brief"), None)
                send_morning_brief()

            elif cmd == "/stats":
                handle_stats(chat_id)

            elif cmd == "/trades":
                handle_open_trades(chat_id)

            elif cmd == "/journal":
                conn  = sqlite3.connect(DB_PATH)
                c     = conn.cursor()
                c.execute("SELECT id, timestamp, pair, direction, entry, sl, tp, risk_r, status, pnl_r FROM trades ORDER BY id DESC LIMIT 10")
                rows  = c.fetchall()
                conn.close()
                if not rows:
                    send_telegram("No trades logged yet.", chat_id)
                else:
                    lines = "<b>LAST 10 TRADES:</b>\n\n"
                    for row in rows:
                        tid, ts, pair, direction, entry, sl, tp, rr, status, pnl = row
                        emoji = "🟢" if status=="closed_win" else "🔴" if status=="closed_loss" else "⚪" if status=="closed_be" else "🔵"
                        pnl_str = f"{'+' if (pnl or 0)>=0 else ''}{pnl or 0}R" if status!="open" else "open"
                        lines += f"{emoji} #{tid} {pair} {direction} @ {entry} | {rr}R | {pnl_str} | {ts}\n"
                    send_telegram(lines, chat_id)

            elif cmd == "/help":
                lt, suffix = london_time()
                send_telegram(
                    f"📊 <b>FUNDAMENTALS BOT</b>\n\n"
                    f"<b>TALK NATURALLY:</b>\n"
                    f"'what's gold doing'\n"
                    f"'am I long or short bias on GBP'\n"
                    f"'is there SMT on XAU/XAG'\n\n"
                    f"<b>LOG A TRADE:</b>\n"
                    f"'long EURUSD 1.1720 sl 1.1700 tp 1.1760'\n"
                    f"'short gold 3200 sl 3210 tp 3180'\n\n"
                    f"<b>CLOSE A TRADE:</b>\n"
                    f"'win #3' / 'loss #3' / 'be #3'\n\n"
                    f"<b>COMMANDS:</b>\n"
                    f"/prices — Live prices\n"
                    f"/bias — AI bias update\n"
                    f"/brief — Morning brief\n"
                    f"/stats — P&L summary\n"
                    f"/trades — Open trades\n"
                    f"/journal — Last 10 trades\n"
                    f"/help — This menu\n\n"
                    f"📸 Send any chart for AI analysis + auto journal\n\n"
                    f"⏰ Sessions ({suffix}):\n"
                    f"Asia 🌏 00:00-07:00\n"
                    f"Frankfurt 🇩🇪 07:00-08:00\n"
                    f"London 🇬🇧 08:00-13:00\n"
                    f"NY 🇺🇸 14:30-21:00",
                    chat_id
                )

            elif not cmd.startswith("/"):
                # Check for trade close commands first
                if any(kw in text_lower for kw in ["win #","loss #","be #","win#","loss#","be#"]) or \
                   re.search(r"(win|loss|be)\s+#?\d+", text_lower):
                    handle_close_trade(text, chat_id)

                # Check for trade logging
                elif any(d in text_lower for d in ["long ","short "]) and \
                     any(a in text_lower for a in PAIR_ALIASES.keys()) and \
                     ("sl" in text_lower or re.search(r"\d+\.?\d*\s+\d+\.?\d*\s+\d+\.?\d*", text_lower)):
                    handle_trade_command(text, chat_id)

                # Everything else — natural language
                else:
                    handle_natural_message(text, chat_id)

except Exception as e:
    print(f"Message handler error: {e}", flush=True)
```

# ─────────────────────────────────────────────

# HTTP SERVER (Render keep-alive)

# ─────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
def do_GET(self):
self.send_response(200)
self.end_headers()
self.wfile.write(b”Fundamentals Bot is running”)
def log_message(self, format, *args):
pass

def run_server():
server = HTTPServer((“0.0.0.0”, 10000), Handler)
print(“HTTP server started”, flush=True)
server.serve_forever()

# ─────────────────────────────────────────────

# STARTUP

# ─────────────────────────────────────────────

init_db()
threading.Thread(target=run_server, daemon=True).start()
time.sleep(5)

lt, suffix = london_time()
print(“Fundamentals Bot starting…”, flush=True)
send_telegram(
f”📊 <b>FUNDAMENTALS BOT IS LIVE</b>\n\n”
f”Just talk to me naturally — ask me anything about the market.\n\n”
f”<b>NEW — Tier 3 features:</b>\n”
f”📁 Chart analysis auto-logged to journal\n”
f”📐 Trade tracker — ‘long EURUSD 1.1720 sl 1.1700 tp 1.1760’\n”
f”📊 /stats /trades /journal\n\n”
f”⏰ Sessions ({suffix}):\n”
f”Asia 🌏 00:00-07:00\n”
f”Frankfurt 🇩🇪 07:00-08:00\n”
f”London 🇬🇧 08:00-13:00\n”
f”NY 🇺🇸 14:30-21:00\n\n”
f”🔴 FF Breaking news: ON\n”
f”📰 Red folder alerts: ON\n”
f”🚨 Volume spikes: ON\n”
f”📊 Hourly bias (active sessions only): ON\n”
f”🌅 Morning brief: 07:00 {suffix}\n”
f”📸 Chart analysis + journal: ON\n\n”
f”🕐 {lt.strftime(’%H:%M’)} {suffix}\n”
f”/help for commands”
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
