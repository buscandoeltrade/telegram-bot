import os
import requests

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
THREAD_ID = os.getenv("THREAD_ID")

def send_message():
    text = (
        "🧠 Contexto de Mercado — AM\n\n"
        "Tendencia BTC: Pendiente\n"
        "OI: Pendiente\n"
        "Funding: Pendiente\n"
        "Liquidaciones 24h: Pendiente\n"
        "BTC.D / USDT.D: Pendiente\n"
        "Fear & Greed: Pendiente\n\n"
        "Conclusión: Pendiente"
    )

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "message_thread_id": int(THREAD_ID),
        "text": text
    }

    r = requests.post(url, data=payload, timeout=20)
    r.raise_for_status()

if __name__ == "__main__":
    send_message()
