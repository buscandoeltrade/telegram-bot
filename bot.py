import os
import requests
from datetime import datetime

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
THREAD_ID = os.getenv("THREAD_ID")

def send_message():

```
text = f"""
```

🧠 Contexto de Mercado — AM

Tendencia BTC: Alcista intradía
OI: +4.8%
Funding: Positivo elevado
Liquidaciones 24h: $210M
BTC.D / USDT.D: Ligeramente alcista
Fear & Greed: 72 (Greed)

Conclusión: Riesgo de long squeeze si el precio pierde soporte.
"""

```
url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

payload = {
    "chat_id": CHAT_ID,
    "message_thread_id": THREAD_ID,
    "text": text
}

requests.post(url, data=payload)
```

if **name** == "**main**":
send_message()
