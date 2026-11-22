"""«Мой 2027» – линейка платных прогнозов на 2027 год (бот и мини-апп).

Всё новое живёт здесь. В main.py – только подключение: setup() при старте и несколько строк в обработчиках
оплаты (Stars и вебхук Tribute), которые передают сюда платежи за прогнозы. Пакеты Энергии и подписка
этот модуль не трогает.

Флаг FORECAST_2027_ENABLED: off (по умолчанию) – как будто модуля нет; admins – видят только админы;
on – видят все. Числа и карты считает код, ИИ пишет текст по базе фрагментов; прогноз хранится в базе
и повторно не генерируется. Язык прогноза – язык человека; при смене языка прогноз пересобирается
из тех же расчётов бесплатно и хранится рядом с прежним.
"""
import asyncio
import hashlib
import hmac
import io
import json
import os
import random
import re
import secrets
import sys
import time
from datetime import date, datetime, timedelta
from typing import Optional

from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
                           Message, PreCheckoutQuery)
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response

YEAR = 2027
HERE = os.path.dirname(os.path.abspath(__file__))
api = APIRouter()
M = None   # модуль main – задаётся в setup(), чтобы не было циклического импорта


# =====================================================================
# НАСТРОЙКИ – цены, этапы, промо. Меняются переменными окружения без правки кода.
# =====================================================================
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def flag() -> str:
    v = os.getenv("FORECAST_2027_ENABLED", "off").strip().lower()
    return {"true": "on", "1": "on", "yes": "on", "false": "off", "0": "off", "": "off"}.get(v, v)


STAGE = _env_int("FORECAST_2027_STAGE", 1)          # 1 – нумерология и таро; 2 – плюс астро и «Весь 2027»
PROMO_PCT = _env_int("FORECAST_2027_PROMO_PCT", 0)  # скидка в % на Stars и ⚡ (карта – цена из карточки Tribute)
PROMO_UNTIL = os.getenv("FORECAST_2027_PROMO_UNTIL", "")   # ГГГГ-ММ-ДД включительно; пусто – без срока

# emoji, название, рубли (для показа, списывает Tribute), звёзды, энергия, этап
PRODUCTS = {
    "full_2027": ("✨", "Весь 2027", _env_int("F27_RUB_FULL", 1990), _env_int("F27_STARS_FULL", 1200),
                  _env_int("F27_ENERGY_FULL", 1990), 2),
    "tarot_2027": ("🃏", "Таро-прогноз 2027", _env_int("F27_RUB_TAROT", 990), _env_int("F27_STARS_TAROT", 600),
                   _env_int("F27_ENERGY_TAROT", 990), 1),
    "astro_2027": ("🪐", "Астропрогноз 2027", _env_int("F27_RUB_ASTRO", 1290), _env_int("F27_STARS_ASTRO", 800),
                   _env_int("F27_ENERGY_ASTRO", 1290), 2),
    "numerology_2027": ("🔢", "Нумерология 2027", _env_int("F27_RUB_NUM", 490), _env_int("F27_STARS_NUM", 300),
                        _env_int("F27_ENERGY_NUM", 490), 1),
}
PARTS = ("numerology_2027", "tarot_2027", "astro_2027")   # что входит в «Весь 2027»
SPHERES = [("love", "❤️ Любовь"), ("money", "💼 Деньги и работа"), ("growth", "🌱 Саморазвитие"), ("all", "✨ Всё сразу")]
SPHERE_PROMPT = {"love": "любовь и отношения", "money": "деньги и работа", "growth": "саморазвитие",
                 "all": "все сферы жизни понемногу"}


def active_products() -> list:
    return [k for k, v in PRODUCTS.items() if v[5] <= STAGE]


def promo_on() -> bool:
    if PROMO_PCT <= 0:
        return False
    if not PROMO_UNTIL:
        return True
    try:
        return date.today() <= date.fromisoformat(PROMO_UNTIL)
    except ValueError:
        return False


def price(kind: str, method: str) -> int:
    emoji, title, rub, stars, energy, _ = PRODUCTS[kind]
    if method == "card":
        return int(M.CARD_PRICES.get(kind) or rub)
    base = stars if method == "stars" else energy
    return max(1, round(base * (100 - PROMO_PCT) / 100)) if promo_on() else base


def visible_for(user: Optional[dict]) -> bool:
    f = flag()
    if f == "on":
        return True
    if f == "admins" and user:
        return user.get("telegram_id") in M.ADMIN_IDS or (user.get("username") or "").lower() in M.ADMIN_USERNAMES
    return False


def card_ok(kind: str, lang: Optional[str] = None) -> bool:
    """Карта (Tribute, рубли) – только для русского интерфейса: остальным цены в ⭐ и ⚡."""
    return M.card_enabled() and kind in M.TRIBUTE_PRODUCT_IDS and (lang or M.LANG.get()) == "ru"


# =====================================================================
# ТЕКСТЫ И ЯЗЫК
# =====================================================================
def T(ru: str, **kw) -> str:
    """Фраза на языке текущего запроса (словарь бота), иначе – по-русски."""
    s = M.tr_ru(ru) or ru
    try:
        return s.format(**kw) if kw else s
    except (KeyError, IndexError):
        return ru.format(**kw) if kw else ru


def ptitle(kind: str) -> str:
    return f"{PRODUCTS[kind][0]} {T(PRODUCTS[kind][1])}"


_base_cache: dict = {}


def base(name: str, lang: str = "ru") -> dict:
    """Фрагменты базы: content/forecast2027/<язык>/<name>.json; нет перевода – русские."""
    key = (name, lang)
    if key not in _base_cache:
        data = {}
        for lg in ([lang, "ru"] if lang != "ru" else ["ru"]):
            path = os.path.join(HERE, "content", "forecast2027", lg, f"{name}.json")
            if os.path.exists(path):
                try:
                    data = json.load(open(path, encoding="utf-8"))
                    break
                except Exception as e:
                    M.log(f"f27 base {path}: {e}")
        _base_cache[key] = data
    return _base_cache[key]


# =====================================================================
# РАСЧЁТЫ – только код
# =====================================================================
def deck_id(arcana: int) -> int:
    """Номер аркана в нумерации матрицы (8 – Справедливость, 11 – Сила, 22 – Шут) → id карты в колоде."""
    return {22: 0, 8: 11, 11: 8}.get(arcana, arcana)


def year_arcana(d: date) -> int:
    return M.reduce22(d.day + d.month + sum(int(x) for x in str(YEAR)))


def teaser_calc(d: date) -> dict:
    py = M.personal_year(d, YEAR)
    arc = year_arcana(d)
    return {"personal_year": py, "year_arcana": arc, "year_card": deck_id(arc)}


def product_calc(kind: str, d: date) -> dict:
    """Расчёты для покупки. Карты месяцев тянутся случайно один раз – дальше берутся из базы."""
    c = teaser_calc(d)
    c["months"] = [M.personal_month(d, YEAR, m) for m in range(1, 13)]
    if kind in ("tarot_2027", "full_2027"):
        pool = [x for x in range(78) if x != c["year_card"]]
        c["month_cards"] = random.sample(pool, 12)
    return c


def card_name(card_id: int) -> str:
    deck = M.get_tarot_deck()
    return M._card_name(deck[card_id]) if 0 <= card_id < len(deck) else ""


# =====================================================================
# БАЗА ДАННЫХ
# =====================================================================
async def run_migrations():
    """migrations/*.sql по порядку, каждый один раз (таблица schema_migrations). Только добавление."""
    await M.pool_ready.wait()
    p = await M.db()
    await p.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, "
                    "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    folder = os.path.join(HERE, "migrations")
    for fn in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not fn.endswith(".sql"):
            continue
        if await p.fetchval("SELECT 1 FROM schema_migrations WHERE name = $1", fn):
            continue
        sql = open(os.path.join(folder, fn), encoding="utf-8").read()
        async with p.acquire() as c:
            async with c.transaction():
                await c.execute(sql)
                await c.execute("INSERT INTO schema_migrations (name) VALUES ($1)", fn)
        M.log(f"Миграция применена: {fn}")


async def get_profile(uid: int) -> dict:
    p = await M.db()
    row = await p.fetchrow("SELECT * FROM forecast_profiles WHERE telegram_id = $1", uid)
    return dict(row) if row else {}


async def save_profile(uid: int, **fields):
    allowed = {"name", "birth_date", "sphere", "source", "teaser_at"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    p = await M.db()
    cols = list(fields)
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "source")
    if "source" in cols:
        sets = (sets + ", " if sets else "") + "source = COALESCE(forecast_profiles.source, EXCLUDED.source)"
    await p.execute(
        f"INSERT INTO forecast_profiles (telegram_id, {', '.join(cols)}) VALUES ($1, "
        f"{', '.join(f'${i + 2}' for i in range(len(cols)))}) ON CONFLICT (telegram_id) DO UPDATE SET {sets}, "
        f"updated_at = NOW()", uid, *[fields[c] for c in cols])


async def owned(uid: int) -> set:
    """Купленные продукты. «Весь 2027» открывает все части."""
    p = await M.db()
    rows = await p.fetch("SELECT DISTINCT product FROM forecast_orders WHERE telegram_id = $1 AND status = 'paid'",
                         uid)
    out = {r["product"] for r in rows}
    if "full_2027" in out:
        out |= set(PARTS)
    return out


def can_buy(kind: str, have: set) -> bool:
    """Часть линейки уже куплена – комплект не предлагаем, только недостающее."""
    if kind in have:
        return False
    if kind == "full_2027" and have & set(PARTS):
        return False
    return True


# =====================================================================
# ОПЛАТА – общий путь для Stars, Tribute и Энергии
# =====================================================================
PAYLOAD_PREFIX = "f27"


def is_payload(raw: str) -> bool:
    return (raw or "").startswith(PAYLOAD_PREFIX + ":")


def parse_payload(raw: str) -> Optional[tuple]:
    parts = (raw or "").split(":")
    if len(parts) != 4 or parts[0] != PAYLOAD_PREFIX or parts[1] not in PRODUCTS or not parts[2].isdigit():
        return None
    return parts[1], int(parts[2])


async def stars_link(kind: str, uid: int) -> str:
    return await M.bot.create_invoice_link(
        title=f"{PRODUCTS[kind][0]} {T(PRODUCTS[kind][1])}"[:32],
        description=T("Персональный прогноз на 2027 год: откроется сразу после оплаты, PDF придёт в чат."),
        payload=f"{PAYLOAD_PREFIX}:{kind}:{uid}:{random.randint(100000, 999999)}",
        provider_token="", currency="XTR", prices=[LabeledPrice(label="Telegram Stars", amount=price(kind, "stars"))])


def card_link(kind: str) -> str:
    return M.CARD_LINKS.get(kind) or (f"https://web.tribute.tg/p/{M.TRIBUTE_PRODUCT_IDS[kind]}"
                                      if kind in M.TRIBUTE_PRODUCT_IDS else "")


async def record_paid(uid: int, kind: str, method: str, amount: int, currency: str, charge_id: str) -> bool:
    """Записать оплату. False – эта оплата уже записана (повторная доставка), ничего не делаем."""
    p = await M.db()
    prof = await get_profile(uid)
    user = await M.get_user(uid) or {}
    oid = await p.fetchval("""
        INSERT INTO forecast_orders (telegram_id, product, method, amount, currency, status, charge_id, source, lang)
        VALUES ($1, $2, $3, $4, $5, 'paid', $6, $7, $8) ON CONFLICT (charge_id) DO NOTHING RETURNING id
    """, uid, kind, method, amount, currency, charge_id, prof.get("source"), M.ulang(user))
    if not oid:
        return False
    M.track(uid, "f27_paid", {"product": kind, "method": method, "amount": amount, "currency": currency})
    asyncio.create_task(fulfil(uid, kind))
    return True


async def on_pre_checkout(q: PreCheckoutQuery):
    ok = parse_payload(q.invoice_payload) is not None
    await q.answer(ok=ok, error_message=None if ok else "Счёт устарел, открой прогноз заново.")


async def on_successful_payment(message: Message):
    pay = message.successful_payment
    parsed = parse_payload(pay.invoice_payload)
    if not parsed:
        M.log(f"f27: неизвестный payload {pay.invoice_payload}")
        return
    kind, uid = parsed
    fresh = await record_paid(uid, kind, "stars", pay.total_amount, "XTR", pay.telegram_payment_charge_id)
    if fresh:
        M.set_lang(await M.get_user(uid))
        await message.answer(T("✨ Оплата прошла! Раскладываю карты на твой 2027…"))


async def on_tribute(uid: int, kind: str, base_key: str, event: str, amount: Optional[str]) -> dict:
    """Вебхук Tribute за товар-прогноз (main.tribute_webhook передаёт сюда только наши товары)."""
    if event != "newdigitalproduct":
        M.log(f"f27: возврат Tribute {kind} для {uid} – прогноз не отзываем, проверь вручную")
        return {"ok": True}
    amt = int(amount) // 100 if str(amount or "").isdigit() else price(kind, "card")
    fresh = await record_paid(uid, kind, "card", amt, M.TRIBUTE_CURRENCY, f"tribute:{base_key}")
    if fresh:
        try:
            M.set_lang(await M.get_user(uid))
            await M.bot.send_message(uid, T("💳 Оплата картой прошла! Раскладываю карты на твой 2027…"))
        except Exception:
            pass
    return {"ok": True}


async def pay_energy(user: dict, kind: str, admin: bool = False) -> bool:
    uid = user["telegram_id"]
    if not can_buy(kind, await owned(uid)):
        raise HTTPException(status_code=400, detail=T("Этот прогноз уже открыт."))
    cost = 0 if admin else price(kind, "energy")
    if cost:
        p = await M.db()
        ok = await p.fetchval("UPDATE users SET balance = balance - $2 WHERE telegram_id = $1 AND balance >= $2 "
                              "RETURNING 1", uid, cost)
        if not ok:
            raise HTTPException(status_code=402, detail=T("Не хватает Энергии."))
    return await record_paid(uid, kind, "energy", cost, "energy", f"energy:{uid}:{kind}:{secrets.token_hex(4)}")


# =====================================================================
# ГЕНЕРАЦИЯ ПРОГНОЗА
# =====================================================================
SECTIONS_RU = [("intro", "Главная тема года"), ("core", ""), ("q1", "Январь – март"), ("q2", "Апрель – июнь"),
               ("q3", "Июль – сентябрь"), ("q4", "Октябрь – декабрь"), ("periods", "Лучшие и сложные периоды"),
               ("advice", "Главный совет на год")]


def core_title(kind: str, calc: dict) -> str:
    if kind == "numerology_2027":
        return T("Твой личный год {n}", n=calc["personal_year"])
    return T("Карта года: {card}", card=card_name(calc["year_card"]))


def section_title(kind: str, key: str, calc: dict) -> str:
    return core_title(kind, calc) if key == "core" else T(dict(SECTIONS_RU)[key])


SYSTEM_COMMON = (
    "Ты таролог и нумеролог проекта «Послание Таро». Ты сам сделал этот прогноз и пишешь его человеку от первого "
    "лица («я вижу», «я бы советовал»), тепло, по-дружески, на «ты», без воды. Это прогноз на {year} год.\n"
    "Главная сфера, на которую человек хочет посмотреть: {sphere} – делай на ней акцент, но не забывай остальное.\n"
    "Опирайся на данные и фрагменты базы: перескажи их своими словами, раскрой подробно и свяжи в цельную историю "
    "года, не копируй дословно. Пиши конкретно: как это может проявиться в делах, отношениях, решениях; что "
    "делать и чего избегать.\n"
    "Это ориентир для самопознания, а не предсказание: без обещаний про здоровье, деньги, беременность, "
    "возвращение партнёра и без «точно случится». Не придумывай события из жизни человека.\n"
    "Без слов «энергия», «вибрации», «вселенная», «трансформация», «потенциал».\n"
    "Каждый раздел – обычные абзацы без заголовков, списков и жирного шрифта.\n"
    "Пол человека неизвестен: пиши без рода. Нельзя «ты будешь полна», «ты готов», «обогащённый», «уверенная». "
    "Можно «у тебя будет много сил», «тебе хочется», «с новым опытом». Не пиши «будь + прилагательное» "
    "(«будь гибким», «будь готов», «будь открыт», «быть осторожным»): пиши глаголом – «сохраняй гибкость», "
    "«приготовься», «открывайся», «действуй осторожно».\n"
)
# Две части – так текст получается подробнее, и каждая проверяется отдельно
PART_GENERAL = [("intro", 60, 130, "главная тема года для этого человека"),
                ("core", 60, 130, "{core}"),
                ("core2", 50, 120, "как это проявится в главной сфере человека, что поможет и что помешает"),
                ("periods", 50, 120, "лучшие и сложные периоды: назови 2–3 самых удачных и 2–3 самых непростых месяца "
                 "по данным и коротко объясни почему; не перечисляй все месяцы подряд"),
                ("advice", 30, 80, "главный совет на год, тепло и конкретно")]
MONTH_WORDS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
               "ноябрь", "декабрь"]
PART_MONTHS = [(f"m{i + 1}", 35, 95, f"{m}: что принесёт месяц по его данным, на что направить силы и что учесть; "
                f"начни с названия месяца") for i, m in enumerate(MONTH_WORDS)]
GENDER_RU = re.compile(r"(?<![а-яё])ты\s+(?:будешь|станешь|была|был|стала|стал)\s+(?:[а-яё]+\s+)?"
                       r"[а-яё]+(?:на|на|ен|ов|ова|ая|ый|ой|ым)(?![а-яё])", re.I)


def _validator(spec: list, lang: str):
    def validate(parsed: dict) -> Optional[dict]:
        out = {}
        for k, lo, hi, _ in spec:
            text = M.clean_ai_text(str(parsed.get(k) or "")).strip()
            n = len(text.split())
            bad = M.SCARY_RE.search(text)
            g = lang == "ru" and (GENDER_RU.search(text) or M.FATE_MASC_RE.search(text))
            if g:   # род правит редактор вторым проходом – здесь только отмечаем
                M.log(f"2027: род в {k}: «{g.group(0)}»")
            if not lo * 0.75 <= n <= hi * 1.3 or bad:
                M.log(f"⚠️ 2027: часть {k} – {n} слов" + (f", «{bad.group(0)}»" if bad else ""))
                return None
            out[k] = text
        return out
    return validate


async def _write_part(system: str, data: str, spec: list, core: str, lang: str) -> Optional[dict]:
    lines = "\n".join(f"• {k} – {what.format(core=core)}: {lo}–{hi} слов." for k, lo, hi, what in spec)
    fmt = "ФОРМАТ: строго JSON {" + ", ".join(f'\"{k}\": \"…\"' for k, *_ in spec) + "}"
    keys = [k for k, *_ in spec]
    schema = {"type": "object", "properties": {k: {"type": "string"} for k in keys}, "required": keys}
    res = await M.call_ai(system + "Разделы JSON:\n" + lines + "\n\n" + fmt, data, max_tokens=3500, schema=schema,
                          validate=_validator(spec, lang))
    if not res:
        return None
    new = await M.humanize_fields(res, {k: k for k in keys}, "")
    out = {}
    for k, lo, hi, _ in spec:
        text = new.get(k) or res[k]
        # Редактор заметно сократил, испортил или добавил род – оставляем исходник
        if (len(text.split()) < max(lo * 0.75, 0.9 * len(res[k].split())) or M.SCARY_RE.search(text)
                or (lang == "ru" and GENDER_RU.search(text))):
            text = res[k]
        out[k] = text
    return out


MONTHS_RU = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
             "ноябрь", "декабрь"]


def data_prompt(kind: str, calc: dict, prof: dict) -> tuple:
    """(тема ядра, данные года, данные месяцев). Год – с фрагментом базы, месяцы – каждый со своим фрагментом;
    общей части месяцы даются коротко, чтобы запросы были легче (минутный лимит ИИ)."""
    years, months = base("years"), base("months")
    cy, cm = base("cards_year"), base("cards_month")
    deck = M.get_tarot_deck()
    nm = lambda cid: deck[cid]["name"].replace("Старший Аркан: ", "")
    if kind == "numerology_2027":
        core = f"личный год {calc['personal_year']} по нумерологии: что он значит и чем станет для человека"
        year = f"Личный год {YEAR}: {calc['personal_year']}. База: {years.get(str(calc['personal_year']), '')}"
        short = [f"{MONTHS_RU[i]} – личный месяц {n}" for i, n in enumerate(calc["months"])]
        full = [f"{MONTHS_RU[i]}: личный месяц {n}. База: {months.get(str(n), '')}" for i, n in enumerate(calc["months"])]
    else:
        yc = calc["year_card"]
        core = "карта года и что она говорит о годе"
        year = f"Карта года: {nm(yc)}. База: {cy.get(str(yc), '')}"
        short = [f"{MONTHS_RU[i]} – {nm(cid)}" for i, cid in enumerate(calc["month_cards"])]
        full = [f"{MONTHS_RU[i]}: карта месяца {nm(cid)}. База: {cm.get(str(cid), '')}"
                for i, cid in enumerate(calc["month_cards"])]
    general = year + "\nМесяцы коротко: " + "; ".join(short)
    return core, general, year + "\n" + "\n".join(full)


async def _retry(make, tries: int = 2, pause: int = 35):
    """Минутный лимит ИИ занят – подождать и попробовать ещё раз."""
    for i in range(tries):
        res = await make()
        if res or i == tries - 1:
            return res
        await asyncio.sleep(pause)


async def generate(kind: str, calc: dict, prof: dict, lang: str) -> Optional[list]:
    tok = M.LANG.set(lang)
    try:
        core, data_general, data_months = data_prompt(kind, calc, prof)
        system = SYSTEM_COMMON.format(year=YEAR, sphere=SPHERE_PROMPT.get(prof.get("sphere") or "all"))
        name = prof.get("name") or ""
        general = await _retry(lambda: _write_part(system + f"Обратись по имени {name} один раз – во вступлении.\n",
                                                   data_general, PART_GENERAL, core, lang))
        if not general:
            return None
        months = await _retry(lambda: _write_part(system + "Имя человека в этой части не используй.\n", data_months,
                                                  PART_MONTHS, core, lang))
        if not months:
            return None
        parts = {"intro": general["intro"], "core": general["core"] + "\n\n" + general["core2"],
                 "periods": general["periods"], "advice": general["advice"]}
        for q in range(4):
            parts[f"q{q + 1}"] = "\n\n".join(months[f"m{q * 3 + i + 1}"] for i in range(3))
        return [{"key": k, "text": parts[k]} for k, _ in SECTIONS_RU]
    finally:
        M.LANG.reset(tok)


_gen_locks: dict = {}


async def ensure_forecast(uid: int, kind: str, lang: str) -> Optional[dict]:
    """Прогноз на языке lang: из базы или сгенерировать (расчёты – один раз, текст – один раз на язык)."""
    lock = _gen_locks.setdefault((uid, kind), asyncio.Lock())
    async with lock:
        p = await M.db()
        row = await p.fetchrow("SELECT * FROM forecasts_2027 WHERE telegram_id = $1 AND product = $2", uid, kind)
        prof = await get_profile(uid)
        if not prof.get("birth_date"):
            return None
        if not row:
            calc = product_calc(kind, prof["birth_date"])
            row = await p.fetchrow("""
                INSERT INTO forecasts_2027 (telegram_id, product, calc, lang) VALUES ($1, $2, $3, $4)
                ON CONFLICT (telegram_id, product) DO UPDATE SET product = EXCLUDED.product RETURNING *
            """, uid, kind, json.dumps(calc), lang)
        texts = json.loads(row["texts"] or "{}")
        if lang in texts:
            return {"calc": json.loads(row["calc"]), "sections": texts[lang], "lang": lang, "row": dict(row)}
        calc = json.loads(row["calc"])
        secs = await generate(kind, calc, prof, lang)
        if not secs:
            if not texts:
                await p.execute("UPDATE forecasts_2027 SET status = 'failed' WHERE id = $1", row["id"])
            return None
        texts[lang] = secs
        await p.execute("UPDATE forecasts_2027 SET texts = $2, status = 'ready' WHERE id = $1",
                        row["id"], json.dumps(texts, ensure_ascii=False))
        return {"calc": calc, "sections": secs, "lang": lang, "row": dict(row)}


def parts_of(kind: str) -> list:
    return [k for k in PARTS if k in active_products()] if kind == "full_2027" else [kind]


async def fulfil(uid: int, kind: str):
    """После оплаты: сгенерировать и прислать в чат (серия сообщений + PDF)."""
    user = await M.get_user(uid) or {"telegram_id": uid}
    lang = M.set_lang(user)
    for part in parts_of(kind):
        fc = None
        for attempt in range(3):
            fc = await ensure_forecast(uid, part, lang)
            if fc:
                break
            await asyncio.sleep(20)
        if not fc:
            M.track(uid, "f27_gen_failed", {"product": part})
            try:
                await M.bot.send_message(uid, T("Прогноз ещё пишется – пришлю его сюда, как только он будет готов. "
                                                "Он уже сохранён в «Моих прогнозах»."))
            except Exception:
                pass
            asyncio.create_task(retry_later(uid, part))
            continue
        await deliver(uid, part, fc)


async def retry_later(uid: int, kind: str):
    for delay in (300, 900, 3600):
        await asyncio.sleep(delay)
        user = await M.get_user(uid) or {}
        lang = M.set_lang(user)
        fc = await ensure_forecast(uid, kind, lang)
        if fc:
            await deliver(uid, kind, fc)
            return


# =====================================================================
# ВЫДАЧА В ЧАТ И PDF
# =====================================================================
def _h(s: str) -> str:
    return M._h(s or "")


async def deliver(uid: int, kind: str, fc: dict) -> bool:
    prof = await get_profile(uid)
    calc, secs = fc["calc"], fc["sections"]
    tok = M.LANG.set(fc["lang"])
    try:
        head = f"<b>{_h(ptitle(kind))}</b>\n{_h(prof.get('name') or '')}"
        await M.send_card_message(uid, {"id": calc["year_card"], "name": M.get_tarot_deck()[calc["year_card"]]["name"]},
                                  head + "\n" + _h(core_title(kind, calc)) if kind != "numerology_2027" else head)
        groups = [("intro", "core"), ("q1", "q2"), ("q3", "q4"), ("periods", "advice")]
        by = {s["key"]: s["text"] for s in secs}
        for g in groups:
            msg = "\n\n".join(f"<b>{_h(section_title(kind, k, calc))}</b>\n{_h(by.get(k, ''))}" for k in g if by.get(k))
            for chunk in _split(msg, 3900):
                await M.bot.send_message(uid, chunk, parse_mode="HTML")
                await asyncio.sleep(0.4)
        pdf = await build_pdf(kind, calc, secs, prof, fc["lang"])
        await M.bot.send_document(uid, BufferedInputFile(pdf, pdf_name(kind, fc["lang"])),
                                  caption=T("📄 Твой прогноз в PDF – сохрани его себе."))
        p = await M.db()
        await p.execute("UPDATE forecasts_2027 SET delivered = TRUE WHERE telegram_id = $1 AND product = $2", uid, kind)
        M.track(uid, "f27_delivered", {"product": kind, "lang": fc["lang"]})
        return True
    except Exception as e:
        M.log(f"f27 deliver {uid} {kind}: {type(e).__name__}: {e}")
        return False
    finally:
        M.LANG.reset(tok)


def _split(text: str, limit: int) -> list:
    out, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > limit and cur:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


def pdf_name(kind: str, lang: str) -> str:
    return f"{kind}_{lang}.pdf"


FONT_DIR = os.path.join(HERE, "fonts")


async def build_pdf(kind: str, calc: dict, secs: list, prof: dict, lang: str) -> bytes:
    """PDF: тёмная обложка с картой года и именем, дальше разделы на светлых страницах.
    Шрифты Noto встроены: латиница с диакритикой, кириллица, казахский, арабский (справа налево)."""
    img = await M.card_jpeg(calc["year_card"], 700)
    month_imgs = {}
    if calc.get("month_cards"):
        for cid in calc["month_cards"]:
            month_imgs[cid] = await M.card_jpeg(cid, 300)
    tok = M.LANG.set(lang)
    try:
        return await asyncio.to_thread(_render_pdf, kind, calc, secs, prof, lang, img, month_imgs)
    finally:
        M.LANG.reset(tok)


def _render_pdf(kind, calc, secs, prof, lang, img, month_imgs) -> bytes:
    from fpdf import FPDF
    rtl = lang == "ar"
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=18)
    body = "naskh" if rtl else "sans"
    if rtl:
        pdf.add_font("naskh", "", os.path.join(FONT_DIR, "NotoNaskhArabic-Regular.ttf"))
        pdf.add_font("naskh", "B", os.path.join(FONT_DIR, "NotoNaskhArabic-Bold.ttf"))
    pdf.add_font("sans", "", os.path.join(FONT_DIR, "NotoSans-Regular.ttf"))
    pdf.add_font("sans", "B", os.path.join(FONT_DIR, "NotoSans-Bold.ttf"))
    pdf.add_font("serif", "B", os.path.join(FONT_DIR, "NotoSerif-Bold.ttf"))
    head = "naskh" if rtl else "serif"
    try:
        pdf.set_text_shaping(True)
    except Exception:
        pass
    # Имя или название на другом алфавите (кириллица в арабском PDF и наоборот) – запасным шрифтом
    pdf.set_fallback_fonts(["sans"] + (["naskh"] if rtl else []), exact_match=False)
    gold, ink, night = (196, 160, 98), (40, 34, 52), (20, 18, 28)
    align = "R" if rtl else "L"

    # Обложка
    pdf.add_page()
    pdf.set_fill_color(*night)
    pdf.rect(0, 0, 210, 297, "F")
    pdf.set_text_color(*gold)
    pdf.set_font(head, "B", 34)
    pdf.set_y(30)
    pdf.cell(0, 14, str(YEAR), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(head, "B", 20)
    pdf.multi_cell(0, 10, T(PRODUCTS[kind][1]), align="C", new_x="LMARGIN", new_y="NEXT")
    if img:
        pdf.image(io.BytesIO(img), x=65, y=80, w=80)
    pdf.set_y(222)
    pdf.set_font(body, "B", 16)
    pdf.set_text_color(242, 237, 228)
    pdf.multi_cell(0, 9, prof.get("name") or "", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(body, "", 11)
    pdf.set_text_color(*gold)
    pdf.multi_cell(0, 7, core_title(kind, calc), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(270)
    pdf.set_font(body, "", 9)
    pdf.set_text_color(160, 150, 175)
    pdf.cell(0, 6, f"{T('Послание Таро')} · @{M.BOT_USERNAME}", align="C")

    # Разделы
    pdf.add_page()
    for s in secs:
        if pdf.get_y() > 250:
            pdf.add_page()
        pdf.set_text_color(*gold)
        pdf.set_font(head, "B", 15)
        pdf.multi_cell(0, 9, section_title(kind, s["key"], calc), align=align, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)
        pdf.set_text_color(*ink)
        pdf.set_font(body, "", 11)
        for para in [x.strip() for x in s["text"].split("\n") if x.strip()]:
            pdf.multi_cell(0, 6.2, para, align=align, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
        pdf.ln(4)

    # Карты месяцев (таро)
    if month_imgs:
        pdf.add_page()
        pdf.set_text_color(*gold)
        pdf.set_font(head, "B", 15)
        pdf.multi_cell(0, 9, T("Карты месяцев"), align=align, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
        w, gap, h = 40, 7, 84
        top = pdf.get_y()
        for i, cid in enumerate(calc["month_cards"]):
            col, row = i % 4, i // 4
            x, y = 15 + col * (w + gap), top + row * h
            data = month_imgs.get(cid)
            if data:
                pdf.image(io.BytesIO(data), x=x, y=y, w=w)
            pdf.set_xy(x, y + 69)
            pdf.set_font(body, "", 8)
            pdf.set_text_color(*ink)
            pdf.multi_cell(w, 4, f"{T(MONTHS_RU[i].capitalize())}: {card_name(cid)}", align="C", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def pdf_token(uid: int, kind: str, lang: str, ttl: int = 86400) -> str:
    exp = int(time.time()) + ttl
    msg = f"{uid}:{kind}:{lang}:{exp}"
    sig = hmac.new(M.WEBHOOK_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{msg}:{sig}"


def check_token(tok: str) -> Optional[tuple]:
    parts = (tok or "").split(":")
    if len(parts) != 5 or not parts[0].isdigit() or not parts[3].isdigit():
        return None
    msg = ":".join(parts[:4])
    sig = hmac.new(M.WEBHOOK_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()[:24]
    if not hmac.compare_digest(sig, parts[4]) or int(parts[3]) < time.time():
        return None
    return int(parts[0]), parts[1], parts[2]


# =====================================================================
# БОТ: меню, сбор данных, тизер, оффер
# =====================================================================
waiting: dict = {}   # telegram_id → ("name"|"date", до какого времени ждём)


def menu_button(user: dict) -> Optional[list]:
    """Строка «🔮 Мой 2027» для главного меню (первой строкой) – если модуль виден человеку."""
    if not visible_for(user):
        return None
    return [InlineKeyboardButton(text=T("🔮 Мой 2027"), callback_data="f27:open")]


def is_start(args: Optional[str]) -> bool:
    a = (args or "").lower()
    return a == str(YEAR) or a.startswith(f"{YEAR}_")


async def open_flow(chat_id: int, user: dict, source: Optional[str] = None):
    uid = user["telegram_id"]
    M.set_lang(user)
    if source:
        await save_profile(uid, source=source[:64])
    M.track(uid, "f27_open", {"source": source})
    prof = await get_profile(uid)
    if not prof.get("name"):
        waiting[uid] = ("name", time.time() + 900)
        await M.bot.send_message(chat_id, T("🔮 Твой 2027 год\n\nКак к тебе обращаться? Напиши имя одним сообщением."))
        return
    if not prof.get("birth_date"):
        await ask_date(chat_id, user)
        return
    if not prof.get("sphere"):
        await ask_sphere(chat_id)
        return
    await send_teaser(chat_id, user)


async def ask_date(chat_id: int, user: dict):
    uid = user["telegram_id"]
    waiting[uid] = ("date", time.time() + 900)
    kb = None
    if user.get("birth_date"):
        bd = user["birth_date"].strftime("%d.%m.%Y")
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=T("Да, {d}", d=bd),
                                                                         callback_data="f27:usedate")]])
    await M.bot.send_message(chat_id, T("Дата рождения в формате ДД.ММ.ГГГГ (например, 14.03.1992):"), reply_markup=kb)


async def ask_sphere(chat_id: int):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=T(t), callback_data=f"f27:sphere:{k}")]
                                               for k, t in SPHERES])
    await M.bot.send_message(chat_id, T("Какая сфера для тебя главная в 2027?"), reply_markup=kb)


def short_line(py: int, lang: str) -> str:
    """Короткая фраза года для тизера – из short_<язык>.json; русскую другим языкам не показываем."""
    if lang == "ru":
        return base("years").get(f"{py}_short", "")
    return base("short", lang).get(str(py), "") if os.path.exists(
        os.path.join(HERE, "content", "forecast2027", lang, "short.json")) else ""


def teaser_text(calc: dict, prof: dict) -> str:
    py = calc["personal_year"]
    short = short_line(py, M.LANG.get())
    return (T("🔮 {name}, твоё число 2027 – <b>{n}</b>", name=_h(prof.get("name") or ""), n=py) + "\n\n"
            + (_h(short) + "\n\n" if short else "")
            + T("Твоя карта года – <b>{card}</b>. Что она значит для тебя – в прогнозе.",
                card=_h(card_name(calc["year_card"]))))


async def offer_kb(user: dict) -> InlineKeyboardMarkup:
    uid = user["telegram_id"]
    have = await owned(uid)
    lang = M.ulang(user)
    rows = []
    for kind in active_products():
        if not can_buy(kind, have):
            continue
        rows.append([InlineKeyboardButton(text=f"{ptitle(kind)} – {price_label(kind, lang)}",
                                          callback_data=f"f27:buy:{kind}")])
    if have:
        rows.append([InlineKeyboardButton(text=T("📜 Мои прогнозы"), callback_data="f27:mine")])
    rows.append([InlineKeyboardButton(text=T("✏️ Изменить данные"), callback_data="f27:edit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def price_label(kind: str, lang: str) -> str:
    st = f"{price(kind, 'stars')} ⭐"
    if card_ok(kind, lang):
        return f"{st} / {price(kind, 'card')} {M.TRIBUTE_CURRENCY_SIGN or '₽'}"
    return f"{st} / {price(kind, 'energy')} ⚡"


async def send_teaser(chat_id: int, user: dict):
    uid = user["telegram_id"]
    prof = await get_profile(uid)
    calc = teaser_calc(prof["birth_date"])
    if not prof.get("teaser_at"):
        await save_profile(uid, teaser_at=datetime.utcnow())
    M.track(uid, "f27_teaser", {"py": calc["personal_year"], "card": calc["year_card"]})
    await M.send_card_message(chat_id, {"id": calc["year_card"], "name": M.get_tarot_deck()[calc["year_card"]]["name"]},
                              teaser_text(calc, prof), await offer_kb(user))


async def pay_kb(user: dict, kind: str) -> InlineKeyboardMarkup:
    """Кнопки оплаты: «Звёздами», затем «Картой» (только ru); ⚡ – если хватает."""
    uid, lang = user["telegram_id"], M.ulang(user)
    stars = InlineKeyboardButton(text=T("⭐ Оплатить звёздами – {n}", n=price(kind, "stars")),
                                 url=await stars_link(kind, uid))
    rows = []
    card = None
    if card_ok(kind, lang) and card_link(kind):
        card = InlineKeyboardButton(text=T("💳 Оплатить картой – {n} {cur}", n=price(kind, "card"),
                                           cur=M.TRIBUTE_CURRENCY_SIGN or "₽"), url=card_link(kind))
    rows.append([stars])        # звёзды первыми для всех языков, карта – второй
    if card:
        rows.append([card])
    bal = user.get("balance") or 0
    if bal >= price(kind, "energy"):
        rows.append([InlineKeyboardButton(text=T("⚡ Оплатить энергией – {n}", n=price(kind, "energy")),
                                          callback_data=f"f27:energy:{kind}")])
    rows.append([InlineKeyboardButton(text=T("← Назад"), callback_data="f27:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def register(dp):
    """Обработчики бота. Регистрируются раньше общих обработчиков текста, но срабатывают только для «2027»."""

    @dp.callback_query(F.data.startswith("f27:"))
    async def f27_cb(cq: CallbackQuery):
        uid = cq.from_user.id
        user = await M.get_user(uid) or {"telegram_id": uid}
        M.set_lang(user)
        if not visible_for(user):
            await cq.answer()
            return
        parts = cq.data.split(":")
        act = parts[1] if len(parts) > 1 else ""
        await cq.answer()
        chat = cq.message.chat.id
        if act == "open":
            await open_flow(chat, user)
        elif act == "usedate" and user.get("birth_date"):
            waiting.pop(uid, None)
            await save_profile(uid, birth_date=user["birth_date"])
            await ask_sphere(chat)
        elif act == "sphere" and len(parts) > 2 and parts[2] in dict(SPHERES):
            await save_profile(uid, sphere=parts[2])
            M.track(uid, "f27_data", {"sphere": parts[2]})
            await send_teaser(chat, user)
        elif act == "edit":
            p = await M.db()
            await p.execute("UPDATE forecast_profiles SET name = NULL, sphere = NULL WHERE telegram_id = $1", uid)
            await open_flow(chat, user)
        elif act == "buy" and len(parts) > 2 and parts[2] in active_products():
            kind = parts[2]
            M.track(uid, "f27_click", {"product": kind})
            if not can_buy(kind, await owned(uid)):
                await M.bot.send_message(chat, T("Этот прогноз уже открыт."), reply_markup=await offer_kb(user))
                return
            text = f"<b>{_h(ptitle(kind))}</b>\n\n" + T(
                "Личный прогноз на 2027 по твоим данным: главная тема года, 4 квартала по месяцам, лучшие и "
                "сложные периоды, совет на год. Откроется сразу после оплаты, PDF придёт сюда.")
            if card_ok(kind, M.ulang(user)):
                text += "\n\n" + T("Картой: на странице оплаты войди через Telegram, а после входа вернись в браузер.")
            await M.bot.send_message(chat, text, parse_mode="HTML", reply_markup=await pay_kb(user, kind))
        elif act == "energy" and len(parts) > 2 and parts[2] in active_products():
            M.track(uid, "f27_click", {"product": parts[2], "method": "energy"})
            try:
                await pay_energy(user, parts[2], uid in M.ADMIN_IDS)
                await M.bot.send_message(chat, T("⚡ Готово! Раскладываю карты на твой 2027…"))
            except HTTPException as e:
                await M.bot.send_message(chat, e.detail)
        elif act == "mine":
            await send_mine(chat, user)
        elif act == "get" and len(parts) > 2 and parts[2] in PRODUCTS:
            if parts[2] not in await owned(uid):
                return
            await M.bot.send_message(chat, T("Готовлю прогноз…"))
            fc = await ensure_forecast(uid, parts[2], M.ulang(user))
            if fc:
                await deliver(uid, parts[2], fc)
            else:
                await M.bot.send_message(chat, T("Прогноз ещё пишется, попробуй через пару минут."))

    def _waiting(kind: str):
        return lambda m: m.from_user and waiting.get(m.from_user.id, ("", 0))[0] == kind \
            and waiting[m.from_user.id][1] > time.time()

    @dp.message(F.text & ~F.text.startswith("/"), _waiting("name"))
    async def f27_name(message: Message):
        uid = message.from_user.id
        name = re.sub(r"\s+", " ", message.text).strip()[:40]
        user = await M.get_user(uid) or {"telegram_id": uid}
        M.set_lang(user)
        if len(name) < 2 or not re.search(r"\w", name):
            await message.answer(T("Напиши имя буквами, например: Анна."))
            return
        waiting.pop(uid, None)
        await save_profile(uid, name=name)
        await ask_date(message.chat.id, user)

    @dp.message(F.text & ~F.text.startswith("/"), _waiting("date"))
    async def f27_date(message: Message):
        uid = message.from_user.id
        user = await M.get_user(uid) or {"telegram_id": uid}
        M.set_lang(user)
        d = M.parse_birth_date(message.text)
        if not d:
            await message.answer(T("Не получилось распознать дату, попробуй ещё раз: ДД.ММ.ГГГГ"))
            return
        waiting.pop(uid, None)
        await save_profile(uid, birth_date=d)
        await ask_sphere(message.chat.id)

    @dp.message(Command("who"))
    async def cmd_who(message: Message, command: CommandObject):
        if not M._is_admin_msg(message):
            return
        await send_who(message, command.args or "")


async def send_mine(chat_id: int, user: dict):
    have = [k for k in await owned(user["telegram_id"]) if k in PRODUCTS and k != "full_2027"]
    if not have:
        await M.bot.send_message(chat_id, T("Здесь появятся твои прогнозы на 2027."), reply_markup=await offer_kb(user))
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=ptitle(k), callback_data=f"f27:get:{k}")]
                                               for k in have])
    await M.bot.send_message(chat_id, T("📜 Мои прогнозы – нажми, чтобы получить прогноз и PDF:"), reply_markup=kb)


# =====================================================================
# /who – кто что нажимал и покупал (админы). Гайды подключатся сюда же.
# =====================================================================
WHO_EVENTS = {"f27_open": "открыл", "f27_teaser": "тизер", "f27_click": "нажал", "f27_paid": "купил",
              "f27_delivered": "получил", "f27_gen_failed": "ошибка генерации"}
FLAGS = {"ru": "🇷🇺", "en": "🇬🇧", "es": "🇪🇸", "kk": "🇰🇿", "uk": "🇺🇦", "uz": "🇺🇿", "tr": "🇹🇷", "pt": "🇧🇷",
         "de": "🇩🇪", "fr": "🇫🇷", "ar": "🇸🇦"}


async def send_who(message: Message, args: str):
    days, funnel, lang = 7, "", ""
    for a in args.lower().split():
        if a.isdigit():
            days = min(int(a), 365)
        elif a in ("2027", "guide"):
            funnel = a
        elif a in FLAGS:
            lang = a
    prefixes = ["f27_"] if funnel == "2027" else ["guide_"] if funnel == "guide" else ["f27_", "guide_"]
    p = await M.db()
    rows = await p.fetch("""
        SELECT e.telegram_id, e.name, e.props, e.created_at, u.username, u.first_name, u.lang, fp.source
        FROM events e LEFT JOIN users u ON u.telegram_id = e.telegram_id
        LEFT JOIN forecast_profiles fp ON fp.telegram_id = e.telegram_id
        WHERE e.created_at > NOW() - make_interval(days => $1) AND (""" +
        " OR ".join(f"e.name LIKE '{x}%'" for x in prefixes) + """)
        ORDER BY e.telegram_id, e.created_at
    """, days)
    people: dict = {}
    for r in rows:
        if lang and (r["lang"] or "ru") != lang:
            continue
        d = people.setdefault(r["telegram_id"], {"u": r, "steps": [], "last": r["created_at"]})
        props = json.loads(r["props"] or "{}") if r["props"] else {}
        what = WHO_EVENTS.get(r["name"], r["name"])
        detail = props.get("product") or props.get("method") or ""
        if r["name"] == "f27_paid":
            detail = f"{props.get('product', '')} {props.get('method', '')}"
        step = f"{what} {detail}".strip()
        if not d["steps"] or d["steps"][-1] != step:
            d["steps"].append(step)
        d["last"] = r["created_at"]
    if not people:
        await message.answer(f"За {days} дн. никого.")
        return
    lines = [f"👥 За {days} дн.: {len(people)} чел."]
    csv = ["telegram_id;username;name;lang;source;steps;last"]
    for uid, d in sorted(people.items(), key=lambda x: x[1]["last"], reverse=True):
        u = d["u"]
        lg = u["lang"] or "ru"
        nick = f"@{u['username']}" if u["username"] else str(uid)
        lines.append(f"• {nick} · {M._h(u['first_name'] or '')} · {FLAGS.get(lg, '')} {lg} · "
                     f"{' → '.join(d['steps'])}" + (f" · {u['source']}" if u["source"] else "") +
                     f" · {d['last']:%d.%m %H:%M}")
        csv.append(";".join(str(x).replace(";", ",") for x in
                            (uid, u["username"] or "", u["first_name"] or "", lg, u["source"] or "",
                             " → ".join(d["steps"]), f"{d['last']:%Y-%m-%d %H:%M}")))
    text = "\n".join(lines)
    if len(text) <= 3900:
        await message.answer(text, parse_mode="HTML")
    else:
        await message.answer("\n".join(lines[:40]) + f"\n… полный список – в файле", parse_mode="HTML")
    if len(people) > 30 or len(text) > 3900:
        await message.answer_document(BufferedInputFile("\n".join(csv).encode("utf-8-sig"), f"who_{days}d.csv"))


# =====================================================================
# МИНИ-АПП
# =====================================================================
def _product_view(kind: str, have: set, lang: str) -> dict:
    e, title, rub, stars, energy, _ = PRODUCTS[kind]
    return {"id": kind, "emoji": e, "title": T(title), "stars": price(kind, "stars"),
            "energy": price(kind, "energy"), "card": price(kind, "card") if card_ok(kind, lang) else None,
            "card_sign": M.TRIBUTE_CURRENCY_SIGN or "₽", "owned": kind in have, "can_buy": can_buy(kind, have),
            "parts": list(parts_of(kind)) if kind == "full_2027" else None}


def _fmt_date(d) -> Optional[str]:
    return d.strftime("%d.%m.%Y") if d else None


@api.get("/api/f27/state")
async def api_state(authorization: str = Header(None)):
    user, _ = await M.auth_user(authorization)
    if not visible_for(user):
        return {"enabled": False}
    uid, lang = user["telegram_id"], M.ulang(user)
    have = await owned(uid)
    prof = await get_profile(uid)
    teaser = None
    if prof.get("birth_date"):
        c = teaser_calc(prof["birth_date"])
        teaser = {"personal_year": c["personal_year"], "year_card": c["year_card"],
                  "card_name": card_name(c["year_card"]),
                  "short": short_line(c["personal_year"], lang)}
    p = await M.db()
    rows = await p.fetch("SELECT product, status, texts FROM forecasts_2027 WHERE telegram_id = $1", uid)
    status = {r["product"]: {"status": r["status"], "langs": list(json.loads(r["texts"] or "{}"))} for r in rows}
    return {"enabled": True, "lang": lang, "show_card_first": False, "promo": promo_on(),
            "products": [_product_view(k, have, lang) for k in active_products()],
            "profile": {"name": prof.get("name") or "", "sphere": prof.get("sphere") or "",
                        "birth_date": _fmt_date(prof.get("birth_date") or user.get("birth_date")),
                        "complete": bool(prof.get("name") and prof.get("birth_date") and prof.get("sphere"))},
            "spheres": [[k, T(t)] for k, t in SPHERES], "teaser": teaser, "forecasts": status,
            "balance": user.get("balance") or 0}


@api.post("/api/f27/profile")
async def api_profile(payload: dict, authorization: str = Header(None)):
    user, _ = await M.auth_user(authorization)
    if not visible_for(user):
        raise HTTPException(status_code=404, detail="off")
    uid = user["telegram_id"]
    name = re.sub(r"\s+", " ", str(payload.get("name") or "")).strip()[:40]
    d = M.parse_birth_date(str(payload.get("birth_date") or ""))
    sphere = str(payload.get("sphere") or "")
    if len(name) < 2:
        raise HTTPException(status_code=400, detail=T("Напиши имя буквами, например: Анна."))
    if not d:
        raise HTTPException(status_code=400, detail=T("Не получилось распознать дату, попробуй ещё раз: ДД.ММ.ГГГГ"))
    if sphere not in dict(SPHERES):
        raise HTTPException(status_code=400, detail=T("Выбери главную сферу."))
    src = str(payload.get("source") or "")[:64] or None
    await save_profile(uid, name=name, birth_date=d, sphere=sphere, source=src)
    await save_profile(uid, teaser_at=datetime.utcnow())
    c = teaser_calc(d)
    M.track(uid, "f27_teaser", {"py": c["personal_year"], "card": c["year_card"], "via": "app"})
    return await api_state(authorization)


@api.post("/api/f27/buy")
async def api_buy(payload: dict, authorization: str = Header(None)):
    user, admin = await M.auth_user(authorization)
    if not visible_for(user):
        raise HTTPException(status_code=404, detail="off")
    M.check_can_pay(user)
    uid = user["telegram_id"]
    kind, method = str(payload.get("product") or ""), str(payload.get("method") or "")
    if kind not in active_products() or method not in ("stars", "card", "energy"):
        raise HTTPException(status_code=400, detail="bad product")
    if not (await get_profile(uid)).get("birth_date"):
        raise HTTPException(status_code=400, detail=T("Сначала заполни данные."))
    if not can_buy(kind, await owned(uid)):
        raise HTTPException(status_code=400, detail=T("Этот прогноз уже открыт."))
    M.track(uid, "f27_click", {"product": kind, "method": method, "via": "app"})
    if method == "stars":
        return {"invoice_link": await stars_link(kind, uid)}
    if method == "card":
        if not card_ok(kind, M.ulang(user)):
            raise HTTPException(status_code=400, detail=T("Оплата картой пока недоступна."))
        return {"payment_url": card_link(kind)}
    await pay_energy(user, kind, admin)
    return {"ok": True}


@api.get("/api/f27/forecast")
async def api_forecast(product: str, authorization: str = Header(None)):
    """Прогноз на языке человека. Куплен, но на этом языке ещё нет – пишется из тех же расчётов (бесплатно)."""
    user, _ = await M.auth_user(authorization)
    uid, lang = user["telegram_id"], M.ulang(user)
    if product not in PRODUCTS or product not in await owned(uid):
        raise HTTPException(status_code=404, detail="not owned")
    p = await M.db()
    row = await p.fetchrow("SELECT * FROM forecasts_2027 WHERE telegram_id = $1 AND product = $2", uid, product)
    texts = json.loads(row["texts"] or "{}") if row else {}
    if lang not in texts:
        lock = _gen_locks.get((uid, product))
        if not (lock and lock.locked()):
            asyncio.create_task(ensure_forecast(uid, product, lang))
        return {"status": "pending", "other_langs": list(texts)}
    calc = json.loads(row["calc"])
    secs = [{"key": s["key"], "title": section_title(product, s["key"], calc), "text": s["text"]} for s in texts[lang]]
    cards = [{"month": T(MONTHS_RU[i].capitalize()), "id": cid, "name": card_name(cid)}
             for i, cid in enumerate(calc.get("month_cards") or [])]
    return {"status": "ready", "title": ptitle(product), "core": core_title(product, calc),
            "year_card": calc["year_card"], "personal_year": calc["personal_year"], "sections": secs,
            "month_cards": cards, "delivered": bool(row["delivered"]),
            "pdf_url": f"/api/f27/pdf?token={pdf_token(uid, product, lang)}"}


@api.post("/api/f27/send-pdf")
async def api_send_pdf(payload: dict, authorization: str = Header(None)):
    """Прислать прогноз и PDF в чат. Бот не может писать – мини-апп покажет кнопку «Скачать PDF»."""
    user, _ = await M.auth_user(authorization)
    uid, lang, product = user["telegram_id"], M.ulang(user), str(payload.get("product") or "")
    if product not in PRODUCTS or product not in await owned(uid):
        raise HTTPException(status_code=404, detail="not owned")
    fc = await ensure_forecast(uid, product, lang)
    if not fc:
        return {"sent": False, "pending": True}
    tok = M.LANG.set(lang)
    try:
        prof = await get_profile(uid)
        pdf = await build_pdf(product, fc["calc"], fc["sections"], prof, lang)
        await M.bot.send_document(uid, BufferedInputFile(pdf, pdf_name(product, lang)),
                                  caption=T("📄 Твой прогноз в PDF – сохрани его себе."))
        return {"sent": True}
    except Exception as e:
        M.log(f"f27 send-pdf {uid}: {e}")
        return {"sent": False}
    finally:
        M.LANG.reset(tok)


@api.get("/api/f27/pdf")
async def api_pdf(token: str):
    t = check_token(token)
    if not t:
        raise HTTPException(status_code=403, detail="link expired")
    uid, product, lang = t
    if product not in await owned(uid):
        raise HTTPException(status_code=404, detail="not owned")
    fc = await ensure_forecast(uid, product, lang)
    if not fc:
        raise HTTPException(status_code=409, detail="pending")
    pdf = await build_pdf(product, fc["calc"], fc["sections"], await get_profile(uid), lang)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{pdf_name(product, lang)}"'})


# =====================================================================
# ПОДКЛЮЧЕНИЕ
# =====================================================================
def setup_bot(main_module, dp):
    """Вызывается в main.py сразу после создания Dispatcher – наши обработчики идут раньше общих."""
    global M
    M = main_module
    register(dp)


def setup_api(app):
    app.include_router(api)


async def on_startup():
    try:
        await run_migrations()
    except Exception as e:
        M.log(f"⚠️ f27 миграции: {e}")
