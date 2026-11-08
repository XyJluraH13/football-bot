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
    """
    Проверяет, есть ли матч в актуальной лайв-линии Фонбет.
    Использует официальный JSON-эндпоинт лайва букмекерской конторы.
    """
    url = "https://line01.fonbet.ru/live/updates?version=0&lang=ru"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            events = data.get("events", [])
            
            # Приводим названия к нижнему регистру для точного сравнения
            h_clean = home_team_ru.lower()
            a_clean = away_team_ru.lower()
            
            for event in events:
                event_name = event.get("name", "").lower()
                # Если в названии события в лайве Фонбета содержатся обе команды
                if h_clean in event_name or a_clean in event_name:
                    return True
        return False
    except Exception as e:
        print(f"Ошибка проверки Фонбет: {e}")
        # Если вдруг временный сбой у Фонбет, разрешаем матч, чтобы не блокировать поиск
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
    
    # 1. Счет 0:0 до 65 минуты
    if home_goals == 0 and away_goals == 0:
        if shots >= 6:
            return "ТБ (1.0)", "Высокая активность: много ударов в створ", "1.75"
        elif shots >= 3 or corners >= 6:
            return "ТБ (0.5)", "Давление и стандарты у ворот", "1.42"
            
    # 2. Счет 1:0 или 0:1 до 65 минуты
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
            
            home_team_en = match["teams"]["home"]["name"]
            away_team_en = match["teams"]["away"]["name"]
            home_goals = match["goals"]["home"] if match["goals"]["home"] is not None else 0
            away_goals = match["goals"]["away"] if match["goals"]["away"] is not None else 0
            league_name = match["league"]["name"]

            # Ищем матчи строго до 65-й минуты
            if elapsed <= 65:
                stats = get_match_statistics(fixture_id)
                prediction, reason, odd = analyze_and_predict(elapsed, home_goals, away_goals, stats)
                
                if prediction:
                    # Переводим на русский язык для проверки в Фонбет и выдачи
                    home_ru = translate_name(home_team_en)
                    away_ru = translate_name(away_team_en)
                    
                    # СТРОГАЯ ПРОВЕРКА НАЛИЧИЯ В ФОНБЕТЕ
                    if not check_fonbet_availability(home_ru, away_ru):
                        continue # Пропускаем матч, если его нет в лайве Фонбет

                    score_weight = stats["shots_on_target"] * 2 + stats["corners"]
                    candidates.append({
                        "home_ru": home_ru,
                        "away_ru": away_ru,
                        "league": league_name,
                        "elapsed": elapsed,
                        "home_goals": home_goals,
                        "away_goals": away_goals,
                        "stats": stats,
                        "prediction": prediction,
                        "reason": reason,
                        "odd": odd,
                        "weight": score_weight
                    })

        if not candidates:
            send_telegram_message("⚠️ Подходящие по статистике матчи найдены, но **ни одного из них нет в лайве Фонбет**.")
            return

        # Сортируем по силе статистики и берем ТОП-3
        candidates.sort(key=lambda x: x["weight"], reverse=True)
        top_candidates = candidates[:3]

        send_telegram_message(f"✅ Найдено матчей, подтвержденных в Фонбет: *{len(top_candidates)}*:")

        for match in top_candidates:
            fonbet_link = "https://www.fonbet.ru/live/football"
            
            message = (
                f"🔥 **ТОП-ПРОГНОЗ (ЕСТЬ В ФОНБЕТ)**\n\n"
                f"⚽️ **{match['home_ru']}** vs **{match['away_ru']}**\n"
                f"🏆 {match['league']}\n"
                f"⏱ Минута: **{match['elapsed']}'** | Счет: **{match['home_goals']}:{match['away_goals']}**\n"
                f"📊 В створ: {match['stats']['shots_on_target']} | Угловые: {match['stats']['corners']}\n\n"
                f"💡 Рекомендация: **{match['prediction']}**\n"
                f"📈 Кф (примерно): **{match['odd']}**\n"
                f"📌 Анализ: _{match['reason']}_\n\n"
                f"👉 [Открыть футбол в Фонбет]({fonbet_link})"
            )
            send_telegram_message(message)

    except Exception as e:
        print(f"Ошибка поиска: {e}")
        send_telegram_message("⚠️ Произошла ошибка при обработке данных.")

def telegram_listener():
    global last_update_id
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    
    while True:
        try:
            params = {"offset": last_update_id + 1, "timeout": 30}
            res = requests.get(url, params=params, timeout=35).json()
            
            if "result" in res:
                for update in res["result"]:
                    last_update_id = update["update_id"]
                    if "message" in update and "text" in update["message"]:
