import json
from datetime import datetime
from pathlib import Path

DATA_FILE = "data/results.json"

def log_trade(pair, direction, result, confidence, expiry):
    Path("data").mkdir(parents=True, exist_ok=True)
    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {"history": []}

    trade = {
        "pair": pair,
        "direction": direction,
        "result": result,
        "confidence": confidence,
        "expiry": expiry,
        "timestamp": datetime.utcnow().isoformat(),
    }

    data["history"].append(trade)
    history = data["history"][-50:]  # Keep last 50

    wins = sum(1 for t in history if t["result"] == "W")
    winrate = round((wins / len(history)) * 100, 2)

    data["winrate"] = winrate
    data["last_result"] = result
    data["live_trade"] = trade

    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def main():
    # Example test
    log_trade("EUR/USD", "BUY", "W", 75.2, "1m")

if __name__ == "__main__":
    main()
