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
scheduler = BackgroundScheduler(timezone=TZ)


# ======================
# Helpers: Data Sources
# ======================
def get_binance_btc_24h():
    url = "https://api.binance.com/api/v3/ticker/24hr"
    r = requests.get(url, params={"symbol": "BTCUSDT"}, timeout=20)
    r.raise_for_status()
    j = r.json()
    price = float(j["lastPrice"])
    chg_pct = float(j["priceChangePercent"])
    vol_usdt = float(j["quoteVolume"])
    return price, chg_pct, vol_usdt


def get_binance_funding_and_oi():
    # Funding + markPrice
    purl = "https://fapi.binance.com/fapi/v1/premiumIndex"
    pr = requests.get(purl, params={"symbol": "BTCUSDT"}, timeout=20)
    pr.raise_for_status()
    pj = pr.json()
    funding = float(pj.get("lastFundingRate", 0.0))
    mark = float(pj.get("markPrice", 0.0))

    # Open Interest
    oiurl = "https://fapi.binance.com/fapi/v1/openInterest"
    oir = requests.get(oiurl, params={"symbol": "BTCUSDT"}, timeout=20)
    oir.raise_for_status()
    oij = oir.json()
    oi = float(oij.get("openInterest", 0.0))
    return funding, mark, oi


def get_binance_trend_ema():
    # Trend simple: EMA50 vs EMA200 en 1H
    url = "https://api.binance.com/api/v3/klines"
    r = requests.get(url, params={"symbol": "BTCUSDT", "interval": "1h", "limit": 220}, timeout=20)
    r.raise_for_status()
    klines = r.json()
    closes = [float(k[4]) for k in klines]

    def ema(values, period):
        k = 2 / (period + 1)
        e = values[0]
        for v in values[1:]:
            e = v * k + e * (1 - k)
        return e

    # usamos últimos 200 para estabilidad
    series = closes[-200:]
    ema50 = ema(series, 50)
    ema200 = ema(series, 200)
    last = closes[-1]

    if ema50 > ema200 and last > ema50:
        trend = "Alcista"
    elif ema50 < ema200 and last < ema50:
        trend = "Bajista"
    else:
        trend = "Rango / Mixta"

    return trend, ema50, ema200


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
    # CoinGecko global (BTC dominance) + USDT dominance aproximada
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


def arrow(pct: float) -> str:
    return "↑" if pct > 0 else ("↓" if pct < 0 else "→")


# ======================
# Telegram sender
# ======================
def send_telegram_message(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Faltan variables BOT_TOKEN o CHAT_ID en Railway")

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


def build_report():
    price, chg_pct, vol_usdt = get_binance_btc_24h()
    funding, mark, oi = get_binance_funding_and_oi()
    trend, ema50, ema200 = get_binance_trend_ema()
    fng_val, fng_cls = get_fear_greed()
    btc_dom, usdt_dom = get_dominance_btc_usdt()

    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")

    # Mensaje sin emojis raros (solo lo normal)
    text = (
        f"Contexto de Mercado — {now} (Suiza)\n\n"
        f"BTC: ${price:,.0f} ({arrow(chg_pct)} {chg_pct:.2f}% 24h)\n"
        f"Volumen 24h (USDT): {vol_usdt:,.0f}\n\n"
        f"Tendencia (1H EMA50/200): {trend}\n"
        f"EMA50: {ema50:,.0f} | EMA200: {ema200:,.0f}\n\n"
        f"OI (Binance Futures): {oi:,.0f}\n"
        f"Funding (último): {funding*100:.4f}%\n"
        f"Mark Price: {mark:,.0f}\n\n"
        f"BTC.D: {btc_dom:.2f}%\n"
        f"USDT.D (aprox): {usdt_dom:.2f}%\n\n"
        f"Fear & Greed: {fng_val} ({fng_cls})\n"
    )
    return text


def job_send():
    try:
        msg = build_report()
        send_telegram_message(msg)
        print("Sent Telegram report OK", flush=True)
    except Exception as e:
        print("ERROR job_send:", repr(e), flush=True)


# ======================
# Scheduler
# ======================
@app.on_event("startup")
def startup_event():
    # 01:00 Suiza (Tokio en tu referencia)
    scheduler.add_job(job_send, "cron", hour=1, minute=0, id="tokyo_1am", replace_existing=True)
    # 09:00 Suiza (Londres en tu referencia)
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
