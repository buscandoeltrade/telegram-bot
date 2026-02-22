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

app = FastAPI(title="Telegram Market Report Bot")


# ======================
# Helpers: Data Sources
# ======================
def get_btc_price_data():
    """
    CoinGecko: BTC price, 24h change %, 24h volume (USD)
    """
    url = "https://api.coingecko.com/api/v3/coins/bitcoin"
    params = {
        "localization": "false",
        "tickers": "false",
        "market_data": "true",
        "community_data": "false",
        "developer_data": "false",
        "sparkline": "false",
    }
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    md = r.json()["market_data"]

    price = float(md["current_price"]["usd"])
    chg_pct = float(md.get("price_change_percentage_24h", 0.0) or 0.0)
    vol_usd = float(md["total_volume"]["usd"])

    return price, chg_pct, vol_usd


def get_btc_usdt_dominance():
    """
    CoinGecko Global:
      - BTC dominance: market_cap_percentage.btc
      - USDT dominance aprox: tether_marketcap / total_marketcap * 100
    """
    g = requests.get("https://api.coingecko.com/api/v3/global", timeout=25)
    g.raise_for_status()
    data = g.json()["data"]

    btc_dom = float(data["market_cap_percentage"].get("btc", 0.0) or 0.0)
    total_mcap = float(data["total_market_cap"].get("usd", 0.0) or 0.0)

    # USDT market cap
    m = requests.get(
        "https://api.coingecko.com/api/v3/coins/markets",
        params={"vs_currency": "usd", "ids": "tether"},
        timeout=25,
    )
    m.raise_for_status()
    usdt_mcap = float(m.json()[0].get("market_cap", 0.0) or 0.0)

    usdt_dom = (usdt_mcap / total_mcap * 100.0) if total_mcap > 0 else 0.0
    return btc_dom, usdt_dom


def get_fear_greed():
    """
    Alternative.me Fear & Greed Index
    """
    url = "https://api.alternative.me/fng/"
    r = requests.get(url, params={"limit": 1, "format": "json"}, timeout=25)
    r.raise_for_status()
    d = r.json()["data"][0]
    value = int(d["value"])
    cls = d["value_classification"]
    return value, cls


def pct_arrow(x: float) -> str:
    return "↑" if x > 0 else ("↓" if x < 0 else "→")


# ======================
# Telegram sender
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

    r = requests.post(url, json=payload, timeout=25)
    r.raise_for_status()


def build_report() -> str:
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")

    # Defaults por si alguna API falla
    price = chg_pct = vol_usd = None
    btc_dom = usdt_dom = None
    fng_val = fng_cls = None

    # BTC price + volume + change
    try:
        price, chg_pct, vol_usd = get_btc_price_data()
    except Exception as e:
        print("ERROR get_btc_price_data:", repr(e), flush=True)

    # Dominance
    try:
        btc_dom, usdt_dom = get_btc_usdt_dominance()
    except Exception as e:
        print("ERROR get_btc_usdt_dominance:", repr(e), flush=True)

    # Fear & Greed
    try:
        fng_val, fng_cls = get_fear_greed()
    except Exception as e:
        print("ERROR get_fear_greed:", repr(e), flush=True)

    # Formato (sin emojis raros; si quieres emojis luego los metemos)
    lines = []
    lines.append(f"Contexto de Mercado — {now} (Suiza)")
    lines.append("")

    if price is not None:
        lines.append(f"BTC: ${price:,.0f} ({pct_arrow(chg_pct)} {chg_pct:.2f}% 24h)")
        lines.append(f"Volumen 24h (USD): {vol_usd:,.0f}")
    else:
        lines.append("BTC: No disponible (API)")
        lines.append("Volumen 24h: No disponible")

    lines.append("")

    if btc_dom is not None and usdt_dom is not None:
        lines.append(f"BTC.D: {btc_dom:.2f}%")
        lines.append(f"USDT.D (aprox): {usdt_dom:.2f}%")
    else:
        lines.append("BTC.D: No disponible")
        lines.append("USDT.D: No disponible")

    lines.append("")

    if fng_val is not None and fng_cls is not None:
        lines.append(f"Fear & Greed: {fng_val} ({fng_cls})")
    else:
        lines.append("Fear & Greed: No disponible")

    lines.append("")
    lines.append("Conclusión: Pendiente")

    return "\n".join(lines)


def job_send():
    try:
        msg = build_report()
        send_telegram_message(msg)
        print("OK job_send: message sent", flush=True)
    except Exception as e:
        print("ERROR job_send:", repr(e), flush=True)


# ======================
# Scheduler + FastAPI
# ======================
scheduler = BackgroundScheduler(timezone=TZ)


@app.on_event("startup")
def startup_event():
    # 01:00 Suiza (Tokyo open en tu referencia)
    scheduler.add_job(job_send, "cron", hour=1, minute=0, id="tokyo_1am", replace_existing=True)
    # 09:00 Suiza (London open en tu referencia)
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
