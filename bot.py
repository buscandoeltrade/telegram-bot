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
# Utils
# ======================
def log(msg: str):
    print(msg, flush=True)

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
# HTTP helper
# ======================
def get_json(url, params=None):
    r = requests.get(
        url,
        params=params or {},
        timeout=25,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    r.raise_for_status()
    return r.json()


# ======================
# DATA: Ticker 24h (Bybit) con fallback OKX
# ======================
def get_ticker_24h():
    # --- Bybit ---
    try:
        url = "https://api.bybit.com/v5/market/tickers"
        j = get_json(url, {"category": "linear", "symbol": "BTCUSDT"})
        if j.get("retCode", 0) != 0:
            raise RuntimeError(f"Bybit ticker retCode={j.get('retCode')} retMsg={j.get('retMsg')}")
        item = j["result"]["list"][0]
        last = safe_float(item.get("lastPrice"))
        chg_pct = safe_float(item.get("price24hPcnt"))
        chg_pct = (chg_pct * 100) if chg_pct is not None else None
        turnover = safe_float(item.get("turnover24h"))  # USD aprox
        return last, chg_pct, turnover, "Bybit"
    except Exception as e:
        log(f"[WARN] Bybit ticker failed: {repr(e)}")

    # --- OKX fallback ---
    try:
        url = "https://www.okx.com/api/v5/market/ticker"
        j = get_json(url, {"instId": "BTC-USDT"})
        data = j.get("data", [])
        if not data:
            raise RuntimeError("OKX ticker empty data")
        item = data[0]
        last = safe_float(item.get("last"))
        open_24 = safe_float(item.get("open24h"))
        chg_pct = None
        if last is not None and open_24 not in (None, 0):
            chg_pct = (last - open_24) / open_24 * 100
        vol_ccy_24 = safe_float(item.get("volCcy24h"))  # en USDT aprox para BTC-USDT
        return last, chg_pct, vol_ccy_24, "OKX"
    except Exception as e:
        log(f"[WARN] OKX ticker failed: {repr(e)}")

    return None, None, None, "N/A"


# ======================
# DATA: Funding + OI (Bybit) con fallback OKX
# ======================
def get_funding_and_oi():
    funding = None
    oi = None
    source = "N/A"

    # --- Bybit ---
    try:
        # Funding
        f_url = "https://api.bybit.com/v5/market/funding/history"
        fj = get_json(f_url, {"category": "linear", "symbol": "BTCUSDT", "limit": 1})
        if fj.get("retCode", 0) != 0:
            raise RuntimeError(f"Bybit funding retCode={fj.get('retCode')} retMsg={fj.get('retMsg')}")
        f_list = fj.get("result", {}).get("list", [])
        if f_list:
            fr = safe_float(f_list[0].get("fundingRate"))
            funding = (fr * 100) if fr is not None else None

        # OI
        oi_url = "https://api.bybit.com/v5/market/open-interest"
        oij = get_json(oi_url, {"category": "linear", "symbol": "BTCUSDT", "intervalTime": "5min", "limit": 1})
        if oij.get("retCode", 0) != 0:
            raise RuntimeError(f"Bybit OI retCode={oij.get('retCode')} retMsg={oij.get('retMsg')}")
        oi_list = oij.get("result", {}).get("list", [])
        if oi_list:
            oi = safe_float(oi_list[0].get("openInterest"))

        if funding is not None or oi is not None:
            source = "Bybit"
            return funding, oi, source

        log("[WARN] Bybit funding/OI returned empty list(s).")
    except Exception as e:
        log(f"[WARN] Bybit funding/OI failed: {repr(e)}")

    # --- OKX fallback ---
    try:
        # Funding rate OKX (swap)
        f_url = "https://www.okx.com/api/v5/public/funding-rate"
        fj = get_json(f_url, {"instId": "BTC-USDT-SWAP"})
        data = fj.get("data", [])
        if data:
            fr = safe_float(data[0].get("fundingRate"))
            funding = (fr * 100) if fr is not None else None

        # OI OKX
        oi_url = "https://www.okx.com/api/v5/public/open-interest"
        oij = get_json(oi_url, {"instId": "BTC-USDT-SWAP"})
        data = oij.get("data", [])
        if data:
            oi = safe_float(data[0].get("oi"))

        source = "OKX"
        return funding, oi, source
    except Exception as e:
        log(f"[WARN] OKX funding/OI failed: {repr(e)}")

    return None, None, "N/A"


def get_fear_greed():
    url = "https://api.alternative.me/fng/"
    j = get_json(url, {"limit": 1, "format": "json"})
    data = j["data"][0]
    return int(data["value"]), data["value_classification"]


def get_dominance_btc_usdt():
    g = get_json("https://api.coingecko.com/api/v3/global")
    gj = g["data"]
    btc_dom = float(gj["market_cap_percentage"].get("btc", 0.0))
    total_mcap = float(gj["total_market_cap"].get("usd", 0.0))

    m = get_json(
        "https://api.coingecko.com/api/v3/coins/markets",
        {"vs_currency": "usd", "ids": "tether"},
    )
    usdt_mcap = float(m[0].get("market_cap", 0.0))
    usdt_dom = (usdt_mcap / total_mcap * 100) if total_mcap > 0 else 0.0
    return btc_dom, usdt_dom


# ======================
# Telegram
# ======================
def send_telegram_message(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Faltan variables BOT_TOKEN o CHAT_ID")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}
    if THREAD_ID:
        payload["message_thread_id"] = int(THREAD_ID)

    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()


# ======================
# Bias
# ======================
def build_bias(chg_pct, funding, fng_val, usdt_dom):
    notes = []
    score = 0

    if chg_pct is not None:
        notes.append("BTC 24h en rojo" if chg_pct < 0 else "BTC 24h en verde")

    if fng_val is not None:
        if fng_val <= 10:
            notes.append("Fear extremo (contrarian alcista)")
            if chg_pct is not None and chg_pct < 0:
                score += 1
        elif fng_val >= 75:
            notes.append("Greed alto (riesgo pullback)")
            score -= 1

    if funding is not None:
        if funding > 0.02:
            notes.append("Funding alto (riesgo squeeze bajista)")
            score -= 1
        elif funding < -0.02:
            notes.append("Funding negativo (riesgo squeeze alcista)")
            score += 1

    if usdt_dom is not None and usdt_dom > 7.5:
        notes.append("USDT.D elevado (presión risk-off)")
        score -= 1

    if score >= 1:
        bias = "Leve Alcista"
    elif score <= -1:
        bias = "Leve Bajista"
    else:
        bias = "Neutral"

    return bias, "; ".join(notes) if notes else "Sin suficientes señales"


# ======================
# Report
# ======================
def build_report():
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M (Suiza)")

    price, chg_pct, vol_usd, ticker_src = get_ticker_24h()
    funding, oi, fo_src = get_funding_and_oi()
    btc_dom, usdt_dom = get_dominance_btc_usdt()
    fng_val, fng_cls = get_fear_greed()

    bias, why = build_bias(chg_pct, funding, fng_val, usdt_dom)

    log(f"[INFO] Ticker source: {ticker_src} | Funding/OI source: {fo_src}")

    text = (
        f"Contexto de Mercado — {now}\n\n"
        f"BTC: ${fmt_money(price,0)} ({pct_arrow(chg_pct or 0)} {fmt_pct(chg_pct,2)} 24h)\n"
        f"Volumen 24h (USD): {fmt_money(vol_usd,0)}\n\n"
        f"Funding: {('No disponible' if funding is None else f'{funding:.4f}%')}\n"
        f"Open Interest: {('No disponible' if oi is None else fmt_money(oi,0))}\n\n"
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
        log("ERROR job_send: " + repr(e))


# ======================
# Scheduler
# ======================
scheduler = BackgroundScheduler(timezone=TZ)

@app.on_event("startup")
def startup_event():
    scheduler.add_job(job_send, "cron", hour=1, minute=0, id="tokyo_1am", replace_existing=True)
    scheduler.add_job(job_send, "cron", hour=9, minute=0, id="london_9am", replace_existing=True)
    scheduler.start()
    log("Scheduler started: 01:00 (Tokio), 09:00 (Londres) Europe/Zurich")

@app.get("/")
def root():
    return {"status": "ok", "time": datetime.now(TZ).isoformat()}

@app.get("/send-now")
def send_now():
    job_send()
    return {"sent": True}
