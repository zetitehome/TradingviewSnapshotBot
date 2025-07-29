import random
import json
import time
import requests
from datetime import datetime

OTC_PAIRS = [
    "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC",
    "AUDUSD-OTC", "EURJPY-OTC", "GBPJPY-OTC"
]

EXPIRY_OPTIONS = [1, 3, 5, 15]  # minutes

UIVISION_WEBHOOK_URL = "http://192.168.1.171:3333/signal"

def generate_fake_signal():
    pair = random.choice(OTC_PAIRS)
    direction = random.choice(["BUY", "SELL"])
    confidence = random.randint(60, 95)
    expiry = random.choice(EXPIRY_OPTIONS)

    signal = {
        "pair": pair,
        "action": direction.lower(),
        "expiry": expiry,
        "amount": 1,  # fixed $1 trade amount
        "winrate": confidence,
        "timestamp": datetime.utcnow().isoformat()
    }
    return signal

def send_signal(signal):
    try:
        resp = requests.post(UIVISION_WEBHOOK_URL, json=signal)
        if resp.status_code == 200:
            print(f"✅ Signal sent: {signal['pair']} {signal['action']} at {signal['expiry']}min")
        else:
            print(f"❌ Failed to send signal: HTTP {resp.status_code}")
    except Exception as e:
        print(f"❌ Error sending signal: {e}")

def main():
    while True:
        signal = generate_fake_signal()
        send_signal(signal)
        time.sleep(60)  # generate every 60 seconds

if __name__ == "__main__":
    main()
        