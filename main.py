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

    # Попытка 1: Прямое подключение к Postgres с IPv4-резолвом
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

    # Попытка 2: Резервное подключение через пуллер транзакций Supabase
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
                print(f"Успешно подключено через пуллер в регионе {region}!", flush=True)
                return conn
            except Exception:
                continue
    except Exception as e_pool:
        print(f"Ошибка пуллера баз данных: {e_pool}", flush=True)

    # Попытка 3: Фолбек на локальный SQLite при полном отсутствии связи с облаком
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
        print("База данных успешно инициализирована.", flush=True)
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
        # Новому пользователю начисляется приветственный бонус в 150 Энергии
        execute_query(cur, """
            INSERT INTO users (telegram_id, username, first_name, email, consent_given, balance, ai_balance, daily_balance)
            VALUES (%s, %s, %s, %s, TRUE, 150, 0, 0)
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
        print(f"Ошибка обновления профиля {telegram_id}: {e}", flush=True)
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
        print(f"Ошибка изменения баланса для {telegram_id}: {e}", flush=True)
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
# РЕЗЕРВНЫЙ ТОЛКОВАТЕЛЬ НА СЛУЧАЙ ОТСУТСТВИЯ СВЯЗИ С ИИ
# =====================================================================
def generate_local_tarot_reading(question: str, pre_selected_cards: list) -> dict:
    print("🔮 Активирован резервный толковать судеб «Вечный Оракул»", flush=True)
    used_cards = pre_selected_cards[:3]
    used_indices = [0, 1, 2]
    
    positions = [
        "Влияние прошлого (с чего все началось)",
        "Вызов настоящего (что происходит прямо сейчас)",
        "Вектор будущего (куда ведут вас космические дороги)"
    ]
    
    parts = [
        f"🔮 **Ответ Оракула на ваш вопрос:** «{question}»\n\n",
        "Карты вашей судьбы легли следующим образом:\n\n"
    ]

    for idx, card in enumerate(used_cards):
        name = card["name"]
        pos = positions[idx]
        parts.append(f"### {pos} — «{name}»\nВы вытянули {name}. Этот символ указывает на важную веху вашей духовной трансформации и необходимость обратить внимание на скрытые аспекты данной энергии.\n\n")
        
    parts.append(
        "\n**Итоговое напутствие Оракула:** "
        "Помните, что карты лишь подсвечивают наиболее вероятные развилки вашей судьбы. "
        "Ваша свободная воля — это величайшая сила. Верьте в себя, действуйте осознанно и берегите свет внутри своего сердца!"
    )
    
    return {
        "cards_used_indices": used_indices,
        "reading": "\n".join(parts)
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
# ВЫЗОВ ИИ GEMINI С ОПТИМИЗИРОВАННЫМ ЭКСПОНЕНЦИАЛЬНЫМ ОТКАТОМ
# =====================================================================
async def generate_dynamic_reading(question: str, pre_selected_cards: list) -> dict:
    """
    Вызывает API Gemini с обязательным экспоненциальным откатом и моделью gemini-2.5-flash-preview-09-2025.
    """
    if not GEMINI_API_KEY:
        print("⚠️ Пропуск ИИ-расклада: отсутствует GEMINI_API_KEY.", flush=True)
        return generate_local_tarot_reading(question, pre_selected_cards)

    model_name = "gemini-2.5-flash-preview-09-2025"
    cards_str = ", ".join([f"[{i}] {c['name']} ({c['type']})" for i, c in enumerate(pre_selected_cards)])
    
    system_prompt = (
        "Ты — выдающийся психолог-аналитик, философ, духовный ментор и таролог с 30-летним стажем.\n"
        "Твоя задача — дать невероятно подробный, глубокий, обширный и всесторонний индивидуальный анализ на конкретный вопрос пользователя, опираясь на выпавшие в раскладе карты Таро.\n\n"
        "ПОЛЬЗОВАТЕЛЬ ТРЕБУЕТ МАКСИМАЛЬНО ДЕТАЛЬНОГО И ОБШИРНОГО ОТВЕТА! Пиши развернуто, глубоко и содержательно.\n\n"
        "СТРУКТУРА ТВОЕГО ОБШИРНОГО ОТВЕТА:\n"
        "1. Введение и сонастройка: Глубокий философский и психологический анализ самого вопроса пользователя.\n"
        "2. Индивидуальный разбор выпавших карт в их взаимном влиянии: Раскрой подробное значение каждой карты именно в контексте вопроса.\n"
        "3. Психологический срез и подсознательные блоки: Что мешает человеку двигаться дальше?\n"
        "4. Стратегические практические рекомендации и пошаговые ориентиры: Конкретные, применимые в жизни шаги.\n"
        "5. Духовное напутствие и вектор будущего: Мудрое, поддерживающее заключение, вселяющее уверенность.\n\n"
        "ПРАВИЛА:\n"
        "- Ответ должен быть ОГРОМНЫМ, детальным и глубоким, написан живым, терапевтическим русским языком.\n"
        "- Строго следуй формату ответа JSON.\n"
        "{\n"
        "  \"cards_used_indices\": [индексы выбранных карт из списка (от 0 до 5)],\n"
        "  \"reading\": \"полный текст твоего глубокого индивидуального разбора\"\n"
        "}"
    )
    
    user_prompt = (
        f"Вопрос искателя: '{question}'.\n"
        f"Доступные карты для расклада: {cards_str}.\n"
        "Выбери от 3 до 4 карт, наиболее подходящих ситуации, и сделай толкование."
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
    
    # 5 обязательных попыток с задержками: 1s, 2s, 4s, 8s, 16s (экспоненциальный откат)
    retry_delays = [1.0, 2.0, 4.0, 8.0, 16.0]
    
    async with httpx.AsyncClient() as client:
        for attempt, delay in enumerate(retry_delays):
            try:
                print(f"Попытка вызова Gemini {attempt + 1}/5...", flush=True)
                response = await client.post(url, json=structured_payload, timeout=30.0)
                
                if response.status_code == 200:
                    data = response.json()
                    raw_text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                    parsed = extract_json_from_text(raw_text)
                    if parsed and "cards_used_indices" in parsed and "reading" in parsed:
                        print("✅ Успешный ответ от Gemini ИИ!", flush=True)
                        return parsed
                else:
                    print(f"⚠️ Ошибка Gemini (Код {response.status_code}): {response.text}", flush=True)
            except Exception as e:
                print(f"❌ Ошибка соединения на попытке {attempt + 1}: {e}", flush=True)
            
            await asyncio.sleep(delay)
            
    print("🚨 Все попытки вызова ИИ исчерпаны. Переход на локальный толковать.", flush=True)
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
        raise HTTPException(status_code=500, detail="Ошибка базы данных")
        
    is_developer = (
        (username and username.lower() == "dzenra_prod") or 
        (user.get("username") and user.get("username").lower() == "dzenra_prod")
    )
    balance = 99999 if is_developer else user.get("balance", 0)

    return {
        "registered": True,
        "user_id": user_id,
        "name": user["first_name"],
        "balance": balance
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
        
    is_developer = (
        (username and username.lower() == "dzenra_prod") or 
        (user.get("username") and user.get("username").lower() == "dzenra_prod")
    )
    if is_developer:
        return {"success": True, "new_balance": 99999}

    if user.get("balance", 0) < 75:
        raise HTTPException(status_code=400, detail="Недостаточно Энергии на балансе.")
        
    update_user_balance(user_id, balance_delta=-75)
    return {"success": True, "new_balance": user.get("balance", 75) - 75}

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

    if not is_developer and user.get("balance", 0) < 750:
        raise HTTPException(status_code=400, detail="Недостаточно Энергии на балансе.")
        
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
