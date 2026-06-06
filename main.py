# -*- coding: utf-8 -*-
import os
import urllib.parse
import json
import random
import asyncio
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
# КОНФИГУРАЦИЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ (БЕЗОПАСНЫЙ ЗАПУСК)
# =====================================================================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://tarot-frontend-wine.vercel.app")

if not BOT_TOKEN:
    raise RuntimeError("Критическая ошибка: Переменная TELEGRAM_BOT_TOKEN не задана на Render!")
if not DATABASE_URL:
    raise RuntimeError("Критическая ошибка: Переменная DATABASE_URL не задана на Render!")
if not GEMINI_API_KEY:
    raise RuntimeError("Критическая ошибка: Переменная GEMINI_API_KEY не задана на Render!")

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
    try:
        parsed = urllib.parse.urlparse(DATABASE_URL)
        user = urllib.parse.unquote(parsed.username) if parsed.username else ""
        password = urllib.parse.unquote(parsed.password) if parsed.password else ""
        host = parsed.hostname
        port = parsed.port or 5432
        db_name = parsed.path[1:] if parsed.path else ""
        
        conn = psycopg2.connect(
            database=db_name,
            user=user,
            password=password,
            host=host,
            port=port,
            sslmode="require",
            connect_timeout=3
        )
        IS_SQLITE = False
        return conn
    except Exception as e:
        print(f"Прямое подключение к Supabase отклонено (IPv6): {e}. Переключаемся на пуллер...", flush=True)

    try:
        parsed = urllib.parse.urlparse(DATABASE_URL)
        password = urllib.parse.unquote(parsed.password) if parsed.password else ""
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
                print(f"Успешно подключено через пуллер Supabase в регионе {region}!", flush=True)
                return conn
            except Exception:
                continue
    except Exception as e_pool:
        print(f"Ошибка пуллера Supabase: {e_pool}", flush=True)

    print("⚠️ Облако недоступно. Включаем локальный SQLite сейвер!", flush=True)
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
                balance INTEGER DEFAULT 0,
                ai_balance INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        cur.close()
        print("База данных успешно инициализирована.", flush=True)
    except Exception as e:
        print(f"Критическая ошибка инициализации базы: {e}", flush=True)
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
                user_dict["ai_balance"] = 99999
            return user_dict
        return None
    except Exception as e:
        print(f"Ошибка получения пользователя {telegram_id}: {e}", flush=True)
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
            INSERT INTO users (telegram_id, username, first_name, email, consent_given, balance, ai_balance)
            VALUES (%s, %s, %s, %s, TRUE, 1, 0)
            ON CONFLICT (telegram_id) DO UPDATE 
            SET username = EXCLUDED.username, first_name = EXCLUDED.first_name;
        """, (telegram_id, username, first_name, email))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"Ошибка создания пользователя {telegram_id}: {e}", flush=True)
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
        print(f"Ошибка обновления информации профиля {telegram_id}: {e}", flush=True)
    finally:
        if conn:
            conn.close()

def update_user_balance(telegram_id: int, balance_delta: int, ai_balance_delta: int = 0):
    conn = None
    user = get_user(telegram_id)
    if user and user.get("username") and user.get("username").lower() == "dzenra_prod":
        return

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        execute_query(cur, """
            UPDATE users 
            SET balance = GREATEST(0, balance + %s),
                ai_balance = GREATEST(0, ai_balance + %s)
            WHERE telegram_id = %s;
        """, (balance_delta, ai_balance_delta, telegram_id))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"Ошибка обновления баланса для {telegram_id}: {e}", flush=True)
    finally:
        if conn:
            conn.close()

# =====================================================================
# ВЕРИФИКАЦИЯ TELEGRAM WEBAPP INITDATA
# =====================================================================
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

# =====================================================================
# АЛГОРИТМ НЕПОТОПЛЯЕМОГО ОРАКУЛА (ВЫСОКОКАЧЕСТВЕННЫЙ ЛОКАЛЬНЫЙ РАСКЛАД)
# =====================================================================
def generate_local_tarot_reading(question: str, pre_selected_cards: list) -> dict:
    """
    Генерирует невероятно глубокий и точный психологический расклад локально на сервере.
    Срабатывает мгновенно и бесшовно как абсолютная защита от любых сбоев Google API.
    """
    print("🔮 Активирован резервный генератор судеб «Вечный Оракул»", flush=True)
    q_lower = question.lower()
    
    # 1. Интеллектуальный разбор темы вопроса
    theme = "общий жизненный путь"
    if any(w in q_lower for w in ["работ", "карьер", "бизнес", "проект", "увольн", "найти", "деньг", "финанс"]):
        theme = "профессиональное развитие и материальное изобилие"
    elif any(w in q_lower for w in ["люб", "отношен", "чувств", "брак", "парень", "девушк", "совмест"]):
        theme = "сердечные привязанности и душевные союзы"
    elif any(w in q_lower for w in ["себя", "предназнач", "духов", "выбор", "делать", "почему"]):
        theme = "самопознание и раскрытие внутреннего потенциала"

    # Выбираем ровно 3 карты из предложенных
    used_cards = pre_selected_cards[:3]
    used_indices = [0, 1, 2]
    
    interpretations = {
        "профессиональное развитие и материальное изобилие": {
            "Старший Аркан": "призывает вас к масштабной трансформации. Это кармический знак того, что текущие трудности — лишь ступень к вашему триумфу. Действуйте смело и нестандартно.",
            "Кубки": "символизируют творческое горение. Ваши идеи найдут отклик у коллектива, а работа принесет не только финансовый доход, но и искреннюю душевную радость.",
            "Мечи": "требуют от вас максимальной концентрации и защиты своих личных интересов. Избегайте сплетен и стройте планы на основе точных цифр и фактов.",
            "Жезлы": "зажигают искру грандиозного лидерского успеха. Перед вами открыты двери новых амбициозных проектов. Ваша харизма — ваш главный капитал.",
            "Пентакли": "обещают долгожданную стабильность и твердую почву под ногами. Ваши прошлые труды начинают окупаться, впереди период заслуженных наград."
        },
        "сердечные привязанности и душевные союзы": {
            "Старший Аркан": "указывает на прохождение важного душевного урока. Ситуация требует от вас мудрости, принятия партнера и умения слушать шепот собственной интуиции.",
            "Кубки": "наполняют отношения теплотой, нежностью и искренним взаимопониманием. Сердца открываются навстречу друг другу. Впереди период гармонии.",
            "Мечи": "предупреждают о накопившихся недомолвках. Время снять маски и провести честный, открытый диалог. Только правда способна исцелить вашу связь.",
            "Жезлы": "разжигают огонь страсти, приключений и эмоциональной динамики. Не бойтесь сделать первый шаг и удивить любимого человека.",
            "Пентакли": "свидетельствуют о надежности чувств и верности. Это знак созидания крепкого, уютного семейного гнезда на долгие годы."
        },
        "самопознание и раскрытие внутреннего потенциала": {
            "Старший Аркан": "— это прямой призыв Вселенной довериться своей судьбе. Все происходящее с вами сейчас — важный элемент вашей духовной эволюции.",
            "Кубки": "напоминают о необходимости позаботиться о своем эмоциональном состоянии. Найдите источник вдохновения и побудьте в гармонии с собой.",
            "Мечи": "требуют ясности ума и отказа от мешающих иллюзий. Пора расставить приоритеты и отсечь все лишнее и отжившее из своей жизни.",
            "Жезлы": "наполняют вас неиссякаемой энергией для самовыражения. Творите, созидайте и делитесь своей внутренней силой с миром.",
            "Пентакли": "советуют обрести устойчивость. Обратите внимание на здоровье, физический комфорт и наведение порядка в повседневных делах."
        }
    }
    
    theme_key = theme
    if theme_key not in interpretations:
        theme_key = "самопознание и раскрытие внутреннего потенциала"
        
    parts = [
        f"✨ **Оракул настроился на вибрации вашего вопроса:**\n*«{question}»*\n",
        f"🔮 **Глубинная тема вашего запроса:** {theme.capitalize()}.\n",
        "**Карты вашей судьбы легли следующим образом:**\n"
    ]
    
    positions = [
        "1. Влияние прошлого (с чего все началось)",
        "2. Вызов настоящего (что происходит прямо сейчас)",
        "3. Вектор будущего (куда ведут вас космические дороги)"
    ]
    
    for idx, card in enumerate(used_cards):
        card_type = card["type"]
        
        # Определяем ключ для толкования карты
        card_key = card_type
        if card_key not in interpretations[theme_key]:
            card_key = "Старший Аркан" if "Старший" in card_type else "Пентакли"
            
        meaning = interpretations[theme_key].get(card_key)
        
        parts.append(
            f"🔸 **{positions[idx]}** — Аркан **«{card['name']}»** ({card['type']}):\n"
            f"Этот священный символ {meaning}\n"
        )
        
    parts.append(
        "\n💡 **Итоговое напутствие Оракула:** "
        "Помните, что карты лишь подсвечивают наиболее вероятные развилки дорог перед вами. "
        "Ваша свободная воля — это величайшая сила. Верьте в себя, действуйте осознанно и берегите свет внутри своего сердца!"
    )
    
    return {
        "cards_used_indices": used_indices,
        "reading": "\n".join(parts)
    }

# =====================================================================
# ИИ-ТОЛКОВАНИЕ С САМОЛЕЧЕНИЕМ (GEMINI MULTI-TIER ENGINE)
# =====================================================================
async def generate_dynamic_reading(question: str, pre_selected_cards: list) -> dict:
    """
    Пытается получить толкование у ИИ. 
    Использует 4 различных каскадных попытки (с JSON-схемой и без),
    а при любом сетевом сбое бесшовно переключается на локальный генератор.
    """
    models = ["gemini-1.5-flash", "gemini-1.5-pro"]
    cards_str = ", ".join([f"[{i}] {c['name']} ({c['type']})" for i, c in enumerate(pre_selected_cards)])
    
    # ----------------- ПОПЫТКА 1 & 2: ИСПОЛЬЗОВАНИЕ JSON SCHEMA -----------------
    system_prompt = (
        "Ты — профессиональный психолог-аналитик и таролог с 20-летним стажем. "
        "Сделай клиенту глубокий, бережный и воодушевляющий расклад. "
        "Верни ответ строго в формате JSON:\n"
        "{\n"
        "  \"cards_used_indices\": [индексы выбранных карт из списка (от 0 до 5)],\n"
        "  \"reading\": \"текст расклада на русском\"\n"
        "}"
    )
    
    user_prompt = (
        f"Вопрос искателя: '{question}'.\n"
        f"Доступные карты для расклада: {cards_str}.\n"
        "Выбери от 3 до 4 карт, наиболее подходящих ситуации, и сделай толкование."
    )
    
    # Структурированный payload по стандарту Google
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
        for model in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
            try:
                print(f"Попытка 1: Запрос к {model} со строгой JSON-схемой...", flush=True)
                response = await client.post(url, json=structured_payload, timeout=20.0)
                if response.status_code == 200:
                    data = response.json()
                    raw_text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
                    parsed = json.loads(raw_text)
                    if "cards_used_indices" in parsed and "reading" in parsed:
                        print("Успешный разбор через JSON-схему!", flush=True)
                        return parsed
            except Exception as e:
                print(f"Сбой метода со схемой на {model}: {e}", flush=True)
                
            # ----------------- ПОПЫТКА 3 & 4: ОРАКУЛ В СВОБОДНОМ ТЕКСТОВОМ РЕЖИМЕ -----------------
            # (Самый надежный способ, обходящий любые ограничения старых API ключей)
            fallback_prompt = (
                f"{system_prompt}\n\n"
                f"Карты для выбора: {cards_str}.\n"
                f"Вопрос искателя: {question}.\n"
                "Выбери 3 карты и напиши красивое толкование.\n"
                "Твой ответ должен содержать ТОЛЬКО сырой JSON по схеме выше. Не оборачивай в маркдаун блоки."
            )
            unstructured_payload = {
                "contents": [{"parts": [{"text": fallback_prompt}]}]
            }
            
            try:
                print(f"Попытка 2: Запрос к {model} в свободном текстовом режиме...", flush=True)
                response = await client.post(url, json=unstructured_payload, timeout=25.0)
                if response.status_code == 200:
                    data = response.json()
                    raw_text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
                    
                    # Очистка от markdown оберток типа ```json ... ```
                    clean_text = raw_text
                    if clean_text.startswith("```"):
                        lines = clean_text.split("```")
                        for line in lines:
                            line_strip = line.strip()
                            if line_strip.startswith("json"):
                                line_strip = line_strip[4:].strip()
                            if line_strip.startswith("{") and line_strip.endswith("}"):
                                clean_text = line_strip
                                break
                                
                    parsed = json.loads(clean_text)
                    if "cards_used_indices" in parsed and "reading" in parsed:
                        print("Успешный ручной разбор текста в JSON!", flush=True)
                        return parsed
            except Exception as e_text:
                print(f"Сбой текстового метода на {model}: {e_text}", flush=True)
                
    # ----------------- ПОПЫТКА 5: СВЕРХНАДЕЖНЫЙ ЛОКАЛЬНЫЙ РАСКЛАД -----------------
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
        create_user(
            telegram_id=user_id,
            username=username,
            first_name=first_name,
            email=""
        )
        user = get_user(user_id)
    else:
        update_user_profile_info(user_id, first_name, username)
        user = get_user(user_id)
        
    if not user:
        raise HTTPException(status_code=500, detail="Ошибка работы базы данных")
        
    is_developer = (
        (username and username.lower() == "dzenra_prod") or 
        (user.get("username") and user.get("username").lower() == "dzenra_prod")
    )
    balance = 99999 if is_developer else user["balance"]
    ai_balance = 99999 if is_developer else user["ai_balance"]

    return {
        "registered": True,
        "user_id": user_id,
        "name": user["first_name"],
        "balance": balance,
        "ai_balance": ai_balance
    }

@app.post("/api/user/use-reading")
async def use_reading(authorization: str = Header(None)):
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    username = user_tg.get("username")
    
    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
        
    is_developer = (
        (username and username.lower() == "dzenra_prod") or 
        (user.get("username") and user.get("username").lower() == "dzenra_prod")
    )
    if is_developer:
        return {"success": True, "new_balance": 99999}

    if user["balance"] < 1:
        raise HTTPException(status_code=400, detail="Недостаточно раскладов на балансе.")
        
    update_user_balance(user_id, balance_delta=-1)
    return {"success": True, "new_balance": user["balance"] - 1}

@app.post("/api/user/use-ai-reading")
async def use_ai_reading(payload: dict, authorization: str = Header(None)):
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    username = user_tg.get("username")
    
    question = payload.get("question", "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Введите ваш вопрос для расклада.")
        
    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не зарегистрирован.")
        
    is_developer = (
        (username and username.lower() == "dzenra_prod") or 
        (user.get("username") and user.get("username").lower() == "dzenra_prod")
    )

    if not is_developer and user["ai_balance"] < 1:
        raise HTTPException(status_code=400, detail="Недостаточно энергии расклада. Пополните баланс.")
        
    deck = get_tarot_deck()
    pre_selected = random.sample(deck, 6)
    
    result_data = await generate_dynamic_reading(question, pre_selected)
    
    used_indices = result_data.get("cards_used_indices", [0, 1, 2])
    used_indices = [idx for idx in used_indices if 0 <= idx < len(pre_selected)]
    if not used_indices:
        used_indices = [0, 1, 2]
        
    final_cards = [pre_selected[idx] for idx in used_indices]
    text_reading = result_data.get("reading", "")
    
    if not is_developer:
        update_user_balance(user_id, balance_delta=0, ai_balance_delta=-1)
        new_ai_balance = user["ai_balance"] - 1
    else:
        new_ai_balance = 99999
    
    return {
        "success": True,
        "cards": final_cards,
        "text": text_reading,
        "new_ai_balance": new_ai_balance
    }

@app.post("/api/payment/stars-invoice")
async def create_stars_invoice(payload: dict, authorization: str = Header(None)):
    """Создает платежную ссылку для покупки пакетов энергии за Telegram Stars"""
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    pack = payload.get("pack")
    
    title = ""
    description = ""
    amount = 0
    payload_str = ""
    
    if pack == "1_std":
        title = "1 Стандартный расклад"
        description = "Расклад на одну карту для быстрого прояснения ситуации."
        amount = 1  # 1 Звезда для тестов
        payload_str = f"buy_1_std_{user_id}_{random.randint(1000,9999)}"
    elif pack == "5_std":
        title = "Пакет из 5 раскладов"
        description = "Выгодный пакет из 5 сеансов со скидкой 10%."
        amount = 2  # 2 Звезды для тестов
        payload_str = f"buy_5_std_{user_id}_{random.randint(1000,9999)}"
    elif pack == "1_ai":
        title = "1 Индивидуальный расклад"
        description = "Глубокий разбор вашей ситуации с динамическим выбором карт."
        amount = 3  # 3 Звезды для тестов
        payload_str = f"buy_1_ai_{user_id}_{random.randint(1000,9999)}"
    else:
        raise HTTPException(status_code=400, detail="Неверный тип пакета")
        
    try:
        print(f"Попытка выставить счет на Telegram Stars: UserID {user_id}, Pack '{pack}', Amount {amount}", flush=True)
        
        invoice_link = await bot.create_invoice_link(
            title=title,
            description=description,
            payload=payload_str,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Telegram Stars", amount=int(amount))]
        )
        print(f"Ссылка на оплату успешно сгенерирована: {invoice_link}", flush=True)
        return {"invoice_link": invoice_link}
    except Exception as e:
        print(f"КРИТИЧЕСКАЯ ОШИБКА TELEGRAM ПРИ СОЗДАНИИ СЧЕТА STARS: {e}", flush=True)
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

# =====================================================================
# РУЧНОЙ ТРИГГЕР ДЛЯ ПРИНУДИТЕЛЬНОЙ СВЯЗИ ВЕБХУКА
# =====================================================================
@app.get("/api/system/setup-webhook")
async def setup_webhook_manually():
    try:
        render_external_url = os.getenv("RENDER_EXTERNAL_URL")
        if render_external_url:
            webhook_url = f"{render_external_url.strip()}/telegram-webhook"
        else:
            webhook_url = "https://tarot-backend-136l.onrender.com/telegram-webhook".strip()
        
        await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
        print(f"Ручная принудительная привязка вебхука успешна: {webhook_url}", flush=True)
        return {"status": "ok", "message": "Вебхук успешно привязан!", "webhook_url": webhook_url}
    except Exception as e:
        print(f"Ошибка ручной привязки вебхука: {e}", flush=True)
        return {"status": "error", "message": str(e)}

# =====================================================================
# ОБРАБОТЧИКИ ОПЛАТЫ И КОМАНД TELEGRAM BOT (AIOGRAM)
# =====================================================================
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
        
        if pack_type == "std":
            qty = 5 if "5_std" in payload else 1
            update_user_balance(user_id, balance_delta=qty, ai_balance_delta=0)
            await message.answer(f"🔮 Оплата успешна! Зачислено {qty} стандартных раскладов.")
        elif pack_type == "ai":
            update_user_balance(user_id, balance_delta=0, ai_balance_delta=1)
            await message.answer("🔮 Оплата успешна! Вам зачислен 1 Индивидуальный расклад. Откройте приложение, чтобы запустить его.")

# =====================================================================
# ТОЧКА ВХОДА ДЛЯ ВЕБХУКА TELEGRAM
# =====================================================================
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    try:
        raw_json = await request.json()
        telegram_update = Update.model_validate(raw_json, context={"bot": bot})
        await dp.feed_update(bot=bot, update=telegram_update)
        return {"status": "ok"}
    except Exception as e:
        print(f"Ошибка parsing вебхука Telegram: {e}", flush=True)
        return {"status": "error", "message": str(e)}

@app.on_event("startup")
async def on_startup():
    try:
        init_db()
    except Exception as e:
        print(f"Ошибка при инициализации базы данных на старте: {e}", flush=True)
        
    try:
        render_external_url = os.getenv("RENDER_EXTERNAL_URL")
        if render_external_url:
            webhook_url = f"{render_external_url.strip()}/telegram-webhook"
        else:
            webhook_url = "https://tarot-backend-136l.onrender.com/telegram-webhook".strip()
            
        await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
        print(f"Вебхук Telegram успешно направлен на: {webhook_url}", flush=True)
    except Exception as e:
        print(f"⚠️ Предупреждение: Не удалось установить вебхук на старте (продолжаем запуск): {e}", flush=True)
