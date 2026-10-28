# -*- coding: utf-8 -*-
"""
Послание Таро — backend v2 (@Poslanie_tarobot, ранее @Tarot_answer_question_bot)

Что изменилось по сравнению с v1:
- Пул соединений asyncpg вместо нового подключения на каждый запрос (было до 15 сек на запрос).
- Проверка подписи Telegram initData (раньше можно было подделать пользователя и получить бесконечный баланс).
- Убраны искусственные задержки asyncio.sleep.
- Вебхук ставится без drop_pending_updates — /start, который будит сервер, больше не теряется.
- Keep-alive, чтобы бесплатный Render не засыпал; /health для UptimeRobot.
- Атомарные списания баланса, идемпотентные платежи (таблица payments).
- Рост: бесплатный первый Быстрый ответ, рефералка, ежедневный пуш «карта дня», скидка на первую покупку,
  кнопка-меню мини-аппа, команды /ref и /stop.
"""
import os
import json
import base64
import hmac
import hashlib
import random
import asyncio
import re
import time
import unicodedata
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
import httpx
import secrets
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Update, LabeledPrice, PreCheckoutQuery, Message, BotCommand, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp, MenuButtonDefault, LinkPreviewOptions,
)

# =====================================================================
# КОНФИГУРАЦИЯ
# =====================================================================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
# llama-3.3-70b-versatile отключён Groq 16.08.2026 (404 model_not_found) — замена gpt-oss-120b
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GEMINI_MODELS = [m.strip() for m in os.getenv("GEMINI_MODELS", "gemini-2.5-flash,gemini-2.5-flash-lite").split(",") if m.strip()]
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://tarot-frontend-wine.vercel.app")
BOT_USERNAME = os.getenv("BOT_USERNAME", "Tarot_answer_question_bot")
PUBLIC_URL = (os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_URL") or "https://tarot-backend-136l.onrender.com").strip().rstrip("/")
ADMIN_USERNAMES = {u.strip().lower() for u in os.getenv("ADMIN_USERNAMES", "").split(",") if u.strip()}
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DEV_MODE = os.getenv("DEV_MODE", "0") == "1"          # только для локальной отладки без Telegram
KEEPALIVE = os.getenv("KEEPALIVE", "1") == "1"         # пинговать себя, чтобы Render не засыпал
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "5"))  # Алматы UTC+5; граница «дня» для карты дня
PUSH_HOUR = int(os.getenv("PUSH_HOUR", "11"))          # 11:00 Алматы = 09:00 МСК
PUSH_WEEKDAY = int(os.getenv("PUSH_WEEKDAY", "0"))     # день недели рассылки: 0=Пн ... 6=Вс (раз в неделю)
PUSH_ENABLED = os.getenv("PUSH_ENABLED", "1") == "1"
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/poslanie_taro_tg").strip()
# Контакт поддержки для /support и /paysupport (обязательные команды для ботов с оплатой Stars)
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "@backoffice_media").strip()  # пусто — кнопка канала скрыта
# Куда пересылать отзывы: ID чата/группы. Пусто — всем из ADMIN_IDS.
FEEDBACK_CHAT_ID = os.getenv("FEEDBACK_CHAT_ID", "").strip()
FEEDBACK_KINDS = {"review": "⭐ Отзыв", "idea": "💡 Идея", "bug": "🛠 Что-то не работает"}
FEEDBACK_LIMIT_PER_HOUR = 3

# Экономика
COST_PRESET = 150
COST_AI = 750
COST_QUICK = int(os.getenv("COST_QUICK", "75"))  # Быстрый ответ: ровно 1 карта
REF_BONUS = int(os.getenv("REF_BONUS", "150"))
FREE_READINGS_ON_START = 1
# Стартовый подарок: кроме Быстрого ответа — первый Стандартный разбор, чтобы человек увидел полный расклад до оплаты
FREE_PRESET_ON_START = int(os.getenv("FREE_PRESET_ON_START", "1"))
# Уточняющая карта к уже сделанному раскладу. 25 делит без остатка все пакеты (75, 150, 450, 750).
COST_CLARIFY = int(os.getenv("COST_CLARIFY", "25"))
MAX_CLARIFY = int(os.getenv("MAX_CLARIFY", "3"))
# Серия Карт Дня: на 3-й день подряд — Энергия, каждый 7-й день — бесплатный Быстрый ответ
STREAK_ENERGY_DAY = 3
STREAK_ENERGY = int(os.getenv("STREAK_ENERGY", "25"))
STREAK_QUICK_EVERY = 7
# «Сбылось?» — через сколько дней после расклада спросить, и возврат «спящих» с балансом
FOLLOWUP_DAYS = int(os.getenv("FOLLOWUP_DAYS", "3"))
WINBACK_IDLE_DAYS = int(os.getenv("WINBACK_IDLE_DAYS", "5"))
WINBACK_REPEAT_DAYS = int(os.getenv("WINBACK_REPEAT_DAYS", "14"))
# Подписка через Telegram Stars (30 дней, продлевается автоматически)
SUB_PRICE = int(os.getenv("SUB_PRICE", "349"))  # 0,70 ⭐ за Энергию — на ~18% выгоднее пакета 450
SUB_ENERGY = int(os.getenv("SUB_ENERGY", "500"))
SUB_PERIOD = 30 * 24 * 3600  # Telegram принимает только 30 дней
SUBS = {"sub_month": (SUB_ENERGY, SUB_PRICE, "Подписка «Послание+» на месяц",
                      f"+{SUB_ENERGY} Энергии каждый месяц и бесплатные уточняющие карты.")}
# Тематические расклады между Стандартным (150) и Индивидуальным (750)
SPREADS = {
    "yesno": {"title": "Да или нет", "emoji": "⚖️", "cost": 100,
              "positions": ["За «да»", "За «нет»", "Итог"],
              "desc": "3 карты: что за, что против и к чему склоняется",
              "default_q": ""},
    "career": {"title": "Работа и деньги", "emoji": "💼", "cost": 250,
               "positions": ["Где ты сейчас", "Что помогает", "Что мешает", "Ближайшая перспектива"],
               "desc": "4 карты о работе, деньгах и следующем шаге",
               "default_q": "Что меня ждёт в работе и финансах?"},
    "month": {"title": "Расклад на месяц", "emoji": "🌙", "cost": 250,
              "positions": ["Первая неделя", "Вторая неделя", "Третья неделя", "Четвёртая неделя"],
              "desc": "4 карты: по одной на каждую неделю",
              "default_q": "Что меня ждёт в ближайший месяц?"},
    "love": {"title": "Отношения", "emoji": "💞", "cost": 300,
             "positions": ["Ты в этих отношениях", "Партнёр", "Что вас связывает", "Что мешает", "К чему всё идёт"],
             "desc": "5 карт: ты, партнёр, связь, препятствия и итог",
             "default_q": ""},
}
# Второй проход «гуманизатора» для Быстрого ответа. 0 — ответ приходит быстрее, но без редактуры
HUMANIZE_QUICK = os.getenv("HUMANIZE_QUICK", "1") == "1"
# Описание в счёте Telegram (не может быть пустым). Без перечня услуг — их стало много.
PACK_DESC = "Энергия для любых раскладов в Послании Таро."
PACKS = {
    # id: (энергия, цена в Stars, цена для первой покупки, заголовок, описание)
    "pack_75": (75, 75, 75, "+75 Энергии", PACK_DESC),
    "pack_150": (150, 150, int(os.getenv("FIRST_PURCHASE_PRICE", "99")), "+150 Энергии", PACK_DESC),
    "pack_350": (350, 300, 300, "+350 Энергии (скидка 14%)", PACK_DESC),
    "pack_450": (450, 383, 383, "+450 Энергии (скидка 15%)", PACK_DESC),
    "pack_750": (750, 600, 600, "+750 Энергии (скидка 20%)", PACK_DESC),
    "pack_1500": (1500, 1125, 1125, "+1500 Энергии (скидка 25%)", PACK_DESC),
}

# Оплата картой через Tribute (Shop API). Без TRIBUTE_API_KEY кнопка «Картой» скрыта.
TRIBUTE_API_KEY = os.getenv("TRIBUTE_API_KEY", "").strip()
TRIBUTE_API = "https://tribute.tg/api/v1"
TRIBUTE_CURRENCY = os.getenv("TRIBUTE_CURRENCY", "rub").lower()   # rub / usd / eur
TRIBUTE_CURRENCY_SIGN = {"rub": "₽", "usd": "$", "eur": "€"}.get(TRIBUTE_CURRENCY, "")


def _parse_prices(raw: str) -> dict:
    out = {}
    for part in raw.split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            try:
                out[k.strip()] = int(v.strip())
            except ValueError:
                pass
    return out


# Цены в целых рублях (или долларах/евро). Меняются переменными окружения без правки кода.
# Цифровые товары магазина Tribute: какой пакет какому товару соответствует.
# Пример: TRIBUTE_PRODUCTS=pack_150:123,pack_450:124,pack_750:125 (ID — число из ссылки web.tribute.tg/p/123)
# Можно указывать код из ссылки (pack_150:Ilb для web.tribute.tg/p/Ilb) или числовой ID товара.
TRIBUTE_PRODUCT_IDS = {k.strip(): v.strip() for k, v in
                       (p.split(":", 1) for p in os.getenv("TRIBUTE_PRODUCTS", "").split(",") if ":" in p)}
PACK_BY_PRODUCT: dict = {}  # числовой product_id из вебхука → пакет (заполняется при старте)
# Цены для показа в мини-аппе. При старте подтягиваются из Tribute (load_tribute_products),
# эти значения — запасные, если API не ответил. Списывает Tribute всегда цену из карточки товара.
CARD_PRICES = _parse_prices(os.getenv("TRIBUTE_PRICES", "pack_150:199,pack_450:499,pack_750:899"))
CARD_LINKS: dict = {}  # pack → веб-ссылка на оплату картой
# Курс рубля к доллару для подсказки «≈ в другой валюте»: сколько долларов стоит 1 рубль (например 0.0125).
# Списание всё равно идёт в TRIBUTE_CURRENCY.
RUB_USD_RATE = float(os.getenv("RUB_USD_RATE", "0.0125"))


def card_enabled() -> bool:
    return bool(TRIBUTE_API_KEY and TRIBUTE_PRODUCT_IDS)


async def load_tribute_products():
    """Находит товары Tribute по коду из ссылки, запоминает их числовой ID (он приходит в вебхуке),
    цену, валюту и ссылку — чтобы в боте было ровно как в магазине."""
    global TRIBUTE_CURRENCY, TRIBUTE_CURRENCY_SIGN
    if not card_enabled():
        return
    for pack, ref in TRIBUTE_PRODUCT_IDS.items():
        CARD_LINKS.setdefault(pack, f"https://web.tribute.tg/p/{ref}")
        if ref.isdigit():
            PACK_BY_PRODUCT[int(ref)] = pack
    for attempt in range(5):
        try:
            r = await http.get(f"{TRIBUTE_API}/products", params={"type": "digital", "size": 100},
                               headers={"Api-Key": TRIBUTE_API_KEY}, timeout=15)
            if r.status_code != 200:
                raise RuntimeError(f"{r.status_code} {r.text[:200]}")
            rows = r.json().get("rows") or []
            break
        except Exception as e:
            log(f"Tribute: не получен список товаров ({e}), попытка {attempt + 1}")
            await asyncio.sleep(10)
    else:
        return
    for pack, ref in TRIBUTE_PRODUCT_IDS.items():
        d = next((x for x in rows if str(x.get("id")) == ref
                  or str(x.get("webLink", "")).rstrip("/").endswith(f"/p/{ref}")
                  or str(x.get("link", "")).endswith(f"startapp=p{ref}")), None)
        if not d:
            log(f"Tribute: товар «{ref}» для {pack} не найден — проверь TRIBUTE_PRODUCTS")
            continue
        PACK_BY_PRODUCT[int(d["id"])] = pack
        CARD_PRICES[pack] = int(d["amount"]) // 100
        cur = str(d.get("currency") or TRIBUTE_CURRENCY).lower()
        TRIBUTE_CURRENCY = cur
        TRIBUTE_CURRENCY_SIGN = {"rub": "₽", "usd": "$", "eur": "€"}.get(cur, "")
        if d.get("webLink"):
            CARD_LINKS[pack] = d["webLink"]
        log(f"Tribute: {pack} → товар {d['id']} ({ref}), {CARD_PRICES[pack]} {cur}, статус {d.get('status')}")


def alt_price(price: int) -> str:
    """Примерная цена во второй валюте: рубли → «≈ $1.86», доллары → «≈ 149 ₽»."""
    if RUB_USD_RATE <= 0:
        return ""
    if TRIBUTE_CURRENCY == "rub":
        return f"≈ ${price * RUB_USD_RATE:.2f}"
    if TRIBUTE_CURRENCY == "usd":
        return f"≈ {round(price / RUB_USD_RATE)} ₽"
    return ""

if not BOT_TOKEN:
    raise RuntimeError("Переменная TELEGRAM_BOT_TOKEN не задана!")
if not DATABASE_URL:
    raise RuntimeError("Переменная DATABASE_URL не задана!")

# Секрет вебхука: Telegram будет присылать его в заголовке, чужие запросы отбрасываем
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or hashlib.sha256(("wh:" + BOT_TOKEN).encode()).hexdigest()[:48]
WEBHOOK_PATH = "/telegram-webhook"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Старый бот после переезда: отвечает «мы переехали» и делает рассылку о переезде.
# Задайте OLD_BOT_TOKEN в Render — без него этот режим выключен.
OLD_BOT_TOKEN = os.getenv("OLD_BOT_TOKEN", "").strip()
old_bot: Optional[Bot] = Bot(token=OLD_BOT_TOKEN) if OLD_BOT_TOKEN else None
OLD_WEBHOOK_PATH = "/old-bot-webhook"
OLD_WEBHOOK_SECRET = hashlib.sha256(("wh-old:" + OLD_BOT_TOKEN).encode()).hexdigest()[:48] if OLD_BOT_TOKEN else ""
MOVED_TEXT = (
    "🔮 Послание Таро переехало в нового бота: @{u}\n\n"
    "Ваш баланс Энергии и бесплатные расклады перенесены — откройте нового бота и нажмите «Запустить»."
)


def moved_kb(start_param: Optional[str] = None) -> InlineKeyboardMarkup:
    """Кнопка на нового бота. start_param (например ref_123) пробрасывается, чтобы старые реферальные ссылки работали."""
    url = f"https://t.me/{BOT_USERNAME}"
    if start_param and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", start_param):
        url += f"?start={start_param}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔮 Перейти в новый бот", url=url)
    ]])
http: Optional[httpx.AsyncClient] = None


def log(*a):
    print(*a, flush=True)


def local_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)


def get_today_str() -> str:
    return local_now().strftime("%Y-%m-%d")


def ref_link(user_id: int) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"


# =====================================================================
# БАЗА ДАННЫХ — один пул соединений на всё приложение
# =====================================================================
pool: Optional[asyncpg.Pool] = None
pool_ready = asyncio.Event()

SUPABASE_POOLER_REGIONS = [
    "aws-0-eu-central-1", "aws-1-eu-central-1", "aws-0-eu-west-1", "aws-0-eu-west-2", "aws-0-eu-west-3",
    "aws-0-eu-north-1", "aws-0-us-east-1", "aws-1-us-east-1", "aws-0-us-east-2", "aws-0-us-west-1",
    "aws-0-ap-southeast-1", "aws-0-ap-south-1", "aws-0-ap-northeast-1",
]


def _dsn_candidates() -> list:
    """Сначала DATABASE_URL как есть. Если это прямой адрес Supabase (db.<id>.supabase.co — только IPv6,
    Render его не видит), добавляем адреса пуллера (IPv4) по всем регионам — проверим их параллельно ОДИН раз."""
    cands = [("direct", DATABASE_URL)]
    p = urllib.parse.urlparse(DATABASE_URL)
    host = p.hostname or ""
    if host.endswith(".supabase.co"):
        parts = host.split(".")
        project_id = parts[1] if parts[0] == "db" else parts[0]
        password = urllib.parse.quote(urllib.parse.unquote(p.password or ""), safe="")
        for region in SUPABASE_POOLER_REGIONS:
            cands.append((region, f"postgresql://postgres.{project_id}:{password}@{region}.pooler.supabase.com:5432/postgres"))
    return cands


async def _try_pool(name: str, dsn: str) -> Optional[asyncpg.Pool]:
    kwargs = dict(min_size=1, max_size=int(os.getenv("DB_POOL_SIZE", "5")), timeout=8,
                  command_timeout=15, statement_cache_size=0)  # statement_cache_size=0 — совместимо с pgbouncer/Supavisor
    if "supabase" in dsn or "sslmode=require" in dsn:
        kwargs["ssl"] = "require"
    try:
        p = await asyncpg.create_pool(dsn, **kwargs)
        async with p.acquire() as c:
            await c.fetchval("SELECT 1")
        log(f"✅ БД подключена через: {name}")
        return p
    except Exception as e:
        log(f"   БД {name}: {type(e).__name__}: {str(e)[:120]}")
        return None


async def init_pool():
    global pool
    cands = _dsn_candidates()
    # 1) прямое подключение
    pool = await _try_pool(*cands[0])
    # 2) пуллеры — параллельно, берём первый успешный
    if pool is None and len(cands) > 1:
        tasks = [asyncio.create_task(_try_pool(n, d)) for n, d in cands[1:]]
        for fut in asyncio.as_completed(tasks):
            res = await fut
            if res and pool is None:
                pool = res
            elif res:
                await res.close()
        if pool:
            log("💡 Совет: укажите в DATABASE_URL адрес пуллера напрямую — старт будет быстрее.")
    if pool is None:
        log("❌ Не удалось подключиться к БД ни одним способом. Проверьте DATABASE_URL.")
        return
    try:
        await init_schema()
    except Exception as e:
        log(f"❌ Ошибка создания таблиц: {e}")
        await pool.close()
        pool = None
        return
    pool_ready.set()


async def init_pool_forever():
    """Если база недоступна (например, Supabase на паузе) — пробуем снова каждые 30 сек,
    чтобы бот сам подключился, когда база проснётся, без ручного перезапуска."""
    attempt = 0
    while not pool_ready.is_set():
        attempt += 1
        try:
            await init_pool()
        except Exception as e:
            log(f"init_pool: {type(e).__name__}: {e}")
        if pool_ready.is_set():
            return
        log(f"⏳ БД недоступна (попытка {attempt}), повтор через 30 сек")
        await asyncio.sleep(30)


async def init_schema():
    async with pool.acquire() as c:
        await c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username VARCHAR(255),
                first_name VARCHAR(255),
                email VARCHAR(255),
                consent_given BOOLEAN DEFAULT FALSE,
                balance INTEGER DEFAULT 0,
                ai_balance INTEGER DEFAULT 0,
                daily_balance INTEGER DEFAULT 0,
                last_daily_date VARCHAR(10) DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        for ddl in [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_daily_date VARCHAR(10) DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS free_readings INTEGER DEFAULT 1",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by BIGINT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS ref_count INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS purchases INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS notify BOOLEAN DEFAULT TRUE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_push_date VARCHAR(10) DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen TIMESTAMP DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_news VARCHAR(32) DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS moved_notified BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS moved_reminded_at TIMESTAMP DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS move_gift_state VARCHAR(10) DEFAULT NULL",  # got / pending / given
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS source VARCHAR(32) DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS free_preset INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_push BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS streak INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS best_streak INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS ref_pending BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_winback TIMESTAMP DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS sub_until TIMESTAMP DEFAULT NULL",
        ]:
            await c.execute(ddl)
        # История раскладов: можно перечитать и добавить уточняющую карту, в том числе после пополнения
        await c.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                kind VARCHAR(16) NOT NULL,
                question TEXT,
                cards TEXT NOT NULL,
                text TEXT NOT NULL,
                clarifications TEXT NOT NULL DEFAULT '[]',
                followup VARCHAR(8) DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await c.execute("CREATE INDEX IF NOT EXISTS readings_user_idx ON readings (telegram_id, created_at DESC)")
        # События воронки: открыл → расклад → магазин → счёт → оплата
        await c.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                name VARCHAR(32) NOT NULL,
                props TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await c.execute("CREATE INDEX IF NOT EXISTS events_name_idx ON events (name, created_at)")
        await c.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                charge_id TEXT PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                pack VARCHAR(32),
                stars INTEGER,
                energy INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await c.execute("""
            CREATE TABLE IF NOT EXISTS tribute_orders (
                uuid TEXT PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                pack VARCHAR(32) NOT NULL,
                energy INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                currency VARCHAR(8) NOT NULL,
                status VARCHAR(16) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                paid_at TIMESTAMP DEFAULT NULL
            );
        """)
        await c.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        await c.execute("""
            CREATE TABLE IF NOT EXISTS reading_ratings (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                kind VARCHAR(16) NOT NULL,
                value SMALLINT NOT NULL,
                question TEXT,
                cards TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await c.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                username VARCHAR(255),
                kind VARCHAR(16) NOT NULL,
                rating SMALLINT,
                text TEXT NOT NULL,
                source VARCHAR(16) DEFAULT 'app',
                replied BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        for ddl in ["ALTER TABLE feedback ADD COLUMN IF NOT EXISTS reading_kind VARCHAR(16)",
                    "ALTER TABLE feedback ADD COLUMN IF NOT EXISTS question TEXT",
                    "ALTER TABLE feedback ADD COLUMN IF NOT EXISTS cards TEXT",
                    "ALTER TABLE tribute_orders ADD COLUMN IF NOT EXISTS reading_id BIGINT DEFAULT NULL"]:
            await c.execute(ddl)


async def db() -> asyncpg.Pool:
    if not pool_ready.is_set():
        try:
            await asyncio.wait_for(pool_ready.wait(), timeout=25)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=503, detail="Сервис просыпается, попробуйте через минуту.")
    return pool


async def ensure_user(tg_user: dict, ref_param: Optional[str] = None) -> tuple:
    """Создаёт пользователя при первом входе (с бесплатным раскладом и реферальным бонусом).
    Возвращает (user_dict, created: bool, referrer_id|None)."""
    p = await db()
    uid = int(tg_user["id"])
    first_name = (tg_user.get("first_name") or "Искатель")[:255]
    username = (tg_user.get("username") or None)
    referrer = None
    # Метка источника из ссылки вида t.me/<бот>?start=src_youtube — сохраняется только для новых пользователей
    source = None
    if ref_param and ref_param.startswith("src_"):
        source = re.sub(r"[^a-z0-9_]", "", ref_param[4:].lower())[:32] or None
    if ref_param and ref_param.startswith("ref_") and ref_param[4:].isdigit():
        referrer = int(ref_param[4:])
        if referrer == uid:
            referrer = None

    async with p.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow("""
                INSERT INTO users (telegram_id, username, first_name, email, consent_given, balance, free_readings,
                                   free_preset, last_seen)
                VALUES ($1, $2, $3, '', TRUE, 0, $4, $5, NOW())
                ON CONFLICT (telegram_id) DO NOTHING
                RETURNING *
            """, uid, username, first_name, FREE_READINGS_ON_START, FREE_PRESET_ON_START)
            created = row is not None
            credited_ref = None
            if created and (source or referrer):
                row = await c.fetchrow("UPDATE users SET source = $2 WHERE telegram_id = $1 RETURNING *",
                                       uid, source or "ref")
            if created and referrer:
                # Другу бонус сразу, пригласившему — после первой покупки друга (credit_referrer_if_due).
                # Так фейковые аккаунты не дают бесплатную Энергию.
                ok = await c.fetchval("SELECT 1 FROM users WHERE telegram_id = $1", referrer)
                if ok:
                    row = await c.fetchrow("""
                        UPDATE users SET referred_by = $2, balance = balance + $3, ref_pending = TRUE
                        WHERE telegram_id = $1 RETURNING *
                    """, uid, referrer, REF_BONUS)
                    credited_ref = referrer
            move_gift = False
            if not created:
                # Подарок за переезд, который не дошёл через старого бота, — начисляем при первом заходе в нового
                move_gift = bool(await c.fetchval(
                    "SELECT move_gift_state = 'pending' FROM users WHERE telegram_id = $1 FOR UPDATE", uid))
                row = await c.fetchrow("""
                    UPDATE users SET username = $2, first_name = $3, last_seen = NOW(), blocked = FALSE,
                        balance = balance + $4,
                        move_gift_state = CASE WHEN $5 THEN 'given' ELSE move_gift_state END
                    WHERE telegram_id = $1 RETURNING *
                """, uid, username, first_name, MOVE_GIFT if move_gift else 0, move_gift)
    result = dict(row)
    result["_move_gift"] = move_gift
    return result, created, credited_ref


async def get_user(uid: int) -> Optional[dict]:
    p = await db()
    row = await p.fetchrow("SELECT * FROM users WHERE telegram_id = $1", uid)
    return dict(row) if row else None


async def add_balance(uid: int, delta: int) -> Optional[int]:
    p = await db()
    return await p.fetchval("UPDATE users SET balance = GREATEST(0, balance + $2) WHERE telegram_id = $1 RETURNING balance", uid, delta)


FREE_FIELDS = {"quick": "free_readings", "preset": "free_preset"}


async def charge(uid: int, cost: int, free: Optional[str] = None) -> str:
    """Атомарно списывает: сначала подарок (free — 'quick' или 'preset'), потом энергию.
    Возвращает 'free:<поле>' или 'paid'. Бросает 400, если не хватает."""
    p = await db()
    col = FREE_FIELDS.get(free or "")
    if col:
        ok = await p.fetchval(f"UPDATE users SET {col} = {col} - 1 WHERE telegram_id = $1 AND {col} > 0 RETURNING 1", uid)
        if ok:
            return f"free:{col}"
    if cost <= 0:
        return "paid"
    ok = await p.fetchval("UPDATE users SET balance = balance - $2 WHERE telegram_id = $1 AND balance >= $2 RETURNING 1", uid, cost)
    if ok:
        return "paid"
    raise HTTPException(status_code=400, detail="Недостаточно Энергии на балансе.")


async def refund(uid: int, mode: str, cost: int):
    p = await db()
    if mode.startswith("free:") and mode[5:] in FREE_FIELDS.values():
        col = mode[5:]
        await p.execute(f"UPDATE users SET {col} = {col} + 1 WHERE telegram_id = $1", uid)
    elif mode == "paid" and cost > 0:
        await p.execute("UPDATE users SET balance = balance + $2 WHERE telegram_id = $1", uid, cost)


def sub_active(user: dict) -> bool:
    until = user.get("sub_until")
    return bool(until and until > datetime.utcnow())


# ── История раскладов и события воронки ───────────────────────────────────
async def save_reading(uid: int, kind: str, question: str, cards: list, text: str) -> Optional[int]:
    """Сохраняет расклад. Ошибка сохранения не должна ломать сам расклад — тогда вернём None."""
    try:
        p = await db()
        rid = await p.fetchval("""
            INSERT INTO readings (telegram_id, kind, question, cards, text)
            VALUES ($1, $2, $3, $4, $5) RETURNING id
        """, uid, kind, question or "", json.dumps(cards, ensure_ascii=False), text or "")
        return rid
    except Exception as e:
        log(f"save_reading {uid}: {e}")
        return None


def reading_payload(row, user: Optional[dict] = None) -> dict:
    clar = json.loads(row["clarifications"] or "[]")
    out = {
        "id": row["id"], "kind": row["kind"], "question": row["question"] or "",
        "cards": json.loads(row["cards"] or "[]"), "text": row["text"],
        "clarifications": clar, "created_at": row["created_at"].isoformat(),
        "clarify_left": max(0, MAX_CLARIFY - len(clar)),
    }
    if user is not None:
        out["clarify_cost"] = 0 if sub_active(user) else COST_CLARIFY
    return out


async def log_event(uid: int, name: str, props: Optional[dict] = None):
    try:
        p = await db()
        await p.execute("INSERT INTO events (telegram_id, name, props) VALUES ($1, $2, $3)",
                        uid, name[:32], json.dumps(props, ensure_ascii=False)[:500] if props else None)
    except Exception as e:
        log(f"event {name}: {e}")


def track(uid: int, name: str, props: Optional[dict] = None):
    """Событие в фоне, чтобы не задерживать ответ."""
    asyncio.create_task(log_event(uid, name, props))


async def credit_referrer_if_due(uid: int):
    """Бонус пригласившему — после первой покупки друга (Stars или картой)."""
    try:
        p = await db()
        u = await p.fetchrow("SELECT referred_by, ref_pending, purchases, first_name FROM users WHERE telegram_id = $1", uid)
        if not u or not u["ref_pending"] or not u["referred_by"] or not (u["purchases"] or 0):
            return
        async with p.acquire() as c:
            async with c.transaction():
                ref = await c.fetchval("UPDATE users SET ref_pending = FALSE WHERE telegram_id = $1 AND ref_pending "
                                       "RETURNING referred_by", uid)
                if not ref:
                    return
                await c.execute("UPDATE users SET balance = balance + $2, ref_count = ref_count + 1 WHERE telegram_id = $1",
                                ref, REF_BONUS)
        track(ref, "ref_credited", {"friend": uid})
        try:
            await bot.send_message(ref, f"🎁 {u['first_name'] or 'Твой друг'} сделал(а) первую покупку по твоей ссылке! "
                                        f"Тебе начислено +{REF_BONUS} Энергии ✨", reply_markup=webapp_kb())
        except Exception:
            pass
    except Exception as e:
        log(f"credit_referrer {uid}: {e}")


# =====================================================================
# АВТОРИЗАЦИЯ — проверка подписи Telegram WebApp initData
# =====================================================================
_SECRET_KEY = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
INIT_DATA_MAX_AGE = 7 * 24 * 3600


def verify_telegram_init_data(init_data: Optional[str]) -> dict:
    """Возвращает {'user': {...}, 'start_param': str|None}. 401 при неверной подписи."""
    if not init_data:
        if DEV_MODE:
            return {"user": {"id": 123456789, "first_name": "Искатель", "username": "test_user"}, "start_param": None}
        raise HTTPException(status_code=401, detail="Откройте Послание Таро через Telegram.")
    try:
        pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise HTTPException(status_code=401, detail="Неверные данные авторизации.")
    data = dict(pairs)
    received_hash = data.pop("hash", "")
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    calc = hmac.new(_SECRET_KEY, data_check.encode(), hashlib.sha256).hexdigest()
    if not received_hash or not hmac.compare_digest(calc, received_hash):
        raise HTTPException(status_code=401, detail="Подпись Telegram не прошла проверку.")
    try:
        if time.time() - int(data.get("auth_date", "0")) > INIT_DATA_MAX_AGE:
            raise HTTPException(status_code=401, detail="Сессия устарела, перезапустите приложение.")
    except ValueError:
        raise HTTPException(status_code=401, detail="Неверные данные авторизации.")
    try:
        user = json.loads(data.get("user", "{}"))
    except Exception:
        user = {}
    if not user.get("id"):
        raise HTTPException(status_code=401, detail="Нет данных пользователя.")
    return {"user": user, "start_param": data.get("start_param")}


def is_admin(user_row: dict) -> bool:
    return (user_row.get("telegram_id") in ADMIN_IDS or
            (user_row.get("username") or "").lower() in ADMIN_USERNAMES)


# =====================================================================
# СТРУКТУРА КАРТ ТАРО
# =====================================================================
def get_tarot_deck():
    major = [
        "Дурак", "Маг", "Верховная Жрица", "Императрица", "Император", "Иерофант",
        "Влюбленные", "Колесница", "Сила", "Отшельник", "Колесо Фортуны", "Справедливость",
        "Повешенный", "Смерть", "Умеренность", "Дьявол", "Башня", "Звезда", "Луна",
        "Солнце", "Суд", "Мир"
    ]
    suits = [("Кубков", "Кубки"), ("Мечей", "Мечи"), ("Жезлов", "Жезлы"), ("Пентаклей", "Пентакли")]
    ranks = ["Туз", "Двойка", "Тройка", "Четверка", "Пятерка", "Шестерка", "Семерка", "Восьмерка", "Девятка", "Десятка", "Паж", "Рыцарь", "Королева", "Король"]

    deck = []
    for i, name in enumerate(major):
        deck.append({"id": i, "name": f"Старший Аркан: {name}", "type": "Старший Аркан"})
    curr_id = 22
    for suit_name, suit_type in suits:
        for rank in ranks:
            deck.append({"id": curr_id, "name": f"{rank} {suit_name}", "type": suit_type})
            curr_id += 1
    return deck

def get_rich_card_meaning(card_name: str, position_type: str) -> str:
    """Глубокий генератор значений карт для fallback."""

    major_meanings = {
        "Дурак": "Эта энергия призывает к абсолютному доверию. Вы стоите на пороге чего-то совершенно нового. Отбросьте прошлый опыт, который отягощает вас. Позвольте себе легкость и спонтанность — Вселенная сейчас страхует вас.",
        "Маг": "В ваших руках сейчас сосредоточены все необходимые ресурсы. Это время активного творения, а не ожидания. Проявите силу воли и заявите о своих намерениях миру — ваша реальность податлива как глина.",
        "Верховная Жрица": "Замрите. Суета сейчас ваш враг. Ответы, которые вы ищете, уже находятся внутри вас. Обратите внимание на сны, случайные знаки и интуитивные озарения. Скрытое скоро станет явным.",
        "Императрица": "Период мощного созидания, плодородия и изобилия. Позвольте процессам развиваться естественно. Окружите себя заботой, красотой и любовью — именно из этого состояния придут лучшие результаты.",
        "Император": "Время взять ответственность на себя. Хаос должен быть структурирован. Опирайтесь на логику, дисциплину и четкие границы. Защищайте свои интересы твердо, но справедливо.",
        "Иерофант": "Ситуация требует обращения к проверенным истинам, традициям или мудрому наставнику. Ищите смысл, а не поверхностную выгоду. Поступайте так, как велит совесть.",
        "Влюбленные": "Аркан глубокого выбора, совершаемого сердцем. Необходимость интегрировать противоречивые части себя. Выбирайте искренне, отбросив страхи — и союз будет благословенным.",
        "Колесница": "Динамика, прорыв и триумф воли. Если возьмете управление на себя и не потеряете фокус, победа будет стремительной. Не время сомневаться — время действовать.",
        "Сила": "Истинная сила кроется не в давлении, а в мягкости, эмпатии и внутреннем стержне. Укротите своих внутренних демонов любовью и терпением.",
        "Отшельник": "Остановитесь и уйдите в тишину. Период самопознания и переоценки ценностей. Ваш путь сейчас — это путь вглубь себя.",
        "Колесо Фортуны": "Все течет и меняется. Вмешиваются силы судьбы. Примите цикличность происходящего: отпустите контроль, доверьтесь потоку.",
        "Справедливость": "Закон кармы в действии. Вы получите ровно то, что посеяли. Требуется абсолютная честность с самим собой и объективность.",
        "Повешенный": "Ситуация зависла, но это пауза для переосмысления. Добровольная жертва малым ради великого. Посмотрите на мир под другим углом.",
        "Смерть": "Не бойтесь этого Аркана. Это глубокая трансформация и завершение отжившего цикла. Старое должно уйти, чтобы освободить место для нового.",
        "Умеренность": "Алхимия души. Время интеграции, исцеления и поиска золотой середины. Никаких крайностей и спешки. Постепенно, капля за каплей, гармония восстанавливается.",
        "Дьявол": "Вы столкнулись с мощной теневой энергией. Зависимости, созависимые отношения или страхи, сковывающие волю. Помните: цепи лишь иллюзия, вы свободны их сбросить.",
        "Башня": "Ложные структуры и иллюзии рушатся. Это больно, но необходимо. Башня расчищает фундамент для постройки чего-то настоящего.",
        "Звезда": "Аркан исцеления, надежды и высшего покровительства. Вы на верном пути, небеса благоволят вам. Мечтайте смело и верьте в своё предназначение.",
        "Луна": "Погружение в сумерки подсознания. Ситуация полна неопределенности и скрытых страхов. Не делайте поспешных выводов, вещи не такие, какими кажутся.",
        "Солнце": "Абсолютный триумф, радость и ясность. Энергия успеха, творчества и взаимной искренности. Тьма рассеялась. Позвольте себе праздновать жизнь.",
        "Суд": "Кармическое пробуждение. Время отпустить старые обиды, простить себя и выйти на новый уровень осознанности. Судьба дает вам шанс начать всё заново.",
        "Мир": "Идеальное завершение цикла. Обретение целостности, гармонии и своего места во Вселенной. То, к чему вы стремились, обретает форму."
    }

    clean_name = card_name.replace("Старший Аркан: ", "").strip()

    if clean_name in major_meanings:
        return major_meanings[clean_name]

    suit_energy = ""
    if "Кубк" in card_name:
        suit_energy = "Энергия воды: чувства, эмоции, интуиция, глубокие привязанности. Важно слушать сердце."
    elif "Меч" in card_name:
        suit_energy = "Энергия воздуха: интеллект, логика, анализ и необходимость мыслить ясно и хладнокровно."
    elif "Жезл" in card_name:
        suit_energy = "Энергия огня: страсть, амбиции, карьера, самореализация, искра творения."
    elif "Пентакл" in card_name:
        suit_energy = "Энергия земли: материальный мир, финансы, здоровье, практичность и стабильность."

    rank_focus = ""
    if "Туз" in card_name: rank_focus = "Чистый импульс, мощный старт, дар свыше и новая возможность."
    elif "Двойка" in card_name: rank_focus = "Поиск баланса, компромисс, двойственность выбора или важное партнерство."
    elif "Тройка" in card_name: rank_focus = "Первые плоды трудов, расширение и творческое взаимодействие."
    elif "Четверка" in card_name: rank_focus = "Стабильность, безопасность, бережное отношение к ресурсам."
    elif "Пятерка" in card_name: rank_focus = "Кризис, конфликт, выход из зоны комфорта для духовного роста."
    elif "Шестерка" in card_name: rank_focus = "Гармония, взаимопомощь, исцеление прошлых ран."
    elif "Семерка" in card_name: rank_focus = "Стратегия, терпение, оценить риски и защитить убеждения."
    elif "Восьмерка" in card_name: rank_focus = "Динамика, упорный труд и концентрация на мастерстве."
    elif "Девятка" in card_name: rank_focus = "Самодостаточность, приближение к идеалу, внутренний комфорт."
    elif "Десятка" in card_name: rank_focus = "Кульминация, полнота ощущений или тяжесть ответственности."
    elif "Паж" in card_name: rank_focus = "Любопытство, обучение, важная новость или свежий взгляд."
    elif "Рыцарь" in card_name: rank_focus = "Целеустремленность, смелость и готовность к переменам."
    elif "Королева" in card_name: rank_focus = "Забота, зрелая эмоциональность, интуиция и гармония."
    elif "Король" in card_name: rank_focus = "Авторитет, лидерство, контроль и ответственность за решения."

    return f"{suit_energy} Эта карта несет следующий смысловой акцент: {rank_focus}"

def generate_local_tarot_reading(question: str, pre_selected_cards: list, reading_type: str = "general") -> dict:
    used_cards = pre_selected_cards[:3]
    used_indices = [0, 1, 2]

    if reading_type == "daily":
        card = pre_selected_cards[0]
        name = card["name"]
        meaning = get_rich_card_meaning(name, "present")
        text = (
            f"**Ваша Карта Дня — {name}**\n\n"
            f"{meaning}\n\n"
            "**Совет на сегодня:** Носите это послание с собой весь день. "
            "Доверяйте своей интуиции и проявляйте осознанность в каждом моменте. "
            "Вселенная говорит с вами через знаки — будьте внимательны."
        )
        return {"cards_used_indices": [0], "reading": text}

    n = 6 if reading_type == "dynamic" else 3
    used_cards = pre_selected_cards[:n]
    titles = (["⏳ Корни ситуации", "⚡ Текущая энергия", "🌙 Скрытое влияние",
               "🧭 Совет карт", "🌱 Ближайшее будущее", "🌟 Итог"] if n == 6
              else ["⏳ Прошлое", "⚡ Настоящее", "🌟 Будущее"])
    parts = [f"Твой вопрос: **«{question}»**"]
    for idx, card in enumerate(used_cards):
        name = card["name"].replace("Старший Аркан: ", "")
        parts.append(f"**{titles[idx]} — {name}**\n{get_rich_card_meaning(card['name'], 'present')}")
    majors = [c for c in used_cards if c["type"] == "Старший Аркан"]
    last = used_cards[-1]["name"].replace("Старший Аркан: ", "")
    parts.append(
        "**🔮 Общая картина**\n"
        + ("В раскладе сильны Старшие Арканы — ситуация важнее, чем кажется, и связана с большими переменами. "
           if majors else "В раскладе только Младшие Арканы — ситуация в твоих руках и решается повседневными шагами. ")
        + f"Ключевая карта итога — {last}: она показывает, куда всё движется."
    )
    parts.append("**✨ Ответ на твой вопрос**\nКарты не дают жёсткого «да» или «нет» — они показывают, что исход зависит от твоих действий в ближайшее время. Опирайся на совет и карту итога.")
    parts.append("**🧭 Что делать**\n• Определи один конкретный шаг, который можешь сделать на этой неделе.\n"
                 "• Обрати внимание на то, что подсвечивает карта настоящего — там главный ресурс.\n"
                 "• Вернись к раскладу через несколько дней и сверь, что изменилось.")
    return {"cards_used_indices": list(range(len(used_cards))), "reading": "\n\n".join(parts)}

# Строим regex-паттерны через chr() — в исходнике только ASCII, никаких скрытых Unicode-символов.
_INVIS_CHARS = "".join(chr(c) for c in [
    0x00AD,  # soft hyphen
    0x200B,  # zero width space
    0x200C,  # zero width non-joiner
    0x200D,  # zero width joiner
    0x200E,  # left-to-right mark
    0x200F,  # right-to-left mark
    0x202A,  # left-to-right embedding
    0x202B,  # right-to-left embedding
    0x202C,  # pop directional formatting
    0x202D,  # left-to-right override
    0x202E,  # right-to-left override
    0x2060,  # word joiner
    0x2061,  # function application (invisible)
    0x2062,  # invisible times
    0x2063,  # invisible separator
    0x2064,  # invisible plus
    0xFEFF,  # BOM / zero width no-break space
])
_INVIS_RE = re.compile("[" + re.escape(_INVIS_CHARS) + "]")
_COMBINING_RE = re.compile("[" + chr(0x0300) + "-" + chr(0x036F) + "]")


_WORDCOUNT_RE = re.compile(r"[ \t]*\(\s*(?:примерно|около|~)?\s*\d+(?:\s*[-–—]\s*\d+)?\s*слов[а]?\s*\)", re.IGNORECASE)


def clean_ai_text(text: str) -> str:
    """Убирает мусорные Unicode-символы из текста AI: диакритику, управляющие символы,
    невидимые разделители и прочие артефакты LLM."""
    if not text:
        return text
    # Модель иногда «дважды экранирует» переносы — в тексте оказываются буквальные \\n.
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", " ").replace('\\"', '"')
    # Нормализация NFC: разложенные символы → предсобранные (убирает комбинирующие символы, оставляя кириллицу чистой)
    text = unicodedata.normalize("NFC", text)
    # Убираем управляющие символы (кроме \n \r \t)
    text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', text)
    # Убираем невидимые и двунаправленные символы
    text = _INVIS_RE.sub("", text)
    # Убираем одиночные комбинирующие диакритические знаки (U+0300-U+036F), которые "прилипают" к кириллице
    text = _COMBINING_RE.sub("", text)
    # Убираем служебные пометки длины, которые модель иногда копирует из промпта: «(60-90 слов)», «(примерно 50 слов)»
    text = _WORDCOUNT_RE.sub("", text)
    return text


def extract_json_from_text(text: str) -> Optional[dict]:
    try:
        text_strip = text.strip()
        start_idx = text_strip.find("{")
        end_idx = text_strip.rfind("}")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_str = text_strip[start_idx:end_idx + 1]
            return json.loads(json_str)
    except Exception as e:
        print(f"⚠️ Ошибка парсинга JSON: {e}", flush=True)
    return None


# =====================================================================
# ВЫЗОВ AI — GROQ (основной) + GEMINI (запасной). Один общий HTTP-клиент.
# =====================================================================

def _clean_reading(parsed: dict) -> dict:
    clean = parsed.get("reading", "") or ""
    for tag in ["<h3>", "</h3>", "<h4>", "</h4>", "<br>", "<br/>", "<br />"]:
        clean = clean.replace(tag, "\n" if "br" in tag else "")
    parsed["reading"] = clean_ai_text(clean)
    return parsed


# ---------------------------------------------------------------------
# Низкоуровневые вызовы: возвращают сырой dict из JSON-ответа модели
# (или None, если ответ оборван/не распарсился).
# ---------------------------------------------------------------------
async def _groq_json(system_prompt: str, user_prompt: str, max_tokens: int) -> Optional[dict]:
    if not GROQ_API_KEY:
        return None
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt + "\n\nОТВЕЧАЙ ТОЛЬКО JSON без markdown-блоков (без ```json). Только чистый JSON."},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.8,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    if GROQ_MODEL.startswith("openai/gpt-oss"):
        # gpt-oss «думает» перед ответом; low — быстрее и не съедает лимит токенов на рассуждения
        payload["reasoning_effort"] = "low"
        payload["include_reasoning"] = False
    t0 = time.monotonic()
    try:
        r = await http.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json=payload, timeout=60.0,
        )
        if r.status_code != 200:
            log(f"⚠️ Groq ошибка {r.status_code}: {r.text[:200]}")
            return None
        choice = r.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            log("⚠️ Groq: ответ оборван по лимиту токенов")
            return None
        parsed = extract_json_from_text(choice["message"]["content"])
        log(f"Groq {time.monotonic() - t0:.1f}s, json={'ok' if parsed else 'fail'}")
        return parsed
    except Exception as e:
        log(f"⚠️ Groq исключение: {type(e).__name__}: {e}")
        return None


async def _gemini_json(system_prompt: str, user_prompt: str, max_tokens: int, schema: Optional[dict]) -> Optional[dict]:
    if not GEMINI_API_KEY:
        return None
    for model in GEMINI_MODELS:
        gen_cfg = {"responseMimeType": "application/json", "maxOutputTokens": max_tokens + 1000}
        if schema:
            gen_cfg["responseSchema"] = schema
        if model.startswith("gemini-2.5"):
            gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}  # без «размышлений» — в разы быстрее
        payload = {
            "contents": [{"parts": [{"text": user_prompt}]}],
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": gen_cfg,
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            r = await http.post(url, json=payload, headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=60.0)
            if r.status_code == 200:
                cand = r.json().get("candidates", [{}])[0]
                if cand.get("finishReason") == "MAX_TOKENS":
                    log(f"⚠️ Gemini {model}: ответ оборван по лимиту")
                    continue
                raw = cand.get("content", {}).get("parts", [{}])[0].get("text", "")
                parsed = extract_json_from_text(raw)
                if parsed:
                    log(f"✅ Gemini ответил через {model}")
                    return parsed
            else:
                log(f"⚠️ Gemini {model} ошибка {r.status_code}: {r.text[:150]}")
        except Exception as e:
            log(f"⚠️ Gemini {model} исключение: {type(e).__name__}: {e}")
    return None


async def call_ai(system_prompt: str, user_prompt: str, max_tokens: int = 2000,
                  schema: Optional[dict] = None, validate=None) -> Optional[dict]:
    """Groq → (повтор Groq) → Gemini. validate(dict) -> dict|None отбраковывает неполные ответы."""
    attempts = [
        lambda: _groq_json(system_prompt, user_prompt, max_tokens),
        lambda: _groq_json(system_prompt, user_prompt, max_tokens),
        lambda: _gemini_json(system_prompt, user_prompt, max_tokens, schema),
    ]
    for attempt in attempts:
        parsed = await attempt()
        if not parsed:
            continue
        result = validate(parsed) if validate else (parsed if parsed.get("reading") else None)
        if result:
            return result if validate else _clean_reading(result)
        log("⚠️ Ответ AI неполный — пробуем ещё раз")
    return None


# ---------------------------------------------------------------------
# ГУМАНИЗАТОР: второй проход редактора по каждому ответу.
# Правила — сжатая версия гайда «Signs of AI writing» (скилл humanizer).
# Каждое поле правится отдельно; если правка не прошла проверку — остаётся исходный текст.
# ---------------------------------------------------------------------
HUMANIZE_ENABLED = os.getenv("HUMANIZE", "1") == "1"
HUMANIZE_TIMEOUT = float(os.getenv("HUMANIZE_TIMEOUT", "25"))

HUMANIZER_PROMPT = (
    "Ты — литературный редактор. Тебе дают части готового расклада Таро в JSON. Перепиши каждую часть так, "
    "чтобы она звучала как живой таролог в переписке, а не как машинный текст. Смысл, предсказания, имена, "
    "сроки и вердикт (скорее да / скорее нет / зависит от…) сохраняй точно. Ничего нового не придумывай. "
    "Не превращай осторожный ответ в категоричное «Да» или «Нет» и не добавляй «точно», «обязательно».\n\n"
    "УБЕРИ ПРИМЕТЫ МАШИННОГО ТЕКСТА:\n"
    "1. «Не X, а Y», «это не просто X, это Y», «дело не в X» — скажи прямо, что есть.\n"
    "2. Финальные фразы, которые повторяют уже сказанное («И это главное.», «Помни об этом.») — удали.\n"
    "3. Псевдоглубокие афоризмы: «язык Вселенной», «ключ к…», «суть кроется в…», «истинная сила в…» — "
    "замени конкретной мыслью.\n"
    "4. Разгоны перед мыслью: «Давай разберёмся», «Важно понимать», «Стоит отметить», «Итак», «Честно?» — удали.\n"
    "5. Спор с тем, чего никто не говорил: «Это не значит, что…», «Не пойми неправильно» — удали.\n"
    "6. Перечисления тройками по привычке — оставь столько пунктов, сколько есть по смыслу.\n"
    "7. Несколько предложений подряд с одинаковым началом — перестрой.\n"
    "8. Тире вместо запятой, двоеточия или точки — замени. Длинных тире в тексте почти не должно быть.\n"
    "9. Нагромождение оговорок («возможно, вероятно, может быть») — оставь одну, если она нужна.\n"
    "10. Штампы: «ключевой», «играет важную роль», «является», «данный», «уникальный», «невероятный», "
    "«гармония» и «энергия» в каждом абзаце, «путь», «трансформация», «потенциал» без конкретики.\n"
    "11. Раздувание значимости: «поворотный момент», «новая глава», «судьбоносный» — оставь факт без пафоса.\n"
    "12. Жирный шрифт на отдельных словах внутри текста — убери.\n"
    "13. Вопросы и предложения в конце («Хочешь узнать больше?»), «Надеюсь, это поможет» — удали.\n\n"
    "РАЗНООБРАЗИЕ: если толкования разных карт повторяют друг друга мыслью, словами или построением фраз, "
    "перепиши так, чтобы каждая карта звучала по-своему и добавляла новое к общей картине. "
    "Начала частей не должны быть одинаковыми.\n\n"
    "ЖИВОЙ РИТМ: чередуй короткие и длинные предложения. Обращение на «ты», тепло и по-человечески. "
    "Длина каждой части — примерно как у исходной.\n\n"
    "ФОРМАТ: верни строго JSON с ТЕМИ ЖЕ ключами, значения — переписанный текст. Без markdown-блоков."
)


async def humanize_fields(fields: dict, labels: dict, question: str, keep_bold: bool = False) -> dict:
    """Возвращает dict с теми же ключами. При любой ошибке или сомнительной правке — исходный текст поля."""
    if not HUMANIZE_ENABLED or not fields:
        return dict(fields)
    payload = {k: v for k, v in fields.items() if v}
    if not payload:
        return dict(fields)
    guide = "\n".join(f"• {k}: {labels.get(k, k)}" for k in payload)
    extra = ("\nСтроки-заголовки, выделенные **жирным**, оставь дословно как есть, на своих местах."
             if keep_bold else "")
    user_prompt = (
        (f"Вопрос человека: «{question}».\n" if question else "")
        + f"Что есть что:\n{guide}{extra}\n\nТЕКСТ (JSON):\n" + json.dumps(payload, ensure_ascii=False)
    )
    total = sum(len(v) for v in payload.values())
    max_tokens = min(6000, total // 2 + 800)

    def validate(parsed: dict) -> Optional[dict]:
        out = {}
        for k, old in payload.items():
            new = clean_ai_text(str(parsed.get(k) or "")).strip()
            ok = 0.5 * len(old) <= len(new) <= 1.6 * len(old) and len(new) >= 15
            if ok and keep_bold:
                ok = old.count("**") == new.count("**")
            out[k] = new if ok else old
        return out if any(out[k] != payload[k] for k in payload) else None

    t0 = time.monotonic()
    try:
        res = await asyncio.wait_for(call_ai(HUMANIZER_PROMPT, user_prompt, max_tokens=max_tokens, validate=validate),
                                     timeout=HUMANIZE_TIMEOUT)
    except Exception as e:
        log(f"⚠️ Гуманизатор: {type(e).__name__}: {e}")
        res = None
    log(f"Гуманизатор {time.monotonic() - t0:.1f}s, {'ok' if res else 'исходный текст'}")
    out = dict(fields)
    if res:
        out.update(res)
    return out


# ---------------------------------------------------------------------
# Структурированный расклад: модель возвращает части по отдельности,
# а текст собираем мы — так каждая карта и итог гарантированно на месте.
# ---------------------------------------------------------------------
STRUCTURED_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "intro": {"type": "STRING"},
        "cards": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "index": {"type": "INTEGER"}, "position": {"type": "STRING"}, "meaning": {"type": "STRING"}},
            "required": ["index", "position", "meaning"]}},
        "synthesis": {"type": "STRING"},
        "answer": {"type": "STRING"},
        "advice": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["intro", "cards", "synthesis", "answer", "advice"],
}

# Правила «живого» текста: убирают типичные приметы машинного письма (по мотивам гайда «Signs of AI writing»).
HUMAN_STYLE = (
    "КАК ПИСАТЬ, ЧТОБЫ ТЕКСТ ЗВУЧАЛ ПО-ЧЕЛОВЕЧЕСКИ:\n"
    "• Говори прямо. Без конструкций «это не просто X, это Y», «не X, а Y», «дело не в X» — просто скажи, что есть.\n"
    "• Без пафосных афоризмов и красивостей ради красивостей: «язык Вселенной», «ключ к гармонии», «суть кроется в…».\n"
    "• Без разгонов перед мыслью: «Давай разберёмся», «Важно понимать», «Стоит отметить», «Итак».\n"
    "• Не заканчивай абзац фразой, которая повторяет уже сказанное. Каждое предложение добавляет новое.\n"
    "• Не перечисляй всё тройками по привычке. Если по смыслу два пункта — пиши два.\n"
    "• Чередуй короткие и длинные предложения. Не начинай подряд несколько предложений одинаково.\n"
    "• Тире — только там, где его требует грамматика. Не ставь тире вместо запятой, двоеточия или скобок.\n"
    "• Без рекламных и канцелярских слов: «уникальный», «невероятный», «играет ключевую роль», «является», «данный».\n"
    "• Жирным выделяй только заголовки частей, не отдельные слова внутри текста.\n"
    "• Конкретика вместо общих слов: привязывай толкование к вопросу и ситуации человека.\n"
    "• Без вопросов и предложений в конце («Хочешь узнать больше?»), без «Надеюсь, это поможет»."
)

# Каждая карта смотрит на всю ситуацию, но со своей стороны — чтобы толкования не повторяли друг друга.
ANGLES_RULE = (
    "РАЗНЫЕ РАКУРСЫ:\n"
    "• Каждая карта рассматривает ситуацию ЦЕЛИКОМ, но со своей стороны, которую задаёт её позиция "
    "(что привело, что происходит сейчас, чувства и мотивы других людей, скрытое, к чему всё ведёт, совет).\n"
    "• Толкования не повторяют друг друга ни мыслью, ни словами, ни построением фраз. Если мысль уже прозвучала "
    "у одной карты, другая её не пересказывает, а добавляет новое.\n"
    "• Начинай каждое толкование по-разному: не «Эта карта…», не «Карта говорит…» у всех подряд.\n"
    "• Опирайся на конкретную символику и значение именно этой карты, чтобы разные карты звучали по-разному."
)

# Ответ должен быть тем, за чем человек пришёл.
ANSWER_RULE = (
    "ОТВЕТ, КОТОРОГО ЖДЁТ ЧЕЛОВЕК:\n"
    "• Вопрос «да/нет» (будет ли, позвонит ли, стоит ли) — НЕ давай категоричного «Да» или «Нет». "
    "Начни с осторожной формулировки: «Скорее всего, да», «Скорее да», «Скорее всего, нет», «Скорее нет», "
    "«Пока скорее нет», а дальше объясни, что показывает карта и от чего зависит исход.\n"
    "• Никогда не пиши голое «Да.» или «Нет.» и не обещай исход наверняка («точно», «обязательно», «100%», "
    "«никогда»). Карты показывают тенденцию, а не приговор.\n"
    "• Вопрос «когда» — назови ориентир по времени (дни, неделя, месяц, до весны) и от чего он зависит.\n"
    "• Вопрос «что он(а) чувствует / думает / как относится» — прямо опиши чувства, мысли и намерения этого человека.\n"
    "• Вопрос «что делать / как поступить» — одно конкретное действие.\n"
    "• Не подменяй ответ общими словами вроде «всё зависит от тебя» или «доверься потоку»."
)

STRUCTURED_FORMAT = (
    "ФОРМАТ ОТВЕТА — строго JSON (без markdown-блоков):\n"
    "{\n"
    '  "intro": "2-3 тёплых предложения — почувствуй суть вопроса",\n'
    '  "cards": [{"index": <номер карты из списка>, "position": "<название позиции>", "meaning": "<толкование>"}, ...],\n'
    '  "synthesis": "как карты говорят ВМЕСТЕ: общая картина, связи между картами, главный сквозной мотив",\n'
    '  "answer": "ответ на вопрос простыми словами: скорее всего да / скорее всего нет / зависит от… — и почему",\n'
    '  "advice": ["конкретный шаг 1", "конкретный шаг 2", "конкретный шаг 3"]\n'
    "}\n\n"
    "ПРАВИЛА:\n"
    "• Опиши КАЖДУЮ выбранную карту — массив cards содержит все выбранные карты, ни одну не пропускай.\n"
    "• answer — главное: человек пришёл за ответом. Не уходи в общие слова, ответь именно на его вопрос.\n"
    "• advice — 3-4 практичных действия (что сделать, что сказать, чего избегать, на что обратить внимание), не абстракции.\n"
    "• Используй только **жирный** Markdown, никаких HTML-тегов и заголовков #.\n"
    "• Никогда не используй слова 'нейросеть', 'ИИ', 'алгоритм', 'в заключение'.\n"
    "• Пиши живым тёплым языком, обращайся на «ты».\n\n"
    + ANGLES_RULE + "\n\n" + ANSWER_RULE + "\n\n" + HUMAN_STYLE
)


def _make_validator(pre_selected_cards: list, min_cards: int, max_cards: int):
    def validate(parsed: dict) -> Optional[dict]:
        raw_cards = parsed.get("cards")
        if not isinstance(raw_cards, list):
            return None
        seen, cards = set(), []
        for c in raw_cards:
            if not isinstance(c, dict):
                continue
            try:
                idx = int(c.get("index"))
            except (TypeError, ValueError):
                continue
            meaning = clean_ai_text(str(c.get("meaning") or "")).strip()
            if not (0 <= idx < len(pre_selected_cards)) or idx in seen or len(meaning) < 40:
                continue
            seen.add(idx)
            cards.append((idx, clean_ai_text(str(c.get("position") or "")).strip(" —-:*"), meaning))
        answer = clean_ai_text(str(parsed.get("answer") or "")).strip()
        advice = [clean_ai_text(str(a)).strip() for a in (parsed.get("advice") or []) if str(a).strip()]
        synthesis = clean_ai_text(str(parsed.get("synthesis") or "")).strip()
        if len(cards) < min_cards or len(answer) < 30 or not advice or len(synthesis) < 40:
            return None
        return {
            "intro": clean_ai_text(str(parsed.get("intro") or "")).strip(),
            "cards": cards[:max_cards],
            "synthesis": synthesis,
            "answer": answer,
            "advice": advice[:5],
        }
    return validate


def _card_name(card: dict) -> str:
    return card["name"].replace("Старший Аркан: ", "")


def _build_structured(norm: dict, pre_selected_cards: list) -> dict:
    parts = []
    if norm.get("intro"):
        parts.append(norm["intro"])
    for idx, position, meaning in norm["cards"]:
        name = _card_name(pre_selected_cards[idx])
        title = f"**{position} — {name}**" if position else f"**{name}**"
        parts.append(f"{title}\n{meaning}")
    parts.append(f"**🔮 Общая картина**\n{norm['synthesis']}")
    parts.append(f"**✨ Ответ на твой вопрос**\n{norm['answer']}")
    parts.append("**🧭 Что делать**\n" + "\n".join(f"• {a}" for a in norm["advice"]))
    return {"cards_used_indices": [i for i, _, _ in norm["cards"]], "reading": "\n\n".join(parts)}


async def _humanize_structured(norm: dict, question: str, pre_selected_cards: list) -> dict:
    """Гуманизатор для Стандартного и Индивидуального разборов: правит каждое поле отдельно."""
    fields, labels = {}, {}
    if norm.get("intro"):
        fields["intro"], labels["intro"] = norm["intro"], "Вступление"
    for n, (idx, position, meaning) in enumerate(norm["cards"]):
        k = f"card_{n}"
        fields[k], labels[k] = meaning, f"Карта: {position or 'позиция'} — {_card_name(pre_selected_cards[idx])}"
    fields["synthesis"], labels["synthesis"] = norm["synthesis"], "Общая картина"
    fields["answer"], labels["answer"] = norm["answer"], "Ответ на вопрос"
    for n, a in enumerate(norm["advice"]):
        fields[f"advice_{n}"], labels[f"advice_{n}"] = a, f"Совет {n + 1}"
    new = await humanize_fields(fields, labels, question)
    out = dict(norm)
    out["intro"] = new.get("intro", norm.get("intro", ""))
    out["cards"] = [(idx, pos, new[f"card_{n}"]) for n, (idx, pos, _) in enumerate(norm["cards"])]
    out["synthesis"], out["answer"] = new["synthesis"], new["answer"]
    out["advice"] = [new[f"advice_{n}"] for n in range(len(norm["advice"]))]
    return out


async def generate_dynamic_reading(question: str, pre_selected_cards: list) -> dict:
    """Индивидуальный разбор — 6-12 карт + общая картина, прямой ответ и шаги."""
    cards_str = ", ".join([f"[{i}] {c['name']}" for i, c in enumerate(pre_selected_cards)])
    system_prompt = (
        "Ты — мастер таро с 30-летним опытом, практикующий в традициях Уэйта-Смит, Хайо Банцхафа и Карла Юнга.\n\n"
        "ЗАДАЧА: глубокий индивидуальный расклад по вопросу человека.\n\n"
        "КОЛИЧЕСТВО КАРТ — выбери сам из списка от 6 до 10 в зависимости от сложности вопроса:\n"
        "• 6 карт — конкретный краткосрочный вопрос\n"
        "• 7-8 карт — ситуация, отношения, решение\n"
        "• 9-10 карт — глубокий вопрос о жизненном пути, предназначении, переменах\n\n"
        "ПОЗИЦИИ: Корни ситуации, Текущая энергия, Скрытое влияние, Внутреннее состояние, Окружение, "
        "Чего опасаться, Совет карт, Ближайшее будущее, Итог (бери нужные под количество карт).\n\n"
        "Толкование каждой карты — 45-70 слов: значение карты по Уэйту, применённое к вопросу, "
        "строго с ракурса своей позиции (Корни — причины, Скрытое влияние — то, чего человек не видит, "
        "Окружение — другие люди и их мотивы, Чего опасаться — риск, Совет — действие, Итог — исход).\n"
        "synthesis — 80-120 слов. answer — 50-90 слов. advice — 3-4 пункта по 10-25 слов.\n\n"
        + STRUCTURED_FORMAT
    )
    user_prompt = f"Вопрос человека: «{question}».\nДоступные карты (выбирай по номеру в квадратных скобках): {cards_str}."
    norm = await call_ai(system_prompt, user_prompt, max_tokens=6000, schema=STRUCTURED_SCHEMA,
                         validate=_make_validator(pre_selected_cards, 6, 10))
    if norm:
        norm = await _humanize_structured(norm, question, pre_selected_cards)
        return _build_structured(norm, pre_selected_cards)
    log("⚠️ Все AI недоступны. Переход на локальные толкования.")
    return generate_local_tarot_reading(question, pre_selected_cards, reading_type="dynamic")


async def generate_preset_reading(question: str, pre_selected_cards: list) -> dict:
    """Стандартный разбор — 3 карты (Прошлое/Настоящее/Будущее) + общая картина, ответ и шаги."""
    cards_str = ", ".join([f"[{i}] {c['name']}" for i, c in enumerate(pre_selected_cards)])
    system_prompt = (
        "Ты — опытный таролог, практикующий по системе Уэйта. Даёшь точные, живые ответы на жизненные вопросы.\n\n"
        "ЗАДАЧА: выбери из списка ровно 3 карты и сделай расклад с позициями «Прошлое», «Настоящее», «Будущее».\n"
        "Толкование каждой карты — 60-90 слов: классическое значение + как оно проявляется в ситуации человека.\n"
        "Ракурсы позиций: «Прошлое» — что привело к ситуации и что из этого до сих пор влияет; "
        "«Настоящее» — что происходит сейчас, что чувствуют и делают участники; "
        "«Будущее» — к чему всё идёт, если ничего не менять.\n"
        "synthesis — 60-90 слов. answer — 40-80 слов. advice — 3 пункта по 10-25 слов.\n\n"
        + STRUCTURED_FORMAT
    )
    user_prompt = f"Вопрос: «{question}».\nДоступные карты (выбирай по номеру в квадратных скобках): {cards_str}."
    norm = await call_ai(system_prompt, user_prompt, max_tokens=3500, schema=STRUCTURED_SCHEMA,
                         validate=_make_validator(pre_selected_cards, 3, 3))
    if norm:
        norm = await _humanize_structured(norm, question, pre_selected_cards)
        return _build_structured(norm, pre_selected_cards)
    log("⚠️ Все AI недоступны для стандартного разбора. Локальные толкования.")
    return generate_local_tarot_reading(question, pre_selected_cards)


# ---------------------------------------------------------------------
# Быстрый ответ: одна карта, при необходимости 1–2 уточняющие.
# Карты тянутся по порядку: [0] — главная, [1] и [2] — уточнения.
# ---------------------------------------------------------------------
QUICK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "cards": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "index": {"type": "INTEGER"}, "meaning": {"type": "STRING"}},
            "required": ["index", "meaning"]}},
        "answer": {"type": "STRING"},
    },
    "required": ["cards", "answer"],
}


def _quick_validator(pre_selected_cards: list):
    def validate(parsed: dict) -> Optional[dict]:
        raw_cards = parsed.get("cards")
        if not isinstance(raw_cards, list):
            return None
        by_idx = {}
        for c in raw_cards:
            if not isinstance(c, dict):
                continue
            try:
                idx = int(c.get("index"))
            except (TypeError, ValueError):
                continue
            meaning = clean_ai_text(str(c.get("meaning") or "")).strip()
            if 0 <= idx < len(pre_selected_cards) and idx not in by_idx and len(meaning) >= 25:
                by_idx[idx] = meaning
        # карты идут строго по порядку вытягивания: 0, затем 1, затем 2
        used = []
        for i in range(min(3, len(pre_selected_cards))):
            if i not in by_idx:
                break
            used.append(i)
        answer = clean_ai_text(str(parsed.get("answer") or "")).strip()
        if not used or len(answer) < 20:
            return None
        return {"answer": answer, "cards": [(i, by_idx[i]) for i in used]}
    return validate


def _build_quick(norm: dict, pre_selected_cards: list) -> dict:
    parts = [f"**✨ Ответ карт**\n{norm['answer']}"]
    for i, meaning in norm["cards"]:
        name = _card_name(pre_selected_cards[i])
        title = f"**🃏 {name}**" if i == 0 else f"**🔎 Уточнение — {name}**"
        parts.append(f"{title}\n{meaning}")
    return {"cards_used_indices": [i for i, _ in norm["cards"]], "reading": "\n\n".join(parts)}


async def generate_quick_reading(question: str, pre_selected_cards: list) -> dict:
    """Быстрый ответ — короткий ответ по одной карте."""
    pre_selected_cards = pre_selected_cards[:1]
    cards_str = f"[0] {pre_selected_cards[0]['name']} ({pre_selected_cards[0]['type']})"
    system_prompt = (
        "Ты — опытный таролог, практикующий по системе Уэйта. Отвечаешь на вопрос быстро, коротко и по делу, "
        "как в живой переписке.\n\n"
        "ЗАДАЧА: ответ по ОДНОЙ вытянутой карте. Других карт нет и не добавляй их.\n"
        "• meaning — 45–75 слов: значение этой карты по Уэйту и что именно она говорит про ситуацию человека. "
        "Рассмотри ситуацию целиком: что происходит, что чувствуют участники, к чему идёт.\n"
        "• answer — ответ на вопрос: 30–55 слов, без воды, и он вытекает из значения карты. "
        "Если в вопросе есть имена — обращайся к человеку по имени и называй того, о ком спрашивают.\n"
        "• Если указана дата рождения — учти знак зодиака одной короткой фразой в answer, без отдельного гороскопа.\n\n"
        "ДЕЛИКАТНЫЕ ВОПРОСЫ: не предсказывай смерть, сроки жизни, болезни и диагнозы, беременность, "
        "исход суда. На такой вопрос мягко скажи, что карты не отвечают на него, и ответь на то, что они "
        "могут показать: состояние человека, отношения, что важно сейчас.\n\n"
        "ФОРМАТ — строго JSON (без markdown-блоков):\n"
        '{"cards": [{"index": 0, "meaning": "..."}], "answer": "..."}\n\n'
        "• Только **жирный** Markdown, без HTML и заголовков #. Не повторяй название карты в начале meaning.\n"
        "• answer и meaning не пересказывают друг друга: meaning объясняет карту, answer отвечает на вопрос.\n"
        "• Никогда не используй слова 'нейросеть', 'ИИ', 'алгоритм'. Обращайся на «ты», тепло и живо.\n\n"
        + ANSWER_RULE + "\n\n" + HUMAN_STYLE
    )
    user_prompt = f"Вопрос: «{question}».\nВытянутая карта: {cards_str}."
    norm = await call_ai(system_prompt, user_prompt, max_tokens=1500, schema=QUICK_SCHEMA,
                         validate=_quick_validator(pre_selected_cards))
    if norm:
        if HUMANIZE_QUICK:
            fields = {"answer": norm["answer"]}
            labels = {"answer": "Ответ на вопрос"}
            for n, (i, meaning) in enumerate(norm["cards"]):
                fields[f"card_{n}"] = meaning
                labels[f"card_{n}"] = "Значение карты — " + _card_name(pre_selected_cards[i])
            new = await humanize_fields(fields, labels, question)
            norm = {"answer": new["answer"],
                    "cards": [(i, new[f"card_{n}"]) for n, (i, _) in enumerate(norm["cards"])]}
        return _build_quick(norm, pre_selected_cards)
    log("⚠️ Все AI недоступны для быстрого ответа. Локальное толкование.")
    card = pre_selected_cards[0]
    name = card["name"].replace("Старший Аркан: ", "")
    text = (f"**✨ Ответ карт**\nКарта не даёт однозначного ответа: исход зависит от твоих шагов "
            f"в ближайшее время. Прислушайся к её совету.\n\n"
            f"**🃏 {name}**\n{get_rich_card_meaning(card['name'], 'present')}")
    return {"cards_used_indices": [0], "reading": text}


async def generate_spread_reading(spread_id: str, question: str, pre_selected_cards: list) -> dict:
    """Тематический расклад: фиксированные позиции из SPREADS, карты — по порядку вытягивания."""
    sp = SPREADS[spread_id]
    n = len(sp["positions"])
    cards = pre_selected_cards[:n]
    cards_str = ", ".join(f"[{i}] {c['name']} — позиция «{sp['positions'][i]}»" for i, c in enumerate(cards))
    system_prompt = (
        "Ты — опытный таролог, практикующий по системе Уэйта. Даёшь точные, живые ответы на жизненные вопросы.\n\n"
        f"ЗАДАЧА: тематический расклад «{sp['title']}». Карты уже вытянуты и стоят на своих позициях — "
        f"опиши ВСЕ {n} карт по порядку, index — номер карты в квадратных скобках, position — название её позиции.\n"
        "Толкование каждой карты — 45-75 слов: классическое значение, применённое к вопросу строго с ракурса позиции.\n"
        "synthesis — 50-80 слов. answer — 40-70 слов. advice — 3 пункта по 10-25 слов.\n\n"
        + STRUCTURED_FORMAT
    )
    user_prompt = f"Вопрос: «{question}».\nКарты по позициям: {cards_str}."
    norm = await call_ai(system_prompt, user_prompt, max_tokens=4000, schema=STRUCTURED_SCHEMA,
                         validate=_make_validator(cards, n, n))
    if norm:
        # позиции — всегда наши, в нашем порядке, даже если модель переставила
        by_idx = {idx: meaning for idx, _, meaning in norm["cards"]}
        norm["cards"] = [(i, sp["positions"][i], by_idx[i]) for i in range(n) if i in by_idx]
        if len(norm["cards"]) == n:
            norm = await _humanize_structured(norm, question, cards)
            return _build_structured(norm, cards)
    log(f"⚠️ AI недоступен для расклада {spread_id}. Локальные толкования.")
    parts = [f"Твой вопрос: **«{question}»**"]
    for i, card in enumerate(cards):
        parts.append(f"**{sp['positions'][i]} — {_card_name(card)}**\n{get_rich_card_meaning(card['name'], 'present')}")
    parts.append("**🧭 Что делать**\n• Опирайся на карту последней позиции: она показывает, куда всё движется.\n"
                 "• Вернись к раскладу через несколько дней и сверь, что изменилось.")
    return {"cards_used_indices": list(range(n)), "reading": "\n\n".join(parts)}


async def generate_clarification(question: str, kind: str, cards: list, reading_text: str, new_card: dict) -> str:
    """Уточняющая карта к уже сделанному раскладу: отвечает на тот же вопрос с учётом выпавших карт."""
    name = _card_name(new_card)
    prev = ", ".join(_card_name(c) for c in cards) or "—"
    system_prompt = (
        "Ты — опытный таролог, практикующий по системе Уэйта. Человек уже получил расклад и вытянул "
        "УТОЧНЯЮЩУЮ карту. Объясни, что она добавляет к раскладу и как уточняет ответ на его вопрос.\n\n"
        "• 60-100 слов, тепло, на «ты», без повторения уже сказанного в раскладе.\n"
        "• Свяжи уточняющую карту с уже выпавшими картами и закончи одним конкретным выводом или шагом.\n"
        "• Только **жирный** Markdown, без заголовков и HTML. Не пиши название карты в начале.\n\n"
        + ANSWER_RULE + "\n\n" + HUMAN_STYLE + "\n\n"
        'ФОРМАТ — строго JSON: {"reading": "текст"}'
    )
    user_prompt = (f"Вопрос: «{question or 'Карта дня'}».\nВыпавшие карты: {prev}.\n"
                   f"Расклад (кратко): {reading_text[:1500]}\n\nУточняющая карта: {new_card['name']} ({new_card['type']}).")

    def validate(parsed: dict) -> Optional[dict]:
        text = clean_ai_text(str(parsed.get("reading") or "")).strip()
        return {"reading": text} if len(text) >= 60 else None

    res = await call_ai(system_prompt, user_prompt, max_tokens=1200, validate=validate)
    body = res["reading"] if res else get_rich_card_meaning(new_card["name"], "present")
    return f"**🔎 Уточнение — {name}**\n{body}"


_DAILY_LABEL_RE = re.compile(r"^[ \t]*\**[ \t]*Основное толкование[ \t]*\**[ \t]*:?[ \t]*\**[ \t]*", re.IGNORECASE | re.MULTILINE)


def _clean_daily_text(text: str) -> str:
    """Убирает служебные пометки, которые модель иногда копирует из промпта."""
    text = _WORDCOUNT_RE.sub("", text or "")
    text = _DAILY_LABEL_RE.sub("", text)
    return text.strip()


async def generate_daily_reading(pre_selected_cards: list) -> dict:
    """Карта дня — живое, детальное толкование одной карты (250-300 слов)."""
    card = pre_selected_cards[0]
    card_name = card["name"].replace("Старший Аркан: ", "")
    card_str = f"[0] {card['name']} ({card['type']})"

    system_prompt = (
        "Ты — мастер таро, хранитель древних символов. Твой стиль — поэтичный, тёплый, живой.\n\n"
        "ЗАДАЧА: дай глубокое толкование Карты Дня — одной карты, которая станет ориентиром на 24 часа.\n\n"
        "Текст состоит из четырёх частей, строго в таком виде:\n\n"
        f"**Карта Дня — {card_name}**\n\n"
        "<абзац толкования>\n\n"
        "**На что обратить внимание сегодня:** <текст>\n\n"
        "**Совет дня:** <текст>\n\n"
        "Что писать в каждой части:\n"
        "• Абзац толкования — архетип карты по традиции Уэйта: светлая и теневая сторона, "
        "символика, что карта говорит о сегодняшнем дне. Живо, с образами. Примерно 120–150 слов. "
        "Абзац начинается сразу с текста, без заголовка.\n"
        "• «На что обратить внимание сегодня» — конкретная область жизни или внутреннее состояние, "
        "которое карта подсвечивает сегодня. Примерно 50–60 слов.\n"
        "• «Совет дня» — одно чёткое действенное напутствие: что сделать, о чём подумать, "
        "чего избежать. Примерно 40–50 слов.\n\n"
        "ВАЖНО:\n"
        "• Никогда не пиши в тексте количество слов, скобки с цифрами и служебные пометки "
        "вроде «Основное толкование».\n"
        "• Только **жирный** Markdown, никаких HTML-тегов.\n"
        "• Никаких слов 'нейросеть', 'ИИ'. Каждый день — уникальный текст. Тепло, лично, вдохновляюще.\n\n"
        + HUMAN_STYLE + "\n\n"
        "Формат ответа — строго JSON (без markdown-блоков):\n"
        "{\"cards_used_indices\": [0], \"reading\": \"текст\"}"
    )

    user_prompt = f"Карта дня: {card_str}."

    result = await call_ai(system_prompt, user_prompt, max_tokens=1800)
    if result:
        text = _clean_daily_text(result.get("reading", ""))
        new = await humanize_fields({"reading": text}, {"reading": "Толкование Карты Дня"}, "",
                                    keep_bold=True)
        result["reading"] = new["reading"]
        return result

    return generate_local_tarot_reading("", pre_selected_cards, reading_type="daily")


# =====================================================================
# ФОНОВЫЕ ЗАДАЧИ: вебхук, keep-alive, ежедневный пуш
# =====================================================================
async def setup_bot():
    """Вебхук БЕЗ drop_pending_updates (раньше терялся /start, который будил сервер),
    плюс команды и кнопка-меню мини-аппа рядом с полем ввода."""
    webhook_url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    try:
        await bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["message", "pre_checkout_query", "callback_query"],
            drop_pending_updates=False,
        )
        log(f"Вебхук: {webhook_url}")
    except Exception as e:
        log(f"⚠️ Не удалось установить вебхук: {e}")
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="🔮 Открыть Послание Таро"),
            BotCommand(command="ref", description="🎁 Пригласить друга и получить энергию"),
            BotCommand(command="daily", description="🌅 Карта Дня каждое утро: вкл/выкл"),
            BotCommand(command="subscription", description="🌟 Подписка «Послание+»"),
            BotCommand(command="stop", description="🔕 Отключить напоминания"),
            BotCommand(command="feedback", description="✍️ Отзыв или идея"),
            BotCommand(command="support", description="💬 Поддержка"),
            BotCommand(command="paysupport", description="💳 Вопросы по оплате"),
            BotCommand(command="terms", description="📜 Условия использования"),
        ])
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🔮 Таро", web_app=WebAppInfo(url=FRONTEND_URL)))
    except Exception as e:
        log(f"⚠️ Команды/меню: {e}")
    if old_bot:
        try:
            await old_bot.set_webhook(
                url=f"{PUBLIC_URL}{OLD_WEBHOOK_PATH}",
                secret_token=OLD_WEBHOOK_SECRET,
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=False,
            )
            await old_bot.set_my_commands([BotCommand(command="start", description="🔮 Перейти в новый бот")])
            await old_bot.set_chat_menu_button(menu_button=MenuButtonDefault())
            log(f"Старый бот: режим «переехали», вебхук {PUBLIC_URL}{OLD_WEBHOOK_PATH}")
        except Exception as e:
            log(f"⚠️ Старый бот: {e}")


async def keepalive_loop():
    """Бесплатный Render засыпает после 15 минут без входящих запросов. Пингуем себя раз в 10 минут."""
    if not KEEPALIVE or not os.getenv("RENDER_EXTERNAL_URL"):
        return
    await asyncio.sleep(60)
    while True:
        try:
            await http.get(f"{PUBLIC_URL}/health", timeout=15)
        except Exception as e:
            log(f"keep-alive: {e}")
        await asyncio.sleep(600)


PUSH_TEXTS = [
    "🃏 {name}, твоя Карта Дня уже ждёт. Узнай, что приготовил этот день ✨",
    "🌙 Доброе утро, {name}! Карта на сегодня уже вытянута для тебя. Открой её 🔮",
    "✨ {name}, вселенная оставила тебе послание на сегодня. Карта Дня — бесплатно.",
    "🔮 {name}, прежде чем день закрутится — загляни в свою Карту Дня.",
]


def webapp_kb(text: str = "🔮 Открыть расклады", channel: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=FRONTEND_URL))]]
    if channel and CHANNEL_URL:
        rows.append([InlineKeyboardButton(text="📢 Наш канал", url=CHANNEL_URL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def paid_kb(reading_id: Optional[int] = None) -> InlineKeyboardMarkup:
    """Кнопка после оплаты: если человек пополнял ради уточняющей карты — ведём обратно в тот расклад."""
    if reading_id:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="🔎 Вернуться к раскладу и вытянуть карту", web_app=WebAppInfo(url=webapp_url(reading_id)))]])
    return webapp_kb()


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def energy_reminder(balance: int) -> str:
    """Строка о балансе для еженедельной рассылки — только если энергии хватает хотя бы на один разбор."""
    if balance < COST_QUICK:
        return ""
    if balance < COST_PRESET:
        q = balance // COST_QUICK
        return (f"\n\n✨ У тебя осталось {balance} Энергии — хватит на {q} "
                f"{_plural(q, 'Быстрый ответ', 'Быстрых ответа', 'Быстрых ответов')}. Задай вопрос картам!")
    n = balance // COST_PRESET
    std = f"{n} {_plural(n, 'Стандартный разбор', 'Стандартных разбора', 'Стандартных разборов')}"
    if balance >= COST_AI:
        what = f"на 🔮 Индивидуальный разбор или {std}"
    else:
        what = f"на {std}"
    return f"\n\n✨ У тебя осталось {balance} Энергии — хватит {what}. Заходи, задай вопрос картам!"


def yesterday_str() -> str:
    return (local_now() - timedelta(days=1)).strftime("%Y-%m-%d")


def webapp_url(reading_id: Optional[int] = None) -> str:
    """Ссылка на мини-апп; с reading_id мини-апп сразу откроет этот расклад."""
    return f"{FRONTEND_URL}?r={int(reading_id)}" if reading_id else FRONTEND_URL


async def send_daily_push(weekly: bool) -> int:
    """Утреннее напоминание: каждый день — тем, кто включил ежедневную карту (daily_push),
    в день недельной рассылки — ещё и всем остальным с notify. Возвращает число отправленных."""
    p = await db()
    today = get_today_str()
    yesterday = yesterday_str()
    sent = 0
    while True:
        rows = await p.fetch("""
            SELECT telegram_id, first_name, free_readings, free_preset, balance, streak, last_daily_date FROM users
            WHERE notify AND NOT blocked AND (daily_push OR $2)
              AND (last_push_date IS DISTINCT FROM $1)
              AND (last_daily_date IS DISTINCT FROM $1)
            LIMIT 200
        """, today, weekly)
        if not rows:
            # тем, кто уже взял карту сам, пуш не нужен — просто отмечаем
            await p.execute("UPDATE users SET last_push_date = $1 WHERE last_daily_date = $1 AND last_push_date IS DISTINCT FROM $1", today)
            break
        for r in rows:
            uid = r["telegram_id"]
            text = random.choice(PUSH_TEXTS).format(name=r["first_name"] or "Искатель")
            streak = r["streak"] or 0
            if streak >= 2 and r["last_daily_date"] == yesterday:
                nxt = STREAK_QUICK_EVERY - streak % STREAK_QUICK_EVERY
                text += (f"\n\n🔥 Серия: {streak} {_plural(streak, 'день', 'дня', 'дней')} подряд. Не прерывай! "
                         f"Через {nxt} {_plural(nxt, 'день', 'дня', 'дней')} ждёт бесплатный Быстрый ответ.")
            if (r["free_preset"] or 0) > 0:
                text += "\n\n🎁 А ещё тебя ждёт бесплатный Стандартный разбор на 3 карты."
            elif (r["free_readings"] or 0) > 0:
                text += "\n\n🎁 А ещё у тебя есть бесплатный Быстрый ответ: задай картам свой вопрос."
            text += energy_reminder(r["balance"] or 0)
            await p.execute("UPDATE users SET last_push_date = $2 WHERE telegram_id = $1", uid, today)
            try:
                await bot.send_message(uid, text, reply_markup=webapp_kb("🃏 Открыть Карту Дня", channel=True))
                sent += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except (TelegramForbiddenError, TelegramBadRequest):
                await p.execute("UPDATE users SET blocked = TRUE WHERE telegram_id = $1", uid)
            except Exception as e:
                log(f"push {uid}: {e}")
            await asyncio.sleep(0.05)  # ~20 сообщений/сек — в пределах лимитов Telegram
    return sent


async def _send_safe(uid: int, text: str, kb: InlineKeyboardMarkup) -> bool:
    """Отправка в фоновых рассылках: помечает заблокировавших, переживает лимиты Telegram."""
    p = await db()
    for _ in range(2):
        try:
            await bot.send_message(uid, text, reply_markup=kb)
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except (TelegramForbiddenError, TelegramBadRequest):
            await p.execute("UPDATE users SET blocked = TRUE WHERE telegram_id = $1", uid)
            return False
        except Exception as e:
            log(f"send {uid}: {e}")
            return False
    return False


FOLLOWUP_KINDS = ("quick", "preset", "ai") + tuple(f"sp_{k}" for k in SPREADS)


async def send_followups() -> int:
    """«Сбылось?» через FOLLOWUP_DAYS после расклада. Одному человеку — не больше одного вопроса за раз:
    берём его последний расклад в окне, остальные помечаем как пропущенные."""
    p = await db()
    rows = await p.fetch("""
        SELECT DISTINCT ON (r.telegram_id) r.id, r.telegram_id, r.question, r.cards, u.first_name
        FROM readings r JOIN users u ON u.telegram_id = r.telegram_id
        WHERE r.followup IS NULL AND r.kind = ANY($1::text[])
          AND r.created_at < NOW() - make_interval(days => $2)
          AND r.created_at > NOW() - make_interval(days => $2 + 4)
          AND u.notify AND NOT u.blocked
        ORDER BY r.telegram_id, r.created_at DESC
        LIMIT 200
    """, list(FOLLOWUP_KINDS), FOLLOWUP_DAYS)
    sent = 0
    for r in rows:
        uid = r["telegram_id"]
        await p.execute("""
            UPDATE readings SET followup = CASE WHEN id = $2 THEN 'sent' ELSE 'skip' END
            WHERE telegram_id = $1 AND followup IS NULL AND created_at < NOW() - make_interval(days => $3)
        """, uid, r["id"], FOLLOWUP_DAYS)
        cards = json.loads(r["cards"] or "[]")
        card = _card_name(cards[0]) if cards else ""
        q = (r["question"] or "").strip()
        text = (f"🔮 {r['first_name'] or 'Искатель'}, несколько дней назад ты спрашивал(а) карты:\n«{q[:200]}»\n\n"
                + (f"Тогда выпала карта {card}. " if card else "") + "Как всё сложилось? Расклад сбылся?")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, сбылось", callback_data=f"fu:{r['id']}:yes"),
             InlineKeyboardButton(text="❌ Нет", callback_data=f"fu:{r['id']}:no")],
            [InlineKeyboardButton(text="⏳ Пока непонятно", callback_data=f"fu:{r['id']}:wait")],
        ])
        if await _send_safe(uid, text, kb):
            sent += 1
            track(uid, "followup_sent", {"reading": r["id"]})
        await asyncio.sleep(0.05)
    return sent


async def send_winback() -> int:
    """Возврат «спящих»: у кого есть Энергия или подарок, но не заходил WINBACK_IDLE_DAYS дней."""
    p = await db()
    rows = await p.fetch("""
        SELECT telegram_id, first_name, balance, free_readings, free_preset FROM users
        WHERE notify AND NOT blocked
          AND (balance >= $1 OR free_readings > 0 OR free_preset > 0)
          AND COALESCE(last_seen, created_at) < NOW() - make_interval(days => $2)
          AND (last_winback IS NULL OR last_winback < NOW() - make_interval(days => $3))
          AND last_push_date IS DISTINCT FROM $4
        LIMIT 200
    """, COST_QUICK, WINBACK_IDLE_DAYS, WINBACK_REPEAT_DAYS, get_today_str())
    sent = 0
    for r in rows:
        uid = r["telegram_id"]
        await p.execute("UPDATE users SET last_winback = NOW() WHERE telegram_id = $1", uid)
        name = r["first_name"] or "Искатель"
        if (r["free_preset"] or 0) > 0:
            text = f"🎁 {name}, твой бесплатный Стандартный разбор на 3 карты всё ещё ждёт. Задай картам вопрос!"
        elif (r["free_readings"] or 0) > 0:
            text = f"🎁 {name}, у тебя есть бесплатный Быстрый ответ. Карты готовы ответить на твой вопрос."
        else:
            text = f"🔮 {name}, давно не виделись!" + energy_reminder(r["balance"] or 0)
        if await _send_safe(uid, text, webapp_kb("🔮 Задать вопрос картам")):
            sent += 1
            track(uid, "winback_sent")
        await asyncio.sleep(0.05)
    return sent


async def push_loop():
    if not PUSH_ENABLED:
        return
    await pool_ready.wait()
    while True:
        try:
            now = local_now()
            # Утром: ежедневная карта для подписавшихся, по PUSH_WEEKDAY — для всех с notify.
            # last_push_date не даёт отправить повторно в тот же день.
            if now.hour >= PUSH_HOUR:
                n = await send_daily_push(weekly=now.weekday() == PUSH_WEEKDAY)
                if n:
                    log(f"📬 Утренний пуш отправлен: {n}")
            # «Сбылось?» и возврат — только днём, не ночью
            if 10 <= now.hour < 21:
                n = await send_followups()
                if n:
                    log(f"📬 «Сбылось?»: {n}")
                n = await send_winback()
                if n:
                    log(f"📬 Возврат спящих: {n}")
        except Exception as e:
            log(f"push_loop: {e}")
        await asyncio.sleep(120)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global http
    http = httpx.AsyncClient(timeout=30.0, limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))
    # Всё тяжёлое — в фоне: сервер начинает принимать запросы сразу
    tasks = [
        asyncio.create_task(init_pool_forever()),
        asyncio.create_task(setup_bot()),
        asyncio.create_task(keepalive_loop()),
        asyncio.create_task(push_loop()),
        asyncio.create_task(load_tribute_products()),
        asyncio.create_task(news_scheduler()),
    ]
    yield
    for t in tasks:
        t.cancel()
    await http.aclose()
    if pool:
        await pool.close()
    await bot.session.close()


# ── Отзывы и предложения ──────────────────────────────────────────────────
def feedback_recipients() -> list:
    if FEEDBACK_CHAT_ID.lstrip("-").isdigit():
        return [int(FEEDBACK_CHAT_ID)]
    return sorted(ADMIN_IDS)


async def save_feedback(uid: int, username: Optional[str], kind: str, rating: Optional[int],
                        text: str, source: str, reading: Optional[dict] = None) -> int:
    """Сохраняет отзыв и пересылает его админам. Пользователь видит только «Спасибо»."""
    text = (text or "").strip()[:2000]
    if kind not in FEEDBACK_KINDS:
        kind = "idea"
    if not (isinstance(rating, int) and 1 <= rating <= 5) or kind != "review":
        rating = None
    p = await db()
    recent = await p.fetchval(
        "SELECT COUNT(*) FROM feedback WHERE telegram_id = $1 AND created_at > NOW() - INTERVAL '1 hour'", uid)
    if recent >= FEEDBACK_LIMIT_PER_HOUR:
        raise HTTPException(status_code=429, detail="Вы уже отправили несколько сообщений. Попробуйте чуть позже 🙏")
    # тот же текст от того же человека за 2 минуты — дубль (двойное нажатие), не пересылаем
    dup = await p.fetchval("""
        SELECT id FROM feedback WHERE telegram_id = $1 AND text = $2
          AND created_at > NOW() - INTERVAL '2 minutes' LIMIT 1
    """, uid, text)
    if dup:
        return dup
    r_kind = r_q = r_cards = None
    if isinstance(reading, dict) and reading.get("kind") in READING_KINDS:
        r_kind = reading["kind"]
        r_q = str(reading.get("question") or "")[:500] or None
        r_cards = str(reading.get("cards") or "")[:300] or None
    fb_id = await p.fetchval("""
        INSERT INTO feedback (telegram_id, username, kind, rating, text, source, reading_kind, question, cards)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id
    """, uid, username, kind, rating, text, source, r_kind, r_q, r_cards)
    u = await p.fetchrow("SELECT balance, purchases FROM users WHERE telegram_id = $1", uid)
    who = f"@{username}" if username else f"id {uid}"
    stats = f"баланс {u['balance']}, покупок {u['purchases'] or 0}" if u else "нет в базе"
    stars = f" {'★' * rating}{'☆' * (5 - rating)}" if rating else ""
    about = "🧭 Откуда: общая кнопка «Отзыв или идея»"
    if r_kind:
        about = f"🧭 После 👎 на расклад: {READING_KINDS[r_kind]}"
        if r_q:
            about += f"\n❓ Вопрос: «{r_q}»"
        if r_cards:
            about += f"\n🃏 Карты: {r_cards}"
    elif source == "bot":
        about = "🧭 Откуда: команда /feedback в боте"
    msg = (f"{FEEDBACK_KINDS[kind]}{stars} #fb{fb_id}\n"
           f"От: {who} ({stats})\n{about}\n\n💬 {text}\n\n"
           "↩️ Ответьте реплаем на это сообщение — бот перешлёт ответ пользователю.")
    for chat in feedback_recipients():
        try:
            await bot.send_message(chat, msg)
        except Exception as e:
            log(f"Отзыв #{fb_id}: не удалось отправить в {chat}: {e}")
    return fb_id


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# =====================================================================
# API ЭНДПОИНТЫ
# =====================================================================
@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"ok": True, "db": pool_ready.is_set()}


async def auth_user(authorization: Optional[str]) -> tuple:
    auth = verify_telegram_init_data(authorization)
    user, _, _ = await ensure_user(auth["user"], auth.get("start_param"))
    return user, is_admin(user)


def profile_payload(user: dict, admin: bool) -> dict:
    return {
        "registered": True,
        "user_id": user["telegram_id"],
        "name": user.get("first_name") or "Искатель",
        "balance": 99999 if admin else user.get("balance", 0),
        "free_readings": 0 if admin else (user.get("free_readings") or 0),
        "free_preset": 0 if admin else (user.get("free_preset") or 0),
        "daily_available": admin or user.get("last_daily_date") != get_today_str(),
        "daily_push": bool(user.get("daily_push")),
        "streak": (user.get("streak") or 0) if user.get("last_daily_date") in (get_today_str(), yesterday_str()) else 0,
        "ref_link": ref_link(user["telegram_id"]),
        "ref_count": user.get("ref_count") or 0,
        "ref_bonus": REF_BONUS,
        "first_purchase": (user.get("purchases") or 0) == 0,
        "costs": {"quick": COST_QUICK, "preset": COST_PRESET, "ai": COST_AI,
                  "clarify": 0 if sub_active(user) else COST_CLARIFY},
        "max_clarify": MAX_CLARIFY,
        "spreads": [{"id": k, "title": v["title"], "emoji": v["emoji"], "cost": v["cost"], "cards": len(v["positions"]),
                     "desc": v["desc"], "default_q": v["default_q"]} for k, v in SPREADS.items()],
        "sub": {"active": sub_active(user), "price": SUB_PRICE, "energy": SUB_ENERGY,
                "until": user["sub_until"].isoformat() if user.get("sub_until") else None},
        "prices": {k: (v[2] if (user.get("purchases") or 0) == 0 else v[1]) for k, v in PACKS.items()},
        "card_enabled": card_enabled(),
        "card_currency": TRIBUTE_CURRENCY_SIGN,
        "card_prices": {k: card_price(k, user) for k in PACKS if k in TRIBUTE_PRODUCT_IDS and k in CARD_PRICES},
        "card_prices_alt": {k: alt_price(card_price(k, user)) for k in PACKS
                            if k in TRIBUTE_PRODUCT_IDS and k in CARD_PRICES},
    }


def card_price(pack: str, user: dict) -> int:
    """Картой — всегда полная цена, скидка первой покупки только для Stars."""
    return CARD_PRICES[pack]


READING_KINDS = {"daily": "🃏 Карта дня", "quick": "⚡ Быстрый ответ", "preset": "🎴 Стандартный", "ai": "🔮 Индивидуальный",
                 **{f"sp_{k}": f"{v['emoji']} {v['title']}" for k, v in SPREADS.items()}}


@app.post("/api/reading-rating")
async def api_reading_rating(payload: dict, authorization: str = Header(None)):
    user, _ = await auth_user(authorization)
    kind = str(payload.get("kind") or "")
    value = 1 if str(payload.get("value")) == "1" else -1
    if kind not in READING_KINDS:
        raise HTTPException(status_code=400, detail="bad kind")
    p = await db()
    # не больше 30 оценок в час от одного человека
    recent = await p.fetchval("SELECT COUNT(*) FROM reading_ratings WHERE telegram_id = $1 "
                              "AND created_at > NOW() - INTERVAL '1 hour'", user["telegram_id"])
    if recent < 30:
        await p.execute("""
            INSERT INTO reading_ratings (telegram_id, kind, value, question, cards)
            VALUES ($1, $2, $3, $4, $5)
        """, user["telegram_id"], kind, value,
            str(payload.get("question") or "")[:500], str(payload.get("cards") or "")[:300])
    return {"ok": True}


@app.post("/api/feedback")
async def api_feedback(payload: dict, authorization: str = Header(None)):
    user, _ = await auth_user(authorization)
    text = str(payload.get("text") or "").strip()
    if len(text) < 3:
        raise HTTPException(status_code=400, detail="Напишите хотя бы пару слов 🙂")
    rating = payload.get("rating")
    rating = int(rating) if str(rating).isdigit() else None
    await save_feedback(user["telegram_id"], user.get("username"), str(payload.get("kind") or "idea"),
                        rating, text, "app", payload.get("reading"))
    return {"ok": True}


@app.get("/api/user/profile")
async def get_user_profile(authorization: str = Header(None)):
    auth = verify_telegram_init_data(authorization)
    user, created, referrer = await ensure_user(auth["user"], auth.get("start_param"))
    uid = user["telegram_id"]
    if referrer:
        asyncio.create_task(notify_referrer(referrer, user.get("first_name")))
    if created:
        track(uid, "signup", {"source": user.get("source")})
    track(uid, "app_open")
    out = profile_payload(user, is_admin(user))
    p = await db()
    out["ref_invited"] = await p.fetchval("SELECT COUNT(*) FROM users WHERE referred_by = $1", uid)
    out["today_daily_id"] = None
    if user.get("last_daily_date") == get_today_str():
        p = await db()
        out["today_daily_id"] = await p.fetchval("""
            SELECT id FROM readings WHERE telegram_id = $1 AND kind = 'daily' ORDER BY created_at DESC LIMIT 1
        """, uid)
    return out


def _question(payload: dict) -> str:
    q = (payload.get("question") or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="Введите вопрос.")
    return q[:500]


async def _finish_reading(uid: int, admin: bool, mode: str, kind: str, question: str, cards: list, text: str) -> dict:
    """Общий хвост всех раскладов: сохранить в историю, событие, свежий баланс."""
    rid = await save_reading(uid, kind, question, cards, text)
    track(uid, "reading", {"kind": kind, "mode": mode})
    fresh = await get_user(uid)
    return {
        "success": True,
        "reading_id": rid,
        "cards": cards,
        "text": text,
        "new_balance": 99999 if admin else fresh["balance"],
        "free_readings": fresh.get("free_readings") or 0,
        "free_preset": fresh.get("free_preset") or 0,
        "used_free": mode.startswith("free"),
        "clarify_left": MAX_CLARIFY,
        "clarify_cost": 0 if sub_active(fresh) else COST_CLARIFY,
    }


@app.post("/api/user/use-preset-ai-reading")
async def use_preset_ai_reading(payload: dict, authorization: str = Header(None)):
    """Стандартный разбор (3 карты) — 150 энергии. Новым пользователям первый — в подарок (free_preset)."""
    user, admin = await auth_user(authorization)
    question = _question(payload)
    uid = user["telegram_id"]
    mode = "admin" if admin else await charge(uid, COST_PRESET, free="preset")

    try:
        pre_selected = random.sample(get_tarot_deck(), 6)
        result_data = await generate_preset_reading(question, pre_selected)
    except Exception as e:
        log(f"preset error: {e}")
        await refund(uid, mode, COST_PRESET)
        raise HTTPException(status_code=500, detail="Не получилось сделать расклад. Энергия возвращена.")

    used = [i for i in result_data.get("cards_used_indices", []) if isinstance(i, int) and 0 <= i < len(pre_selected)]
    used = list(dict.fromkeys(used))[:3]
    if len(used) < 3:
        used = [0, 1, 2]
    return await _finish_reading(uid, admin, mode, "preset", question,
                                 [pre_selected[i] for i in used], result_data.get("reading", ""))


@app.post("/api/user/use-quick-reading")
async def use_quick_reading(payload: dict, authorization: str = Header(None)):
    """Быстрый ответ — COST_QUICK энергии (по умолчанию 75): ровно 1 карта."""
    user, admin = await auth_user(authorization)
    question = _question(payload)
    uid = user["telegram_id"]
    mode = "admin" if admin else await charge(uid, COST_QUICK, free="quick")  # подарок — первый Быстрый ответ

    try:
        pre_selected = random.sample(get_tarot_deck(), 1)
        result_data = await generate_quick_reading(question, pre_selected)
    except Exception as e:
        log(f"quick error: {e}")
        await refund(uid, mode, COST_QUICK)
        raise HTTPException(status_code=500, detail="Не получилось сделать расклад. Энергия возвращена.")

    return await _finish_reading(uid, admin, mode, "quick", question, [pre_selected[0]], result_data.get("reading", ""))


@app.post("/api/user/use-spread")
async def use_spread(payload: dict, authorization: str = Header(None)):
    """Тематический расклад из SPREADS."""
    user, admin = await auth_user(authorization)
    spread_id = str(payload.get("spread") or "")
    if spread_id not in SPREADS:
        raise HTTPException(status_code=400, detail="Неизвестный расклад.")
    sp = SPREADS[spread_id]
    question = (payload.get("question") or "").strip()[:500] or sp["default_q"]
    if not question:
        raise HTTPException(status_code=400, detail="Введите вопрос.")
    uid = user["telegram_id"]
    mode = "admin" if admin else await charge(uid, sp["cost"])
    n = len(sp["positions"])
    try:
        pre_selected = random.sample(get_tarot_deck(), n)
        result_data = await generate_spread_reading(spread_id, question, pre_selected)
    except Exception as e:
        log(f"spread {spread_id} error: {e}")
        await refund(uid, mode, sp["cost"])
        raise HTTPException(status_code=500, detail="Не получилось сделать расклад. Энергия возвращена.")
    return await _finish_reading(uid, admin, mode, f"sp_{spread_id}", question, pre_selected,
                                 result_data.get("reading", ""))


async def _streak_reward(uid: int, streak: int) -> Optional[str]:
    """Награда за серию Карт Дня. Возвращает текст для показа или None."""
    p = await db()
    if streak > 0 and streak % STREAK_QUICK_EVERY == 0:
        await p.execute("UPDATE users SET free_readings = free_readings + 1 WHERE telegram_id = $1", uid)
        track(uid, "streak_reward", {"streak": streak, "kind": "quick"})
        return f"🔥 {streak} дней подряд! Дарим бесплатный Быстрый ответ."
    if streak == STREAK_ENERGY_DAY:
        await p.execute("UPDATE users SET balance = balance + $2 WHERE telegram_id = $1", uid, STREAK_ENERGY)
        track(uid, "streak_reward", {"streak": streak, "kind": "energy"})
        return f"🔥 3 дня подряд! Дарим +{STREAK_ENERGY} Энергии. На 7-й день подарим бесплатный Быстрый ответ."
    return None


@app.post("/api/user/use-daily-ai-reading")
async def use_daily_ai_reading(authorization: str = Header(None)):
    """Карта дня — бесплатно, 1 раз в сутки (граница суток — по TZ_OFFSET_HOURS).
    Повторный запрос в тот же день возвращает уже полученную карту, а не ошибку."""
    user, admin = await auth_user(authorization)
    uid = user["telegram_id"]
    today = get_today_str()
    p = await db()
    streak = user.get("streak") or 0
    if not admin:
        streak = await p.fetchval("""
            UPDATE users SET
                streak = CASE WHEN last_daily_date = $3 THEN streak + 1 ELSE 1 END,
                best_streak = GREATEST(best_streak, CASE WHEN last_daily_date = $3 THEN streak + 1 ELSE 1 END),
                last_daily_date = $2
            WHERE telegram_id = $1 AND last_daily_date IS DISTINCT FROM $2 RETURNING streak
        """, uid, today, yesterday_str())
        if streak is None:
            row = await p.fetchrow("""
                SELECT * FROM readings WHERE telegram_id = $1 AND kind = 'daily' ORDER BY created_at DESC LIMIT 1
            """, uid)
            if row and row["created_at"] > datetime.utcnow() - timedelta(hours=26):
                fresh = await get_user(uid)
                out = reading_payload(row, fresh)
                out.update({"success": True, "repeat": True, "reading_id": row["id"],
                            "new_balance": fresh["balance"], "streak": fresh.get("streak") or 0})
                return out
            raise HTTPException(status_code=429, detail="Карта дня ещё готовится. Загляните через минуту 🌙")

    pre_selected = random.sample(get_tarot_deck(), 1)
    try:
        result_data = await generate_daily_reading(pre_selected)
    except Exception as e:  # день уже отмечен — карта должна прийти в любом случае
        log(f"daily error: {e}")
        result_data = generate_local_tarot_reading("", pre_selected, reading_type="daily")
    reward = None if admin else await _streak_reward(uid, streak)
    out = await _finish_reading(uid, admin, "free", "daily", "", [pre_selected[0]], result_data.get("reading", ""))
    out.update({"streak": streak, "streak_reward": reward})
    return out


@app.post("/api/user/use-ai-reading")
async def use_ai_reading(payload: dict, authorization: str = Header(None)):
    """Индивидуальный разбор — 750 энергии, 6–12 карт."""
    user, admin = await auth_user(authorization)
    question = _question(payload)
    uid = user["telegram_id"]
    mode = "admin" if admin else await charge(uid, COST_AI)

    try:
        pre_selected = random.sample(get_tarot_deck(), 12)
        result_data = await generate_dynamic_reading(question, pre_selected)
    except Exception as e:
        log(f"ai reading error: {e}")
        await refund(uid, mode, COST_AI)
        raise HTTPException(status_code=500, detail="Не получилось сделать расклад. Энергия возвращена.")

    used = [i for i in result_data.get("cards_used_indices", []) if isinstance(i, int) and 0 <= i < len(pre_selected)]
    used = list(dict.fromkeys(used))[:12]
    if len(used) < 6:
        used = list(range(6))
    return await _finish_reading(uid, admin, mode, "ai", question,
                                 [pre_selected[i] for i in used], result_data.get("reading", ""))


# ── История раскладов и уточняющая карта ──────────────────────────────────
@app.get("/api/readings")
async def list_readings(authorization: str = Header(None), limit: int = 30):
    user, _ = await auth_user(authorization)
    p = await db()
    rows = await p.fetch("""
        SELECT id, kind, question, cards, clarifications, created_at FROM readings
        WHERE telegram_id = $1 ORDER BY created_at DESC LIMIT $2
    """, user["telegram_id"], max(1, min(limit, 100)))
    track(user["telegram_id"], "history_open")
    return {"readings": [{
        "id": r["id"], "kind": r["kind"], "title": READING_KINDS.get(r["kind"], r["kind"]),
        "question": r["question"] or "", "created_at": r["created_at"].isoformat(),
        "cards": [_card_name(c) for c in json.loads(r["cards"] or "[]")],
        "clarifications": len(json.loads(r["clarifications"] or "[]")),
    } for r in rows]}


@app.get("/api/readings/{reading_id}")
async def get_reading(reading_id: int, authorization: str = Header(None)):
    user, _ = await auth_user(authorization)
    p = await db()
    row = await p.fetchrow("SELECT * FROM readings WHERE id = $1 AND telegram_id = $2", reading_id, user["telegram_id"])
    if not row:
        raise HTTPException(status_code=404, detail="Расклад не найден.")
    return reading_payload(row, user)


@app.post("/api/readings/{reading_id}/clarify")
async def clarify_reading(reading_id: int, authorization: str = Header(None)):
    """Уточняющая карта к раскладу: COST_CLARIFY Энергии (подписчикам бесплатно), до MAX_CLARIFY на расклад."""
    user, admin = await auth_user(authorization)
    uid = user["telegram_id"]
    p = await db()
    row = await p.fetchrow("SELECT * FROM readings WHERE id = $1 AND telegram_id = $2", reading_id, uid)
    if not row:
        raise HTTPException(status_code=404, detail="Расклад не найден.")
    if len(json.loads(row["clarifications"] or "[]")) >= MAX_CLARIFY:
        raise HTTPException(status_code=400, detail="К этому раскладу уже вытянуты все уточняющие карты.")
    cost = 0 if (admin or sub_active(user)) else COST_CLARIFY
    mode = await charge(uid, cost)

    try:
        cards = json.loads(row["cards"] or "[]")
        taken = {c["name"] for c in cards} | {c["card"]["name"] for c in json.loads(row["clarifications"] or "[]")}
        new_card = random.choice([c for c in get_tarot_deck() if c["name"] not in taken])
        text = await generate_clarification(row["question"] or "", row["kind"], cards, row["text"], new_card)
    except Exception as e:
        log(f"clarify error: {e}")
        await refund(uid, mode, cost)
        raise HTTPException(status_code=500, detail="Не получилось вытянуть карту. Энергия возвращена.")

    item = {"card": new_card, "text": text, "at": datetime.utcnow().isoformat()}
    async with p.acquire() as c:
        async with c.transaction():
            cur = await c.fetchval("SELECT clarifications FROM readings WHERE id = $1 FOR UPDATE", reading_id)
            clar = json.loads(cur or "[]")
            if len(clar) >= MAX_CLARIFY:
                clar = None
            else:
                clar.append(item)
                await c.execute("UPDATE readings SET clarifications = $2 WHERE id = $1",
                                reading_id, json.dumps(clar, ensure_ascii=False))
    if clar is None:  # параллельный запрос успел первым
        await refund(uid, mode, cost)
        raise HTTPException(status_code=400, detail="К этому раскладу уже вытянуты все уточняющие карты.")
    track(uid, "clarify", {"kind": row["kind"], "n": len(clar), "paid": cost > 0})
    fresh = await get_user(uid)
    return {"success": True, "clarification": item, "clarify_left": MAX_CLARIFY - len(clar),
            "new_balance": 99999 if admin else fresh["balance"]}


# ── Настройки и события из мини-аппа ──────────────────────────────────────
@app.post("/api/user/settings")
async def user_settings(payload: dict, authorization: str = Header(None)):
    user, _ = await auth_user(authorization)
    p = await db()
    if "daily_push" in payload:
        on = bool(payload.get("daily_push"))
        await p.execute("UPDATE users SET daily_push = $2, notify = notify OR $2 WHERE telegram_id = $1",
                        user["telegram_id"], on)
        track(user["telegram_id"], "daily_push_on" if on else "daily_push_off")
    return {"ok": True}


CLIENT_EVENTS = {"store_open", "upsell_click", "clarify_click", "share_click", "story_share", "spread_open",
                 "sub_click", "pay_start"}


@app.post("/api/event")
async def client_event(payload: dict, authorization: str = Header(None)):
    user, _ = await auth_user(authorization)
    name = str(payload.get("name") or "")
    if name in CLIENT_EVENTS:
        props = payload.get("props") if isinstance(payload.get("props"), dict) else None
        track(user["telegram_id"], name, props)
    return {"ok": True}


@app.post("/api/payment/stars-invoice")
async def create_stars_invoice(payload: dict, authorization: str = Header(None)):
    user, _ = await auth_user(authorization)
    pack = payload.get("pack")
    extra = {}
    if pack in SUBS:
        if sub_active(user):
            raise HTTPException(status_code=400, detail="Подписка уже активна.")
        energy, price, title, description = SUBS[pack]
        extra["subscription_period"] = SUB_PERIOD
    elif pack in PACKS:
        energy, price, first_price, title, description = PACKS[pack]
        if (user.get("purchases") or 0) == 0 and first_price < price:
            price = first_price
            title += " · цена первой покупки"
    else:
        raise HTTPException(status_code=400, detail="Неверный тип пакета")
    # 4-я часть payload — расклад, к которому вернуть человека после оплаты
    rid = payload.get("reading_id")
    tail = f":r{int(rid)}" if str(rid or "").isdigit() else ""
    invoice_payload = f"{pack}:{user['telegram_id']}:{random.randint(100000, 999999)}{tail}"
    try:
        link = await bot.create_invoice_link(
            title=title, description=description, payload=invoice_payload,
            provider_token="", currency="XTR",
            prices=[LabeledPrice(label="Telegram Stars", amount=int(price))],
            **extra,
        )
        track(user["telegram_id"], "invoice", {"pack": pack, "method": "stars", "reading": bool(tail)})
        return {"invoice_link": link}
    except Exception as e:
        log(f"Ошибка создания инвойса: {e}")
        raise HTTPException(status_code=500, detail="Не удалось создать счёт, попробуйте ещё раз.")


@app.post("/api/payment/tribute-order")
async def create_tribute_order(payload: dict, authorization: str = Header(None)):
    """Отдаёт веб-ссылку цифрового товара Tribute для оплаты картой.
    Заказ в нашей БД — «намерение купить», чтобы мини-апп мог дождаться зачисления."""
    if not card_enabled():
        raise HTTPException(status_code=400, detail="Оплата картой пока недоступна.")
    user, _ = await auth_user(authorization)
    pack = payload.get("pack")
    if pack not in PACKS or pack not in TRIBUTE_PRODUCT_IDS:
        raise HTTPException(status_code=400, detail="Неверный тип пакета")
    uid = user["telegram_id"]
    energy = PACKS[pack][0]
    price = card_price(pack, user)
    order_id = f"intent:{uid}:{secrets.token_hex(6)}"
    rid = payload.get("reading_id")
    rid = int(rid) if str(rid or "").isdigit() else None
    p = await db()
    await p.execute("""
        INSERT INTO tribute_orders (uuid, telegram_id, pack, energy, amount, currency, reading_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (uuid) DO NOTHING
    """, order_id, uid, pack, energy, price * 100, TRIBUTE_CURRENCY, rid)
    track(uid, "invoice", {"pack": pack, "method": "card", "reading": bool(rid)})
    return {
        "order_id": order_id,
        "payment_url": CARD_LINKS.get(pack) or f"https://web.tribute.tg/p/{TRIBUTE_PRODUCT_IDS[pack]}",
        "webapp_url": "",
    }


def _tribute_signature_ok(raw: bytes, signature: str) -> bool:
    if not (TRIBUTE_API_KEY and signature):
        return False
    digest = hmac.new(TRIBUTE_API_KEY.encode(), raw, hashlib.sha256).digest()
    candidates = (digest.hex(), base64.b64encode(digest).decode())
    sig = signature.strip()
    if sig.lower().startswith("sha256="):
        sig = sig[7:]
    return hmac.compare_digest(sig.lower(), candidates[0]) or hmac.compare_digest(sig, candidates[1])


def _event_key(name: str) -> str:
    # shopOrderRefunded / shop_order_refunded → shoporderrefunded
    return (name or "").replace("_", "").replace(".", "").replace("-", "").lower()


def _find_key(obj, names: tuple, depth: int = 0):
    """Ищет первое непустое значение по любому из имён ключей, в том числе во вложенных объектах."""
    if depth > 4:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if _event_key(k) in names and isinstance(v, (str, int)) and str(v):
                return str(v)
        for v in obj.values():
            r = _find_key(v, names, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, names, depth + 1)
            if r:
                return r
    return None


@app.get("/tribute-webhook")
async def tribute_webhook_check():
    """Проверка адреса из браузера или из кабинета Tribute (раньше отдавала 405)."""
    return {"ok": True}


@app.post("/tribute-webhook")
async def tribute_webhook(request: Request):
    """Вебхуки цифровых товаров: new_digital_product (оплата) и digital_product_refund (возврат)."""
    raw = await request.body()
    if not _tribute_signature_ok(raw, request.headers.get("trbt-signature", "")):
        log("Tribute: неверная подпись вебхука")
        raise HTTPException(status_code=401, detail="bad signature")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="bad json")
    # Сырое тело — чтобы видеть точный формат Tribute (секретов в нём нет)
    log(f"Tribute тело: {raw[:1500].decode('utf-8', 'replace')}")

    event = _event_key(data.get("name") or _find_key(data, ("name", "event", "type")) or "")
    pl = data.get("payload") if isinstance(data.get("payload"), dict) else data
    product_id = _find_key(pl, ("productid",))
    tg_id = _find_key(pl, ("telegramuserid",))
    purchase_id = _find_key(pl, ("purchaseid", "id"))
    if event not in ("newdigitalproduct", "digitalproductrefund"):
        return {"ok": True}  # подписки, донаты и прочее не обрабатываем
    if not (product_id and tg_id and product_id.isdigit() and tg_id.isdigit()):
        log(f"Tribute: нет product_id/telegram_user_id в событии {event}")
        return {"ok": True}
    pack = PACK_BY_PRODUCT.get(int(product_id))
    if not pack:
        log(f"Tribute: товар {product_id} не привязан к пакету (проверь TRIBUTE_PRODUCTS)")
        return {"ok": True}
    uid, energy = int(tg_id), PACKS[pack][0]
    # Ключ от повторной доставки: id покупки, а если его нет — товар + покупатель + время события
    base = purchase_id or f"{product_id}:{uid}:{data.get('created_at') or _find_key(data, ('createdat',))}"

    p = await db()
    if event == "newdigitalproduct":
        async with p.acquire() as c:
            async with c.transaction():
                fresh = await c.fetchval("""
                    INSERT INTO payments (charge_id, telegram_id, pack, stars, energy)
                    VALUES ($1, $2, $3, 0, $4) ON CONFLICT (charge_id) DO NOTHING RETURNING 1
                """, f"tribute:{base}", uid, pack, energy)
                if not fresh:
                    return {"ok": True}  # уже зачислено
                user_ok = await c.fetchval(
                    "UPDATE users SET balance = balance + $2, purchases = purchases + 1 WHERE telegram_id = $1 RETURNING 1",
                    uid, energy)
                # Отмечаем последнее «намерение» этого пакета — мини-апп увидит статус paid
                rid = await c.fetchval("""
                    UPDATE tribute_orders SET status = 'paid', paid_at = NOW()
                    WHERE uuid = (SELECT uuid FROM tribute_orders WHERE telegram_id = $1 AND pack = $2
                                  AND status = 'pending' ORDER BY created_at DESC LIMIT 1)
                    RETURNING reading_id
                """, uid, pack)
        if not user_ok:
            log(f"Tribute: оплатил {uid}, но его нет в users — энергия не зачислена, проверь вручную")
            return {"ok": True}
        track(uid, "payment", {"pack": pack, "method": "card"})
        asyncio.create_task(credit_referrer_if_due(uid))
        try:
            await bot.send_message(uid, f"💳 Оплата картой прошла! Зачислено +{energy} Энергии ✨",
                                   reply_markup=paid_kb(rid))
        except Exception:
            pass
    else:
        done = await p.fetchval("""
            INSERT INTO payments (charge_id, telegram_id, pack, stars, energy)
            VALUES ($1, $2, $3, 0, $4) ON CONFLICT (charge_id) DO NOTHING RETURNING 1
        """, f"tribute-refund:{base}", uid, pack, -energy)
        if done:
            await add_balance(uid, -energy)
    return {"ok": True}


@app.get("/api/user/payment-status")
async def payment_status(order_id: str, authorization: str = Header(None)):
    user, _ = await auth_user(authorization)
    p = await db()
    status = await p.fetchval("SELECT status FROM tribute_orders WHERE uuid = $1 AND telegram_id = $2",
                              order_id, user["telegram_id"])
    fresh = await p.fetchrow("SELECT balance FROM users WHERE telegram_id = $1", user["telegram_id"])
    return {"status": status or "unknown", "balance": fresh["balance"] if fresh else 0}


@app.get("/api/system/setup-webhook")
async def setup_webhook_manually(key: str = ""):
    # Без ключа кто угодно мог дёргать переустановку вебхука. Ключ — WEBHOOK_SECRET.
    if not hmac.compare_digest(key, WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="forbidden")
    await setup_bot()
    info = await bot.get_webhook_info()
    return {"status": "ok", "webhook_url": info.url, "pending": info.pending_update_count, "last_error": info.last_error_message}


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    raw = await request.json()
    try:
        update = Update.model_validate(raw, context={"bot": bot})
        await dp.feed_update(bot=bot, update=update)
    except Exception as e:
        log(f"Ошибка обработки апдейта: {e}")
    return {"ok": True}


@app.post(OLD_WEBHOOK_PATH)
async def old_bot_webhook(request: Request):
    """Старый бот: на любое сообщение отвечает ссылкой на нового бота."""
    if not old_bot or request.headers.get("X-Telegram-Bot-Api-Secret-Token") != OLD_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    raw = await request.json()
    try:
        msg = raw.get("message") or (raw.get("callback_query") or {}).get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        # Старая реферальная ссылка t.me/<старый_бот>?start=ref_123 приходит как «/start ref_123» —
        # передаём параметр в кнопку, чтобы пригласивший получил бонус уже в новом боте.
        text = (raw.get("message") or {}).get("text") or ""
        start_param = text.split(maxsplit=1)[1].strip() if text.startswith("/start ") else None
        if chat_id:
            await old_bot.send_message(chat_id, MOVED_TEXT.format(u=BOT_USERNAME), reply_markup=moved_kb(start_param))
    except Exception as e:
        log(f"Старый бот, ответ: {e}")
    return {"ok": True}


MOVE_GIFT = int(os.getenv("MOVE_GIFT", "150"))  # подарок в честь переезда каждому, кто уже есть в базе


def moved_broadcast_text(old_balance: int, new_balance: int) -> str:
    head = f"🔮 Послание Таро переехало в нового бота: @{BOT_USERNAME}\n\n"
    if old_balance > 0:
        body = (f"Ваш баланс {old_balance} Энергии перенесён в новый бот, "
                f"а в честь переезда дарим ещё +{MOVE_GIFT} ✨\n"
                f"Теперь у вас {new_balance} Энергии.")
    else:
        body = (f"В честь переезда дарим вам +{MOVE_GIFT} Энергии ✨ "
                f"Этого хватит на Стандартный разбор на 3 карты.")
    return head + body + "\n\nОткройте нового бота и нажмите «Запустить», чтобы забрать подарок."


async def send_moved_broadcast() -> tuple:
    """Рассылка о переезде через старого бота. Подарок MOVE_GIFT начисляется ТОЛЬКО тем, кому сообщение дошло:
    - новые пользователи, которые никогда не были в старом боте, подарок не получат (старый бот не может им написать);
    - кто заблокировал старого бота — тоже без подарка.
    Каждому — один раз (флаг moved_notified). Возвращает (отправлено, не доставлено, подарков)."""
    p = await db()
    sent = failed = gifted = 0
    while True:
        rows = await p.fetch("SELECT telegram_id, balance FROM users WHERE NOT moved_notified LIMIT 200")
        if not rows:
            break
        for r in rows:
            uid, balance = r["telegram_id"], r["balance"] or 0
            # Сначала ставим флаг, чтобы повторный запуск никого не задел дважды
            await p.execute("UPDATE users SET moved_notified = TRUE WHERE telegram_id = $1", uid)
            text = moved_broadcast_text(balance, balance + MOVE_GIFT)
            delivered = False
            for attempt in range(2):
                try:
                    await old_bot.send_message(uid, text, reply_markup=moved_kb())
                    delivered = True
                    break
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except Exception:
                    break  # не был в старом боте или заблокировал его
            if delivered:
                await p.execute("UPDATE users SET balance = balance + $2 WHERE telegram_id = $1", uid, MOVE_GIFT)
                sent += 1
                gifted += 1
            else:
                failed += 1
            await asyncio.sleep(0.05)
    return sent, failed, gifted


def moved_reminder_text(balance: int) -> str:
    text = (f"🔮 Напоминаем: Послание Таро теперь живёт в новом боте — @{BOT_USERNAME}\n\n"
            "Здесь, в старом боте, разборы больше не работают.")
    if balance > 0:
        text += f"\n\nВас уже ждут {balance} Энергии — подарок за переезд на месте."
    return text + "\n\nНажмите кнопку ниже, а в новом боте — «Запустить»."


async def moved_reminder_targets(cutoff: datetime) -> list:
    """Кому напоминать: получили первое сообщение о переезде, но с даты cutoff так и не открыли нового бота
    (last_seen обновляется только при /start в новом боте и при входе в мини-апп) и не получали напоминание последние сутки."""
    p = await db()
    return await p.fetch("""
        SELECT telegram_id, balance FROM users
        WHERE moved_notified
          AND (last_seen IS NULL OR last_seen < $1)
          AND (moved_reminded_at IS NULL OR moved_reminded_at < NOW() - INTERVAL '1 day')
    """, cutoff)


async def send_moved_reminder(cutoff: datetime) -> tuple:
    """Повторное напоминание о переезде через старого бота. Без подарка. Возвращает (отправлено, не доставлено)."""
    p = await db()
    sent = failed = 0
    for r in await moved_reminder_targets(cutoff):
        uid = r["telegram_id"]
        await p.execute("UPDATE users SET moved_reminded_at = NOW() WHERE telegram_id = $1", uid)
        delivered = False
        for attempt in range(2):
            try:
                await old_bot.send_message(uid, moved_reminder_text(r["balance"] or 0), reply_markup=moved_kb())
                delivered = True
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Exception:
                break  # заблокировал старого бота или никогда в нём не был
        if delivered:
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)
    return sent, failed


async def missed_gift_candidates(cutoff: datetime) -> list:
    """Пользователи старого бота (зарегистрированы до cutoff), по которым ещё не решено, дошёл ли подарок за переезд."""
    p = await db()
    return await p.fetch("""
        SELECT telegram_id FROM users
        WHERE moved_notified AND created_at < $1 AND move_gift_state IS NULL
    """, cutoff)


async def old_bot_reachable(uid: int) -> Optional[bool]:
    """Может ли старый бот написать человеку. Если может — первое сообщение с подарком до него дошло.
    None — непонятно (сбой сети), такого пропускаем и проверим при следующем запуске."""
    for attempt in range(2):
        try:
            await old_bot.send_chat_action(uid, "typing")
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except (TelegramForbiddenError, TelegramBadRequest):
            return False
        except Exception:
            return None
    return None


async def send_missed_gifts(cutoff: datetime) -> tuple:
    """Доначисляет подарок MOVE_GIFT тем, кому не дошла первая рассылка.
    Пишем через НОВОГО бота; кто его ещё не запускал — получит подарок автоматически при первом /start.
    Возвращает (уже получали, подарено сейчас, подарок ждёт /start, пропущено)."""
    p = await db()
    had = given = pending = skipped = 0
    for r in await missed_gift_candidates(cutoff):
        uid = r["telegram_id"]
        reachable = await old_bot_reachable(uid)
        if reachable is None:
            skipped += 1
            continue
        if reachable:
            await p.execute("UPDATE users SET move_gift_state = 'got' WHERE telegram_id = $1", uid)
            had += 1
            await asyncio.sleep(0.05)
            continue
        # Помечаем «ждёт», чтобы подарок не начислился дважды, а потом пробуем вручить через нового бота
        balance = await p.fetchval("""
            UPDATE users SET move_gift_state = 'pending' WHERE telegram_id = $1 AND move_gift_state IS NULL
            RETURNING balance
        """, uid)
        if balance is None:
            continue
        text = (f"🔮 Послание Таро переехало в этот бот, и в честь переезда мы дарим вам +{MOVE_GIFT} Энергии ✨\n"
                f"Теперь на балансе {balance + MOVE_GIFT} — хватит на Стандартный разбор на 3 карты.")
        try:
            await bot.send_message(uid, text, reply_markup=webapp_kb())
            await p.execute("""
                UPDATE users SET balance = balance + $2, move_gift_state = 'given'
                WHERE telegram_id = $1 AND move_gift_state = 'pending'
            """, uid, MOVE_GIFT)
            given += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            pending += 1  # подарок не потеряется: начислится при /start
        except Exception:
            pending += 1  # нового бота ещё не запускал — подарок начислится при /start
        await asyncio.sleep(0.05)
    return had, given, pending, skipped


# =====================================================================
# БОТ: команды и платежи
# =====================================================================
async def notify_referrer(referrer_id: int, friend_name: Optional[str]):
    try:
        await bot.send_message(
            referrer_id,
            f"🎁 {friend_name or 'Твой друг'} пришёл(а) по твоей ссылке! Твои +{REF_BONUS} Энергии придут "
            "после его первой покупки ✨",
            reply_markup=webapp_kb(),
        )
    except Exception:
        pass


@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject):
    feedback_waiting.pop(message.from_user.id, None)
    u = message.from_user
    tg_user = {"id": u.id, "first_name": u.first_name, "username": u.username}
    try:
        user, created, referrer = await ensure_user(tg_user, command.args)
    except Exception as e:
        log(f"/start db: {e}")
        user, created, referrer = {}, False, None

    lines = [f"Приветствую тебя, {u.first_name}! 🔮", "",
             "Это Послание Таро. Задай вопрос — и карты ответят.", ""]
    if user.get("_move_gift"):
        lines.append(f"🎁 В честь переезда в новый бот мы начислили вам +{MOVE_GIFT} Энергии ✨ "
                     f"Теперь на балансе {user.get('balance', 0)}.")
    if created:
        track(u.id, "signup", {"source": user.get("source")})
        lines.append("🎁 В подарок: первый Быстрый ответ" +
                     (" и первый Стандартный разбор на 3 карты." if FREE_PRESET_ON_START else "."))
        if referrer:
            lines.append(f"✨ Ты пришёл(а) по приглашению — тебе начислено +{REF_BONUS} Энергии.")
    elif (user.get("free_preset") or 0) > 0:
        lines.append("🎁 У тебя есть бесплатный Стандартный разбор на 3 карты.")
    elif (user.get("free_readings") or 0) > 0:
        lines.append("🎁 У тебя есть бесплатный Быстрый ответ.")
    lines += ["🃏 Карта Дня — бесплатно каждый день.",
              f"⚡ Быстрый ответ одной картой на любой вопрос — {COST_QUICK} Энергии.",
              "🎴 Стандартный и 🔮 Индивидуальный разборы — за Энергию.", "",
              "Жми кнопку ниже 👇"]
    await message.answer("\n".join(lines), reply_markup=webapp_kb(channel=True))
    if referrer:
        asyncio.create_task(notify_referrer(referrer, u.first_name))


@dp.message(Command("ref"))
async def cmd_ref(message: Message):
    uid = message.from_user.id
    user = await get_user(uid)
    count = (user or {}).get("ref_count") or 0
    invited = await (await db()).fetchval("SELECT COUNT(*) FROM users WHERE referred_by = $1", uid)
    link = ref_link(uid)
    share = "https://t.me/share/url?" + urllib.parse.urlencode({"url": link, "text": "🔮 Послание Таро в Telegram: первый ответ карт бесплатно, Карта Дня каждый день"})
    await message.answer(
        f"🎁 Приглашай друзей: за каждого вы оба получите +{REF_BONUS} Энергии.\n"
        "Друг получает бонус сразу, а ты после его первой покупки.\n\n"
        f"Твоя ссылка:\n{link}\n\nПришло по ссылке: {invited}\nБонусов получено: {count}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📤 Отправить другу", url=share)]]),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    p = await db()
    await p.execute("UPDATE users SET notify = FALSE WHERE telegram_id = $1", message.from_user.id)
    await message.answer("🔕 Напоминания отключены. Включить снова: /notify")


@dp.message(Command("notify"))
async def cmd_notify(message: Message):
    p = await db()
    await p.execute("UPDATE users SET notify = TRUE WHERE telegram_id = $1", message.from_user.id)
    await message.answer("🔔 Напоминания снова включены. Пришлю Карту Дня в понедельник утром.\n"
                         "Хочешь получать её каждое утро? /daily")


@dp.message(Command("daily"))
async def cmd_daily(message: Message):
    p = await db()
    on = await p.fetchval("""
        UPDATE users SET daily_push = NOT daily_push, notify = TRUE WHERE telegram_id = $1 RETURNING daily_push
    """, message.from_user.id)
    track(message.from_user.id, "daily_push_on" if on else "daily_push_off")
    if on:
        await message.answer("🌅 Готово! Карта Дня будет приходить каждое утро. Выключить: /daily")
    else:
        await message.answer("Ежедневная Карта Дня выключена. Напомню о ней раз в неделю. Включить снова: /daily")


@dp.message(Command("subscription"))
async def cmd_subscription(message: Message):
    user = await get_user(message.from_user.id) or {}
    if sub_active(user):
        until = user["sub_until"] + timedelta(hours=TZ_OFFSET_HOURS)
        await message.answer(
            f"🌟 Подписка «Послание+» активна до {until:%d.%m.%Y}.\n"
            f"Каждый месяц +{SUB_ENERGY} Энергии, уточняющие карты бесплатно.\n\n"
            "Отменить продление можно в Telegram: Настройки → Мои звёзды → Подписки.",
            reply_markup=webapp_kb())
    else:
        await message.answer(
            f"🌟 Подписка «Послание+»: {SUB_PRICE} ⭐ в месяц.\n"
            f"• +{SUB_ENERGY} Энергии каждый месяц (дешевле, чем пакетами);\n"
            "• уточняющие карты к любому раскладу бесплатно.\n\n"
            "Оформить можно в магазине приложения.", reply_markup=webapp_kb("🌟 Открыть магазин"))


@dp.callback_query(F.data.startswith("fu:"))
async def followup_answer(q: CallbackQuery):
    """Ответ на «Сбылось?»."""
    try:
        _, rid, ans = q.data.split(":")
        rid = int(rid)
    except ValueError:
        await q.answer()
        return
    if ans not in ("yes", "no", "wait"):
        await q.answer()
        return
    p = await db()
    ok = await p.fetchval("UPDATE readings SET followup = $3 WHERE id = $1 AND telegram_id = $2 RETURNING 1",
                          rid, q.from_user.id, ans)
    await q.answer()
    if not ok:
        return
    track(q.from_user.id, f"followup_{ans}", {"reading": rid})
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if ans == "yes":
        text = "✨ Как здорово! Карты рады, что попали в точку. Если появился новый вопрос, они готовы ответить."
        kb = webapp_kb("🔮 Задать новый вопрос")
    elif ans == "no":
        text = ("Спасибо за честность 🙏 Карты показывают тенденцию, и она могла измениться. "
                "Вытяни уточняющую карту к тому раскладу: она покажет, что сейчас влияет на ситуацию.")
        kb = paid_kb(rid)
    else:
        text = "Хорошо 🌙 Иногда событиям нужно чуть больше времени. Расклад сохранён в «Моих раскладах»."
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="📜 Открыть расклад", web_app=WebAppInfo(url=webapp_url(rid)))]])
    await q.message.answer(text, reply_markup=kb)


@dp.message(Command("funnel"))
async def cmd_funnel(message: Message, command: CommandObject):
    """Только для админов: воронка за N дней (по умолчанию 7)."""
    if not _is_admin_msg(message):
        return
    days = int(command.args) if command.args and command.args.strip().isdigit() else 7
    p = await db()
    steps = [("Новые пользователи", "SELECT COUNT(*) FROM users WHERE created_at > NOW() - make_interval(days => $1)"),
             ("Открыли мини-апп", "app_open"), ("Сделали расклад", "reading"), ("Открыли магазин", "store_open"),
             ("Создали счёт", "invoice"), ("Оплатили", "payment")]
    lines = [f"📊 Воронка за {days} дн. (уникальные пользователи)\n"]
    for title, q in steps:
        if q.startswith("SELECT"):
            n = await p.fetchval(q, days)
        else:
            n = await p.fetchval("SELECT COUNT(DISTINCT telegram_id) FROM events "
                                 "WHERE name = $1 AND created_at > NOW() - make_interval(days => $2)", q, days)
        lines.append(f"{title}: {n}")
    rows = await p.fetch("""
        SELECT name, COUNT(*) AS n, COUNT(DISTINCT telegram_id) AS u FROM events
        WHERE created_at > NOW() - make_interval(days => $1)
          AND name IN ('clarify', 'upsell_click', 'followup_yes', 'followup_no', 'followup_wait',
                       'daily_push_on', 'streak_reward', 'ref_credited', 'history_open', 'story_share', 'sub_click')
        GROUP BY name ORDER BY n DESC
    """, days)
    if rows:
        lines.append("\nДругие события (раз / людей):")
        lines += [f"{r['name']}: {r['n']} / {r['u']}" for r in rows]
    subs = await p.fetchval("SELECT COUNT(*) FROM users WHERE sub_until > NOW()")
    lines.append(f"\n🌟 Активных подписок: {subs}")
    lines.append("Другой период: /funnel 30")
    await message.answer("\n".join(lines))


@dp.message(Command("moved_broadcast"))
async def cmd_moved_broadcast(message: Message):
    """Только для админов: разослать всем пользователям старого бота сообщение о переезде."""
    user = await get_user(message.from_user.id) or {"telegram_id": message.from_user.id, "username": message.from_user.username}
    if not is_admin(user):
        return
    if not old_bot:
        await message.answer("OLD_BOT_TOKEN не задан в Render — рассылать не через что.")
        return
    await message.answer("📨 Начинаю рассылку о переезде через старого бота…")
    sent, failed, gifted = await send_moved_broadcast()
    await message.answer(
        f"✅ Готово. Сообщение и подарок +{MOVE_GIFT} получили: {gifted}.\n"
        f"Не доставлено (новые пользователи или заблокировали старого бота): {failed}."
    )


@dp.message(Command("moved_remind"))
async def cmd_moved_remind(message: Message, command: CommandObject):
    """Только для админов. /moved_remind 2026-10-26 — показать, скольким уйдёт напоминание;
    /moved_remind 2026-10-26 go — разослать. Дата — день первой рассылки о переезде:
    кто открывал нового бота с этой даты, напоминание не получит."""
    user = await get_user(message.from_user.id) or {"telegram_id": message.from_user.id, "username": message.from_user.username}
    if not is_admin(user):
        return
    if not old_bot:
        await message.answer("OLD_BOT_TOKEN не задан в Render — рассылать не через что.")
        return
    args = (command.args or "").split()
    try:
        cutoff = datetime.strptime(args[0], "%Y-%m-%d")
    except (IndexError, ValueError):
        await message.answer("Укажите дату первой рассылки о переезде:\n"
                             "/moved_remind 2026-10-26 — посчитать, кому уйдёт\n"
                             "/moved_remind 2026-10-26 go — отправить")
        return
    targets = await moved_reminder_targets(cutoff)
    if len(args) < 2 or args[1].lower() != "go":
        await message.answer(f"Напоминание получат {len(targets)} чел. — не открывали нового бота с {args[0]}.\n"
                             f"Отправить: /moved_remind {args[0]} go\n\nТекст:\n\n{moved_reminder_text(150)}")
        return
    await message.answer(f"📨 Отправляю напоминание {len(targets)} пользователям…")
    sent, failed = await send_moved_reminder(cutoff)
    await message.answer(f"✅ Готово. Доставлено: {sent}. Не доставлено (заблокировали старого бота): {failed}.")


@dp.message(Command("moved_gift"))
async def cmd_moved_gift(message: Message, command: CommandObject):
    """Только для админов. /moved_gift 2026-10-26 — посчитать; /moved_gift 2026-10-26 go — вручить подарок тем,
    до кого не дошла первая рассылка. Дата — день переезда: кто зарегистрировался позже, подарок не получит."""
    user = await get_user(message.from_user.id) or {"telegram_id": message.from_user.id, "username": message.from_user.username}
    if not is_admin(user):
        return
    if not old_bot:
        await message.answer("OLD_BOT_TOKEN не задан в Render — не через что проверить, кому дошла рассылка.")
        return
    args = (command.args or "").split()
    try:
        cutoff = datetime.strptime(args[0], "%Y-%m-%d")
    except (IndexError, ValueError):
        await message.answer("Укажите дату переезда:\n"
                             "/moved_gift 2026-10-26 — посчитать\n"
                             "/moved_gift 2026-10-26 go — вручить подарки")
        return
    candidates = await missed_gift_candidates(cutoff)
    if len(args) < 2 or args[1].lower() != "go":
        await message.answer(
            f"Пользователей старого бота на проверку: {len(candidates)}.\n"
            f"Кому первая рассылка дошла — пропущу, остальным начислю +{MOVE_GIFT} Энергии.\n"
            f"Запустить: /moved_gift {args[0]} go")
        return
    await message.answer(f"🔎 Проверяю {len(candidates)} пользователей…")
    had, given, pending, skipped = await send_missed_gifts(cutoff)
    await message.answer(
        f"✅ Готово.\n"
        f"Уже получали подарок: {had}\n"
        f"Подарено сейчас (написал через нового бота): {given}\n"
        f"Ждут подарка до первого запуска нового бота: {pending}\n"
        f"Не удалось проверить (можно запустить ещё раз): {skipped}")


@dp.message(Command("sources"))
async def cmd_sources(message: Message):
    """Только для админов: сколько пользователей пришло из каждого источника и сколько из них купили."""
    user = await get_user(message.from_user.id) or {"telegram_id": message.from_user.id, "username": message.from_user.username}
    if not is_admin(user):
        return
    p = await db()
    rows = await p.fetch("""
        SELECT COALESCE(source, 'без метки') AS src, COUNT(*) AS users,
               COUNT(*) FILTER (WHERE purchases > 0) AS buyers
        FROM users GROUP BY 1 ORDER BY 2 DESC
    """)
    lines = ["📊 Откуда пришли пользователи:", ""]
    for r in rows:
        lines.append(f"{r['src']}: {r['users']} (купили: {r['buyers']})")
    await message.answer("\n".join(lines))


@dp.message(Command("support"))
async def cmd_support(message: Message):
    await message.answer(
        "💬 Поддержка Послания Таро\n\n"
        f"Если что-то работает не так или есть вопрос по раскладам, напишите {SUPPORT_CONTACT}. "
        "Отвечаем обычно в течение дня.\n\n"
        "Вопросы по оплате: /paysupport\n"
        "Условия использования: /terms"
    )


@dp.message(Command("paysupport"))
async def cmd_paysupport(message: Message):
    await message.answer(
        "💳 Вопросы по оплате\n\n"
        "Если звёзды списались, а Энергия не пришла, или оплата не открывается, "
        f"напишите {SUPPORT_CONTACT} и укажите:\n"
        "• дату и время оплаты;\n"
        "• какой пакет покупали;\n"
        f"• ваш ID: {message.from_user.id}\n\n"
        "Мы проверим платёж и зачислим Энергию или вернём звёзды.\n\n"
        "Если оплата не открывается на компьютере, попробуйте с телефона."
    )


@dp.message(Command("terms"))
async def cmd_terms(message: Message):
    await message.answer(
        "📜 Условия использования\n\n"
        "1. Послание Таро — развлекательный сервис. Расклады не являются советом в медицинских, "
        "юридических или финансовых вопросах и не заменяют обращения к специалисту.\n\n"
        "2. Энергия покупается за Telegram Stars и тратится на расклады внутри бота. "
        "Энергия не обменивается обратно на звёзды или деньги.\n\n"
        "3. Если из-за технической ошибки Энергия списалась, а расклад не пришёл, "
        "или Энергия не зачислилась после оплаты, напишите в /paysupport — исправим или вернём звёзды.\n\n"
        "4. Пользуясь ботом, вы соглашаетесь с этими условиями и с правилами Telegram.\n\n"
        f"Связь: {SUPPORT_CONTACT}"
    )


def _parse_invoice_payload(raw: str) -> Optional[tuple]:
    """«pack:uid:rand» или «pack:uid:rand:r<reading_id>» → (pack, uid, reading_id|None)."""
    parts = (raw or "").split(":")
    if len(parts) not in (3, 4) or (parts[0] not in PACKS and parts[0] not in SUBS) or not parts[1].isdigit():
        return None
    rid = int(parts[3][1:]) if len(parts) == 4 and parts[3][:1] == "r" and parts[3][1:].isdigit() else None
    return parts[0], int(parts[1]), rid


@dp.pre_checkout_query()
async def process_pre_checkout(q: PreCheckoutQuery):
    ok = _parse_invoice_payload(q.invoice_payload) is not None
    await q.answer(ok=ok, error_message=None if ok else "Счёт устарел, создайте новый в приложении.")


@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    pay = message.successful_payment
    parsed = _parse_invoice_payload(pay.invoice_payload)
    if not parsed:
        log(f"Неизвестный payload платежа: {pay.invoice_payload}")
        return
    pack, uid, rid = parsed
    is_sub = pack in SUBS
    energy = SUBS[pack][0] if is_sub else PACKS[pack][0]
    # Подписка: Telegram сообщает дату окончания оплаченного периода; продления приходят таким же сообщением
    exp = getattr(pay, "subscription_expiration_date", None)
    sub_until = (datetime.utcfromtimestamp(exp) if isinstance(exp, int) else
                 exp.astimezone(timezone.utc).replace(tzinfo=None) if isinstance(exp, datetime) else
                 datetime.utcnow() + timedelta(seconds=SUB_PERIOD))
    p = await db()
    async with p.acquire() as c:
        async with c.transaction():
            inserted = await c.fetchval("""
                INSERT INTO payments (charge_id, telegram_id, pack, stars, energy)
                VALUES ($1, $2, $3, $4, $5) ON CONFLICT (charge_id) DO NOTHING RETURNING 1
            """, pay.telegram_payment_charge_id, uid, pack, pay.total_amount, energy)
            if not inserted:
                return  # повторная доставка того же платежа — уже зачислено
            await c.execute("UPDATE users SET balance = balance + $2, purchases = purchases + 1 WHERE telegram_id = $1", uid, energy)
            if is_sub:
                await c.execute("UPDATE users SET sub_until = $2 WHERE telegram_id = $1", uid, sub_until)
    track(uid, "payment", {"pack": pack, "method": "stars", "stars": pay.total_amount,
                           "renewal": bool(getattr(pay, "is_recurring", False) and not getattr(pay, "is_first_recurring", False))})
    asyncio.create_task(credit_referrer_if_due(uid))
    if is_sub:
        await message.answer(f"🌟 Подписка «Послание+» активна до {sub_until + timedelta(hours=TZ_OFFSET_HOURS):%d.%m.%Y}.\n"
                             f"Зачислено +{energy} Энергии, уточняющие карты теперь бесплатно ✨\n\n"
                             "Управлять подпиской: /subscription", reply_markup=paid_kb(rid))
    else:
        await message.answer(f"🔮 Оплата успешна! Зачислено +{energy} Энергии.", reply_markup=paid_kb(rid))


# ── Отзывы через бота: /feedback ──────────────────────────────────────────
feedback_waiting: dict = {}  # telegram_id → до какого времени ждём текст отзыва


def _is_admin_msg(message: Message) -> bool:
    u = message.from_user
    return bool(u) and (u.id in ADMIN_IDS or (u.username or "").lower() in ADMIN_USERNAMES)


@dp.message(Command("feedback"))
async def cmd_feedback(message: Message, command: CommandObject):
    u = message.from_user
    if command.args and len(command.args.strip()) >= 3:
        try:
            await save_feedback(u.id, u.username, "idea", None, command.args, "bot")
            await message.answer("🙏 Спасибо! Ваше сообщение получено, мы обязательно его прочитаем.")
        except HTTPException as e:
            await message.answer(e.detail)
        return
    feedback_waiting[u.id] = time.time() + 600
    await message.answer(
        "✍️ Напишите одним сообщением отзыв, идею или что работает не так — мы прочитаем всё.\n\n"
        "Передумали? Просто отправьте /start.")


@dp.message(F.reply_to_message & F.text, _is_admin_msg)
async def admin_feedback_reply(message: Message):
    """Админ отвечает реплаем на пересланный отзыв — ответ уходит пользователю."""
    found = re.search(r"#fb(\d+)", message.reply_to_message.text or "")
    if not found:
        return
    fb_id = int(found.group(1))
    p = await db()
    uid = await p.fetchval("SELECT telegram_id FROM feedback WHERE id = $1", fb_id)
    if not uid:
        await message.reply("Отзыв не найден.")
        return
    try:
        await bot.send_message(uid, f"💬 Ответ на ваше сообщение:\n\n{message.text}", reply_markup=webapp_kb())
        await p.execute("UPDATE feedback SET replied = TRUE WHERE id = $1", fb_id)
        await message.reply("✅ Ответ отправлен.")
    except Exception as e:
        await message.reply(f"Не удалось отправить: {e}")


@dp.message(F.text & ~F.text.startswith("/"), lambda m: feedback_waiting.get(m.from_user.id, 0) > time.time())
async def feedback_text(message: Message):
    u = message.from_user
    feedback_waiting.pop(u.id, None)
    try:
        await save_feedback(u.id, u.username, "idea", None, message.text, "bot")
        await message.answer("🙏 Спасибо! Ваше сообщение получено, мы обязательно его прочитаем.",
                             reply_markup=webapp_kb())
    except HTTPException as e:
        await message.answer(e.detail)


# ── Статистика оценок раскладов: /ratings [дней] (только для админов) ─────
@dp.message(Command("ratings"))
async def cmd_ratings(message: Message, command: CommandObject):
    if not _is_admin_msg(message):
        return
    days = int(command.args) if command.args and command.args.strip().isdigit() else 7
    p = await db()
    rows = await p.fetch("""
        SELECT kind, COUNT(*) FILTER (WHERE value > 0) AS up, COUNT(*) FILTER (WHERE value < 0) AS down
        FROM reading_ratings WHERE created_at > NOW() - make_interval(days => $1)
        GROUP BY kind
    """, days)
    lines = [f"📊 Оценки раскладов за {days} дн.\n"]
    tu = td = 0
    for kind, title in READING_KINDS.items():
        r = next((x for x in rows if x["kind"] == kind), None)
        up, down = (r["up"], r["down"]) if r else (0, 0)
        tu += up; td += down
        share = f"{round(up * 100 / (up + down))}%" if up + down else "—"
        lines.append(f"{title}: 👍 {up} · 👎 {down} · нравится {share}")
    total = f"{round(tu * 100 / (tu + td))}%" if tu + td else "—"
    lines.append(f"\nВсего: 👍 {tu} · 👎 {td} · нравится {total}")
    bad = await p.fetch("""
        SELECT kind, question, cards, created_at FROM reading_ratings
        WHERE value < 0 AND created_at > NOW() - make_interval(days => $1)
        ORDER BY created_at DESC LIMIT 5
    """, days)
    if bad:
        lines.append("\nПоследние 👎:")
        for b in bad:
            q = (b["question"] or "без вопроса")[:120]
            lines.append(f"• {READING_KINDS.get(b['kind'], b['kind'])} {b['created_at']:%d.%m %H:%M} — «{q}» ({b['cards'] or '—'})")
    lines.append("\nДругой период: /ratings 30")
    await message.answer("\n".join(lines))


# ── Рассылка новостей: /news (превью себе) и /news go (всем) ──────────────
NEWS_ID = "2026-10-29-history-clarify"   # поменяй для следующей рассылки, чтобы она ушла заново
news_running = False


def news_text(name: str, balance: int, free_readings: int) -> str:
    text = (
        f"✨ {name}, в Послании Таро большое обновление!\n\n"
        "📜 «Мои расклады»: все твои расклады теперь сохраняются, их можно перечитать.\n"
        "🔎 Уточняющая карта: если остался вопрос, вытяни ещё одну карту к любому раскладу.\n"
        "💞 Новые расклады: «Отношения», «Да или нет», «Работа и деньги» и «Расклад на месяц».\n"
        "🔥 Серия Карт Дня: заходи каждый день и получай подарки.\n"
        "🌟 Подписка «Послание+»: Энергия каждый месяц и бесплатные уточнения."
    )
    if balance >= COST_QUICK:
        if balance >= COST_PRESET:
            n = balance // COST_PRESET
            what = f"{n} {_plural(n, 'Стандартный разбор', 'Стандартных разбора', 'Стандартных разборов')}"
            if balance >= COST_AI:
                what = f"🔮 Индивидуальный разбор или {what}"
        else:
            q = balance // COST_QUICK
            what = f"{q} {_plural(q, 'Быстрый ответ', 'Быстрых ответа', 'Быстрых ответов')}"
        text += (f"\n\n🔮 У тебя на балансе {balance} Энергии, этого хватит на {what}. "
                 "Заходи и задай картам свой вопрос!")
    elif free_readings > 0:
        text += "\n\n🎁 А ещё тебя ждёт бесплатный Быстрый ответ. Заходи и задай картам свой вопрос!"
    else:
        text += "\n\n🃏 Заходи за бесплатной Картой дня, она уже ждёт тебя."
    return text


async def run_news_broadcast(admin_chat: int):
    global news_running
    news_running = True
    p = await db()
    sent = failed = 0
    today = get_today_str()
    try:
        while True:
            rows = await p.fetch("""
                SELECT telegram_id, first_name, balance, free_readings FROM users
                WHERE notify AND NOT blocked AND last_news IS DISTINCT FROM $1
                LIMIT 200
            """, NEWS_ID)
            if not rows:
                break
            for r in rows:
                uid = r["telegram_id"]
                # отмечаем сразу: при перезапуске второй раз не отправим. Ежедневный пуш сегодня тоже не нужен.
                await p.execute("UPDATE users SET last_news = $2, last_push_date = $3 WHERE telegram_id = $1",
                                uid, NEWS_ID, today)
                text = news_text(r["first_name"] or "Искатель", r["balance"] or 0, r["free_readings"] or 0)
                for attempt in range(2):
                    try:
                        await bot.send_message(uid, text, reply_markup=webapp_kb("🔮 Открыть Послание Таро"))
                        sent += 1
                        break
                    except TelegramRetryAfter as e:
                        await asyncio.sleep(e.retry_after + 1)
                    except (TelegramForbiddenError, TelegramBadRequest):
                        await p.execute("UPDATE users SET blocked = TRUE WHERE telegram_id = $1", uid)
                        failed += 1
                        break
                    except Exception as e:
                        log(f"news {uid}: {e}")
                        failed += 1
                        break
                await asyncio.sleep(0.05)
    finally:
        news_running = False
    await bot.send_message(admin_chat, f"✅ Рассылка завершена. Доставлено: {sent}. Не доставлено: {failed}.")


# Время для /news at — по Алматы (UTC+5). Для Москвы поставь NEWS_UTC_OFFSET=3 в Render.
NEWS_TZ = timezone(timedelta(hours=int(os.getenv("NEWS_UTC_OFFSET", "5"))))


async def news_scheduler():
    """Раз в 30 сек проверяет, не пора ли запустить запланированную рассылку.
    План хранится в базе, поэтому переживает перезапуск Render."""
    while True:
        await asyncio.sleep(30)
        try:
            if not pool_ready.is_set() or news_running:
                continue
            p = await db()
            row = await p.fetchrow("SELECT value FROM bot_settings WHERE key = 'news_at'")
            if not row:
                continue
            at_iso, chat = row["value"].split("|")
            if datetime.now(timezone.utc) >= datetime.fromisoformat(at_iso):
                await p.execute("DELETE FROM bot_settings WHERE key = 'news_at'")
                log("📨 Запускаю запланированную рассылку")
                try:
                    await bot.send_message(int(chat), "📨 Время пришло, начинаю рассылку.")
                except Exception:
                    pass
                asyncio.create_task(run_news_broadcast(int(chat)))
        except Exception as e:
            log(f"news_scheduler: {e}")


@dp.message(Command("news"))
async def cmd_news(message: Message, command: CommandObject):
    """/news — превью и статус; /news go — сейчас; /news at 13:00 — по расписанию; /news cancel — отменить план."""
    if not _is_admin_msg(message):
        return
    p = await db()
    arg = (command.args or "").strip().lower()
    if arg == "go":
        if news_running:
            await message.answer("Рассылка уже идёт.")
            return
        await message.answer("📨 Начинаю рассылку. Пришлю итог, когда закончу.")
        asyncio.create_task(run_news_broadcast(message.chat.id))
        return
    if arg.startswith("at"):
        t = re.match(r"at\s+(\d{1,2})[:.](\d{2})$", arg)
        if not t or int(t.group(1)) > 23 or int(t.group(2)) > 59:
            await message.answer("Формат: /news at 13:00")
            return
        now = datetime.now(NEWS_TZ)
        at = now.replace(hour=int(t.group(1)), minute=int(t.group(2)), second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
        await p.execute("""
            INSERT INTO bot_settings (key, value) VALUES ('news_at', $1)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, f"{at.astimezone(timezone.utc).isoformat()}|{message.chat.id}")
        day = "сегодня" if at.date() == now.date() else "завтра"
        await message.answer(f"⏰ Рассылка запланирована на {day}, {at:%d.%m} в {at:%H:%M} "
                             f"(UTC+{int(NEWS_TZ.utcoffset(None).total_seconds() // 3600)}).\n"
                             "Отменить: /news cancel")
        return
    if arg == "cancel":
        await p.execute("DELETE FROM bot_settings WHERE key = 'news_at'")
        await message.answer("❌ Запланированная рассылка отменена.")
        return
    me = await p.fetchrow("SELECT first_name FROM users WHERE telegram_id = $1", message.from_user.id)
    left = await p.fetchval("SELECT COUNT(*) FROM users WHERE notify AND NOT blocked "
                            "AND last_news IS DISTINCT FROM $1", NEWS_ID)
    plan = await p.fetchval("SELECT value FROM bot_settings WHERE key = 'news_at'")
    name = (me and me["first_name"]) or "Искатель"
    await message.answer("👀 Превью (с балансом 300):\n\n" + news_text(name, 300, 0),
                         reply_markup=webapp_kb("🔮 Открыть Послание Таро"))
    await message.answer("👀 Превью (баланс 0, есть подарок):\n\n" + news_text(name, 0, 1))
    await message.answer("👀 Превью (баланс 0):\n\n" + news_text(name, 0, 0))
    status = "не запланирована"
    if plan:
        at = datetime.fromisoformat(plan.split("|")[0]).astimezone(NEWS_TZ)
        status = f"запланирована на {at:%d.%m %H:%M}"
    await message.answer(f"Получателей: {left}. Рассылка {status}.\n\n"
                         "/news at 13:00 — запланировать\n/news go — отправить сейчас\n/news cancel — отменить план")

