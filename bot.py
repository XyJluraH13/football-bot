import os
import re
import time
import html
import threading
from difflib import SequenceMatcher

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

# ====== НАСТРОЙКИ ======
MAX_MINUTE = 65          # не позже 65-й минуты
MIN_TEAM_PRESSURE = 4.0  # минимальный live-индекс моментов у команды, которая может забить
PREMATCH_WEIGHT = 0.08   # вклад предматчевого прогноза (0..100% -> 0..8 баллов)
MAX_RESULTS = 3
TOP_LEAGUE_IDS = {2, 3, 848, 39, 140, 135, 78, 61, 88, 94}

# --- Фонбет (неофициальный API, может меняться!) ---
FONBET_BASE_URL = os.environ.get("FONBET_BASE_URL", "https://line-lb51.bk6bba-resources.com")
FONBET_LANG = os.environ.get("FONBET_LANG", "en")
FONBET_SCOPE = os.environ.get("FONBET_SCOPE", "1600")
NAME_MATCH_MIN = 0.72    # порог похожести названий команд (0..1)
MINUTE_TOLERANCE = 6     # допуск по минуте при сопоставлении матча
# =======================

API_HOST = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
translator = GoogleTranslator(source="auto", target="ru")
last_update_id = 0
sent_cache = {}  # fixture_id -> total_goals


# ---------------------------------------------------------------- утилиты
def translate_name(text):
    try:
        return translator.translate(text) or text
    except Exception:
        return text


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    try:
        res = requests.post(url, json=payload, timeout=15).json()
        return res.get("result", {}).get("message_id")
    except Exception as e:
        print(f"Ошибка Telegram: {e}")
        return None


def to_int(v):
    try:
        return int(str(v).replace("%", ""))
    except Exception:
        return 0


# ---------------------------------------------------------------- Фонбет
BAD_LEAGUE_WORDS = ("esport", "e-sport", "cyber", "virtual", "fifa", "efootball",
                    "кибер", "виртуал", "симуляц", "fc 2", "pes")
STOP_TOKENS = {"fc", "cf", "sc", "ac", "fk", "afc", "club", "de", "the", "sk", "cd", "ud", "if"}


def norm_name(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-zа-яё0-9 ]+", " ", s)
    toks = [t for t in s.split() if t not in STOP_TOKENS]
    return " ".join(toks)


def tags(s):
    s = (s or "").lower()
    t = set(re.findall(r"\bu\s?(\d{2})\b", s))
    if re.search(r"\b(women|w|жен)\b|\(w\)", s):
        t.add("w")
    if re.search(r"\b(ii|b|res|reserves)\b", s):
        t.add("res")
    return t


def name_sim(a, b):
    if tags(a) != tags(b):
        return 0.0
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return 0.0
    r = SequenceMatcher(None, na, nb).ratio()
    # бонус, если все слова одного названия входят в другое ("Man City" ~ "Manchester City")
    ta, tb = set(na.split()), set(nb.split())
    if ta and tb and (ta <= tb or tb <= ta):
        r = max(r, 0.85)
    # сокращения: "Man City" ~ "Manchester City" (каждое слово — начало слова в другом названии)
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if short and all(any(w.startswith(x) or x.startswith(w) for w in long_) for x in short):
        r = max(r, 0.8)
    return r


def load_fonbet_live_football():
    """Возвращает список live-футбольных матчей Фонбета:
    [{id, home, away, league, hg, ag, minute}]
    Бросает исключение, если Фонбет недоступен."""
    url = f"{FONBET_BASE_URL}/events/listBase"
    params = {"lang": FONBET_LANG, "scopeMarket": FONBET_SCOPE}
    r = requests.get(url, params=params, headers=UA, timeout=25)
    r.raise_for_status()
    data = r.json()

    sports = {s["id"]: s for s in data.get("sports", [])}

    def root(sid):
        seen = 0
        while sid in sports and sports[sid].get("parentId") and seen < 6:
            sid = sports[sid]["parentId"]
            seen += 1
        return sports.get(sid)

    miscs = {m["id"]: m for m in data.get("eventMiscs", [])}
    result = []
    for ev in data.get("events", []):
        if ev.get("level") not in (1, None):
            continue
        if ev.get("place") not in ("live", None):
            continue
        if not ev.get("team1") or not ev.get("team2"):
            continue
        seg = sports.get(ev.get("sportId"))
        rt = root(ev.get("sportId"))
        if not seg or not rt:
            continue
        root_name = (rt.get("name") or "").lower()
        if root_name not in ("football", "футбол", "soccer"):
            continue
        league = seg.get("name") or ""
        if any(w in league.lower() for w in BAD_LEAGUE_WORDS):
            continue
        if "(" in ev["team1"] or "(" in ev["team2"]:   # кибер-матчи с никами игроков
            continue
        misc = miscs.get(ev["id"], {})
        if not misc and ev.get("place") is None:
            continue
        minute = None
        if misc.get("timerSeconds") is not None:
            minute = int(misc["timerSeconds"] // 60)
        result.append({
            "id": ev["id"], "home": ev["team1"], "away": ev["team2"], "league": league,
            "hg": int(misc.get("team1Score", 0) or 0), "ag": int(misc.get("team2Score", 0) or 0),
            "minute": minute,
        })
    return result


def find_fixture_for_fb(fb, fixtures):
    """Ищет в live-списке API-Football матч, соответствующий матчу Фонбета (только ради статистики)."""
    best, best_score = None, 0.0
    for fx in fixtures:
        if fx["hg"] != fb["hg"] or fx["ag"] != fb["ag"]:
            continue
        if fb["minute"] is not None and abs(fx["elapsed"] - fb["minute"]) > MINUTE_TOLERANCE:
            continue
        s_home = name_sim(fx["home"], fb["home"])
        s_away = name_sim(fx["away"], fb["away"])
        if s_home < 0.6 or s_away < 0.6:
            continue
        score = (s_home + s_away) / 2
        if score >= NAME_MATCH_MIN and score > best_score:
            best, best_score = fx, score
    return best


# ---------------------------------------------------------------- API-Football
def get_team_statistics(fixture_id, home_id, away_id):
    """Live-статистика отдельно по каждой команде."""
    empty = lambda: {"on_target": 0, "shots": 0, "inside_box": 0, "corners": 0}
    out = {"home": empty(), "away": empty()}
    try:
        res = requests.get(f"{API_HOST}/fixtures/statistics", headers=HEADERS,
                           params={"fixture": fixture_id}, timeout=10).json()
        for team_stat in res.get("response", []):
            tid = team_stat.get("team", {}).get("id")
            side = "home" if tid == home_id else "away" if tid == away_id else None
            if not side:
                continue
            for item in team_stat.get("statistics", []):
                t, val = item.get("type"), item.get("value")
                if not val:
                    continue
                if t == "Shots on Goal":
                    out[side]["on_target"] += to_int(val)
                elif t == "Total Shots":
                    out[side]["shots"] += to_int(val)
                elif t == "Shots insidebox":
                    out[side]["inside_box"] += to_int(val)
                elif t == "Corner Kicks":
                    out[side]["corners"] += to_int(val)
    except Exception as e:
        print(f"Ошибка статистики {fixture_id}: {e}")
    return out


_prematch_cache = {}


def get_prematch(fixture_id):
    """Предматчевый прогноз API-Football: шансы на победу, сравнение атаки и формы.
    Возвращает {'home': 0..100, 'away': 0..100, 'win': {...}} или None."""
    if fixture_id in _prematch_cache:
        return _prematch_cache[fixture_id]
    result = None
    try:
        res = requests.get(f"{API_HOST}/predictions", headers=HEADERS,
                           params={"fixture": fixture_id}, timeout=10).json()
        r = (res.get("response") or [None])[0]
        if r:
            pct = r.get("predictions", {}).get("percent", {})
            comp = r.get("comparison", {})
            result = {"win": {"home": to_int(pct.get("home")), "away": to_int(pct.get("away"))}}
            for side in ("home", "away"):
                parts = [to_int(pct.get(side)),
                         to_int(comp.get("att", {}).get(side)),
                         to_int(comp.get("form", {}).get(side))]
                parts = [p for p in parts if p > 0]
                result[side] = sum(parts) / len(parts) if parts else 0
    except Exception as e:
        print(f"Ошибка прогноза {fixture_id}: {e}")
    _prematch_cache[fixture_id] = result
    return result


def live_index(st):
    return (st["on_target"] * 2.0 + st["inside_box"] * 1.0
            + max(st["shots"] - st["on_target"], 0) * 0.4 + st["corners"] * 0.6)


# ---------------------------------------------------------------- поиск
def load_apifootball_live():
    """Live-матчи API-Football (нужны для статистики и предматчевых прогнозов)."""
    try:
        res = requests.get(f"{API_HOST}/fixtures", headers=HEADERS,
                           params={"live": "all"}, timeout=20).json()
    except Exception as e:
        print(f"API-Football недоступен: {e}")
        return []
    out = []
    for m in res.get("response", []):
        out.append({
            "fid": m["fixture"]["id"], "elapsed": m["fixture"]["status"]["elapsed"] or 0,
            "hg": m["goals"]["home"] or 0, "ag": m["goals"]["away"] or 0,
            "home": m["teams"]["home"]["name"], "away": m["teams"]["away"]["name"],
            "home_id": m["teams"]["home"]["id"], "away_id": m["teams"]["away"]["id"],
            "league_id": m["league"]["id"],
        })
    return out


def search_top_matches():
    try:
        send_telegram_message("🔎 <b>Ищу матчи на Фонбете и анализирую моменты...</b>")

        # 1) Матчи — только Фонбет
        try:
            fb_list = load_fonbet_live_football()
        except Exception as e:
            print(f"Фонбет недоступен: {e}")
            send_telegram_message("❌ Не удалось загрузить линию Фонбета. Проверьте /debug.")
            return
        fb_list = [fb for fb in fb_list
                   if fb["minute"] is not None and 10 <= fb["minute"] <= MAX_MINUTE]
        if not fb_list:
            send_telegram_message(f"❌ На Фонбете нет live-матчей с минутой до {MAX_MINUTE}.")
            return

        # 2) Статистика и прогнозы — API-Football
        fixtures = load_apifootball_live()
        candidates = []
        for fb in fb_list:
            fx = find_fixture_for_fb(fb, fixtures)
            if not fx:
                continue
            st = get_team_statistics(fx["fid"], fx["home_id"], fx["away_id"])
            pre = get_prematch(fx["fid"])
            scores = {}
            for side in ("home", "away"):
                live = live_index(st[side])
                prem = pre[side] if pre else 0
                scores[side] = (live, live + prem * PREMATCH_WEIGHT)
            side = "home" if scores["home"][1] >= scores["away"][1] else "away"
            live_val, total_val = scores[side]
            if live_val < MIN_TEAM_PRESSURE:
                continue
            weight = total_val + (30 if fx["league_id"] in TOP_LEAGUE_IDS else 0)
            candidates.append({
                "fb": fb, "side": side, "stats": st, "pre": pre, "weight": weight,
                "fid": fb["id"], "total": fb["hg"] + fb["ag"],
            })

        if not candidates:
            send_telegram_message("⚠️ Матчи на Фонбете есть, но явного давления у какой-либо команды пока нет.")
            return

        candidates.sort(key=lambda x: x["weight"], reverse=True)
        top = []
        for c in candidates:
            if sent_cache.get(c["fid"]) == c["total"]:
                continue
            top.append(c)
            if len(top) == MAX_RESULTS:
                break
        if not top:
            send_telegram_message("ℹ️ Новых сигналов нет — по найденным матчам уже отправлял.")
            return

        send_telegram_message(f"✅ Найдено: <b>{len(top)}</b>")
        for c in top:
            sent_cache[c["fid"]] = c["total"]
            fb, side, st, pre = c["fb"], c["side"], c["stats"], c["pre"]
            home = translate_name(fb["home"])
            away = translate_name(fb["away"])
            team = home if side == "home" else away
            me, opp = st[side], st["away" if side == "home" else "home"]
            lines = [
                f"🎯 Удары в створ: {me['on_target']} против {opp['on_target']}",
                f"📐 Угловые: {me['corners']} против {opp['corners']}",
                f"🥅 Удары из штрафной: {me['inside_box']} против {opp['inside_box']}",
            ]
            if pre and pre[side]:
                lines.append(f"📊 Предматчевый прогноз: шансы на победу {pre['win'][side]}%, "
                             f"преимущество по атаке и форме {int(pre[side])}%")
            msg = (
                f"🔥 <b>ВОЗМОЖЕН ГОЛ (LIVE)</b>\n\n"
                f"⚽️ <b>{html.escape(home)}</b> — <b>{html.escape(away)}</b>\n"
                f"🏆 {html.escape(fb['league'])}\n"
                f"⏱ Минута: <b>{fb['minute']}'</b> | Счёт: <b>{fb['hg']}:{fb['ag']}</b>\n\n"
                f"👉 Возможно, гол забьёт: <b>{html.escape(team)}</b>\n\n"
                + "\n".join(lines) +
                f'\n\n🔗 <a href="https://www.fonbet.ru/live/football">Открыть Фонбет</a>'
            )
            send_telegram_message(msg)
    except Exception as e:
        print(f"Ошибка поиска: {e}")
        send_telegram_message("⚠️ Ошибка при обработке данных.")


def debug_fonbet():
    """/debug — проверка связи с Фонбетом."""
    try:
        fb_list = load_fonbet_live_football()
    except Exception as e:
        send_telegram_message(f"❌ Фонбет не отвечает: <code>{html.escape(str(e))[:300]}</code>")
        return
    ok = [x for x in fb_list if x["minute"] is not None and x["minute"] <= MAX_MINUTE]
    send_telegram_message(
        f"📡 Live-футбола на Фонбете: <b>{len(fb_list)}</b>\n"
        f"Из них с минутой до {MAX_MINUTE}: <b>{len(ok)}</b>")


def telegram_listener():
    global last_update_id
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    while True:
        try:
            params = {"offset": last_update_id + 1, "timeout": 30}
            res = requests.get(url, params=params, timeout=35).json()
            for update in res.get("result", []):
                last_update_id = update["update_id"]
                msg = update.get("message")
                if not msg or "text" not in msg:
                    continue
                if str(msg["chat"]["id"]) != str(CHAT_ID):
                    continue
                text = msg["text"].strip().lower()
                if text in ("поиск", "/search", "найти"):
                    search_top_matches()
                elif text == "/debug":
                    debug_fonbet()
                elif text in ("/start", "старт"):
                    send_telegram_message(
                        "👋 Бот готов!\n\nНапишите <b>поиск</b> — найду live-матчи на Фонбете до "
                        f"{MAX_MINUTE}-й минуты и подскажу, какая команда может забить, "
                        "по live-статистике и предматчевому прогнозу.\n"
                        "<b>/debug</b> — проверка связи с Фонбетом.")
        except Exception as e:
            print(f"Ошибка прослушки Telegram: {e}")
            time.sleep(5)


if __name__ == "__main__":
    t = threading.Thread(target=telegram_listener, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
