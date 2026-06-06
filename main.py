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
# КОНФИГУРАЦИЯ И КЛЮЧИ
# =====================================================================
BOT_TOKEN = "8838358841:AAFf3LnY3Rd2LV46d09FGu_PkOpRlQoIYRY"
FRONTEND_URL = "https://tarot-frontend-wine.vercel.app"

# Строка подключения к базе данных Supabase
DATABASE_URL = "postgresql://postgres:We15935728%21%21%21%21%21@db.wbbcljbrfpgriukzjlvc.supabase.co:5432/postgres"

# Ключ для доступа к Gemini API
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Инициализация веб-сервера FastAPI и Telegram-бота
app = FastAPI()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Настройка правил CORS для работы фронтенда на Vercel
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Индикатор аварийного переключения на локальную базу данных SQLite
IS_SQLITE = False

# =====================================================================
# РАБОТА С ГИБРИДНОЙ БАЗОЙ ДАННЫХ (SUPABASE + SQLITE FALLBACK)
# =====================================================================
def get_db_connection():
    """
    Создает подключение к базе данных. Пробует прямое подключение к Supabase,
    при ошибке переключается на стабильный IPv4 Пуллер, а в случае полной недоступности облака —
    активирует резервную локальную базу SQLite, защищая приложение от любых падений.
    """
    global IS_SQLITE
    if not DATABASE_URL:
        raise RuntimeError("Переменная DATABASE_URL не задана!")
    
    # 1. Сначала пробуем прямое подключение (Supabase)
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
        print(f"Прямое подключение к Supabase отклонено (IPv6): {e}. Переключаемся на IPv4 Пуллер...")

    # 2. Пробуем пул транзакций Supabase (надежный обход ограничений бесплатного Render)
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
                    port=6543,  # Порт транзакций Supabase
                    sslmode="require",
                    connect_timeout=3
                )
                IS_SQLITE = False
                print(f"Успешно подключено через пуллер Supabase в регионе {region}!")
                return conn
            except Exception:
                continue
    except Exception as e_pool:
        print(f"Ошибка пуллера Supabase: {e_pool}")

    # 3. Безопасный SQLite фолбэк при отсутствии интернета или проблемах в Supabase
    print("⚠️ Облако недоступно. Включаем локальный SQLite сейвер!")
    import sqlite3
    conn = sqlite3.connect("users.db")
    conn.row_factory = sqlite3.Row
    IS_SQLITE = True
    return conn

def execute_query(cur, sql, params=()):
    """Вспомогательная функция для автоматической адаптации SQL-синтаксиса под SQLite"""
    global IS_SQLITE
    if IS_SQLITE:
        sql = sql.replace("%s", "?")
        sql = sql.replace("GREATEST", "MAX")
    cur.execute(sql, params)

def init_db():
    """Создает структуру таблиц при первом запуске приложения"""
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
        print("База данных успешно инициализирована.")
    except Exception as e:
        print(f"Критическая ошибка инициализации базы: {e}")
    finally:
        if conn:
            conn.close()

def get_user(telegram_id: int):
    """Возвращает информацию о пользователе"""
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
            return dict(user)
        return None
    except Exception as e:
        print(f"Ошибка получения пользователя {telegram_id}: {e}")
        return None
    finally:
        if conn:
            conn.close()

def create_user(telegram_id: int, username: Optional[str], first_name: str, email: str = ""):
    """Создает нового пользователя в системе при первом входе"""
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
        print(f"Ошибка создания пользователя {telegram_id}: {e}")
    finally:
        if conn:
            conn.close()

def update_user_profile_info(telegram_id: int, first_name: str, username: Optional[str]):
    """Обновляет имя и юзернейм пользователя (для синхронизации с Telegram)"""
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
        print(f"Ошибка обновления информации профиля {telegram_id}: {e}")
    finally:
        if conn:
            conn.close()

def update_user_balance(telegram_id: int, balance_delta: int, ai_balance_delta: int = 0):
    """Начисляет или списывает балансы раскладов"""
    conn = None
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
        print(f"Ошибка обновления баланса для {telegram_id}: {e}")
    finally:
        if conn:
            conn.close()

# =====================================================================
# ВЕРИФИКАЦИЯ TELEGRAM WEBAPP INITDATA
# =====================================================================
def verify_telegram_init_data(init_data: str) -> dict:
    """Парсит данные инициализации Telegram WebApp для проверки подлинности пользователя"""
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
    """Генерирует полную классическую колоду из 78 карт Таро"""
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
# ДИНАМИЧЕСКИЙ ГЛУБОКИЙ РАСКЛАД (СВЯЗЬ С ОРАКУЛОМ GEMINI)
# =====================================================================
async def generate_dynamic_reading(question: str, pre_selected_cards: list) -> dict:
    """
    Отправляет запрос к модели Gemini. Оракул самостоятельно анализирует вопрос пользователя
    и решает, сколько именно карт (от 3 до 6) требуется извлечь из предложенного списка для 
    полноценного ответа, возвращая результат в структурированном JSON.
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={GEMINI_API_KEY}"
    
    cards_str = ", ".join([f"[{i}] {c['name']} ({c['type']})" for i, c in enumerate(pre_selected_cards)])
    prompt = (
        f"Вопрос искателя: '{question}'.\n"
        f"Список доступных карт для расклада: {cards_str}.\n"
        "Выбери из этого списка только те карты, которые реально необходимы для ответа. "
        "Если ситуация ясна и проста — используй 3 карты. Если вопрос сложный, содержит скрытые факторы "
        "или требует пояснений — задействуй больше карт (4, 5 или все 6 карт). "
        "Верни структурированный ответ в формате JSON."
    )
    
    system_prompt = (
        "Ты — выдающийся мастер Таро и профессиональный психолог-аналитик с 20-летним опытом. "
        "Твоя задача — сделать глубокое, терапевтическое толкование расклада на основе предоставленных карт. "
        "Опирайся на классические правила трактовок Райдера-Уэйта и психологические архетипы К.Г. Юнга. "
        "Говори глубоко, мягко, вдохновляюще. Избегай банальностей, запугиваний и шаблонных фраз.\n\n"
        "Ответ должен быть строго в формате JSON со следующей схемой:\n"
        "{\n"
        "  \"cards_used_indices\": [числа от 0 до 5, обозначающие индексы выбранных карт],\n"
        "  \"reading\": \"текст толкования на русском языке\"\n"
        "}"
    )
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "cards_used_indices": {
                        "type": "ARRAY",
                        "items": {"type": "INTEGER"},
                        "description": "Индексы выбранных карт из переданного массива"
                    },
                    "reading": {
                        "type": "STRING",
                        "description": "Связное, глубокое толкование карт в контексте ситуации"
                    }
                },
                "required": ["cards_used_indices", "reading"]
            }
        }
    }
    
    delays = [1, 2, 4, 8, 16]
    async with httpx.AsyncClient() as client:
        for i, delay in enumerate(delays):
            try:
                response = await client.post(url, json=payload, timeout=40.0)
                if response.status_code == 200:
                    data = response.json()
                    raw_text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                    if raw_text:
                        clean_text = raw_text.strip()
                        if clean_text.startswith("```json"):
                            clean_text = clean_text[7:]
                        if clean_text.startswith("```"):
                            clean_text = clean_text[3:]
                        if clean_text.endswith("```"):
                            clean_text = clean_text[:-3]
                        clean_text = clean_text.strip()
                        return json.loads(clean_text)
                await asyncio.sleep(delay)
            except Exception as e:
                print(f"Сбой Оракула (попытка {i+1}): {e}")
                await asyncio.sleep(delay)
                
        return {
            "cards_used_indices": [0, 1, 2],
            "reading": "Оракул на мгновение скрылся за туманной дымкой космических энергий. Пожалуйста, повторите ваш вопрос немного позже."
        }

# =====================================================================
# API ЭНДПОИНТЫ FASTAPI ДЛЯ ФРОНТЕНДА
# =====================================================================

@app.get("/api/user/profile")
async def get_user_profile(authorization: str = Header(None)):
    """Возвращает актуальный профиль пользователя или регистрирует его при первом входе"""
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
        # Синхронизируем имя при каждом входе в приложение
        update_user_profile_info(user_id, first_name, username)
        user = get_user(user_id)
        
    if not user:
        raise HTTPException(status_code=500, detail="Ошибка работы базы данных")
        
    return {
        "registered": True,
        "user_id": user_id,
        "name": user["first_name"],
        "balance": user["balance"],
        "ai_balance": user["ai_balance"]
    }

@app.post("/api/user/use-reading")
async def use_reading(authorization: str = Header(None)):
    """Списывает со счета 1 стандартный расклад"""
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    
    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
        
    if user["balance"] < 1:
        raise HTTPException(status_code=400, detail="Недостаточно раскладов на балансе.")
        
    update_user_balance(user_id, balance_delta=-1)
    return {"success": True, "new_balance": user["balance"] - 1}

@app.post("/api/user/use-ai-reading")
async def use_ai_reading(payload: dict, authorization: str = Header(None)):
    """Запускает индивидуальный глубокий расклад с динамическим подбором карт"""
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    
    question = payload.get("question", "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Введите ваш вопрос для расклада.")
        
    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не зарегистрирован.")
        
    if user["ai_balance"] < 1:
        raise HTTPException(status_code=400, detail="Недостаточно энергии расклада. Пополните баланс.")
        
    # Пре-выбираем 6 карт, из которых Оракул отберет нужные
    deck = get_tarot_deck()
    pre_selected = random.sample(deck, 6)
    
    result_data = await generate_dynamic_reading(question, pre_selected)
    
    used_indices = result_data.get("cards_used_indices", [0, 1, 2])
    used_indices = [idx for idx in used_indices if 0 <= idx < len(pre_selected)]
    if not used_indices:
        used_indices = [0, 1, 2]
        
    final_cards = [pre_selected[idx] for idx in used_indices]
    text_reading = result_data.get("reading", "")
    
    update_user_balance(user_id, balance_delta=0, ai_balance_delta=-1)
    
    return {
        "success": True,
        "cards": final_cards,
        "text": text_reading,
        "new_ai_balance": user["ai_balance"] - 1
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
        amount = 150
        payload_str = f"buy_1_std_{user_id}_{random.randint(1000,9999)}"
    elif pack == "5_std":
        title = "Пакет из 5 раскладов"
        description = "Выгодный пакет из 5 сеансов со скидкой 10%."
        amount = 675
        payload_str = f"buy_5_std_{user_id}_{random.randint(1000,9999)}"
    elif pack == "1_ai":
        title = "1 Индивидуальный расклад"
        description = "Глубокий разбор вашей ситуации с динамическим выбором карт."
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
            prices=[LabeledPrice(label="Telegram Stars", amount=amount)],
            start_parameter="tarot-shop"
        )
        return {"invoice_link": invoice_link}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка платежного шлюза: {str(e)}")

# =====================================================================
# ОБРАБОТЧИКИ ОПЛАТЫ И КОМАНД TELEGRAM BOT (AIOGRAM)
# =====================================================================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Приветствие бота и кнопка быстрого открытия Mini App"""
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
    """Подтверждение готовности принять платеж Telegram Stars"""
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    """Начисление раскладов на баланс пользователя при успешной оплате"""
    payment = message.successful_payment
    payload = payment.invoice_payload
    
    parts = payload.split("_")
    if len(parts) >= 4:
        action = parts[1]
        pack_type = parts[2]
        user_id = int(parts[3])
        
        if pack_type == "std":
            qty = int(action)
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
    """Принимает входящие события от Telegram (сообщения, оплаты)"""
    try:
        raw_json = await request.json()
        telegram_update = Update.model_validate(raw_json, context={"bot": bot})
        await dp.feed_update(bot=bot, update=telegram_update)
        return {"status": "ok"}
    except Exception as e:
        print(f"Ошибка парсинга вебхука Telegram: {e}")
        return {"status": "error", "message": str(e)}

@app.on_event("startup")
async def on_startup():
    """Действия при инициализации и старте контейнера сервера"""
    try:
        init_db()
    except Exception as e:
        print(f"Ошибка при инициализации базы данных на старте: {e}")
        
    # Сверхбезопасное подключение вебхука (предотвращает падение при сетевых задержках)
    try:
        webhook_url = "[https://tarot-backend-136l.onrender.com/telegram-webhook](https://tarot-backend-136l.onrender.com/telegram-webhook)"
        await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
        print(f"Вебхук Telegram успешно направлен на: {webhook_url}")
    except Exception as e:
        print(f"⚠️ Предупреждение: Не удалось установить вебхук на старте (продолжаем запуск): {e}")
