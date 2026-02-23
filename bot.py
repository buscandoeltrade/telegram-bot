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

# Opcional (si algún día quieres liquidaciones reales 24h)
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "").strip()

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


def get_bybit_funding_and_oi():
    """
    Bybit public API (sin key):
      - fundingRate (actual)
      - openInterest (contratos) y openInterestValue (USD) cuando está disponible
    """
    url = "https://api.bybit.com/v5/market/tickers"
    params = {"category": "linear", "symbol": "BTCUSDT"}
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    j = r.json()

    # Estructura típica: {"retCode":0,"result":{"list":[{...}]}}
    item = (j.get("result", {}).get("list") or [None])[0]
    if not item:
        raise RuntimeError("Bybit tickers: sin datos")

    funding = float(item.get("fundingRate", 0.0) or 0.0)

    # OI puede venir en diferentes llaves dependiendo del producto/response
    oi_contracts = item.get("openInterest")
    oi_value = item.get("openInterestValue")

    oi_contracts = float(oi_contracts) if oi_contracts not in (None, "", "0") else None
    oi_value = float(oi_value) if oi_value not in (None, "", "0") else None

    return funding, oi_contracts, oi_value


def pct_arrow(x: float) -> str:
    return "↑" if x > 0 else ("↓" if x < 0 else "→")


def fmt_money(x: float, decimals=0) -> str:
    if x is None:
        return "No disponible"
    return f"{x:,.{decimals}f}"


def fmt_pct(x: float, decimals=2) -> str:
    if x is None:
        return "No disponible"
    return f"{x:.{decimals}f}%"


def compute_bias(chg_pct, funding, usdt_dom, fng_val):
    """
    Heurística simple (útil, no mágica):
    - Precio 24h (+/-)
    - Funding (si está muy positivo: crowd long; si negativo: crowd short)
    - USDT.D alto = risk-off (tendencia bajista o cautela)
    - Fear&Greed extremo: contrarian
    """
    score = 0
    reasons = []

    if chg_pct is not None:
        if chg_pct > 0:
            score += 1
            reasons.append("BTC 24h en verde")
        elif chg_pct < 0:
            score -= 1
            reasons.append("BTC 24h en rojo")

    if funding is not None:
        # funding en Bybit viene como decimal (ej 0.0001 = 0.01%)
        if funding > 0.0002:
            score -= 1
            reasons.append("Funding positivo (crowd long)")
        elif funding < -0.0002:
            score += 1
            reasons.append("Funding negativo (crowd short)")

    if usdt_dom is not None:
        if usdt_dom >= 8.0:
            score -= 1
            reasons.append("USDT.D alto (risk-off)")
        elif usdt_dom <= 6.5:
            score += 1
            reasons.append("USDT.D bajo (risk-on)")

    if fng_val is not None:
        if fng_val <= 20:
            score += 1
            reasons.append("Fear extremo (contrarian alcista)")
        elif fng_val >= 80:
            score -= 1
            reasons.append("Greed extremo (contrarian bajista)")

    if score >= 2:
        bias = "Alcista"
    elif score <= -2:
        bias = "Bajista"
    else:
        bias = "Neutral"

    return bias, reasons[:3]  # máximo 3 razones para que sea útil/rápido


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

    price = chg_pct = vol_usd = None
    btc_dom = usdt_dom = None
    fng_val = fng_cls = None
    funding = None
    oi_contracts = None
    oi_value = None

    # CoinGecko
    try:
        price, chg_pct, vol_usd = get_btc_price_data()
    except Exception as e:
        print("ERROR get_btc_price_data:", repr(e), flush=True)

    try:
        btc_dom, usdt_dom = get_btc_usdt_dominance()
    except Exception as e:
        print("ERROR get_btc_usdt_dominance:", repr(e), flush=True)

    # Fear & Greed
    try:
        fng_val, fng_cls = get_fear_greed()
    except Exception as e:
        print("ERROR get_fear_greed:", repr(e), flush=True)

    # Funding + OI (Bybit)
    try:
        funding, oi_contracts, oi_value = get_bybit_funding_and_oi()
    except Exception as e:
        print("ERROR get_bybit_funding_and_oi:", repr(e), flush=True)

    # Liquidaciones 24h (sin key => no disponible)
    liq_24h = None  # aquí lo conectamos cuando tengas key

    bias, reasons = compute_bias(chg_pct, funding, usdt_dom, fng_val)

    lines = []
    lines.append(f"Contexto de Mercado — {now} (Suiza)")
    lines.append("")

    # BTC + volumen
    if price is not None:
        lines.append(f"BTC: ${fmt_money(price, 0)} ({pct_arrow(chg_pct)} {chg_pct:.2f}% 24h)")
        lines.append(f"Volumen 24h (USD): {fmt_money(vol_usd, 0)}")
    else:
        lines.append("BTC: No disponible (API)")
        lines.append("Volumen 24h: No disponible")

    lines.append("")

    # Funding / OI / Liquidaciones
    if funding is not None:
        lines.append(f"Funding (Bybit): {funding*100:.4f}%")
    else:
        lines.append("Funding: No disponible")

    if oi_value is not None:
        lines.append(f"Open Interest (Bybit, USD): {fmt_money(oi_value, 0)}")
    elif oi_contracts is not None:
        lines.append(f"Open Interest (Bybit, contratos): {fmt_money(oi_contracts, 0)}")
    else:
        lines.append("Open Interest: No disponible")

    lines.append(f"Liquidaciones 24h: {fmt_money(liq_24h, 0)}")

    lines.append("")

    # Dominancias + F&G
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
    if reasons:
        lines.append(f"Conclusión (BIAS): {bias} — " + "; ".join(reasons))
    else:
        lines.append(f"Conclusión (BIAS): {bias}")

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
    scheduler.add_job(job_send, "cron", hour=1, minute=0, id="tokyo_1am", replace_existing=True)
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
