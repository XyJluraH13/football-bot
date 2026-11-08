import os
import time
import requests
from flask import Flask
from deep_translator import GoogleTranslator

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is active!"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
API_KEY = os.environ.get("API_KEY")

translator = GoogleTranslator(source='auto', target='ru')
last_update_id = 0

def translate_name(text):
    try:
        return translator.translate(text)
    except:
        return text

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
    try:
        res = requests.post(url, json=payload).json()
        return res.get("result", {}).get("message_id")
    except Exception as e:
        print(f"Ошибка Telegram: {e}")
        return None

def check_fonbet_availability(home_team_ru, away_team_ru):
    url = "https://line01.fonbet.ru/live/updates?version=0&lang=ru"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            events = data.get("events", [])
            h_clean = home_team_ru.lower()
            a_clean = away_team_ru.lower()
            for event in events:
                event_name = event.get("name", "").lower()
                if h_clean in event_name or a_clean in event_name:
                    return True
        return False
    except Exception as e:
        print(f"Ошибка проверки Фонбет: {e}")
        return True

def get_match_statistics(fixture_id):
    headers = {"x-apisports-key": API_KEY}
    url = "https://v3.football.api-sports.io/fixtures/statistics"
    params = {"fixture": fixture_id}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=10).json()
        stats = {"shots_on_target": 0, "corners": 0}
        if "response" in res and res["response"]:
            for team_stat in res["response"]:
                for item in team_stat.get("statistics", []):
                    if item["type"] == "Shots on Goal" and item["value"]:
                        stats["shots_on_target"] += int(item["value"])
                    if item["type"] == "Corner Kicks" and item["value"]:
                        stats["corners"] += int(item["value"])
        return stats
    except:
        return {"shots_on_target": 0, "corners": 0}

def analyze_and_predict(elapsed, home_goals, away_goals, stats):
    shots = stats["shots_on_target"]
    corners = stats["corners"]
    
    if home_goals == 0 and away_goals == 0:
        if shots >= 6:
            return "ТБ (1.0)", "Высокая активность: много ударов в створ", "1.75"
        elif shots >= 3 or corners >= 6:
            return "ТБ (0.5)", "Давление и стандарты у ворот", "1.42"
            
    elif (home_goals + away_goals) == 1:
        if shots >= 7:
            return "ТБ (1.5)", "Открытая игра, назревает второй гол", "1.85"
        elif shots >= 4 and corners >= 5:
            return "Обе Забьют (Да)", "Проигрывающие активно идут вперед", "1.95"

    return None, None, None

def search_top_matches():
    headers = {"x-apisports-key": API_KEY}
    url = "https://v3.football.api-sports.io/fixtures"
    params = {"live": "all"}
    
    try:
        send_telegram_message("🔍 *Сканирую матчи и сверяю с линией Фонбет...*")
        response = requests.get(url, headers=headers, params=params, timeout=15).json()
        
        if "response" not in response or not response["response"]:
            send_telegram_message("❌ Сейчас нет подходящих матчей в лайве.")
            return

        candidates = []

        for match in response["response"]:
            fixture_id = match["fixture"]["id"]
            elapsed = match["fixture"]["status"]["elapsed"] or 0
            
            home_team_
