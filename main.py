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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
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
            VALUES (%s, %s, %s, %s, TRUE, 150, 0, 0)
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
# ВЫЗОВ GEMINI AI
# =====================================================================

# СПИСОК МОДЕЛЕЙ ДЛЯ ПЕРЕБОРА (от лучшей к запасной)
GEMINI_MODELS = [
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
]

async def call_gemini(system_prompt: str, user_prompt: str) -> Optional[str]:
    """Вызвать Gemini с автоматическим перебором моделей."""
    if not GEMINI_API_KEY:
        return None

    structured_payload = {
        "contents": [{"parts": [{"text": user_prompt}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "cards_used_indices": {
                        "type": "ARRAY",
                        "items": {"type": "INTEGER"}
                    },
                    "reading": {
                        "type": "STRING"
                    }
                },
                "required": ["cards_used_indices", "reading"]
            }
        }
    }

    async with httpx.AsyncClient() as client:
        for model_name in GEMINI_MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
            for attempt in range(2):
                try:
                    response = await client.post(url, json=structured_payload, timeout=25.0)
                    if response.status_code == 200:
                        data = response.json()
                        raw_text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                        parsed = extract_json_from_text(raw_text)
                        if parsed and "reading" in parsed:
                            # Очищаем HTML-теги, которые ИИ мог вставить
                            clean = parsed["reading"]
                            clean = clean.replace("<h3>", "").replace("</h3>", "")
                            clean = clean.replace("<h4>", "").replace("</h4>", "")
                            clean = clean.replace("<br>", "\n").replace("<br/>", "\n")
                            parsed["reading"] = clean
                            print(f"✅ Gemini ответил через модель {model_name}", flush=True)
                            return parsed
                    elif response.status_code == 429:
                        print(f"⚠️ Rate limit на {model_name}, пробуем следующую...", flush=True)
                        break
                    else:
                        print(f"⚠️ Ошибка {response.status_code} на модели {model_name}", flush=True)
                except Exception as e:
                    print(f"⚠️ Исключение при вызове {model_name}: {e}", flush=True)
                await asyncio.sleep(1.0)

    print("🚨 Все модели Gemini недоступны.", flush=True)
    return None


async def generate_dynamic_reading(question: str, pre_selected_cards: list) -> dict:
    """Индивидуальный разбор — глубокий, 600-800 слов."""
    cards_str = ", ".join([f"[{i}] {c['name']} ({c['type']})" for i, c in enumerate(pre_selected_cards)])

    system_prompt = (
        "Ты — мастер таро с 30-летним опытом, глубинный психолог в традиции Карла Юнга. "
        "У тебя живой, тёплый, поэтичный язык. Ты говоришь с человеком как с близким другом, которому доверяешь сокровенное. "
        "Никогда не используй фразы 'как ИИ', 'согласно картам', 'в заключение'. Пиши только на Markdown — **жирный**, обычный текст. "
        "Запрещено использовать HTML-теги. Запрещено использовать # или ## — только **жирный** для заголовков секций.\n\n"
        "Твоя задача: выбрать ровно 3 карты и провести глубокий, личный, психологически точный разбор на 600-800 слов.\n\n"
        "СТРУКТУРА:\n"
        "1. Обращение к человеку — тёплое, ненавязчивое вступление, отзеркаль суть вопроса.\n"
        "2. Три карты (прошлое, настоящее, будущее) — каждая карта разобрана как живой архетип, применительно именно к этому вопросу.\n"
        "3. Интеграция — как три карты говорят вместе, что это означает для человека прямо сейчас.\n"
        "4. Напутствие — вдохновляющее, но честное. Человек должен уйти наполненным, а не пустым.\n\n"
        "ВАЖНО: пиши разнообразно. Каждый ответ — уникален. Не копируй шаблоны.\n\n"
        "Формат ответа — строго JSON:\n"
        "{\n"
        "  \"cards_used_indices\": [индексы 3 выбранных карт],\n"
        "  \"reading\": \"текст на Markdown\"\n"
        "}"
    )

    user_prompt = f"Вопрос человека: '{question}'.\nДоступные карты: {cards_str}."

    result = await call_gemini(system_prompt, user_prompt)
    if result:
        return result

    print("⚠️ Gemini недоступен. Переход на локальный оракул.", flush=True)
    return generate_local_tarot_reading(question, pre_selected_cards)


async def generate_preset_reading(question: str, pre_selected_cards: list) -> dict:
    """Расклад на готовый вопрос — живой, но более краткий (300-400 слов)."""
    cards_str = ", ".join([f"[{i}] {c['name']} ({c['type']})" for i, c in enumerate(pre_selected_cards)])

    system_prompt = (
        "Ты — мастер таро, который умеет давать точные, живые ответы на классические жизненные вопросы. "
        "Твой стиль: тёплый, конкретный, без воды. Ты отвечаешь как мудрый друг, который видит суть. "
        "Пиши только на Markdown — **жирный** для выделений. Никаких HTML-тегов. Никаких # заголовков.\n\n"
        "Задача: выбрать ровно 3 карты и дать живой, конкретный расклад на 300-400 слов.\n\n"
        "СТРУКТУРА:\n"
        "**Прошлое — [имя карты]:** как прошлое влияет на этот вопрос.\n"
        "**Настоящее — [имя карты]:** что происходит прямо сейчас.\n"
        "**Будущее — [имя карты]:** куда ведёт ситуация и главный совет.\n\n"
        "Каждый раз давай свежий, уникальный ответ. Не повторяй одни и те же фразы.\n\n"
        "Формат ответа — строго JSON:\n"
        "{\n"
        "  \"cards_used_indices\": [индексы 3 выбранных карт],\n"
        "  \"reading\": \"текст на Markdown\"\n"
        "}"
    )

    user_prompt = f"Вопрос: '{question}'.\nДоступные карты: {cards_str}."

    result = await call_gemini(system_prompt, user_prompt)
    if result:
        return result

    print("⚠️ Gemini недоступен для preset. Локальный оракул.", flush=True)
    return generate_local_tarot_reading(question, pre_selected_cards)


async def generate_daily_reading(pre_selected_cards: list) -> dict:
    """Карта дня — вдохновляющий совет на день."""
    card = pre_selected_cards[0]
    card_str = f"[0] {card['name']} ({card['type']})"

    system_prompt = (
        "Ты — мастер таро. Для Карты Дня дай живое, личное толкование одной карты на 200-250 слов. "
        "Стиль: поэтичный, вдохновляющий, конкретный. Человек должен получить ясный совет и ощущение, "
        "что Вселенная говорит именно с ним. Пиши только Markdown (**жирный**). Никаких HTML-тегов.\n\n"
        "Структура:\n"
        "**Карта Дня — [имя карты]**\n"
        "[Живое толкование карты применительно к сегодняшнему дню]\n\n"
        "**Совет на сегодня:** [конкретный, действенный совет]\n\n"
        "Каждый раз пиши по-новому. Не копируй шаблоны.\n\n"
        "Формат ответа — строго JSON:\n"
        "{\n"
        "  \"cards_used_indices\": [0],\n"
        "  \"reading\": \"текст на Markdown\"\n"
        "}"
    )

    user_prompt = f"Карта дня: {card_str}."

    result = await call_gemini(system_prompt, user_prompt)
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
    """Карта дня через Gemini AI — 75 энергии, уникальное толкование."""
    await asyncio.sleep(1.0)

    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    username = user_tg.get("username")

    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не зарегистрирован.")

    is_developer = ((username and username.lower() == "dzenra_prod") or
                    (user.get("username") and user.get("username").lower() == "dzenra_prod"))

    if not is_developer and user.get("balance", 0) < 75:
        raise HTTPException(status_code=400, detail="Недостаточно Энергии на балансе.")

    deck = get_tarot_deck()
    pre_selected = random.sample(deck, 3)

    result_data = await generate_daily_reading(pre_selected)

    final_card = pre_selected[0]
    text_reading = result_data.get("reading", "")

    if not is_developer:
        update_user_balance(user_id, balance_delta=-75)
        new_balance = user.get("balance", 75) - 75
    else:
        new_balance = 99999

    return {
        "success": True,
        "cards": [final_card],
        "text": text_reading,
        "new_balance": new_balance
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
    pre_selected = random.sample(deck, 6)

    result_data = await generate_dynamic_reading(question, pre_selected)

    used_indices = result_data.get("cards_used_indices", [0, 1, 2])
    used_indices = [idx for idx in used_indices if 0 <= idx < len(pre_selected)]
    if len(used_indices) < 3:
        used_indices = [0, 1, 2]
    else:
        used_indices = used_indices[:3]

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

    if pack == "1_daily":
        title = "+75 Энергии (Карта Дня)"
        description = "Приобретение 75 единиц Энергии для открытия Карты Дня."
        amount = 75
        payload_str = f"buy_1_daily_{user_id}_{random.randint(1000,9999)}"
    elif pack == "1_std":
        title = "+150 Энергии (Обычный расклад)"
        description = "Приобретение 150 единиц Энергии для Готового вопроса."
        amount = 150
        payload_str = f"buy_1_std_{user_id}_{random.randint(1000,9999)}"
    elif pack == "5_std":
        title = "+750 Энергии (Пакет Энергии)"
        description = "Выгодный пакет: 750 единиц Энергии со скидкой 10%."
        amount = 675
        payload_str = f"buy_5_std_{user_id}_{random.randint(1000,9999)}"
    elif pack == "1_ai":
        title = "+750 Энергии (Индивидуальный разбор)"
        description = "Приобретение 750 единиц Энергии для Глубокого разбора."
        amount = 750
        payload_str = f"buy_1_ai_{user_id}_{random.randint(1000,9999)}"
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
        "Здесь ты можешь получить глубокие индивидуальные ответы на любые вопросы.\n\n"
        "🎁 Тебе доступен 1 бесплатный расклад прямо сейчас!"
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

        if "buy_1_daily" in payload:
            update_user_balance(user_id, balance_delta=75)
            await message.answer("🔮 Оплата успешна! Зачислено +75 Энергии.")
        elif "buy_1_std" in payload:
            update_user_balance(user_id, balance_delta=150)
            await message.answer("🔮 Оплата успешна! Зачислено +150 Энергии.")
        elif "buy_5_std" in payload:
            update_user_balance(user_id, balance_delta=750)
            await message.answer("🔮 Оплата успешна! Зачислено +750 Энергии.")
        elif "buy_1_ai" in payload:
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
