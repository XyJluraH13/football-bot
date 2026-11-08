import os
import threading
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

# Состояние бота (включен/выключен пользователем)
bot_active = True
last_update_id = 0

# Словарь для проверки результатов после матча
tracked_matches = {}

def translate_name(text):
    try:
        return translator.translate(text)
    except:
        return text

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload).json()
        return res.get("result", {}).get("message_id")
    except Exception as e:
        print(f"Ошибка Telegram: {e}")
        return None

def check_telegram_commands():
    global bot_active, last_update_id
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"offset": last_update_id + 1, "timeout": 2}
    try:
        res = requests.get(url, params=params, timeout=5).json()
        if "result" in res:
            for update in res["result"]:
                last_update_id = update["update_id"]
                if "message" in update and "text" in update["message"]:
                    text = update["message"]["text"].strip().lower()
                    chat = str(update["message"]["chat"]["id"])
                    
                    if chat == str(CHAT_ID):
                        if text == "/off":
                            bot_active = False
                            send_telegram_message("⏸ **Бот выключен.** Сигналы временно не отправляются. Напишите /on, чтобы включить.")
                        elif text == "/on":
                            bot_active = True
                            send_telegram_message("▶️ **Бот включен!** Снова сканирую матчи...")
                        elif text == "/status":
                            status_text = "🟢 Включен и работает" if bot_active else "🔴 Выключен"
                            send_telegram_message(f"Статус бота: {status_text}")
    except Exception as e:
        print(f"Ошибка проверки команд: {e}")

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
    
    # 1. Сет 0:0 до 65 минуты
    if home_goals == 0 and away_goals == 0:
        if shots >= 6:
            return "ТБ (1.0)", "Высокая активность: много ударов", "1.75"
        elif shots >= 3 or corners >= 6:
            return "ТБ (0.5)", "Давление и стандарты у ворот", "1.42"
            
    # 2. Счет 1:0 или 0:1 до 65 минуты
    elif (home_goals + away_goals) == 1:
        if shots >= 7:
            return "ТБ (1.5)", "Открытая игра, назревает второй гол", "1.85"
        elif shots >= 4 and corners >= 5:
            return "Обе Забьют (Да)", "Проигрывающие активно идут вперед", "1.95"

    return None, None, None

def check_live_matches():
    headers = {"x-apisports-key": API_KEY}
    url = "https://v3.football.api-sports.io/fixtures"
    params = {"live": "all"}
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=15).json()
        if "response" not in response:
            return

        candidates = []

        for match in response["response"]:
            fixture_id = match["fixture"]["id"]
            status = match["fixture"]["status"]["short"]
            elapsed = match["fixture"]["status"]["elapsed"] or 0
            
            home_team_en = match["teams"]["home"]["name"]
            away_team_en = match["teams"]["away"]["name"]
            home_goals = match["goals"]["home"] if match["goals"]["home"] is not None else 0
            away_goals = match["goals"]["away"] if match["goals"]["away"] is not None else 0
            league_name = match["league"]["name"]

            # Проверка завершенных матчей (итоги)
            if status in ["FT", "AET", "PEN"] and fixture_id in tracked_matches:
                bet_info = tracked_matches.pop(fixture_id)
                prediction = bet_info["prediction"]
                total_goals = home_goals + away_goals
                is_win = False
                
                if prediction == "ТБ (0.5)" and total_goals > 0: is_win = True
                elif prediction == "ТБ (1.0)" and total_goals > 1: is_win = True
                elif prediction == "ТБ (1.5)" and total_goals > 1: is_win = True
                elif prediction == "Обе Забьют (Да)" and home_goals > 0 and away_goals > 0: is_win = True
                
                status_icon = "✅ **СТАВКА ЗАШЛА! (ВИН)**" if is_win else "❌ **СТАВКА НЕ ЗАШЛА**"
                report_msg = (
                    f"📊 **ИТОГ МАТЧА**\n\n"
                    f"⚽️ {bet_info['home_ru']} — {bet_info['away_ru']}\n"
                    f"🏁 Итог: **{home_goals}:{away_goals}**\n"
                    f"🎯 Прогноз: {prediction}\n\n"
                    f"Результат: {status_icon}"
                )
                send_telegram_message(report_msg)
                continue

            # Сбор кандидатов (до 65 минуты, еще не отслеживаем)
            if bot_active and elapsed <= 65 and fixture_id not in tracked_matches:
                stats = get_match_statistics(fixture_id)
                prediction, reason, odd = analyze_and_predict(elapsed, home_goals, away_goals, stats)
                
                if prediction:
                    # Оценка веса матча для выбора топ-2-3 (по сумме ударов и угловых)
                    score_weight = stats["shots_on_target"] * 2 + stats["corners"]
                    candidates.append({
                        "fixture_id": fixture_id,
                        "home_en": home_team_en,
                        "away_en": away_team_en,
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

        # Сортируем кандидатов по наилучшей статистике и берем ТОП-3
        candidates.sort(key=lambda x: x["weight"], reverse=True)
        top_candidates = candidates[:3]

        for match in top_candidates:
            fixture_id = match["fixture_id"]
            home_ru = translate_name(match["home_en"])
            away_ru = translate_name(match["away_en"])
            
            message = (
                f"🔥 **ТОП-ПРОГНОЗ (LIVE)**\n\n"
                f"⚽️ **{home_ru}** vs **{away_ru}**\n"
                f"🏆 {match['league']}\n"
                f"⏱ Минута: **{match['elapsed']}'** | Счет: **{match['home_goals']}:{match['away_goals']}**\n"
                f"📊 В створ: {match['stats']['shots_on_target']} | Угловые: {match['stats']['corners']}\n\n"
                f"💡 Ставка: **{match['prediction']}**\n"
                f"📈 Кф (примерно): **{match['odd']}**\n"
                f"📌 Причина: _{match['reason']}_"
            )
            
            send_telegram_message(message)
            
            # Сохраняем для проверки после матча
            tracked_matches[fixture_id] = {
                "home_ru": home_ru,
                "away_ru": away_ru,
                "prediction": match["prediction"]
            }

    except Exception as e:
        print(f"Ошибка проверки матчей: {e}")

def worker_loop():
    send_telegram_message("🤖 Умный бот запущен! Управление командами: /on (включить), /off (выключить), /status.")
    while True:
        check_telegram_commands()
        if bot_active:
            check_live_matches()
        time.sleep(60)

if __name__ == "__main__":
    t = threading.Thread(target=worker_loop)
    t.daemon = True
    t.start()
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
