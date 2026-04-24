def _send_ff_alert(headline, url=""):
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    prompt = f"""Breaking headline on ForexFactory: "{headline}"

You are Joshua's M1 SMC/SMT trading assistant in London. Respond in plain text, no markdown, 5 lines max:

Line 1 — PAIRS: Which of EUR/USD, GBP/USD, XAU/USD, XAG/USD are directly affected and how
Line 2 — FLOW: Dollar direction — risk-on or risk-off, institutional bias
Line 3 — M1 REACTION: Expected sweep direction on M1, likely displacement, which side liquidity gets taken
Line 4 — SETUP: Trade or avoid. If trade — which pair, long or short, what to wait for before entry
Line 5 — CONTEXT: Session timing relevance (Asia/Frankfurt/London/NY) and urgency level"""

    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 400,
        "system": STRATEGY_CONTEXT,
        "messages": [{"role": "user", "content": prompt}]
    }

    try:
        r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=30)
        ai = r.json()["content"][0]["text"]
        ai = ai.replace("**", "").replace("##", "").replace("# ", "")
    except Exception as e:
        print(f"Claude FF error: {e}", flush=True)
        ai = "AI analysis unavailable."

    lt, suffix = london_time()
    session = get_session() or "Off hours"

    msg = (
        f"🔴 <b>FF BREAKING NEWS</b>\n\n"
        f"📰 <b>{headline}</b>\n"
    )
    if url:
        msg += f"🔗 {url}\n"
    msg += (
        f"\n🕐 {lt.strftime('%H:%M')} {suffix} | {session}\n\n"
        f"🤖 <b>M1 REACTION ANALYSIS:</b>\n\n"
        f"{ai}"
    )
    send_telegram(msg)
    print(f"[FF] Alert sent: {headline}", flush=True)
