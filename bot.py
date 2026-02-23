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
THREAD_ID = os.getenv("THREAD_ID", "").strip()  # opcional

# Opcional: para mostrar Fear & Greed de Coinglass (si NO lo pones, no pasa nada)
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "").strip()

TZ = pytz.timezone("Europe/Zurich")
app = FastAPI(title="Telegram Market Context Bot")

# ======================
# Data sources
# ======================
def get_binance_btc_24h():
    # Nota: en Railway a veces Binance bloquea por región/IP. Si te pasa, avísame y lo cambiamos a otro endpoint.
    url = "https://api.binance.com/api/v3/ticker/24hr"
    r = requests.get(url, params={"symbol": "BTCUSDT"}, timeout=20)
    r.raise_for_status()
    j = r.json()
    price = float(j["lastPrice"])
    chg_pct = float(j["priceChangePercent"])
    vol_usdt = float(j["quoteVolume"])
    return price, chg_pct, vol_usdt


def get_binance_funding_and_oi():
    # Funding + Mark
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


def get_fear_greed_alternative():
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

    # USDT market cap
    m = requests.get(
        "https://api.coingecko.com/api/v3/coins/markets",
        params={"vs_currency": "usd", "ids": "tether"},
        timeout=20,
    )
    m.raise_for_status()
    usdt_mcap = float(m.json()[0].get("market_cap", 0.0))

    usdt_dom = (usdt_mcap / total_mcap * 100) if total_mcap > 0 else 0.0
    return btc_dom, usdt_dom


def get_coinglass_fng_optional():
    """
    Opcional.
    Coinglass normalmente requiere API key.
    Si NO tienes COINGLASS_API_KEY, regresamos None y NO rompemos nada.
    """
    if not COINGLASS_API_KEY:
        return None

    try:
        # Endpoint típico (puede cambiar). Si falla, regresamos None sin romper el bot.
        url = "https://open-api.coinglass.com/public/v2/index/fearGreed"
        headers = {"coinglassSecret": COINGLASS_API_KEY}
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        j = r.json()

        # Estructura común: {"code":"0","data":[{"value":14,...}]}
        data = j.get("data")
        if isinstance(data, list) and data:
            val = data[0].get("value")
            if val is not None:
                return int(val)
        return None
    except Exception:
        return None


# ======================
# Smart conclusion (SMC-lite)
# ======================
def classify_environment(chg_pct_24h, fng_value, funding, usdt_dom):
    """
    Reglas simples (pero útiles) para tu reporte.
    No intenta "predecir", solo clasificar contexto.
    """

    # thresholds
    funding_pct = funding * 100.0  # funding en porcentaje
    btc_red = chg_pct_24h < 0
    fear_extreme = fng_value <= 10
    greed_high = fng_value >= 70
    usdt_high = usdt_dom >= 7.5  # ajustable

    # 🔵 Acumulación probable (contrarian alcista)
    # Miedo extremo + BTC en rojo + funding bajo/neutral + USDT.D alto (risk-off) => posible piso/absorción
    if fear_extreme and btc_red and funding_pct <= 0.02 and usdt_high:
        return "🔵 Acumulación probable", "Contrarian alcista (pánico/risk-off suele marcar zonas de oportunidad)"

    # 🔴 Riesgo alto (euforia)
    # Greed alto + funding elevado + USDT.D bajo => apalancamiento y euforia, riesgo de squeeze
    if greed_high and funding_pct >= 0.05 and usdt_dom <= 6.0:
        return "🔴 Riesgo alto", "Euforia + apalancamiento (riesgo de barrida/flush)"

    # 🟡 Transición
    return "🟡 Transición", "Contexto mixto (esperar confirmación / estructura)"


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

    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()


def build_report():
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M (Suiza)")

    price, chg_pct, vol_usdt = get_binance_btc_24h()
    funding, mark, oi = get_binance_funding_and_oi()
    fng_val, fng_cls = get_fear_greed_alternative()
    btc_dom, usdt_dom = get_dominance_btc_usdt()

    # opcional coinglass
    cg_fng = get_coinglass_fng_optional()

    # Clasificación smart money
    env_label, env_reason = classify_environment(chg_pct, fng_val, funding, usdt_dom)

    # Formato
    arrow = "↑" if chg_pct > 0 else ("↓" if chg_pct < 0 else "→")
    funding_pct = funding * 100.0

    lines = []
    lines.append(f"Contexto de Mercado — {now}\n")
    lines.append(f"BTC: ${price:,.0f} ({arrow} {chg_pct:.2f}% 24h)")
    lines.append(f"Volumen 24h (USDT): {vol_usdt:,.0f}\n")

    lines.append(f"Funding: {funding_pct:.4f}%")
    lines.append(f"Open Interest: {oi:,.0f}\n")

    lines.append(f"BTC.D: {btc_dom:.2f}%")
    lines.append(f"USDT.D (aprox): {usdt_dom:.2f}%")

    lines.append(f"Fear & Greed (Alternative): {fng_val} ({fng_cls})")
    if cg_fng is not None:
        lines.append(f"Fear & Greed (Coinglass): {cg_fng}")
    lines.append("")

    lines.append(f"Entorno (SM): {env_label}")
    lines.append(f"Conclusión (BIAS): {env_reason}")

    return "\n".join(lines)


def job_send():
    try:
        msg = build_report()
        send_telegram_message(msg)
    except Exception as e:
        print("ERROR job_send:", repr(e), flush=True)


# ======================
# Scheduler + FastAPI
# ======================
scheduler = BackgroundScheduler(timezone=TZ)

@app.on_event("startup")
def startup_event():
    # 01:00 Suiza (Tokyo open según tu regla)
    scheduler.add_job(job_send, "cron", hour=1, minute=0, id="tokyo_1am", replace_existing=True)
    # 09:00 Suiza (London open según tu regla)
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
