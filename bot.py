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
SHOW_TOP = 5            # команда /matches: сколько матчей со статистикой показать (по ударам в створ)
MAX_STATS_CALLS = 20     # лимит запросов статистики за один вызов /matches
MAX_UNLINKED_SHOWN = 10  # сколько "не найденных в API-Football" матчей показать для диагностики
SCAN_INTERVAL = 90       # пауза между проверками (сек), чтобы не выжигать лимит API
MAX_SCAN_MINUTES = 180   # через сколько минут непрерывного поиска остановиться само
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


def best_candidate(fb, fixtures):
    """Ближайший по названиям матч API-Football (без учёта счёта и минуты) — для диагностики."""
    best, best_score = None, 0.0
    for fx in fixtures:
        score = (name_sim(fx["home"], fb["home"]) + name_sim(fx["away"], fb["away"])) / 2
        if score > best_score:
            best, best_score = fx, score
    return best, best_score


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
last_api_error = ""


def _num(v):
    try:
        return float(str(v).replace("%", ""))
    except Exception:
        return 0.0


def get_team_statistics(fixture_id, home_id, away_id):
    """Live-статистика отдельно по каждой команде. out['ok'] = True, если API вернул данные."""
    global last_api_error
    empty = lambda: {"on_target": 0, "shots": 0, "inside_box": 0, "corners": 0,
                     "possession": 0, "xg": 0.0, "yellow": 0, "red": 0}
    out = {"home": empty(), "away": empty(), "ok": False}
    try:
        res = requests.get(f"{API_HOST}/fixtures/statistics", headers=HEADERS,
                           params={"fixture": fixture_id}, timeout=10).json()
        errs = res.get("errors")
        if errs:
            last_api_error = str(errs)[:200]
            print(f"API-Football errors: {errs}")
        for team_stat in res.get("response", []):
            tid = team_stat.get("team", {}).get("id")
            side = "home" if tid == home_id else "away" if tid == away_id else None
            if not side:
                continue
            out["ok"] = True
            for item in team_stat.get("statistics", []):
                t, val = item.get("type"), item.get("value")
                if not val:
                    continue
                if t == "Shots on Goal":
                    out[side]["on_target"] = int(_num(val))
                elif t == "Total Shots":
                    out[side]["shots"] = int(_num(val))
                elif t == "Shots insidebox":
                    out[side]["inside_box"] = int(_num(val))
                elif t == "Corner Kicks":
                    out[side]["corners"] = int(_num(val))
                elif t == "Ball Possession":
                    out[side]["possession"] = int(_num(val))
                elif t == "expected_goals":
                    out[side]["xg"] = _num(val)
                elif t == "Yellow Cards":
                    out[side]["yellow"] = int(_num(val))
                elif t == "Red Cards":
                    out[side]["red"] = int(_num(val))
    except Exception as e:
        last_api_error = str(e)[:200]
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


def find_signals():
    """Один проход анализа. Возвращает (список_сигналов, ошибка_или_None)."""
    try:
        fb_list = load_fonbet_live_football()
    except Exception as e:
        print(f"Фонбет недоступен: {e}")
        return [], "fonbet"
    fb_list = [fb for fb in fb_list
               if fb["minute"] is not None and 10 <= fb["minute"] <= MAX_MINUTE]
    if not fb_list:
        return [], None

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

    candidates.sort(key=lambda x: x["weight"], reverse=True)
    top = []
    for c in candidates:
        if sent_cache.get(c["fid"]) == c["total"]:
            continue
        top.append(c)
        if len(top) == MAX_RESULTS:
            break
    return top, None


def send_signals(top):
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


# ---------------------------------------------------------------- непрерывный поиск
stop_event = threading.Event()
search_thread = None


def search_loop():
    started = time.time()
    scans, fonbet_fails, warned = 0, 0, False
    while not stop_event.is_set():
        try:
            top, err = find_signals()
        except Exception as e:
            print(f"Ошибка цикла поиска: {e}")
            top, err = [], "error"
        scans += 1
        if err:
            fonbet_fails += 1
            if fonbet_fails >= 3 and not warned:
                send_telegram_message("⚠️ Фонбет несколько раз не отвечал. Продолжаю пробовать "
                                      "(проверьте /debug).")
                warned = True
        else:
            fonbet_fails = 0
        if top:
            send_signals(top)
            send_telegram_message("🏁 Поиск завершён. Напишите <b>поиск</b>, чтобы искать дальше.")
            return
        if time.time() - started > MAX_SCAN_MINUTES * 60:
            send_telegram_message(
                f"⏹ Искал {MAX_SCAN_MINUTES} мин ({scans} проверок) — подходящих моментов не было. "
                "Напишите <b>поиск</b>, чтобы запустить снова.")
            return
        stop_event.wait(SCAN_INTERVAL)
    send_telegram_message("⏹ Поиск остановлен.")


def start_search():
    global search_thread
    if search_thread and search_thread.is_alive():
        send_telegram_message("⏳ Поиск уже идёт. Чтобы остановить, напишите <b>стоп</b>.")
        return
    stop_event.clear()
    send_telegram_message(
        f"🔎 <b>Поиск запущен.</b> Проверяю Фонбет каждые {SCAN_INTERVAL} сек и пришлю матч, "
        "как только появится команда с явным давлением. Остановить: <b>стоп</b>.")
    search_thread = threading.Thread(target=search_loop, daemon=True)
    search_thread.start()


def stop_search():
    if search_thread and search_thread.is_alive():
        stop_event.set()
    else:
        send_telegram_message("Сейчас поиск не запущен.")


def send_chunks(blocks, sep="\n\n"):
    """Склеивает блоки в сообщения не длиннее лимита Telegram."""
    cur = ""
    for blk in blocks:
        if cur and len(cur) + len(sep) + len(blk) > 3500:
            send_telegram_message(cur)
            cur = ""
        cur = blk if not cur else cur + sep + blk
    if cur:
        send_telegram_message(cur)


def show_matches():
    """/matches — как Фонбет и API-Football соотносятся: связки, статистика, диагностика."""
    global last_api_error
    last_api_error = ""
    try:
        send_telegram_message("🔎 Собираю матчи с Фонбета и статистику...")
        try:
            fb_all = load_fonbet_live_football()
        except Exception as e:
            send_telegram_message(f"❌ Фонбет не отвечает: <code>{html.escape(str(e))[:300]}</code>")
            return
        fb_list = [fb for fb in fb_all if fb["minute"] is not None and fb["minute"] <= MAX_MINUTE]
        if not fb_list:
            send_telegram_message(f"На Фонбете {len(fb_all)} live-матчей, но с минутой до {MAX_MINUTE} нет.")
            return

        fixtures = load_apifootball_live()
        linked, unlinked = [], []
        for fb in fb_list:
            fx = find_fixture_for_fb(fb, fixtures)
            (linked if fx else unlinked).append((fb, fx))

        rows = []
        for fb, fx in linked[:MAX_STATS_CALLS]:
            st = get_team_statistics(fx["fid"], fx["home_id"], fx["away_id"])
            ot = st["home"]["on_target"] + st["away"]["on_target"]
            rows.append({"fb": fb, "fx": fx, "st": st, "ot": ot})
        with_stats = sorted([r for r in rows if r["st"]["ok"]], key=lambda r: r["ot"], reverse=True)
        no_stats = [r for r in rows if not r["st"]["ok"]]

        send_telegram_message(
            f"📡 Фонбет, live-футбол: <b>{len(fb_all)}</b> (до {MAX_MINUTE}-й минуты: <b>{len(fb_list)}</b>)\n"
            f"⚽️ В API-Football live-матчей: <b>{len(fixtures)}</b>\n"
            f"🔗 Сопоставлено: <b>{len(linked)}</b> | не найдено: <b>{len(unlinked)}</b>\n"
            f"📊 Статистика получена: <b>{len(with_stats)}</b> из {len(rows)} запрошенных"
            + (f"\n⚠️ Ответ API: <code>{html.escape(last_api_error)}</code>" if last_api_error else ""))

        # Связанные матчи со статистикой — топ по ударам в створ (без порога)
        for r in with_stats[:SHOW_TOP]:
            fb, fx, st = r["fb"], r["fx"], r["st"]
            h, a = st["home"], st["away"]
            home = html.escape(translate_name(fb["home"]))
            away = html.escape(translate_name(fb["away"]))
            send_telegram_message(
                f"⚽️ <b>{home}</b> — <b>{away}</b>\n"
                f"🏆 {html.escape(fb['league'])}\n"
                f"⏱ Фонбет: {fb['minute']}' {fb['hg']}:{fb['ag']} | "
                f"API: {fx['elapsed']}' {fx['hg']}:{fx['ag']}\n"
                f"🔗 API-Football: {html.escape(fx['home'])} — {html.escape(fx['away'])}\n\n"
                f"🎯 В створ: <b>{h['on_target']}</b> : <b>{a['on_target']}</b>\n"
                f"💥 Всего ударов: {h['shots']} : {a['shots']}\n"
                f"🥅 Из штрафной: {h['inside_box']} : {a['inside_box']}\n"
                f"📐 Угловые: {h['corners']} : {a['corners']}\n"
                f"⚖️ Владение: {h['possession']}% : {a['possession']}%\n"
                f"📈 xG: {h['xg']:.2f} : {a['xg']:.2f}\n"
                f"🟨 Жёлтые: {h['yellow']} : {a['yellow']} | 🟥 Красные: {h['red']} : {a['red']}")

        # Сопоставлены, но статистики нет
        if no_stats:
            blocks = [f"• {html.escape(r['fb']['home'])} — {html.escape(r['fb']['away'])} "
                      f"({r['fb']['minute']}')" for r in no_stats]
            send_chunks(["🚫 <b>Сопоставлены, но API не вернул статистику:</b>\n" + "\n".join(blocks)])

        # Не нашли пару в API-Football — показываем почему
        if unlinked:
            lines = []
            for fb, _ in unlinked[:MAX_UNLINKED_SHOWN]:
                cand, sc = best_candidate(fb, fixtures)
                head = (f"• {html.escape(fb['home'])} — {html.escape(fb['away'])} "
                        f"({fb['minute']}', {fb['hg']}:{fb['ag']})")
                if cand and sc >= 0.5:
                    why = []
                    if (cand["hg"], cand["ag"]) != (fb["hg"], fb["ag"]):
                        why.append(f"счёт API {cand['hg']}:{cand['ag']}")
                    if abs(cand["elapsed"] - fb["minute"]) > MINUTE_TOLERANCE:
                        why.append(f"минута API {cand['elapsed']}'")
                    if sc < NAME_MATCH_MIN:
                        why.append("названия слишком разные")
                    lines.append(f"{head}\n   ≈ {html.escape(cand['home'])} — {html.escape(cand['away'])} "
                                 f"(сходство {sc:.2f}; {', '.join(why) or 'причина неясна'})")
                else:
                    lines.append(f"{head}\n   нет похожих в API-Football (лига может не поддерживаться)")
            extra = len(unlinked) - MAX_UNLINKED_SHOWN
            if extra > 0:
                lines.append(f"…и ещё {extra}")
            send_chunks(["❓ <b>Не найдены в API-Football:</b>\n" + "\n".join(lines)])

        if len(linked) > MAX_STATS_CALLS:
            send_telegram_message(f"ℹ️ Статистика запрошена только по {MAX_STATS_CALLS} из {len(linked)} "
                                  "сопоставленных матчей (экономия лимита API).")
    except Exception as e:
        print(f"Ошибка /matches: {e}")
        send_telegram_message("⚠️ Ошибка при сборе матчей.")


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
                    start_search()
                elif text in ("матчи", "/matches"):
                    threading.Thread(target=show_matches, daemon=True).start()
                elif text in ("стоп", "/stop", "stop"):
                    stop_search()
                elif text == "/debug":
                    debug_fonbet()
                elif text in ("/start", "старт"):
                    send_telegram_message(
                        "👋 Бот готов!\n\nНапишите <b>поиск</b> — найду live-матчи на Фонбете до "
                        f"{MAX_MINUTE}-й минуты и подскажу, какая команда может забить, "
                        "по live-статистике и предматчевому прогнозу. Бот сам проверяет лайв, пока не найдёт матч.\n"
                        "<b>матчи</b> — показать, как матчи Фонбета сопоставляются со статистикой.\n"
                        "<b>стоп</b> — остановить поиск.\n"
                        "<b>/debug</b> — проверка связи с Фонбетом.")
        except Exception as e:
            print(f"Ошибка прослушки Telegram: {e}")
            time.sleep(5)


if __name__ == "__main__":
    t = threading.Thread(target=telegram_listener, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
