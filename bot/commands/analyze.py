import json

def analyze():
    # Replace with your real analysis logic later
    result = {
        "pair": "EUR/USD",
        "side": "buy",
        "expiry": 3,
        "confidence": 82
    }
    print(json.dumps(result))

if __name__ == "__main__":
    analyze()
