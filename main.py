# -*- coding: utf-8 -*-
import os
import urllib.parse
import json
import random
import asyncio
import socket
from typing import Optional
from datetime import datetime

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import psycopg2
from psycopg2.extras import RealDictCursor

from aiogram import Bot, Dispatcher, F
from aiogram.types import Update, LabeledPrice, PreCheckoutQuery, Message
from aiogram.filters import Command

# =====================================================================
# КОНФИГУРАЦИЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
# =====================================================================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://tarot-frontend-wine.vercel.app")

if not BOT_TOKEN:
    raise RuntimeError("Критическая ошибка: Переменная TELEGRAM_BOT_TOKEN не задана!")
if not DATABASE_URL:
    raise RuntimeError("Критическая ошибка: Переменная DATABASE_URL не задана!")

app = FastAPI()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

IS_SQLITE = False

# =====================================================================
# РАБОТА С БАЗОЙ ДАННЫХ
# =====================================================================
def get_db_connection():
    global IS_SQLITE

    db_url_clean = DATABASE_URL.strip().replace("\r", "").replace("\n", "")
    parsed = urllib.parse.urlparse(db_url_clean)
    user = urllib.parse.unquote(parsed.username) if parsed.username else ""
    password = urllib.parse.unquote(parsed.password) if parsed.password else ""
    host = parsed.hostname
    port = parsed.port or 5432
    db_name = parsed.path[1:] if parsed.path else ""

    try:
        try:
            ips = socket.getaddrinfo(host, None, socket.AF_INET)
            if ips:
                resolved_host = ips[0][4][0]
                host = resolved_host
        except Exception:
            pass

        conn = psycopg2.connect(
            database=db_name,
            user=user,
            password=password,
            host=host,
            port=port,
            sslmode="require",
            connect_timeout=4
        )
        IS_SQLITE = False
        return conn
    except Exception as e:
        print(f"Прямое подключение отклонено: {e}. Пробуем пуллер...", flush=True)

    try:
        project_id = "wbbcljbrfpgriukzjlvc"
        if parsed.hostname and ".supabase.co" in parsed.hostname:
            host_parts = parsed.hostname.split(".")
            if len(host_parts) >= 2:
                project_id = host_parts[1] if host_parts[0] == "db" else host_parts[0]

        pooler_user = f"postgres.{project_id}"
        regions = ["aws-0-eu-central-1", "aws-0-us-east-1", "aws-0-us-west-1", "aws-0-ap-southeast-1"]

        for region in regions:
            pooler_host = f"{region}.pooler.supabase.com"
            try:
                conn = psycopg2.connect(
                    database="postgres",
                    user=pooler_user,
                    password=password,
                    host=pooler_host,
                    port=6543,
                    sslmode="require",
                    connect_timeout=3
                )
                IS_SQLITE = False
                return conn
            except Exception:
                continue
    except Exception as e_pool:
        print(f"Ошибка пуллера: {e_pool}", flush=True)

    print("⚠️ Облачная БД недоступна. Переход на SQLite!", flush=True)
    import sqlite3
    conn = sqlite3.connect("users.db")
    conn.row_factory = sqlite3.Row
    IS_SQLITE = True
    return conn

def execute_query(cur, sql, params=()):
    global IS_SQLITE
    if IS_SQLITE:
        sql = sql.replace("%s", "?")
        sql = sql.replace("GREATEST", "MAX")
    cur.execute(sql, params)

def init_db():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        execute_query(cur, """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username VARCHAR(255),
                first_name VARCHAR(255),
                email VARCHAR(255),
                consent_given BOOLEAN DEFAULT FALSE,
                balance INTEGER DEFAULT 150,
                ai_balance INTEGER DEFAULT 0,
                daily_balance INTEGER DEFAULT 0,
                last_daily_date VARCHAR(10) DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # Добавляем колонку если её нет (для существующих БД)
        try:
            execute_query(cur, "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_daily_date VARCHAR(10) DEFAULT NULL;")
        except Exception:
            pass
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"Ошибка инициализации БД: {e}", flush=True)
    finally:
        if conn:
            conn.close()

def get_user(telegram_id: int):
    global IS_SQLITE
    conn = None
    try:
        conn = get_db_connection()
        if IS_SQLITE:
            cur = conn.cursor()
        else:
            cur = conn.cursor(cursor_factory=RealDictCursor)

        execute_query(cur, "SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
        user = cur.fetchone()
        cur.close()
        if user:
            user_dict = dict(user)
            if user_dict.get("username") and user_dict.get("username").lower() == "dzenra_prod":
                user_dict["balance"] = 99999
            return user_dict
        return None
    except Exception as e:
        return None
    finally:
        if conn:
            conn.close()

def create_user(telegram_id: int, username: Optional[str], first_name: str, email: str = ""):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        execute_query(cur, """
            INSERT INTO users (telegram_id, username, first_name, email, consent_given, balance, ai_balance, daily_balance)
            VALUES (%s, %s, %s, %s, TRUE, 0, 0, 0)
            ON CONFLICT (telegram_id) DO UPDATE
            SET username = EXCLUDED.username, first_name = EXCLUDED.first_name;
        """, (telegram_id, username, first_name, email))
        conn.commit()
        cur.close()
    except Exception as e:
        pass
    finally:
        if conn:
            conn.close()

def update_user_profile_info(telegram_id: int, first_name: str, username: Optional[str]):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        execute_query(cur, """
            UPDATE users
            SET first_name = %s, username = %s
            WHERE telegram_id = %s;
        """, (first_name, username, telegram_id))
        conn.commit()
        cur.close()
    except Exception as e:
        pass
    finally:
        if conn:
            conn.close()

def update_user_balance(telegram_id: int, balance_delta: int):
    conn = None
    user = get_user(telegram_id)
    if user and user.get("username") and user.get("username").lower() == "dzenra_prod":
        return

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        execute_query(cur, """
            UPDATE users
            SET balance = GREATEST(0, balance + %s)
            WHERE telegram_id = %s;
        """, (balance_delta, telegram_id))
        conn.commit()
        cur.close()
    except Exception as e:
        pass
    finally:
        if conn:
            conn.close()

def get_today_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")

def check_and_use_daily_card(telegram_id: int) -> bool:
    """Проверяет, может ли пользователь получить карту дня сегодня (1 раз в сутки).
    Если может — обновляет дату и возвращает True. Иначе False."""
    global IS_SQLITE
    today = get_today_str()
    conn = None
    try:
        conn = get_db_connection()
        if IS_SQLITE:
            cur = conn.cursor()
        else:
            cur = conn.cursor(cursor_factory=RealDictCursor)
        execute_query(cur, "SELECT last_daily_date FROM users WHERE telegram_id = %s", (telegram_id,))
        row = cur.fetchone()
        if not row:
            cur.close()
            return False
        last_date = dict(row).get("last_daily_date")
        if last_date == today:
            cur.close()
            return False  # Уже использовал сегодня
        execute_query(cur, "UPDATE users SET last_daily_date = %s WHERE telegram_id = %s", (today, telegram_id))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"Ошибка проверки карты дня: {e}", flush=True)
        return True  # При ошибке — разрешаем
    finally:
        if conn:
            conn.close()

def verify_telegram_init_data(init_data: str) -> dict:
    if not init_data:
        return {"id": 123456789, "first_name": "Искатель", "username": "test_user"}
    try:
        parsed_data = dict(urllib.parse.parse_qsl(init_data))
        if "user" in parsed_data:
            return json.loads(parsed_data["user"])
        return {"id": 123456789, "first_name": "Искатель", "username": "test_user"}
    except Exception:
        return {"id": 123456789, "first_name": "Искатель", "username": "test_user"}

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
# ВЫЗОВ AI — GROQ (основной) + GEMINI (запасной)
# =====================================================================

async def call_groq(system_prompt: str, user_prompt: str) -> Optional[dict]:
    """Groq API (OpenAI-совместимый). Бесплатно, быстро, качественно."""
    if not GROQ_API_KEY:
        return None

    # Просим ИИ вернуть строго JSON
    full_system = system_prompt + "\n\nОТВЕЧАЙ ТОЛЬКО JSON без markdown-блоков (без ```json). Только чистый JSON."

    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": full_system},
            {"role": "user",   "content": user_prompt}
        ],
        "temperature": 0.85,
        "max_tokens": 2000,
        "response_format": {"type": "json_object"}
    }

    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json"
                },
                json=payload,
                timeout=30.0
            )
            if r.status_code == 200:
                raw = r.json()["choices"][0]["message"]["content"]
                parsed = extract_json_from_text(raw)
                if parsed and "reading" in parsed:
                    # Убираем возможные HTML-теги
                    clean = parsed["reading"]
                    for tag in ["<h3>","</h3>","<h4>","</h4>","<br>","<br/>"]:
                        clean = clean.replace(tag, "\n" if "br" in tag else "")
                    parsed["reading"] = clean
                    print("✅ Groq ответил успешно", flush=True)
                    return parsed
            else:
                print(f"⚠️ Groq ошибка {r.status_code}: {r.text[:200]}", flush=True)
        except Exception as e:
            print(f"⚠️ Groq исключение: {e}", flush=True)

    return None


GEMINI_MODELS = ["gemini-2.0-flash", "gemini-1.5-flash"]

async def call_gemini(system_prompt: str, user_prompt: str) -> Optional[dict]:
    """Gemini — запасной вариант если Groq недоступен."""
    if not GEMINI_API_KEY:
        return None

    payload = {
        "contents": [{"parts": [{"text": user_prompt}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "cards_used_indices": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                    "reading": {"type": "STRING"}
                },
                "required": ["cards_used_indices", "reading"]
            }
        }
    }

    async with httpx.AsyncClient() as client:
        for model in GEMINI_MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
            try:
                r = await client.post(url, json=payload, timeout=25.0)
                if r.status_code == 200:
                    raw = r.json().get("candidates",[{}])[0].get("content",{}).get("parts",[{}])[0].get("text","")
                    parsed = extract_json_from_text(raw)
                    if parsed and "reading" in parsed:
                        print(f"✅ Gemini ответил через {model}", flush=True)
                        return parsed
                else:
                    print(f"⚠️ Gemini {model} ошибка {r.status_code}", flush=True)
            except Exception as e:
                print(f"⚠️ Gemini {model} исключение: {e}", flush=True)
            await asyncio.sleep(0.5)

    return None


async def call_ai(system_prompt: str, user_prompt: str) -> Optional[dict]:
    """Единая точка вызова AI: сначала Groq, потом Gemini."""
    result = await call_groq(system_prompt, user_prompt)
    if result:
        return result
    print("⚠️ Groq недоступен, пробуем Gemini...", flush=True)
    result = await call_gemini(system_prompt, user_prompt)
    return result


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

    result = await call_ai(system_prompt, user_prompt)
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

    result = await call_ai(system_prompt, user_prompt)
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

    result = await call_ai(system_prompt, user_prompt)
    if result:
        return result

    return generate_local_tarot_reading("", pre_selected_cards, reading_type="daily")

# =====================================================================
# API ЭНДПОИНТЫ
# =====================================================================

@app.get("/api/user/profile")
async def get_user_profile(authorization: str = Header(None)):
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    first_name = user_tg.get("first_name", "Искатель")
    username = user_tg.get("username")

    user = get_user(user_id)
    if not user:
        create_user(telegram_id=user_id, username=username, first_name=first_name, email="")
        user = get_user(user_id)
    else:
        update_user_profile_info(user_id, first_name, username)
        user = get_user(user_id)

    if not user:
        raise HTTPException(status_code=500, detail="Ошибка базы данных")

    is_developer = ((username and username.lower() == "dzenra_prod") or
                    (user.get("username") and user.get("username").lower() == "dzenra_prod"))
    balance = 99999 if is_developer else user.get("balance", 0)

    return {"registered": True, "user_id": user_id, "name": user["first_name"], "balance": balance}


@app.post("/api/user/use-reading")
async def use_reading(authorization: str = Header(None)):
    """Списание 150 энергии за готовый вопрос (без AI)."""
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    username = user_tg.get("username")

    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    is_developer = ((username and username.lower() == "dzenra_prod") or
                    (user.get("username") and user.get("username").lower() == "dzenra_prod"))
    if is_developer:
        return {"success": True, "new_balance": 99999}

    if user.get("balance", 0) < 150:
        raise HTTPException(status_code=400, detail="Недостаточно Энергии на балансе.")

    update_user_balance(user_id, balance_delta=-150)
    return {"success": True, "new_balance": user.get("balance", 150) - 150}


@app.post("/api/user/use-daily-reading")
async def use_daily_reading(authorization: str = Header(None)):
    """Списание 75 энергии за карту дня."""
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    username = user_tg.get("username")

    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    is_developer = ((username and username.lower() == "dzenra_prod") or
                    (user.get("username") and user.get("username").lower() == "dzenra_prod"))
    if is_developer:
        return {"success": True, "new_balance": 99999}

    if user.get("balance", 0) < 75:
        raise HTTPException(status_code=400, detail="Недостаточно Энергии на балансе.")

    update_user_balance(user_id, balance_delta=-75)
    return {"success": True, "new_balance": user.get("balance", 75) - 75}


@app.post("/api/user/use-preset-ai-reading")
async def use_preset_ai_reading(payload: dict, authorization: str = Header(None)):
    """Готовый вопрос через Gemini AI — 150 энергии, живой уникальный ответ."""
    await asyncio.sleep(1.5)

    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    username = user_tg.get("username")

    question = payload.get("question", "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Введите вопрос.")

    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не зарегистрирован.")

    is_developer = ((username and username.lower() == "dzenra_prod") or
                    (user.get("username") and user.get("username").lower() == "dzenra_prod"))

    if not is_developer and user.get("balance", 0) < 150:
        raise HTTPException(status_code=400, detail="Недостаточно Энергии на балансе.")

    deck = get_tarot_deck()
    pre_selected = random.sample(deck, 6)

    result_data = await generate_preset_reading(question, pre_selected)

    used_indices = result_data.get("cards_used_indices", [0, 1, 2])
    used_indices = [idx for idx in used_indices if 0 <= idx < len(pre_selected)]
    if len(used_indices) < 3:
        used_indices = [0, 1, 2]
    else:
        used_indices = used_indices[:3]

    final_cards = [pre_selected[idx] for idx in used_indices]
    text_reading = result_data.get("reading", "")

    if not is_developer:
        update_user_balance(user_id, balance_delta=-150)
        new_balance = user.get("balance", 150) - 150
    else:
        new_balance = 99999

    return {
        "success": True,
        "cards": final_cards,
        "text": text_reading,
        "new_balance": new_balance
    }


@app.post("/api/user/use-daily-ai-reading")
async def use_daily_ai_reading(authorization: str = Header(None)):
    """Карта дня — бесплатно, 1 раз в сутки."""
    await asyncio.sleep(0.8)

    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    username = user_tg.get("username")

    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не зарегистрирован.")

    is_developer = (username and username.lower() == "dzenra_prod") or \
                   (user.get("username") and user.get("username").lower() == "dzenra_prod")

    # Проверяем лимит 1 раз в день (для разработчика — без ограничений)
    if not is_developer:
        allowed = check_and_use_daily_card(user_id)
        if not allowed:
            raise HTTPException(status_code=429, detail="Карта дня уже получена сегодня. Возвращайтесь завтра.")

    deck = get_tarot_deck()
    pre_selected = random.sample(deck, 3)

    result_data = await generate_daily_reading(pre_selected)

    final_card = pre_selected[0]
    text_reading = result_data.get("reading", "")

    return {
        "success": True,
        "cards": [final_card],
        "text": text_reading,
        "new_balance": 99999 if is_developer else user.get("balance", 0)
    }


@app.post("/api/user/use-ai-reading")
async def use_ai_reading(payload: dict, authorization: str = Header(None)):
    """Индивидуальный разбор — 750 энергии, глубокий анализ."""
    await asyncio.sleep(2.0)

    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    username = user_tg.get("username")

    question = payload.get("question", "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Введите ваш вопрос для расклада.")

    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не зарегистрирован.")

    is_developer = ((username and username.lower() == "dzenra_prod") or
                    (user.get("username") and user.get("username").lower() == "dzenra_prod"))

    if not is_developer and user.get("balance", 0) < 750:
        raise HTTPException(status_code=400, detail="Недостаточно Энергии на балансе.")

    deck = get_tarot_deck()
    pre_selected = random.sample(deck, 12)  # 12 карт — AI выберет 6-12

    result_data = await generate_dynamic_reading(question, pre_selected)

    used_indices = result_data.get("cards_used_indices", list(range(6)))
    used_indices = [idx for idx in used_indices if 0 <= idx < len(pre_selected)]
    if len(used_indices) < 6:
        used_indices = list(range(min(6, len(pre_selected))))

    final_cards = [pre_selected[idx] for idx in used_indices]
    text_reading = result_data.get("reading", "")

    if not is_developer:
        update_user_balance(user_id, balance_delta=-750)
        new_balance = user.get("balance", 750) - 750
    else:
        new_balance = 99999

    return {
        "success": True,
        "cards": final_cards,
        "text": text_reading,
        "new_balance": new_balance
    }


@app.post("/api/payment/stars-invoice")
async def create_stars_invoice(payload: dict, authorization: str = Header(None)):
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    pack = payload.get("pack")

    title = ""
    description = ""
    amount = 0
    payload_str = ""

    if pack == "pack_150":
        title = "+150 Энергии"
        description = "1 Стандартный разбор — расклад на 3 карты по вашему вопросу."
        amount = 150
        payload_str = f"buy_150_{user_id}_{random.randint(1000,9999)}"
    elif pack == "pack_450":
        title = "+450 Энергии (скидка 15%)"
        description = "3 Стандартных разбора по выгодной цене — скидка 15%."
        amount = 383  # 450 * 0.85 = 382.5 → 383 звезды
        payload_str = f"buy_450_{user_id}_{random.randint(1000,9999)}"
    elif pack == "pack_750":
        title = "+750 Энергии"
        description = "1 Индивидуальный разбор — глубокий персональный анализ от 6 до 12 карт."
        amount = 750
        payload_str = f"buy_750_{user_id}_{random.randint(1000,9999)}"
    else:
        raise HTTPException(status_code=400, detail="Неверный тип пакета")

    try:
        invoice_link = await bot.create_invoice_link(
            title=title,
            description=description,
            payload=payload_str,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Telegram Stars", amount=int(amount))]
        )
        return {"invoice_link": invoice_link}
    except Exception as e:
        print(f"Критическая ошибка создания инвойса: {e}", flush=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/system/setup-webhook")
async def setup_webhook_manually():
    try:
        render_external_url = os.getenv("RENDER_EXTERNAL_URL")
        if render_external_url:
            webhook_url = f"{render_external_url.strip()}/telegram-webhook"
        else:
            webhook_url = "https://tarot-backend-136l.onrender.com/telegram-webhook".strip()

        await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
        return {"status": "ok", "message": "Вебхук успешно привязан!", "webhook_url": webhook_url}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@dp.message(Command("start"))
async def cmd_start(message: Message):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔮 Открыть Оракул", web_app=WebAppInfo(url=FRONTEND_URL))]
    ])

    welcome_text = (
        f"Приветствуем тебя, {message.from_user.first_name}!\n\n"
        "Я — древний Оракул Таро, соединенный с мудростью веков.\n"
        "Здесь ты можешь получить глубокие ответы на любые вопросы.\n\n"
        "🃏 Карта Дня — бесплатно каждые сутки.\n"
        "🎴 Стандартный и Индивидуальный разборы доступны за Энергию."
    )
    await message.answer(welcome_text, reply_markup=keyboard)


@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payment = message.successful_payment
    payload = payment.invoice_payload

    parts = payload.split("_")
    if len(parts) >= 4:
        user_id = int(parts[3])

        if "buy_150" in payload:
            update_user_balance(user_id, balance_delta=150)
            await message.answer("🔮 Оплата успешна! Зачислено +150 Энергии.")
        elif "buy_450" in payload:
            update_user_balance(user_id, balance_delta=450)
            await message.answer("🔮 Оплата успешна! Зачислено +450 Энергии.")
        elif "buy_750" in payload:
            update_user_balance(user_id, balance_delta=750)
            await message.answer("🔮 Оплата успешна! Зачислено +750 Энергии.")


@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    try:
        raw_json = await request.json()
        telegram_update = Update.model_validate(raw_json, context={"bot": bot})
        await dp.feed_update(bot=bot, update=telegram_update)
        return {"status": "ok"}
    except Exception as e:
        print(f"Ошибка вебхука Telegram: {e}", flush=True)
        return {"status": "error", "message": str(e)}


@app.on_event("startup")
async def on_startup():
    try:
        init_db()
    except Exception as e:
        print(f"Ошибка инициализации БД на старте: {e}", flush=True)

    try:
        render_external_url = os.getenv("RENDER_EXTERNAL_URL")
        if render_external_url:
            webhook_url = f"{render_external_url.strip()}/telegram-webhook"
        else:
            webhook_url = "https://tarot-backend-136l.onrender.com/telegram-webhook".strip()

        await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
        print(f"Вебхук Telegram направлен на: {webhook_url}", flush=True)
    except Exception as e:
        print(f"⚠️ Не удалось установить вебхук: {e}", flush=True)
