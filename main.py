# -*- coding: utf-8 -*-
"""
Таро Оракул — backend v2 (@Tarot_answer_question_bot)

Что изменилось по сравнению с v1:
- Пул соединений asyncpg вместо нового подключения на каждый запрос (было до 15 сек на запрос).
- Проверка подписи Telegram initData (раньше можно было подделать пользователя и получить бесконечный баланс).
- Убраны искусственные задержки asyncio.sleep.
- Вебхук ставится без drop_pending_updates — /start, который будит сервер, больше не теряется.
- Keep-alive, чтобы бесплатный Render не засыпал; /health для UptimeRobot.
- Атомарные списания баланса, идемпотентные платежи (таблица payments).
- Рост: бесплатный первый расклад, рефералка, ежедневный пуш «карта дня», скидка на первую покупку,
  кнопка-меню мини-аппа, команды /ref и /stop.
"""
import os
import json
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
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Update, LabeledPrice, PreCheckoutQuery, Message, BotCommand,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp, LinkPreviewOptions,
)

# =====================================================================
# КОНФИГУРАЦИЯ
# =====================================================================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GEMINI_MODELS = [m.strip() for m in os.getenv("GEMINI_MODELS", "gemini-2.5-flash,gemini-2.5-flash-lite").split(",") if m.strip()]
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://tarot-frontend-wine.vercel.app")
BOT_USERNAME = os.getenv("BOT_USERNAME", "Tarot_answer_question_bot")
PUBLIC_URL = (os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_URL") or "https://tarot-backend-136l.onrender.com").strip().rstrip("/")
ADMIN_USERNAMES = {u.strip().lower() for u in os.getenv("ADMIN_USERNAMES", "dzenra_prod").split(",") if u.strip()}
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DEV_MODE = os.getenv("DEV_MODE", "0") == "1"          # только для локальной отладки без Telegram
KEEPALIVE = os.getenv("KEEPALIVE", "1") == "1"         # пинговать себя, чтобы Render не засыпал
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "5"))  # Алматы UTC+5; граница «дня» для карты дня
PUSH_HOUR = int(os.getenv("PUSH_HOUR", "10"))          # 10:00 Алматы = 08:00 МСК
PUSH_ENABLED = os.getenv("PUSH_ENABLED", "1") == "1"

# Экономика
COST_PRESET = 150
COST_AI = 750
REF_BONUS = int(os.getenv("REF_BONUS", "150"))
FREE_READINGS_ON_START = 1
PACKS = {
    # id: (энергия, цена в Stars, цена для первой покупки, заголовок, описание)
    "pack_150": (150, 150, int(os.getenv("FIRST_PURCHASE_PRICE", "99")), "+150 Энергии", "1 Стандартный разбор — расклад на 3 карты по вашему вопросу."),
    "pack_450": (450, 383, 383, "+450 Энергии (скидка 15%)", "3 Стандартных разбора по выгодной цене."),
    "pack_750": (750, 750, 750, "+750 Энергии", "1 Индивидуальный разбор — глубокий анализ от 6 до 12 карт."),
}

if not BOT_TOKEN:
    raise RuntimeError("Переменная TELEGRAM_BOT_TOKEN не задана!")
if not DATABASE_URL:
    raise RuntimeError("Переменная DATABASE_URL не задана!")

# Секрет вебхука: Telegram будет присылать его в заголовке, чужие запросы отбрасываем
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or hashlib.sha256(("wh:" + BOT_TOKEN).encode()).hexdigest()[:48]
WEBHOOK_PATH = "/telegram-webhook"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
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
    await init_schema()
    pool_ready.set()


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
        ]:
            await c.execute(ddl)
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


async def db() -> asyncpg.Pool:
    if not pool_ready.is_set():
        try:
            await asyncio.wait_for(pool_ready.wait(), timeout=25)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=503, detail="Оракул просыпается, попробуйте через минуту.")
    return pool


async def ensure_user(tg_user: dict, ref_param: Optional[str] = None) -> tuple:
    """Создаёт пользователя при первом входе (с бесплатным раскладом и реферальным бонусом).
    Возвращает (user_dict, created: bool, referrer_id|None)."""
    p = await db()
    uid = int(tg_user["id"])
    first_name = (tg_user.get("first_name") or "Искатель")[:255]
    username = (tg_user.get("username") or None)
    referrer = None
    if ref_param and ref_param.startswith("ref_") and ref_param[4:].isdigit():
        referrer = int(ref_param[4:])
        if referrer == uid:
            referrer = None

    async with p.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow("""
                INSERT INTO users (telegram_id, username, first_name, email, consent_given, balance, free_readings, last_seen)
                VALUES ($1, $2, $3, '', TRUE, 0, $4, NOW())
                ON CONFLICT (telegram_id) DO NOTHING
                RETURNING *
            """, uid, username, first_name, FREE_READINGS_ON_START)
            created = row is not None
            credited_ref = None
            if created and referrer:
                ok = await c.fetchval("""
                    UPDATE users SET balance = balance + $2, ref_count = ref_count + 1
                    WHERE telegram_id = $1 RETURNING telegram_id
                """, referrer, REF_BONUS)
                if ok:
                    row = await c.fetchrow("""
                        UPDATE users SET referred_by = $2, balance = balance + $3
                        WHERE telegram_id = $1 RETURNING *
                    """, uid, referrer, REF_BONUS)
                    credited_ref = referrer
            if not created:
                row = await c.fetchrow("""
                    UPDATE users SET username = $2, first_name = $3, last_seen = NOW(), blocked = FALSE
                    WHERE telegram_id = $1 RETURNING *
                """, uid, username, first_name)
    return dict(row), created, credited_ref


async def get_user(uid: int) -> Optional[dict]:
    p = await db()
    row = await p.fetchrow("SELECT * FROM users WHERE telegram_id = $1", uid)
    return dict(row) if row else None


async def add_balance(uid: int, delta: int) -> Optional[int]:
    p = await db()
    return await p.fetchval("UPDATE users SET balance = GREATEST(0, balance + $2) WHERE telegram_id = $1 RETURNING balance", uid, delta)


async def charge(uid: int, cost: int, allow_free: bool) -> str:
    """Атомарно списывает: сначала бесплатный расклад (если разрешено), потом энергию.
    Возвращает 'free' или 'paid'. Бросает 400, если не хватает."""
    p = await db()
    if allow_free:
        ok = await p.fetchval("UPDATE users SET free_readings = free_readings - 1 WHERE telegram_id = $1 AND free_readings > 0 RETURNING 1", uid)
        if ok:
            return "free"
    ok = await p.fetchval("UPDATE users SET balance = balance - $2 WHERE telegram_id = $1 AND balance >= $2 RETURNING 1", uid, cost)
    if ok:
        return "paid"
    raise HTTPException(status_code=400, detail="Недостаточно Энергии на балансе.")


async def refund(uid: int, mode: str, cost: int):
    p = await db()
    if mode == "free":
        await p.execute("UPDATE users SET free_readings = free_readings + 1 WHERE telegram_id = $1", uid)
    elif mode == "paid":
        await p.execute("UPDATE users SET balance = balance + $2 WHERE telegram_id = $1", uid, cost)


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
        raise HTTPException(status_code=401, detail="Откройте Оракула через Telegram.")
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
            raise HTTPException(status_code=401, detail="Сессия устарела, перезапустите Оракула.")
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

    positions = ["прошлое", "настоящее", "будущее"]

    parts = [
        f"Оракул услышал ваш вопрос: **«{question}»**\n\n"
        "Три карты открыты. Каждая говорит о своём пласте вашей ситуации.\n\n"
    ]

    position_titles = [
        "⏳ Корни ситуации (Прошлое)",
        "⚡ Вызов настоящего",
        "🌟 Вектор будущего"
    ]

    for idx, card in enumerate(used_cards):
        name = card["name"]
        pos = position_titles[idx]
        meaning = get_rich_card_meaning(name, positions[idx])
        parts.append(f"**{pos} — {name}**\n{meaning}\n\n")

    parts.append(
        "**Итог Оракула:**\n"
        "Карты не выносят окончательный приговор — они подсвечивают энергетические токи вашей жизни. "
        "Интегрируйте этот опыт, доверяйте себе и действуйте из состояния любви и осознанности."
    )

    return {
        "cards_used_indices": used_indices,
        "reading": "".join(parts)
    }

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


def clean_ai_text(text: str) -> str:
    """Убирает мусорные Unicode-символы из текста AI: диакритику, управляющие символы,
    невидимые разделители и прочие артефакты LLM."""
    if not text:
        return text
    # Нормализация NFC: разложенные символы → предсобранные (убирает комбинирующие символы, оставляя кириллицу чистой)
    text = unicodedata.normalize("NFC", text)
    # Убираем управляющие символы (кроме \n \r \t)
    text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', text)
    # Убираем невидимые и двунаправленные символы
    text = _INVIS_RE.sub("", text)
    # Убираем одиночные комбинирующие диакритические знаки (U+0300-U+036F), которые "прилипают" к кириллице
    text = _COMBINING_RE.sub("", text)
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


async def call_groq(system_prompt: str, user_prompt: str, max_tokens: int) -> Optional[dict]:
    if not GROQ_API_KEY:
        return None
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt + "\n\nОТВЕЧАЙ ТОЛЬКО JSON без markdown-блоков (без ```json). Только чистый JSON."},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.85,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    t0 = time.monotonic()
    try:
        r = await http.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json=payload, timeout=45.0,
        )
        if r.status_code == 200:
            choice = r.json()["choices"][0]
            parsed = extract_json_from_text(choice["message"]["content"])
            if parsed and parsed.get("reading"):
                log(f"✅ Groq {time.monotonic() - t0:.1f}s (finish={choice.get('finish_reason')})")
                return _clean_reading(parsed)
            log(f"⚠️ Groq вернул неполный JSON (finish={choice.get('finish_reason')})")
        else:
            log(f"⚠️ Groq ошибка {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log(f"⚠️ Groq исключение: {type(e).__name__}: {e}")
    return None


async def call_gemini(system_prompt: str, user_prompt: str, max_tokens: int) -> Optional[dict]:
    if not GEMINI_API_KEY:
        return None
    for model in GEMINI_MODELS:
        gen_cfg = {
            "responseMimeType": "application/json",
            "maxOutputTokens": max_tokens + 500,
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "cards_used_indices": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                    "reading": {"type": "STRING"},
                },
                "required": ["cards_used_indices", "reading"],
            },
        }
        if model.startswith("gemini-2.5"):
            gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}  # без «размышлений» — в разы быстрее
        payload = {
            "contents": [{"parts": [{"text": user_prompt}]}],
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": gen_cfg,
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            r = await http.post(url, json=payload, headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=40.0)
            if r.status_code == 200:
                raw = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                parsed = extract_json_from_text(raw)
                if parsed and parsed.get("reading"):
                    log(f"✅ Gemini ответил через {model}")
                    return _clean_reading(parsed)
            else:
                log(f"⚠️ Gemini {model} ошибка {r.status_code}: {r.text[:150]}")
        except Exception as e:
            log(f"⚠️ Gemini {model} исключение: {type(e).__name__}: {e}")
    return None


async def call_ai(system_prompt: str, user_prompt: str, max_tokens: int = 2000) -> Optional[dict]:
    """Сначала Groq, потом Gemini. Если оба недоступны — None (сработает локальный оракул)."""
    result = await call_groq(system_prompt, user_prompt, max_tokens)
    if result:
        return result
    return await call_gemini(system_prompt, user_prompt, max_tokens)


async def generate_dynamic_reading(question: str, pre_selected_cards: list) -> dict:
    """Индивидуальный разбор — 6 до 12 карт, глубокий анализ на основе классических традиций таро."""
    cards_str = ", ".join([f"[{i}] {c['name']} ({c['type']})" for i, c in enumerate(pre_selected_cards)])

    system_prompt = (
        "Ты — мастер таро с 30-летним опытом, практикующий в традициях Артура Эдварда Уэйта (Таро Уэйта-Смит), "
        "Хайо Банцхафа и Карла Юнга. Ты глубоко знаешь архетипическую психологию и применяешь её к толкованию карт.\n\n"
        "ЗАДАЧА: Провести глубокий индивидуальный расклад по вопросу человека.\n\n"
        "КОЛИЧЕСТВО КАРТ — выбери сам от 6 до 12 в зависимости от сложности вопроса:\n"
        "• 6 карт — вопрос конкретный и краткосрочный\n"
        "• 8-9 карт — вопрос о ситуации, отношениях или решении\n"
        "• 10-12 карт — глубокий экзистенциальный вопрос о жизненном пути, предназначении, трансформации\n\n"
        "СТРУКТУРА РАСКЛАДА (пиши именно так, используй только **жирный** Markdown, никаких HTML-тегов):\n\n"
        "Вступление — 2-3 предложения: почувствуй суть вопроса, обратись к человеку тепло и лично.\n\n"
        "Для каждой карты — отдельный блок:\n"
        "**[Название позиции] — [Имя карты]**\n"
        "Толкование на 60-90 слов: классическое значение карты по Уэйту + применение к конкретному вопросу + "
        "что это говорит о состоянии человека прямо сейчас. Используй образные метафоры.\n\n"
        "ПОЗИЦИИ для 6 карт: Корни ситуации, Текущая энергия, Скрытое влияние, Совет карт, Ближайшее будущее, Итог\n"
        "ПОЗИЦИИ для 8-9 карт: добавь Внутреннее состояние, Окружение, Чего бояться\n"
        "ПОЗИЦИИ для 10-12 карт: используй Кельтский крест или собственную систему позиций\n\n"
        "Интеграция (100-150 слов) — как карты говорят вместе: какой сквозной архетип прослеживается, "
        "что хочет сказать коллективное бессознательное через этот расклад.\n\n"
        "Напутствие (50-70 слов) — конкретное, вдохновляющее, честное. Человек должен уйти наполненным.\n\n"
        "ВАЖНО: Пиши живым тёплым языком. Никогда не используй слова 'нейросеть', 'ИИ', 'алгоритм', 'в заключение'. "
        "Каждый расклад уникален. Общий объём — 800-1200 слов.\n\n"
        "Формат ответа — строго JSON (без markdown-блоков):\n"
        "{\"cards_used_indices\": [список индексов выбранных карт], \"reading\": \"текст расклада\"}"
    )

    user_prompt = f"Вопрос человека: «{question}».\nДоступные карты для выбора: {cards_str}."

    result = await call_ai(system_prompt, user_prompt, max_tokens=4000)
    if result:
        return result

    print("⚠️ Все AI недоступны. Переход на локальный оракул.", flush=True)
    return generate_local_tarot_reading(question, pre_selected_cards)


async def generate_preset_reading(question: str, pre_selected_cards: list) -> dict:
    """Стандартный разбор — 3 карты, подробный и детальный (500-600 слов)."""
    cards_str = ", ".join([f"[{i}] {c['name']} ({c['type']})" for i, c in enumerate(pre_selected_cards)])

    system_prompt = (
        "Ты — опытный таролог, практикующий по системе Артура Уэйта. "
        "Ты умеешь давать точные, живые, детальные ответы на классические жизненные вопросы.\n\n"
        "ЗАДАЧА: выбери ровно 3 карты и проведи подробный расклад «Прошлое — Настоящее — Будущее».\n\n"
        "СТРУКТУРА (пиши только Markdown **жирный**, никаких HTML-тегов):\n\n"
        "Вступление (2-3 предложения) — почувствуй вопрос, обратись к человеку лично и тепло.\n\n"
        "**Прошлое — [Имя карты]**\n"
        "80-100 слов: как прошлый опыт, прошлые решения или давние события сформировали текущую ситуацию. "
        "Раскрой классическое значение карты и покажи, как оно отражается в истории человека.\n\n"
        "**Настоящее — [Имя карты]**\n"
        "80-100 слов: что происходит в жизни человека прямо сейчас — какие силы действуют, "
        "какие внутренние или внешние конфликты определяют момент. Будь конкретен и точен.\n\n"
        "**Будущее — [Имя карты]**\n"
        "80-100 слов: куда ведёт ситуация при текущем развитии событий, какой совет дают карты, "
        "что нужно принять или изменить. Дай ясное и вдохновляющее направление.\n\n"
        "**Совет Оракула** (70-90 слов) — итоговое напутствие: как три карты говорят вместе, "
        "что сквозной смысл расклада говорит о пути человека. Заканчивай на тёплой, утвердительной ноте.\n\n"
        "ВАЖНО: Никогда не используй слова 'нейросеть', 'ИИ', 'алгоритм'. "
        "Пиши живо, поэтично, с метафорами. Каждый расклад уникален — не повторяй шаблоны. "
        "Общий объём — 500-600 слов.\n\n"
        "Формат ответа — строго JSON (без markdown-блоков):\n"
        "{\"cards_used_indices\": [индекс1, индекс2, индекс3], \"reading\": \"текст расклада\"}"
    )

    user_prompt = f"Вопрос: «{question}».\nДоступные карты: {cards_str}."

    result = await call_ai(system_prompt, user_prompt, max_tokens=1800)
    if result:
        return result

    print("⚠️ Все AI недоступны для стандартного разбора. Локальный оракул.", flush=True)
    return generate_local_tarot_reading(question, pre_selected_cards)


async def generate_daily_reading(pre_selected_cards: list) -> dict:
    """Карта дня — живое, детальное толкование одной карты (250-300 слов)."""
    card = pre_selected_cards[0]
    card_str = f"[0] {card['name']} ({card['type']})"

    system_prompt = (
        "Ты — мастер таро, хранитель древних символов. Твой стиль — поэтичный, тёплый, живой.\n\n"
        "ЗАДАЧА: дай глубокое толкование Карты Дня — одной карты, которая станет ориентиром на 24 часа.\n\n"
        "СТРУКТУРА (только Markdown **жирный**, никаких HTML-тегов):\n\n"
        "**Карта Дня — [Имя карты]**\n\n"
        "Основное толкование (120-150 слов): раскрой архетип карты по традиции Уэйта — "
        "её светлую и теневую сторону, символику, что она говорит о сегодняшнем дне. "
        "Сделай это живо, с образами и метафорами.\n\n"
        "**На что обратить внимание сегодня:** (50-60 слов) — конкретная область жизни или внутреннее состояние, "
        "которое карта подсвечивает именно сегодня.\n\n"
        "**Совет дня:** (40-50 слов) — одно чёткое, действенное напутствие. "
        "Что сделать, о чём подумать, чего избежать.\n\n"
        "ВАЖНО: Никаких слов 'нейросеть', 'ИИ'. Каждый день — уникальный текст. Тепло, лично, вдохновляюще.\n\n"
        "Формат ответа — строго JSON (без markdown-блоков):\n"
        "{\"cards_used_indices\": [0], \"reading\": \"текст\"}"
    )

    user_prompt = f"Карта дня: {card_str}."

    result = await call_ai(system_prompt, user_prompt, max_tokens=900)
    if result:
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
            BotCommand(command="start", description="🔮 Открыть Оракула"),
            BotCommand(command="ref", description="🎁 Пригласить друга и получить энергию"),
            BotCommand(command="stop", description="🔕 Отключить утреннюю карту дня"),
        ])
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🔮 Оракул", web_app=WebAppInfo(url=FRONTEND_URL)))
    except Exception as e:
        log(f"⚠️ Команды/меню: {e}")


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
    "🌙 Доброе утро, {name}! Оракул вытянул для тебя карту на сегодня. Открой её 🔮",
    "✨ {name}, вселенная оставила тебе послание на сегодня. Карта Дня — бесплатно.",
    "🔮 {name}, прежде чем день закрутится — загляни в свою Карту Дня.",
]


def webapp_kb(text: str = "🔮 Открыть Оракул") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=FRONTEND_URL))]])


async def send_daily_push() -> int:
    """Отправляет утреннее напоминание всем, кто ещё не получил его сегодня. Возвращает число отправленных."""
    p = await db()
    today = get_today_str()
    sent = 0
    while True:
        rows = await p.fetch("""
            SELECT telegram_id, first_name, free_readings FROM users
            WHERE notify AND NOT blocked
              AND (last_push_date IS DISTINCT FROM $1)
              AND (last_daily_date IS DISTINCT FROM $1)
            LIMIT 200
        """, today)
        if not rows:
            # тем, кто уже взял карту сам, пуш не нужен — просто отмечаем
            await p.execute("UPDATE users SET last_push_date = $1 WHERE last_daily_date = $1 AND last_push_date IS DISTINCT FROM $1", today)
            break
        for r in rows:
            uid = r["telegram_id"]
            text = random.choice(PUSH_TEXTS).format(name=r["first_name"] or "Искатель")
            if (r["free_readings"] or 0) > 0:
                text += "\n\n🎁 А ещё у тебя есть бесплатный расклад на 3 карты."
            await p.execute("UPDATE users SET last_push_date = $2 WHERE telegram_id = $1", uid, today)
            try:
                await bot.send_message(uid, text, reply_markup=webapp_kb("🃏 Открыть Карту Дня"))
                sent += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except (TelegramForbiddenError, TelegramBadRequest):
                await p.execute("UPDATE users SET blocked = TRUE WHERE telegram_id = $1", uid)
            except Exception as e:
                log(f"push {uid}: {e}")
            await asyncio.sleep(0.05)  # ~20 сообщений/сек — в пределах лимитов Telegram
    return sent


async def push_loop():
    if not PUSH_ENABLED:
        return
    await pool_ready.wait()
    while True:
        try:
            if local_now().hour >= PUSH_HOUR:
                n = await send_daily_push()
                if n:
                    log(f"📬 Утренний пуш отправлен: {n}")
        except Exception as e:
            log(f"push_loop: {e}")
        await asyncio.sleep(120)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global http
    http = httpx.AsyncClient(timeout=30.0, limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))
    # Всё тяжёлое — в фоне: сервер начинает принимать запросы сразу
    tasks = [
        asyncio.create_task(init_pool()),
        asyncio.create_task(setup_bot()),
        asyncio.create_task(keepalive_loop()),
        asyncio.create_task(push_loop()),
    ]
    yield
    for t in tasks:
        t.cancel()
    await http.aclose()
    if pool:
        await pool.close()
    await bot.session.close()


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
        "free_readings": user.get("free_readings") or 0,
        "daily_available": admin or user.get("last_daily_date") != get_today_str(),
        "ref_link": ref_link(user["telegram_id"]),
        "ref_count": user.get("ref_count") or 0,
        "ref_bonus": REF_BONUS,
        "first_purchase": (user.get("purchases") or 0) == 0,
        "prices": {k: (v[2] if (user.get("purchases") or 0) == 0 else v[1]) for k, v in PACKS.items()},
    }


@app.get("/api/user/profile")
async def get_user_profile(authorization: str = Header(None)):
    auth = verify_telegram_init_data(authorization)
    user, created, referrer = await ensure_user(auth["user"], auth.get("start_param"))
    if referrer:
        asyncio.create_task(notify_referrer(referrer, user.get("first_name")))
    return profile_payload(user, is_admin(user))


def _question(payload: dict) -> str:
    q = (payload.get("question") or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="Введите вопрос.")
    return q[:500]


@app.post("/api/user/use-preset-ai-reading")
async def use_preset_ai_reading(payload: dict, authorization: str = Header(None)):
    """Стандартный разбор (3 карты): первый — бесплатно, далее 150 энергии."""
    user, admin = await auth_user(authorization)
    question = _question(payload)
    uid = user["telegram_id"]
    mode = "admin" if admin else await charge(uid, COST_PRESET, allow_free=True)

    try:
        pre_selected = random.sample(get_tarot_deck(), 6)
        result_data = await generate_preset_reading(question, pre_selected)
    except Exception as e:
        log(f"preset error: {e}")
        await refund(uid, mode, COST_PRESET)
        raise HTTPException(status_code=500, detail="Оракул не смог провести расклад. Энергия возвращена.")

    used = [i for i in result_data.get("cards_used_indices", []) if isinstance(i, int) and 0 <= i < len(pre_selected)]
    used = list(dict.fromkeys(used))[:3]
    if len(used) < 3:
        used = [0, 1, 2]
    fresh = await get_user(uid)
    return {
        "success": True,
        "cards": [pre_selected[i] for i in used],
        "text": result_data.get("reading", ""),
        "new_balance": 99999 if admin else fresh["balance"],
        "free_readings": fresh.get("free_readings") or 0,
        "used_free": mode == "free",
    }


@app.post("/api/user/use-daily-ai-reading")
async def use_daily_ai_reading(authorization: str = Header(None)):
    """Карта дня — бесплатно, 1 раз в сутки (граница суток — по TZ_OFFSET_HOURS)."""
    user, admin = await auth_user(authorization)
    uid = user["telegram_id"]
    today = get_today_str()
    if not admin:
        p = await db()
        ok = await p.fetchval("""
            UPDATE users SET last_daily_date = $2
            WHERE telegram_id = $1 AND last_daily_date IS DISTINCT FROM $2 RETURNING 1
        """, uid, today)
        if not ok:
            raise HTTPException(status_code=429, detail="Карта дня уже получена сегодня. Возвращайтесь завтра 🌙")

    pre_selected = random.sample(get_tarot_deck(), 1)
    result_data = await generate_daily_reading(pre_selected)
    return {
        "success": True,
        "cards": [pre_selected[0]],
        "text": result_data.get("reading", ""),
        "new_balance": 99999 if admin else user.get("balance", 0),
    }


@app.post("/api/user/use-ai-reading")
async def use_ai_reading(payload: dict, authorization: str = Header(None)):
    """Индивидуальный разбор — 750 энергии, 6–12 карт."""
    user, admin = await auth_user(authorization)
    question = _question(payload)
    uid = user["telegram_id"]
    mode = "admin" if admin else await charge(uid, COST_AI, allow_free=False)

    try:
        pre_selected = random.sample(get_tarot_deck(), 12)
        result_data = await generate_dynamic_reading(question, pre_selected)
    except Exception as e:
        log(f"ai reading error: {e}")
        await refund(uid, mode, COST_AI)
        raise HTTPException(status_code=500, detail="Оракул не смог провести расклад. Энергия возвращена.")

    used = [i for i in result_data.get("cards_used_indices", []) if isinstance(i, int) and 0 <= i < len(pre_selected)]
    used = list(dict.fromkeys(used))[:12]
    if len(used) < 6:
        used = list(range(6))
    fresh = await get_user(uid)
    return {
        "success": True,
        "cards": [pre_selected[i] for i in used],
        "text": result_data.get("reading", ""),
        "new_balance": 99999 if admin else fresh["balance"],
    }


@app.post("/api/payment/stars-invoice")
async def create_stars_invoice(payload: dict, authorization: str = Header(None)):
    user, _ = await auth_user(authorization)
    pack = payload.get("pack")
    if pack not in PACKS:
        raise HTTPException(status_code=400, detail="Неверный тип пакета")
    energy, price, first_price, title, description = PACKS[pack]
    if (user.get("purchases") or 0) == 0 and first_price < price:
        price = first_price
        title += " · цена первой покупки"
    invoice_payload = f"{pack}:{user['telegram_id']}:{random.randint(100000, 999999)}"
    try:
        link = await bot.create_invoice_link(
            title=title, description=description, payload=invoice_payload,
            provider_token="", currency="XTR",
            prices=[LabeledPrice(label="Telegram Stars", amount=int(price))],
        )
        return {"invoice_link": link}
    except Exception as e:
        log(f"Ошибка создания инвойса: {e}")
        raise HTTPException(status_code=500, detail="Не удалось создать счёт, попробуйте ещё раз.")


@app.get("/api/system/setup-webhook")
async def setup_webhook_manually():
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


# =====================================================================
# БОТ: команды и платежи
# =====================================================================
async def notify_referrer(referrer_id: int, friend_name: Optional[str]):
    try:
        await bot.send_message(
            referrer_id,
            f"🎁 {friend_name or 'Твой друг'} пришёл(а) по твоей ссылке! Тебе начислено +{REF_BONUS} Энергии ✨",
            reply_markup=webapp_kb(),
        )
    except Exception:
        pass


@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject):
    u = message.from_user
    tg_user = {"id": u.id, "first_name": u.first_name, "username": u.username}
    try:
        user, created, referrer = await ensure_user(tg_user, command.args)
    except Exception as e:
        log(f"/start db: {e}")
        user, created, referrer = {}, False, None

    lines = [f"Приветствую тебя, {u.first_name}! 🔮", "",
             "Я — Оракул Таро. Задай вопрос — и карты ответят.", ""]
    if created:
        lines.append("🎁 Твой первый расклад на 3 карты — в подарок.")
        if referrer:
            lines.append(f"✨ Ты пришёл(а) по приглашению — тебе начислено +{REF_BONUS} Энергии.")
    elif (user.get("free_readings") or 0) > 0:
        lines.append("🎁 У тебя есть бесплатный расклад на 3 карты.")
    lines += ["🃏 Карта Дня — бесплатно каждый день.",
              "🎴 Стандартный и 🔮 Индивидуальный разборы — за Энергию.", "",
              "Жми кнопку ниже 👇"]
    await message.answer("\n".join(lines), reply_markup=webapp_kb())
    if referrer:
        asyncio.create_task(notify_referrer(referrer, u.first_name))


@dp.message(Command("ref"))
async def cmd_ref(message: Message):
    uid = message.from_user.id
    user = await get_user(uid)
    count = (user or {}).get("ref_count") or 0
    link = ref_link(uid)
    share = "https://t.me/share/url?" + urllib.parse.urlencode({"url": link, "text": "🔮 Оракул Таро в Telegram — первый расклад бесплатно, Карта Дня каждый день"})
    await message.answer(
        f"🎁 Приглашай друзей — за каждого вы оба получите +{REF_BONUS} Энергии.\n\n"
        f"Твоя ссылка:\n{link}\n\nУже пришло по ссылке: {count}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📤 Отправить другу", url=share)]]),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    p = await db()
    await p.execute("UPDATE users SET notify = FALSE WHERE telegram_id = $1", message.from_user.id)
    await message.answer("🔕 Утренние напоминания отключены. Включить снова: /notify")


@dp.message(Command("notify"))
async def cmd_notify(message: Message):
    p = await db()
    await p.execute("UPDATE users SET notify = TRUE WHERE telegram_id = $1", message.from_user.id)
    await message.answer("🔔 Утренняя Карта Дня снова включена.")


@dp.pre_checkout_query()
async def process_pre_checkout(q: PreCheckoutQuery):
    parts = (q.invoice_payload or "").split(":")
    ok = len(parts) == 3 and parts[0] in PACKS and parts[1].isdigit()
    await q.answer(ok=ok, error_message=None if ok else "Счёт устарел, создайте новый в приложении.")


@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    pay = message.successful_payment
    parts = (pay.invoice_payload or "").split(":")
    if len(parts) != 3 or parts[0] not in PACKS:
        log(f"Неизвестный payload платежа: {pay.invoice_payload}")
        return
    pack, uid = parts[0], int(parts[1])
    energy = PACKS[pack][0]
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
    await message.answer(f"🔮 Оплата успешна! Зачислено +{energy} Энергии.", reply_markup=webapp_kb())
