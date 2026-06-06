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

# Инициализация веб-сервера FastAPI и Telegram-бота
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
# РАБОТА С ГИБРИДНОЙ БАЗОЙ ДАННЫХ (SUPABASE + SQLITE FALLBACK)
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
        print(f"Прямое подключение к базе данных отклонено: {e}. Пробуем пуллер...", flush=True)

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
        print(f"Ошибка пуллера баз данных: {e_pool}", flush=True)

    print("⚠️ Облачная СУБД недоступна. Переход на локальный SQLite!", flush=True)
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
        print(f"Ошибка инициализации базы данных: {e}", flush=True)
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
# СТРУКТУРА КАРТ ТАРО И ГЛУБОКАЯ БАЗА ЗНАЧЕНИЙ ДЛЯ ФОЛБЕКА
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
    """Глубокий генератор значений карт, если Gemini недоступен. Никаких сухих отписок."""
    
    major_meanings = {
        "Дурак": "Эта энергия призывает к абсолютному доверию. Вы стоите на пороге чего-то совершенно нового. Отбросьте прошлый опыт, который отягощает вас. Позвольте себе легкость и спонтанность — Вселенная сейчас страхует вас.",
        "Маг": "В ваших руках сейчас сосредоточены все необходимые ресурсы. Это время активного творения, а не ожидания. Проявите силу воли и заявите о своих намерениях миру — ваша реальность податлива как глина.",
        "Верховная Жрица": "Замрите. Суета сейчас ваш враг. Ответы, которые вы ищете, уже находятся внутри вас. Обратите внимание на сны, случайные знаки и интуитивные озарения. Скрытое скоро станет явным.",
        "Императрица": "Период мощного созидания, плодородия и изобилия. Позвольте процессам развиваться естественно, как растет цветок. Окружите себя заботой, красотой и любовью — именно из этого состояния придут лучшие результаты.",
        "Император": "Время взять ответственность на себя. Хаос должен быть структурирован. Опирайтесь на логику, дисциплину и четкие границы. Защищайте свои интересы твердо, но справедливо.",
        "Иерофант": "Ситуация требует обращения к проверенным истинам, традициям или мудрому наставнику. Ищите смысл, а не поверхностную выгоду. Поступайте так, как велит совесть, даже если это кажется сложным.",
        "Влюбленные": "Аркан глубокого выбора, совершаемого сердцем. Это не просто партнерство, это необходимость интегрировать противоречивые части себя. Выбирайте искренне, отбросив страхи — и союз будет благословенным.",
        "Колесница": "Динамика, прорыв и триумф воли. Вы столкнулись с противоречивыми силами, но если возьмете управление на себя и не потеряете фокус, победа будет стремительной. Не время сомневаться — время действовать.",
        "Сила": "Истинная сила кроется не в давлении, а в мягкости, эмпатии и внутреннем стержне. Укротите своих внутренних демонов любовью и терпением. То, что кажется непреодолимым, сдастся перед вашим спокойствием.",
        "Отшельник": "Остановитесь и уйдите в тишину. Внешний мир сейчас не даст вам ответов. Период самопознания, переоценки ценностей и поиска своего внутреннего света. Ваш путь сейчас — это путь вглубь себя.",
        "Колесо Фортуны": "Все течет и меняется. Ситуация набирает неожиданный оборот, вмешиваются силы судьбы. Примите цикличность происходящего: отпустите контроль, доверьтесь потоку и будьте готовы поймать удачу за хвост.",
        "Справедливость": "Закон кармы в действии. Вы получите ровно то, что посеяли. Требуется абсолютная честность с самим собой, объективность и холодный рассудок. Ищите баланс и поступайте по совести.",
        "Повешенный": "Ситуация зависла, но это не наказание, а пауза для переосмысления. Добровольная жертва малым ради великого. Посмотрите на мир под другим углом — именно в состоянии отказа от старых шаблонов придет озарение.",
        "Смерть": "Не бойтесь этого Аркана. Это глубокая трансформация и естественное завершение отжившего цикла. Старое должно умереть, чтобы освободить место для нового. Отпустите то, за что судорожно держитесь.",
        "Умеренность": "Алхимия души. Время интеграции, исцеления и поиска золотой середины. Никаких крайностей и спешки. Постепенно, капля за каплей, гармония восстанавливается. Проявите терпение.",
        "Дьявол": "Вы столкнулись с мощной теневой энергией. Это могут быть зависимости, созависимые отношения, материальные привязки или ваши собственные страхи, которые сковывают волю. Помните: цепи лишь иллюзия, вы свободны их сбросить.",
        "Башня": "Громоотвод очищения. Ложные структуры, иллюзии и устаревшие убеждения рушатся. Это больно, но необходимо. Башня расчищает фундамент для постройки чего-то настоящего и искреннего. Не сопротивляйтесь переменам.",
        "Звезда": "После бури всегда выходит свет. Аркан исцеления, надежды и высшего покровительства. Вы на верном пути, небеса благоволят вам. Мечтайте смело, вдохновляйтесь и верьте в свое предназначение.",
        "Луна": "Погружение в сумерки подсознания. Ситуация полна неопределенности, иллюзий и скрытых страхов. Не делайте поспешных выводов, вещи не такие, какими кажутся. Доверяйте интуиции и не позволяйте тревоге взять верх.",
        "Солнце": "Абсолютный триумф, радость и ясность. Энергия успеха, творчества и взаимной искренности. Тьма рассеялась. Позвольте себе праздновать жизнь, сиять и делиться этим теплом с окружающими.",
        "Суд": "Кармическое пробуждение. Глубинный зов к перерождению. Время отпустить старые обиды, простить себя и выйти на совершенно новый уровень осознанности. Судьба дает вам шанс начать всё заново, с чистой совестью.",
        "Мир": "Идеальное завершение цикла. Обретение целостности, гармонии и своего места во Вселенной. То, к чему вы стремились, обретает форму. Празднуйте свой путь — границы стерты, перед вами открыт весь мир."
    }

    clean_name = card_name.replace("Старший Аркан: ", "").strip()
    
    if clean_name in major_meanings:
        return major_meanings[clean_name]

    # Генератор для Младших Арканов
    suit_energy = ""
    if "Кубк" in card_name:
        suit_energy = "Энергия воды: чувства, эмоции, интуиция, глубокие привязанности и душевные порывы. Важно слушать сердце."
    elif "Меч" in card_name:
        suit_energy = "Энергия воздуха: интеллект, логика, преодоление иллюзий, анализ и необходимость мыслить ясно и хладнокровно."
    elif "Жезл" in card_name:
        suit_energy = "Энергия огня: страсть, амбиции, карьера, самореализация, искра творения и активные действия."
    elif "Пентакл" in card_name:
        suit_energy = "Энергия земли: материальный мир, финансы, здоровье, практичность, стабильность и осязаемые результаты."

    rank_focus = ""
    if "Туз" in card_name: rank_focus = "Чистый импульс, мощный старт, дар свыше и новая возможность, которую нужно хватать."
    elif "Двойка" in card_name: rank_focus = "Поиск баланса, компромисс, двойственность выбора или важное партнерство."
    elif "Тройка" in card_name: rank_focus = "Первые плоды трудов, расширение, творческое взаимодействие и поддержка окружения."
    elif "Четверка" in card_name: rank_focus = "Стабильность, безопасность, но иногда и застой, требующий бережного отношения к ресурсам."
    elif "Пятерка" in card_name: rank_focus = "Кризис, конфликт, выход из зоны комфорта, который необходим для духовного роста."
    elif "Шестерка" in card_name: rank_focus = "Гармония, взаимопомощь, исцеление прошлых ран и светлая ностальгия."
    elif "Семерка" in card_name: rank_focus = "Необходимость проявить стратегию, терпение, оценить риски или защитить свои убеждения."
    elif "Восьмерка" in card_name: rank_focus = "Динамика, скорость, упорный труд и концентрация на достижении мастерства."
    elif "Девятка" in card_name: rank_focus = "Самодостаточность, приближение к идеалу, глубокий внутренний комфорт."
    elif "Десятка" in card_name: rank_focus = "Кульминация, предел развития масти, полнота ощущений или ответственность."
    elif "Паж" in card_name: rank_focus = "Любопытство, новые импульсы, обучение, важная новость или свежий взгляд на вещи."
    elif "Рыцарь" in card_name: rank_focus = "Целеустремленность, стремительное движение, смелость и готовность к переменам."
    elif "Королева" in card_name: rank_focus = "Забота, зрелая эмоциональность, интуиция и умение взращивать проекты в гармонии."
    elif "Король" in card_name: rank_focus = "Авторитет, лидерство, контроль, логика и ответственность за принятые решения."
    else: rank_focus = "Необходимость внимательно прислушаться к текущим вибрациям ситуации."

    return f"{suit_energy} Эта карта несет следующий смысловой акцент: {rank_focus}"

def generate_local_tarot_reading(question: str, pre_selected_cards: list) -> dict:
    used_cards = pre_selected_cards[:3]
    used_indices = [0, 1, 2]
    
    parts = [
        f"Уважаемый искатель, карты услышали ваш вопрос: *«{question}»*.\n\n"
        "Ответ Оракула сформирован на основе древних архетипов. Внимательно вчитайтесь в каждое слово, позвольте интуиции откликнуться.\n\n"
    ]

    positions = [
        "1. Влияние прошлого (Корни ситуации)",
        "2. Вызов настоящего (Ваш фокус сейчас)",
        "3. Вектор будущего (Совет и исход)"
    ]

    for idx, card in enumerate(used_cards):
        name = card["name"]
        pos = positions[idx]
        meaning = get_rich_card_meaning(name, "general")
        parts.append(f"**{pos} — {name}**\n{meaning}\n\n")
        
    parts.append(
        "**Глубокий итог:**\n"
        "Помните, что карты не выносят окончательный приговор, они лишь подсвечивают энергетические токи вашей жизни. "
        "Всё в ваших руках. Интегрируйте этот опыт, доверяйте себе и действуйте из состояния любви и осознанности."
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
# ВЫЗОВ ИИ GEMINI (С ПРЕМИАЛЬНЫМ ПСИХОЛОГИЧЕСКИМ ПРОМПТОМ)
# =====================================================================
async def generate_dynamic_reading(question: str, pre_selected_cards: list) -> dict:
    if not GEMINI_API_KEY:
        print("⚠️ Gemini отключен. Переход к премиальному локальному Оракулу.", flush=True)
        return generate_local_tarot_reading(question, pre_selected_cards)

    model_name = "gemini-2.5-flash-preview-09-2025"
    cards_str = ", ".join([f"[{i}] {c['name']} ({c['type']})" for i, c in enumerate(pre_selected_cards)])
    
    # Полностью переписанный промпт для избежания сухости и роботоподобности
    system_prompt = (
        "Ты — элитный, невероятно эмпатичный таролог и глубинный психолог (в стиле Карла Юнга). "
        "У тебя живой, теплый, метафоричный язык. Никаких сухих фраз вроде 'В заключение' или 'Как ИИ'.\n\n"
        "Пользователь задал сокровенный вопрос. Выбери ровно 3 карты из предложенных (Прошлое, Настоящее, Будущее) "
        "и проведи терапевтичный, глубокий разбор.\n\n"
        "СТРУКТУРА ОТВЕТА (Пиши сплошным красивым текстом, используй Markdown для жирного шрифта, НИКАКИХ HTML-тегов!):\n"
        "1. Введение: Мягко и философски отреагируй на вопрос пользователя.\n"
        "2. Расклад (3 карты): Опиши каждую выбранную карту. Опиши не просто ее классическое значение, а то, как она ИНДИВИДУАЛЬНО отвечает на вопрос пользователя. Работай с подсознанием.\n"
        "3. Напутствие: Вдохновляющий, сильный и успокаивающий итог. Человек должен уйти с чувством наполненности.\n\n"
        "ФОРМАТ ОТВЕТА СТРОГО JSON:\n"
        "{\n"
        "  \"cards_used_indices\": [индексы выбранных 3 карт],\n"
        "  \"reading\": \"текст твоего потрясающего ответа в формате Markdown (никаких html тегов <br> или <h3>)\"\n"
        "}"
    )
    
    user_prompt = (
        f"Вопрос: '{question}'.\n"
        f"Карты: {cards_str}.\n"
    )
    
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
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
    retry_delays = [1.0, 2.0, 4.0]
    
    async with httpx.AsyncClient() as client:
        for attempt, delay in enumerate(retry_delays):
            try:
                response = await client.post(url, json=structured_payload, timeout=30.0)
                if response.status_code == 200:
                    data = response.json()
                    raw_text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                    parsed = extract_json_from_text(raw_text)
                    if parsed and "reading" in parsed:
                        # Удаляем любые случайные HTML теги, если ИИ их все же вставил
                        clean_reading = parsed["reading"].replace("<h3>", "").replace("</h3>", "").replace("<br>", "\n")
                        parsed["reading"] = clean_reading
                        return parsed
            except Exception as e:
                pass
            await asyncio.sleep(delay)
            
    print("🚨 Вызов ИИ исчерпан. Переход на локальный толковать.", flush=True)
    return generate_local_tarot_reading(question, pre_selected_cards)

# =====================================================================
# API ЭНДПОИНТЫ FASTAPI ДЛЯ ФРОНТЕНДА
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

@app.post("/api/user/use-ai-reading")
async def use_ai_reading(payload: dict, authorization: str = Header(None)):
    # ИСКУССТВЕННАЯ ПАУЗА ДЛЯ МАГИИ АНИМАЦИИ (чтобы карты красиво тасовались 2.5 секунды)
    await asyncio.sleep(2.5)

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
    # Гарантируем, что всегда будет 3 карты (если ИИ почему-то отдал меньше/больше)
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
        action = parts[1]
        pack_type = parts[2]
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
        print(f"Ошибка инициализации базы данных на старте: {e}", flush=True)
        
    try:
        render_external_url = os.getenv("RENDER_EXTERNAL_URL")
        if render_external_url:
            webhook_url = f"{render_external_url.strip()}/telegram-webhook"
        else:
            webhook_url = "https://tarot-backend-136l.onrender.com/telegram-webhook".strip()
            
        await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
        print(f"Вебхук Telegram успешно направлен на: {webhook_url}", flush=True)
    except Exception as e:
        print(f"⚠️ Предупреждение: Не удалось установить вебхук: {e}", flush=True)
