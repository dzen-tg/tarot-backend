"""Астрология для «Мой 2027»: натальная карта и транзиты 2027 – только расчёт, без ИИ.

Считает skyfield по тем же эфемеридам DE421, что и «Небо сегодня» (main._sky_load).
Без времени рождения – положения планет на полдень и только планетарные транзиты;
со временем и городом – ещё асцендент, MC и дома (равнодомная система от асцендента).
Город → координаты и часовой пояс: встроенный список GeoNames (content/geo/cities.tsv), без внешних сервисов.
"""
import math
import os
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
SIGNS_RU = ["Овен", "Телец", "Близнецы", "Рак", "Лев", "Дева", "Весы", "Скорпион", "Стрелец", "Козерог",
            "Водолей", "Рыбы"]
NATAL_BODIES = [("sun", "Солнце", "sun"), ("moon", "Луна", "moon"), ("mercury", "Меркурий", "mercury"),
                ("venus", "Венера", "venus"), ("mars", "Марс", "mars"), ("jupiter", "Юпитер", "jupiter barycenter"),
                ("saturn", "Сатурн", "saturn barycenter")]
TRANSIT_BODIES = [("jupiter", "Юпитер", "jupiter barycenter"), ("saturn", "Сатурн", "saturn barycenter")]
# Быстрые планеты – чтобы у каждого месяца были свои данные (медленные весь год почти на месте)
MONTH_BODIES = [("mars", "Марс", "mars"), ("venus", "Венера", "venus")]
ASPECTS = [(0, "соединение"), (60, "секстиль"), (90, "квадрат"), (120, "трин"), (180, "оппозиция")]
ORB = 3.0
HOUSE_THEMES = {1: "личность и самоощущение", 2: "деньги и ресурсы", 3: "общение и учёба", 4: "дом и семья",
                5: "любовь, творчество, дети", 6: "работа и здоровье-режим", 7: "партнёрство и брак",
                8: "кризисы, общие деньги, близость", 9: "путешествия и смыслы", 10: "карьера и статус",
                11: "друзья и планы", 12: "уединение и внутренняя работа"}


def sign_of(lon: float) -> str:
    return SIGNS_RU[int(lon % 360 // 30)]


def _deg_in_sign(lon: float) -> str:
    return f"{int(lon % 30)}°"


# ── Город рождения ─────────────────────────────────────────────────────
_cities: list = []


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower()).replace("ё", "е").replace("й", "и")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-zа-я0-9ңғқөұүһәі]+", " ", s).strip()


def _load_cities():
    if _cities:
        return _cities
    path = os.path.join(HERE, "content", "geo", "cities.tsv")
    if not os.path.exists(path):
        return _cities
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) < 7:
                continue
            name, country, lat, lon, tz, pop, alts = p[:7]
            raw = [a for a in alts.split("|") if a] or [name]
            _cities.append({"name": name, "country": country, "lat": float(lat), "lon": float(lon), "tz": tz,
                            "pop": int(pop or 0), "raw": raw, "names": {_norm(x) for x in raw} | {_norm(name)}})
    return _cities


def find_city(query: str, limit: int = 5) -> list:
    """Точное совпадение названия (любая запись: русская, латинская, казахская…), крупные города первыми."""
    q = _norm(query)
    if len(q) < 2:
        return []
    exact = [c for c in _load_cities() if q in c["names"]]
    if not exact:
        exact = [c for c in _load_cities() if any(n.startswith(q) for n in c["names"])]
    exact.sort(key=lambda c: -c["pop"])
    out, seen = [], set()
    for c in exact:
        label = next((x for x in c["raw"] if _norm(x) == q), None) or \
            next((x for x in c["raw"] if _norm(x).startswith(q)), c["name"])
        key = (label, c["country"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": label, "country": c["country"], "lat": c["lat"], "lon": c["lon"], "tz": c["tz"]})
        if len(out) >= limit:
            break
    return out


def utc_birth(d: date, hhmm: Optional[str], tz_name: Optional[str], lon: Optional[float]) -> datetime:
    """Момент рождения в UTC. Нет времени – полдень по местному; нет пояса – по долготе."""
    h, m = (12, 0)
    if hhmm and re.fullmatch(r"\d{1,2}:\d{2}", hhmm):
        h, m = (int(x) for x in hhmm.split(":"))
    local = datetime(d.year, d.month, d.day, h, m)
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return local.replace(tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)
        except Exception:
            pass
    shift = timedelta(hours=round((lon or 0) / 15))
    return (local - shift).replace(tzinfo=timezone.utc)


# ── Натальная карта ─────────────────────────────────────────────────────
def _asc_mc(t, lat: float, lon: float) -> tuple:
    ramc = math.radians((t.gast * 15 + lon) % 360)
    eps = math.radians(23.4393 - 0.0000004 * (t.tt - 2451545.0))
    phi = math.radians(lat)
    asc = math.degrees(math.atan2(math.cos(ramc), -(math.sin(ramc) * math.cos(eps) + math.tan(phi) * math.sin(eps))))
    mc = math.degrees(math.atan2(math.sin(ramc), math.cos(ramc) * math.cos(eps)))
    return asc % 360, mc % 360


def natal(sky_load, lon_fn, d: date, hhmm: Optional[str], lat: Optional[float], lon: Optional[float],
          tz_name: Optional[str]) -> dict:
    ts, eph = sky_load()
    moment = utc_birth(d, hhmm, tz_name, lon)
    t = ts.from_datetime(moment)
    pts = {}
    for key, name, body in NATAL_BODIES:
        pts[key] = {"name": name, "lon": float(lon_fn(eph, body, t)) % 360}
    out = {"time_known": bool(hhmm), "points": pts}
    if hhmm and lat is not None and lon is not None:
        asc, mc = _asc_mc(t, lat, lon)
        out["points"]["asc"] = {"name": "Асцендент", "lon": asc}
        out["points"]["mc"] = {"name": "MC (вершина карьеры)", "lon": mc}
        out["asc"] = asc
    for p in out["points"].values():
        p["sign"] = sign_of(p["lon"])
    # Без времени Луна может быть в соседнем знаке – отмечаем, ИИ пишет о ней осторожно
    out["moon_uncertain"] = not hhmm
    return out


def house_of(lon: float, asc: float) -> int:
    return int(((lon - asc) % 360) // 30) + 1


# ── Транзиты 2027 ───────────────────────────────────────────────────────
def transits(sky_load, lon_fn, nat: dict, year: int) -> dict:
    """По месяцам: знак Юпитера и Сатурна, дом (если известно время) и точные аспекты к натальным точкам."""
    from skyfield import almanac
    ts, eph = sky_load()
    months = []
    # Новолуния года: знак (и дом) – тема, которая «запускается» в этом месяце
    tp, yp = almanac.find_discrete(ts.utc(year, 1, 1), ts.utc(year + 1, 1, 1), almanac.moon_phases(eph))
    new_moons = {}
    for t, y in zip(tp, yp):
        if int(y) == 0:
            lon = float(lon_fn(eph, "sun", t)) % 360
            nm = {"date": t.utc_datetime().strftime("%d.%m"), "sign": sign_of(lon)}
            if "asc" in nat:
                nm["house"] = house_of(lon, nat["asc"])
            new_moons.setdefault(t.utc_datetime().month, nm)
    targets = {k: v for k, v in nat["points"].items() if k in ("sun", "moon", "mercury", "venus", "mars", "asc", "mc")}
    for m in range(1, 13):
        t = ts.utc(year, m, 15)
        item = {"month": m, "planets": [], "aspects": []}
        for key, name, body in TRANSIT_BODIES:
            tl = float(lon_fn(eph, body, t)) % 360
            pl = {"key": key, "name": name, "sign": sign_of(tl)}
            if "asc" in nat:
                pl["house"] = house_of(tl, nat["asc"])
                pl["house_theme"] = HOUSE_THEMES[pl["house"]]
            item["planets"].append(pl)
            for tk, tp in targets.items():
                if key == "moon" or (tk == "moon" and nat.get("moon_uncertain")):
                    continue
                diff = abs((tl - tp["lon"] + 180) % 360 - 180)
                for ang, aname in ASPECTS:
                    if abs(diff - ang) <= ORB:
                        item["aspects"].append(f"{name} – {aname} к натальной точке «{tp['name']}»")
        for key, name, body in MONTH_BODIES:
            ml = float(lon_fn(eph, body, t)) % 360
            fast = {"key": key, "name": name, "sign": sign_of(ml), "fast": True}
            if "asc" in nat:
                fast["house"] = house_of(ml, nat["asc"])
                fast["house_theme"] = HOUSE_THEMES[fast["house"]]
            item["planets"].append(fast)
        if m in new_moons:
            item["new_moon"] = new_moons[m]
        months.append(item)
    # Смена знака Юпитера и Сатурна за год
    ingress = []
    for key, name, body in TRANSIT_BODIES:
        prev = None
        for day in range(0, 366, 3):
            t = ts.utc(year, 1, 1 + day)
            s = sign_of(float(lon_fn(eph, body, t)))
            if prev and s != prev:
                ingress.append(f"{name} переходит из знака {prev} в знак {s} (около {t.utc_datetime():%d.%m})")
            prev = s
    return {"months": months, "ingress": ingress}


def natal_lines(nat: dict) -> list:
    out = []
    for k, p in nat["points"].items():
        line = f"{p['name']}: {p['sign']} {_deg_in_sign(p['lon'])}"
        if k == "moon" and nat.get("moon_uncertain"):
            line += " (время рождения неизвестно – Луна может быть в соседнем знаке)"
        if "asc" in nat and k not in ("asc", "mc"):
            line += f", {house_of(p['lon'], nat['asc'])}-й дом"
        out.append(line)
    return out
