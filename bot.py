import os
import requests
from datetime import datetime

import pytz
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler

# ======================
# ENV
# ======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
THREAD_ID = os.getenv("THREAD_ID", "").strip()  # opcional (topics)

TZ = pytz.timezone("Europe/Zurich")
app = FastAPI(title="Telegram Market Bot")

# ======================
# Helpers
# ======================
def safe_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default

def pct_arrow(x: float) -> str:
    return "↑" if x > 0 else ("↓" if x < 0 else "→")

def fmt_money(x: float, decimals=0):
    if x is None:
        return "No disponible"
    return f"{x:,.{decimals}f}"

def fmt_pct(x: float, decimals=2):
    if x is None:
        return "No disponible"
    return f"{x:.{decimals}f}%"

# ======================
# Data Sources (FREE)
# ======================
def get_bybit_ticker_24h():
    # Precio + %24h + volumen 24h (turnover USD)
    url = "https://api.bybit.com/v5/market/tickers"
    r = requests.get(url, params={"category": "linear", "symbol": "BTCUSDT"}, timeout=20)
    r.raise_for_status()
    j = r.json()
    item = j["result"]["list"][0]
    last = safe_float(item.get("lastPrice"))
    chg_pct = safe_float(item.get("price24hPcnt"))
    # bybit devuelve 0.0123 = 1.23%
    chg_pct = (chg_pct * 100) if chg_pct is not None else None
    turnover = safe_float(item.get("turnover24h"))  # en USD aprox
    return last, chg_pct, turnover

def get_bybit_funding_and_oi():
    # Funding rate
    f_url = "https://api.bybit.com/v5/market/funding/history"
    fr = requests.get(
        f_url,
        params={"category": "linear", "symbol": "BTCUSDT", "limit": 1},
        timeout=20,
    )
    fr.raise_for_status()
    fj = fr.json()
    f_list = fj.get("result", {}).get("list", [])
    funding = safe_float(f_list[0].get("fundingRate")) if f_list else None  # ej: 0.0001
    funding = (funding * 100) if funding is not None else None

    # Open interest
    oi_url = "https://api.bybit.com/v5/market/open-interest"
    oir = requests.get(
        oi_url,
        params={"category": "linear", "symbol": "BTCUSDT", "intervalTime": "5min", "limit": 1},
        timeout=20,
    )
    oir.raise_for_status()
    oij = oir.json()
    oi_list = oij.get("result", {}).get("list", [])
    oi = safe_float(oi_list[0].get("openInterest")) if oi_list else None

    return funding, oi

def get_bybit_liquidations_24h():
    # Suma liquidaciones long + short de las últimas 24h (Bybit)
    # OJO: Bybit da “qty” por evento; lo convertimos a USD aprox con precio del momento si viene.
    url = "https://api.bybit.com/v5/market/liquidation"
    r = requests.get(
        url,
        params={"category": "linear", "symbol": "BTCUSDT", "limit": 200},
        timeout=20,
    )
    r.raise_for_status()
    j = r.json()
    events = j.get("result", {}).get("list", [])
    if not events:
        return None

    now_ts = int(datetime.now(tz=TZ).timestamp() * 1000)
    day_ms = 24 * 60 * 60 * 1000

    total_usd = 0.0
    used_any = False

    for e in events:
        ts = int(e.get("time", 0))
        if now_ts - ts > day_ms:
            continue
        qty = safe_float(e.get("qty"))
        price = safe_float(e.get("price"))
        if qty is None:
            continue
        # Si hay price, hacemos qty*price ~ USD
        if price is not None:
            total_usd += qty * price
            used_any = True

    return total_usd if used_any else None

def get_fear_greed():
    url = "https://api.alternative.me/fng/"
    r = requests.get(url, params={"limit": 1, "format": "json"}, timeout=20)
    r.raise_for_status()
    j = r.json()
    data = j["data"][0]
    value = int(data["value"])
    cls = data["value_classification"]
    return value, cls

def get_dominance_btc_usdt():
    g = requests.get("https://api.coingecko.com/api/v3/global", timeout=20)
    g.raise_for_status()
    gj = g.json()["data"]

    btc_dom = float(gj["market_cap_percentage"].get("btc", 0.0))
    total_mcap = float(gj["total_market_cap"].get("usd", 0.0))

    m = requests.get(
        "https://api.coingecko.com/api/v3/coins/markets",
        params={"vs_currency": "usd", "ids": "tether"},
        timeout=20,
    )
    m.raise_for_status()
    usdt_mcap = float(m.json()[0].get("market_cap", 0.0))

    usdt_dom = (usdt_mcap / total_mcap * 100) if total_mcap > 0 else 0.0
    return btc_dom, usdt_dom

# ======================
# Telegram
# ======================
def send_telegram_message(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Faltan variables BOT_TOKEN o CHAT_ID")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if THREAD_ID:
        payload["message_thread_id"] = int(THREAD_ID)

    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()

# ======================
# Report + Bias
# ======================
def build_bias(chg_pct, funding, oi, fng_val, btc_dom, usdt_dom):
    # Regla simple pero útil:
    # - Fear extremo + rojo 24h suele ser contrarian alcista (rebote)
    # - Funding muy positivo + OI alto suele ser riesgo de squeeze bajista
    # - USDT.D alto suele pesar en risk-on

    notes = []

    if chg_pct is not None:
        notes.append("BTC 24h en rojo" if chg_pct < 0 else "BTC 24h en verde")

    if fng_val is not None:
        if fng_val <= 10:
            notes.append("Fear extremo (contrarian alcista)")
        elif fng_val >= 75:
            notes.append("Greed alto (riesgo pullback)")

    if funding is not None:
        if funding > 0.02:
            notes.append("Funding alto (posible squeeze bajista)")
        elif funding < -0.02:
            notes.append("Funding negativo (posible squeeze alcista)")

    if usdt_dom is not None and usdt_dom > 7.5:
        notes.append("USDT.D elevado (presión risk-off)")

    # Determinar sesgo
    score = 0
    # contrarian
    if fng_val is not None and fng_val <= 10 and chg_pct is not None and chg_pct < 0:
        score += 1
    # risk-off
    if usdt_dom is not None and usdt_dom > 7.5:
        score -= 1
    # funding squeeze risk
    if funding is not None and funding > 0.02:
        score -= 1
    if funding is not None and funding < -0.02:
        score += 1

    if score >= 1:
        bias = "Leve Alcista"
    elif score <= -1:
        bias = "Leve Bajista"
    else:
        bias = "Neutral"

    return bias, "; ".join(notes) if notes else "Sin suficientes señales"

def build_report():
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M (Suiza)")

    price, chg_pct, vol_usd = get_bybit_ticker_24h()
    funding, oi = get_bybit_funding_and_oi()
    liq_24h = get_bybit_liquidations_24h()

    btc_dom, usdt_dom = get_dominance_btc_usdt()
    fng_val, fng_cls = get_fear_greed()

    bias, why = build_bias(chg_pct, funding, oi, fng_val, btc_dom, usdt_dom)

    text = (
        f"Contexto de Mercado — {now}\n\n"
        f"BTC: ${fmt_money(price,0)} ({pct_arrow(chg_pct or 0)} {fmt_pct(chg_pct,2)} 24h)\n"
        f"Volumen 24h (USD): {fmt_money(vol_usd,0)}\n\n"
        f"Funding: {('No disponible' if funding is None else f'{funding:.4f}%')}\n"
        f"Open Interest: {('No disponible' if oi is None else fmt_money(oi,0))}\n"
        f"Liquidaciones 24h: {('No disponible' if liq_24h is None else f'${fmt_money(liq_24h,0)}')}\n\n"
        f"BTC.D: {btc_dom:.2f}%\n"
        f"USDT.D (aprox): {usdt_dom:.2f}%\n"
        f"Fear & Greed: {fng_val} ({fng_cls})\n\n"
        f"Conclusión (BIAS): {bias} — {why}"
    )
    return text

def job_send():
    try:
        msg = build_report()
        send_telegram_message(msg)
    except Exception as e:
        print("ERROR job_send:", repr(e), flush=True)

# ======================
# Scheduler
# ======================
scheduler = BackgroundScheduler(timezone=TZ)

@app.on_event("startup")
def startup_event():
    # 01:00 Suiza (Tokyo open - como lo quieres)
    scheduler.add_job(job_send, "cron", hour=1, minute=0, id="tokyo_1am", replace_existing=True)
    # 09:00 Suiza (London open - como lo quieres)
    scheduler.add_job(job_send, "cron", hour=9, minute=0, id="london_9am", replace_existing=True)

    scheduler.start()
    print("Scheduler started: 01:00 (Tokio), 09:00 (Londres) Europe/Zurich", flush=True)

@app.get("/")
def root():
    return {"status": "ok", "time": datetime.now(TZ).isoformat()}

@app.get("/send-now")
def send_now():
    job_send()
    return {"sent": True}
