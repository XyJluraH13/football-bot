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
MIN_ODD = 1.20           # коэффициент не ниже 1.2
MIN_PRESSURE = 6.0       # минимальный индекс голевых моментов
MAX_RESULTS = 3
TOP_LEAGUE_IDS = {2, 3, 848, 39, 140, 135, 78, 61, 88, 94}

# --- Фонбет (неофициальный API, может меняться!) ---
FONBET_BASE_URL = os.environ.get("FONBET_BASE_URL", "https://line-lb51.bk6bba-resources.com")
FONBET_LANG = os.environ.get("FONBET_LANG", "en")
FONBET_SCOPE = os.environ.get("FONBET_SCOPE", "1600")
# id факторов "Тотал больше" в линии Фонбета. Если кф не находятся — запустите /debug
FONBET_OVER_IDS = {int(x) for x in os.environ.get("FONBET_OVER_IDS", "930,1696").split(",")}
# Матчи и кф берутся ТОЛЬКО с Фонбета. API-Football нужен лишь для статистики ударов/угловых
# (на Фонбете её нет). Если статистику найти не удалось — показывать матч без неё?
ALLOW_WITHOUT_STATS = False
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


def parse_line(f):
    """Достаёт линию тотала (x.5) из фактора Фонбета."""
    pt = f.get("pt")
    if pt:
        m = re.search(r"\d+(?:[.,]\d+)?", str(pt))
        if m:
            x = float(m.group().replace(",", "."))
            if (x * 2) % 2 == 1:
                return x
    p = f.get("p")
    if p is not None:
        try:
            x = float(p)
        except (TypeError, ValueError):
            return None
        if x > 20:
            x /= 100.0
        if (x * 2) % 2 == 1:
            return x
    return None


def load_fonbet_live_football():
    """Возвращает список live-футбольных матчей Фонбета:
    [{id, home, away, league, hg, ag, minute, totals:{линия: кф_больше}}]
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
    factors = {}
    for cf in data.get("customFactors", []):
        factors[cf.get("e")] = cf.get("factors", [])

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
        totals = {}
        for f in factors.get(ev["id"], []):
            if f.get("f") in FONBET_OVER_IDS and not f.get("b") and f.get("v"):
                line = parse_line(f)
                if line is not None:
                    totals[line] = float(f["v"])
        result.append({
            "id": ev["id"], "home": ev["team1"], "away": ev["team2"], "league": league,
            "hg": int(misc.get("team1Score", 0) or 0), "ag": int(misc.get("team2Score", 0) or 0),
            "minute": minute, "totals": totals,
        })
    return result, factors


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
def get_match_statistics(fixture_id):
    stats = {"on_target": 0, "shots": 0, "inside_box": 0, "corners": 0}
    try:
        res = requests.get(f"{API_HOST}/fixtures/statistics", headers=HEADERS,
                           params={"fixture": fixture_id}, timeout=10).json()
        for team_stat in res.get("response", []):
            for item in team_stat.get("statistics", []):
                t, val = item.get("type"), item.get("value")
                if not val:
                    continue
                if t == "Shots on Goal":
                    stats["on_target"] += to_int(val)
                elif t == "Total Shots":
                    stats["shots"] += to_int(val)
                elif t == "Shots insidebox":
                    stats["inside_box"] += to_int(val)
                elif t == "Corner Kicks":
                    stats["corners"] += to_int(val)
    except Exception as e:
        print(f"Ошибка статистики {fixture_id}: {e}")
    return stats


def pressure_score(elapsed, stats):
    base = (stats["on_target"] * 2.0
            + stats["inside_box"] * 1.0
            + (stats["shots"] - stats["on_target"]) * 0.4
            + stats["corners"] * 0.6)
    pace = base / max(elapsed, 1) * 10
    return base, pace


# ---------------------------------------------------------------- поиск
def load_apifootball_live():
    """Один запрос: live-матчи API-Football (нужны только для привязки статистики)."""
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
            "league_id": m["league"]["id"], "league": m["league"]["name"],
        })
    return out


def search_top_matches():
    try:
        send_telegram_message("🔎 <b>Ищу матчи в лайве Фонбета...</b>")

        # 1) Матчи и кф — только Фонбет
        try:
            fb_list, _ = load_fonbet_live_football()
        except Exception as e:
            print(f"Фонбет недоступен: {e}")
            send_telegram_message("❌ Не удалось загрузить линию Фонбета. Проверьте /debug.")
            return
        if not fb_list:
            send_telegram_message("❌ На Фонбете сейчас нет live-футбола.")
            return

        pre = []
        for fb in fb_list:
            if fb["minute"] is None or fb["minute"] < 10 or fb["minute"] > MAX_MINUTE:
                continue
            line = fb["hg"] + fb["ag"] + 0.5      # 0:0 -> ТБ 0.5, 1:0 -> ТБ 1.5 ...
            odd = fb["totals"].get(line)
            if odd is None or odd < MIN_ODD:
                continue
            pre.append({"fb": fb, "line": line, "odd": odd})

        if not pre:
            send_telegram_message(
                f"⚠️ На Фонбете нет матчей с минутой ≤ {MAX_MINUTE} и кф на ТБ ≥ {MIN_ODD}.")
            return

        # 2) Статистика моментов — из API-Football (на Фонбете её нет)
        fixtures = load_apifootball_live()
        candidates = []
        for p in pre:
            fb = p["fb"]
            fx = find_fixture_for_fb(fb, fixtures)
            if fx:
                stats = get_match_statistics(fx["fid"])
                base, pace = pressure_score(fb["minute"], stats)
                if base < MIN_PRESSURE:
                    continue
                weight = base + pace * 2 + (30 if fx["league_id"] in TOP_LEAGUE_IDS else 0)
            else:
                if not ALLOW_WITHOUT_STATS:
                    continue
                stats, weight = None, 0
            candidates.append({**p, "stats": stats, "weight": weight,
                               "fid": fb["id"], "total": fb["hg"] + fb["ag"]})

        if not candidates:
            send_telegram_message(
                "⚠️ Матчи на Фонбете есть, но по ним нет достаточной статистики моментов "
                f"(порог {MIN_PRESSURE}) или статистика недоступна.")
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
            fb = c["fb"]
            home = html.escape(translate_name(fb["home"]))
            away = html.escape(translate_name(fb["away"]))
            s = c["stats"]
            if s:
                stat_line = (f"🎯 В створ: {s['on_target']} | Ударов: {s['shots']} | "
                             f"Из штрафной: {s['inside_box']} | Угловые: {s['corners']}\n")
            else:
                stat_line = "📊 Статистика моментов недоступна\n"
            msg = (
                f"🔥 <b>ТОП-ПРОГНОЗ (LIVE, Фонбет)</b>\n\n"
                f"⚽️ <b>{home}</b> — <b>{away}</b>\n"
                f"🏆 {html.escape(fb['league'])}\n"
                f"⏱ Минута: <b>{fb['minute']}'</b> | Счёт: <b>{fb['hg']}:{fb['ag']}</b>\n"
                f"{stat_line}\n"
                f"📌 Ставка: <b>ТБ {c['line']}</b>\n"
                f"💰 Кф Фонбет: <b>{c['odd']}</b>\n\n"
                f'🔗 <a href="https://www.fonbet.ru/live/football">Открыть Фонбет</a>'
            )
            send_telegram_message(msg)
    except Exception as e:
        print(f"Ошибка поиска: {e}")
        send_telegram_message("⚠️ Ошибка при обработке данных.")


def debug_fonbet():
    """/debug — проверка связи с Фонбетом и вывод id факторов для настройки."""
    try:
        fb_list, factors = load_fonbet_live_football()
    except Exception as e:
        send_telegram_message(f"❌ Фонбет не отвечает: <code>{html.escape(str(e))[:300]}</code>")
        return
    with_totals = [x for x in fb_list if x["totals"]]
    send_telegram_message(
        f"📡 Live-футбола на Фонбете: <b>{len(fb_list)}</b>\n"
        f"С найденными тоталами (id {sorted(FONBET_OVER_IDS)}): <b>{len(with_totals)}</b>")
    if fb_list and not with_totals:
        ev = fb_list[0]
        rows = []
        for f in factors.get(ev["id"], [])[:40]:
            rows.append(f"f={f.get('f')} p={f.get('p')} pt={f.get('pt')} v={f.get('v')}")
        send_telegram_message(
            f"Тоталы не распознаны. Факторы матча {html.escape(ev['home'])} — "
            f"{html.escape(ev['away'])}:\n<code>" + html.escape("\n".join(rows)) + "</code>\n\n"
            "Найдите строки с p/pt вида 0.5, 1.5, 2.5 и задайте их f в переменной "
            "FONBET_OVER_IDS (для «больше» — обычно меньший id из пары).")


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
                        "👋 Бот готов!\n\nНапишите <b>поиск</b> — найду лайв-матчи из линии Фонбета "
                        f"с большим количеством моментов, кф ≥ {MIN_ODD}, до {MAX_MINUTE}-й минуты.\n"
                        "<b>/debug</b> — проверка связи с Фонбетом.")
        except Exception as e:
            print(f"Ошибка прослушки Telegram: {e}")
            time.sleep(5)


if __name__ == "__main__":
    t = threading.Thread(target=telegram_listener, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
