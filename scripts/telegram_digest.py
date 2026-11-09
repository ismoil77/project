"""Daily Telegram digest: weather in Dushanbe + outfit from wardrobe/wardrobe.json.

Run by .github/workflows/telegram.yml.
  MODE=morning  — today + next 3 days
  MODE=evening  — what to lay out for tomorrow
Env: TELEGRAM_TOKEN (repo secret). The chat id is discovered from the bot's
updates (write the bot any message once) and remembered in .github/telegram_chat_id.
Outfit rules mirror index.html / wardrobe/AI_GUIDE.md.
"""
import datetime as dt
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CHAT_FILE = ROOT / ".github" / "telegram_chat_id"
LAT, LON, TZ = 38.56, 68.77, dt.timezone(dt.timedelta(hours=5))
SITE = "https://ismoil77.github.io/project/"
TARGET = {"Спорт": 1, "Дом": 1.5, "Повседневно": 2, "Работа": 3, "Праздник / выход": 4}
NEUTRAL = {"нейтральный", "землистый"}
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


# ---------------------------------------------------------------- weather
def forecast(days: int) -> list[dict]:
    """[{date, morning, feel, max, rain_hour, rain_p}] — Open-Meteo, fallback MET Norway."""
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=30, params={
            "latitude": LAT, "longitude": LON, "timezone": "Asia/Dushanbe", "forecast_days": days,
            "hourly": "temperature_2m,apparent_temperature,precipitation_probability",
            "daily": "temperature_2m_max"})
        w = r.json()
        H = w["hourly"]
        idx = {t: i for i, t in enumerate(H["time"])}
        out = []
        for k, d in enumerate(w["daily"]["time"]):
            at = lambda h: idx[f"{d}T{h:02d}:00"]
            probs = [(h, H["precipitation_probability"][at(h)] or 0) for h in range(6, 20)]
            wet = [h for h, p in probs if p >= 50]
            out.append({"date": d, "morning": H["temperature_2m"][at(7)], "feel": H["apparent_temperature"][at(7)],
                        "max": w["daily"]["temperature_2m_max"][k], "rain_hour": wet[0] if wet else None,
                        "rain_p": f"{max(p for _, p in probs)}%"})
        return out
    except Exception as e:  # noqa: BLE001
        print("open-meteo failed:", e, file=sys.stderr)
    r = requests.get("https://api.met.no/weatherapi/locationforecast/2.0/compact", timeout=30,
                     params={"lat": LAT, "lon": LON}, headers={"User-Agent": "personal-weather-digest/1.0 github.com/ismoil77"})
    by_day: dict[str, list] = {}
    for e in r.json()["properties"]["timeseries"]:
        t = dt.datetime.fromisoformat(e["time"].replace("Z", "+00:00")).astimezone(TZ)
        det = e["data"]["instant"]["details"]
        nxt = e["data"].get("next_1_hours") or e["data"].get("next_6_hours") or {}
        by_day.setdefault(t.date().isoformat(), []).append(
            (t.hour, det["air_temperature"], det["wind_speed"] * 3.6, nxt.get("details", {}).get("precipitation_amount", 0)))
    out = []
    for d in sorted(by_day):
        a = by_day[d]
        if not any(abs(h - 7) <= 2 for h, *_ in a):
            continue
        h7 = min(a, key=lambda x: abs(x[0] - 7))
        t, v = h7[1], h7[2]
        feel = 13.12 + .6215 * t - 11.37 * v ** .16 + .3965 * t * v ** .16 if t <= 10 and v > 4.8 else t
        wet = [x for x in a if 6 <= x[0] <= 19 and x[3] >= 0.2]
        out.append({"date": d, "morning": t, "feel": feel, "max": max(x[1] for x in a),
                    "rain_hour": wet[0][0] if wet else None, "rain_p": f"{max(x[3] for x in wet):.1f} мм" if wet else "сухо"})
    return out[:days]


# ---------------------------------------------------------------- outfit
def has_flaws(i):
    return bool(i.get("flaws")) or i.get("condition") == "есть дефекты"


def allowed(i, occ):
    if i.get("status") == "archived":
        return False
    if i.get("occasions") and occ not in i["occasions"]:
        return False
    if occ == "Праздник / выход" and (has_flaws(i) or i.get("condition") == "только дом/спорт"):
        return False
    if occ == "Работа" and i.get("condition") == "только дом/спорт":
        return False
    return True


def score(i, t, chosen, occ):
    s = -abs(i.get("formality", 2) - TARGET[occ]) * 3
    lo, hi = i.get("temp", [-50, 50])
    s -= max(0, lo - 3 - t) + max(0, t - hi - 3)
    fam = (i.get("color") or {}).get("family", "нейтральный")
    accents = {(c.get("color") or {}).get("name") for c in chosen if (c.get("color") or {}).get("family") not in NEUTRAL}
    if fam not in NEUTRAL and (i.get("color") or {}).get("name") not in accents:
        s -= 2.5 if accents else 0.5
    if i.get("pattern", "однотонный") != "однотонный" and any(c.get("pattern", "однотонный") != "однотонный" for c in chosen):
        s -= 3
    if has_flaws(i):
        s -= 0.5
    return s


def best(items, t, chosen, occ):
    return max(items, key=lambda i: score(i, t, chosen, occ), default=None)


def outfit(items, t, rain, occ):
    pool = [i for i in items if allowed(i, occ)]
    tops = [i for i in pool if i["category"] == "Верх"]
    outer = [i for i in pool if i["category"] == "Верхняя одежда" and t <= i["temp"][1] + 3]
    need_outer = t < 10 or (rain and t < 16)
    layers = 0 if t >= 20 else 1 if t >= 13 else 1 if (need_outer and outer and t >= 3) else 2
    pick, warns = [], []
    base = best([i for i in tops if i.get("warmth", 2) <= 1], t, pick, occ)
    if base:
        pick.append(base)
    mids = [i for i in tops if i.get("warmth", 2) >= 2]
    for _ in range(layers):
        m = best([x for x in mids if x not in pick and not any(c.get("type") == x.get("type") for c in pick)], t, pick, occ)
        if not m:
            break
        pick.append(m)
    if need_outer:
        if rain and any(o.get("waterproof") is True for o in outer):
            outer = [o for o in outer if o.get("waterproof") is True]
        o = best(outer, t, pick, occ)
        if o:
            pick.append(o)
            if t < o["temp"][0] - 3:
                warns.append(f"«{o['name']}» тонкая для такого холода — нужна тёплая куртка.")
        else:
            warns.append("Нужна куртка — её пока нет.")
    bottom = best([i for i in pool if i["category"] == "Низ" and i["temp"][0] - 8 <= t <= i["temp"][1] + 3], t, pick, occ)
    if bottom:
        pick.append(bottom)
    shoes = [i for i in pool if i["category"] == "Обувь" and i["temp"][0] - 5 <= t <= i["temp"][1] + 5
             and (not bottom or i.get("formality", 2) <= bottom.get("formality", 2) + 1)]
    if rain and any(s.get("waterproof") is True for s in shoes):
        shoes = [s for s in shoes if s.get("waterproof") is True]
    shoe = best(shoes, t, pick, occ)
    if shoe:
        pick.append(shoe)
    else:
        warns.append("Обуви в каталоге нет — добавь фото обуви.")
    if t < 3:
        warns.append("Шапка и перчатки.")
    for i in pick:
        if has_flaws(i):
            warns.append(f"{i['name']}: {', '.join(i.get('flaws') or ['дефект'])}.")
        if i.get("sheer") and i.get("layer") == "база" and sum(p["category"] == "Верх" for p in pick) == 1 and occ in ("Работа", "Праздник / выход"):
            warns.append(f"«{i['name']}» просвечивает — надень майку под низ.")
    return pick, warns


# ---------------------------------------------------------------- message
def occasion(d: dt.date):
    return "Работа" if d.weekday() < 5 else "Повседневно"


def fmt_t(x):
    x = round(x)
    return f"+{x}°" if x > 0 else f"{x}°"


def day_line(items, f, label):
    d = dt.date.fromisoformat(f["date"])
    occ = occasion(d)
    rain = f["rain_hour"] is not None
    pick, warns = outfit(items, f["feel"], rain, occ)
    umb = f"☂️ зонт, дождь с ~{f['rain_hour']}:00" if rain else "без зонта"
    head = f"<b>{label} ({WD[d.weekday()]} {d.day:02d}.{d.month:02d})</b> — {occ.lower()}\nутром {fmt_t(f['morning'])} (ощущ. {fmt_t(f['feel'])}), днём {fmt_t(f['max'])}, {umb}"
    return head, pick, warns


def build(mode):
    items = json.loads((ROOT / "wardrobe" / "wardrobe.json").read_text(encoding="utf-8"))["items"]
    now = dt.datetime.now(TZ)
    if mode == "evening":
        fc = [f for f in forecast(3) if f["date"] > now.date().isoformat()][:1]
        if not fc:
            return "Прогноз на завтра сейчас недоступен."
        head, pick, warns = day_line(items, fc[0], "Завтра")
        lines = [head, "", "<b>Приготовь с вечера:</b>"] + [f"• {i['name']}" for i in pick]
        if warns:
            lines += ["", "<b>Учти:</b> " + " ".join(dict.fromkeys(warns))]
        lines.append("")
    else:
        fc = forecast(4)
        lines = []
        for k, f in enumerate(fc):
            d = dt.date.fromisoformat(f["date"])
            delta = (d - now.date()).days
            label = "Сегодня" if delta == 0 else "Завтра" if delta == 1 else ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"][d.weekday()]
            head, pick, warns = day_line(items, f, label)
            if k == 0:
                lines += [head, "👕 " + " + ".join(i["name"] for i in pick)]
                if warns:
                    lines.append("⚠️ " + " ".join(dict.fromkeys(warns)))
                lines.append("")
            else:
                lines.append(head + "\n👕 " + " + ".join(i["name"] for i in pick))
                cold = [w for w in warns if "куртк" in w]
                if cold:
                    lines.append("⚠️ " + cold[0])
                lines.append("")
    lines += [f'<a href="{SITE}">Гардероб</a>']
    return "\n".join(lines)


# ---------------------------------------------------------------- telegram
def chat_id(token):
    if os.environ.get("TELEGRAM_CHAT_ID"):
        return os.environ["TELEGRAM_CHAT_ID"]
    if CHAT_FILE.exists():
        return CHAT_FILE.read_text().strip()
    ups = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30).json().get("result", [])
    chats = [u[k]["chat"]["id"] for u in ups for k in ("message", "my_chat_member") if k in u]
    if not chats:
        raise SystemExit("Бот пока не знает твой чат: открой бота в Telegram и отправь ему любое сообщение, потом запусти снова.")
    CHAT_FILE.write_text(str(chats[-1]))
    return str(chats[-1])


def main():
    token = os.environ["TELEGRAM_TOKEN"].strip()
    text = build(os.environ.get("MODE", "morning"))
    print(text)
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=30, data={
        "chat_id": chat_id(token), "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"})
    if not r.json().get("ok"):
        raise SystemExit(f"Telegram error: {r.text}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "preview":
        print(build(sys.argv[2] if len(sys.argv) > 2 else "morning"))
    else:
        main()
