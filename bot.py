import os
import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from datetime import datetime
import pytz

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
THREAD_ID = os.getenv("THREAD_ID")

def get_market_data():

    # Fear & Greed
    fg = requests.get("https://api.alternative.me/fng/").json()
    fear_greed = fg["data"][0]["value"]

    # Funding BTC
    funding = requests.get(
        "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"
    ).json()["lastFundingRate"]

    # BTC Dominance
    btc_d = requests.get(
        "https://api.coingecko.com/api/v3/global"
    ).json()["data"]["market_cap_percentage"]["btc"]

    text = (
        "🧠 Contexto de Mercado — AM\n\n"
        f"Tendencia BTC: Analizando...\n"
        f"Funding: {funding}\n"
        f"BTC Dominance: {btc_d:.2f}%\n"
        f"Fear & Greed: {fear_greed}\n\n"
        "Conclusión: Pendiente análisis manual"
    )

    return text


def send_message():

    text = get_market_data()

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHAT_ID,
        "message_thread_id": int(THREAD_ID),
        "text": text
    }

    requests.post(url, data=payload)


scheduler = BlockingScheduler(timezone="Europe/Zurich")

# Tokio 1 AM
scheduler.add_job(send_message, "cron", hour=1, minute=0)

# Londres 9 AM
scheduler.add_job(send_message, "cron", hour=9, minute=0)

print("Bot corriendo...")
scheduler.start()
