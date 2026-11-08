import os
import threading
import time
import requests
from flask import Flask
app = Flask(__name__)
@app.route("/")
def home():
    return "Bot is active and running!"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
API_KEY = os.environ.get("API_KEY")
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Ошибка Telegram: {e}")
def check_live_matches():
    headers = {"x-apisports-key": API_KEY}
    url = "https://v3.football.api-sports.io/fixtures"
    params = {"live": "all"}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        data = response.json()
        if "response" in data:
            for match in data["response"]:
                elapsed = match["fixture"]["status"]["elapsed"]
                home_team = match["teams"]["home"]["name"]
                away_team = match["teams"]["away"]["name"]
                home_goals = match["goals"]["home"]
                away_goals = match["goals"]["away"]
                if home_goals is None or away_goals is None:
                    continue
                # Условие: идет 75-83 минута, счет 0:0
                if 75 <= elapsed <= 83 and home_goals == 0 and away_goals == 0:
                    message = (
                        f" **МАТЧ ПОД ЛЕСЕНКУ (LIVE)**\n\n"
                        f"⚽️ {home_team} vs {away_team}\n"
                        f"⏱ Минута: {elapsed}'\n"
                        f" Счет: {home_goals}:{away_goals}\n\n"
                        f" Ставка: **ТБ (0.5)**"
                    )
                    send_telegram_message(message)
    except Exception as e:
        print(f"Ошибка API: {e}")
def worker_loop():
    send_telegram_message("🤖 Бот успешно запущен в облаке Render!")
    while True:
        check_live_matches()
        time.sleep(120) # Проверка каждые 2 минуты
if __name__ == "__main__":
    t = threading.Thread(target=worker_loop)
    t.daemon = True
    t.start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

