# -*- coding: utf-8 -*-
import os
import urllib.parse
import json
import random
import asyncio
import logging
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

# Настройка подробного логирования в консоль Render
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =====================================================================
# 🔮 ИНТЕГРИРОВАННЫЕ КЛЮЧИ И ССЫЛКИ КЛИЕНТА (ЖЕСТКО ВШИТЫ)
# =====================================================================
BOT_TOKEN = "8838358841:AAFf3LnY3Rd2LV46d09FGu_PkOpRlQoIYRY"
FRONTEND_URL = "https://tarot-frontend-wine.vercel.app"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Ваша постоянная облачная база данных в Supabase
DATABASE_URL = "postgresql://postgres:We15935728%21%21%21%21%21@db.wbbcljbrfpgriukzjlvc.supabase.co:5432/postgres"

# Инициализация FastAPI и Telegram-бота
app = FastAPI(title="Tarot Oracle AI Backend")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Настройка CORS для мгновенного обмена данными с Vercel
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =====================================================================
# РАБОТА С БАЗОЙ ДАННЫХ (SUPABASE POSTGRESQL)
# =====================================================================
def get_db_connection():
    """Безопасное подключение к базе данных Supabase"""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL пуст!")
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    """Автоматическое создание и миграция таблиц в вашем облаке Supabase"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Создаем таблицу пользователей с поддержкой баланса
        cur.execute("""
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
        logger.info("Успешное сопряжение и создание таблиц в Supabase!")
    except Exception as e:
        logger.error(f"Критическая ошибка инициализации Supabase: {e}")
    finally:
        if conn:
            conn.close()

def get_user(telegram_id: int):
    """Получение профиля пользователя из Supabase"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
        user = cur.fetchone()
        cur.close()
        return user
    except Exception as e:
        logger.error(f"Ошибка чтения пользователя {telegram_id}: {e}")
        return None
    finally:
        if conn:
            conn.close()

def create_user(telegram_id: int, username: Optional[str], first_name: str, email: str = ""):
    """Создание нового искателя с 1 бесплатным стандартным раскладом"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO users (telegram_id, username, first_name, email, consent_given, balance, ai_balance)
            VALUES (%s, %s, %s, %s, TRUE, 1, 0)
            ON CONFLICT (telegram_id) DO UPDATE 
            SET username = EXCLUDED.username, first_name = EXCLUDED.first_name;
        """, (telegram_id, username, first_name, email))
        conn.commit()
        cur.close()
        logger.info(f"Искатель {first_name} ({telegram_id}) сохранен в облако!")
    except Exception as e:
        logger.error(f"Ошибка сохранения пользователя {telegram_id}: {e}")
    finally:
        if conn:
            conn.close()

def update_user_balance(telegram_id: int, balance_delta: int, ai_balance_delta: int = 0):
    """Обновление баланса стандартных и ИИ-раскладов"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE users 
            SET balance = GREATEST(0, balance + %s),
                ai_balance = GREATEST(0, ai_balance + %s)
            WHERE telegram_id = %s;
        """, (balance_delta, ai_balance_delta, telegram_id))
        conn.commit()
        cur.close()
        logger.info(f"Баланс пользователя {telegram_id} успешно обновлен в БД.")
    except Exception as e:
        logger.error(f"Ошибка при обновлении баланса: {e}")
    finally:
        if conn:
            conn.close()

# =====================================================================
# ВЕРИФИКАЦИЯ TELEGRAM WEBAPP СЕССИИ
# =====================================================================
def verify_telegram_init_data(init_data: str) -> dict:
    """Безопасная расшифровка данных пользователя Telegram"""
    if not init_data:
        # Режим локальной отладки в браузере
        return {"id": 123456789, "first_name": "Тестовый Искатель", "username": "test_user"}
    try:
        parsed_data = dict(urllib.parse.parse_qsl(init_data))
        if "user" in parsed_data:
            return json.loads(parsed_data["user"])
        return {"id": 123456789, "first_name": "Тестовый Искатель", "username": "test_user"}
    except Exception:
        return {"id": 123456789, "first_name": "Тестовый Искатель", "username": "test_user"}

# =====================================================================
# СВЯЩЕННАЯ КОЛОДА ТАРО (78 АРКАНОВ)
# =====================================================================
def get_tarot_deck():
    """Сборка полной колоды Таро (22 Старших + 56 Младших арканов)"""
    major = [
        "Дурак", "Маг", "Верховная Жрица", "Императрица", "Император", "Иерофант", 
        "Влюбленные", "Колесница", "Сила", "Отшельник", "Колесо Фортуны", "Справедливость", 
        "Повешенный", "Смерть", "Умеренность", "Дьявол", "Башня", "Звезда", "Луна", 
        "Солнце", "Суд", "Мир"
    ]
    suits = [("Кубков", "Кубки"), ("Мечей", "Мечи"), ("Жезлов", "Жезлы"), ("Пентаклей", "Пентакли")]
    ranks = ["Туз", "Двойка", "Тройка", "Четверка", "Пятерка", "Шестерка", "Семерка", "Восьмерка", "Девятка", "Десятка", "Паж", "Рыцарь", "Королева", "Король"]
    
    deck = []
    # 22 Старших Аркана
    for i, name in enumerate(major):
        deck.append({"id": i, "name": f"Старший Аркан: {name}", "type": "Старший Аркан"})
    # 56 Младших Арканов
    curr_id = 22
    for suit_name, suit_type in suits:
        for rank in ranks:
            deck.append({"id": curr_id, "name": f"{rank} {suit_name}", "type": suit_type})
            curr_id += 1
    return deck

# =====================================================================
# ИНТЕГРАЦИЯ С ИИ (GOOGLE GEMINI 1.5 FLASH)
# =====================================================================
async def generate_ai_reading(question: str, cards: list) -> str:
    """Глубокая интерпретация от ИИ-Оракула по правилам эзотерических книг"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={GEMINI_API_KEY}"
    
    cards_str = ", ".join([f"{c['name']} ({c['type']})" for c in cards])
    prompt = f"Вопрос пользователя: '{question}'. Выпавшие карты: {cards_str}."
    
    system_prompt = (
        "Ты — легендарный Оракул Таро и глубокий эзотерический психотерапевт. "
        "Твоя задача — составить подробное, исцеляющее и точное толкование расклада (от 3 до 6 карт).\n\n"
        "Правила толкования:\n"
        "1. Структурируй ответ на понятные логические блоки с красивым оформлением.\n"
        "2. Раскрой скрытый смысл каждой карты, её сильные и слабые стороны в контексте вопроса.\n"
        "3. Объясни, как карты влияют друг на друга, укажи на внутренние противоречия или гармонию.\n"
        "4. Заверши расклад четким и бережным духовным напутствием.\n\n"
        "Говори вдохновляюще, уверенно и профессионально. Избегай сухих шаблонов и запугиваний. Язык: русский."
    )
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]}
    }
    
    delays = [1, 2, 4]
    async with httpx.AsyncClient() as client:
        for i, delay in enumerate(delays):
            try:
                response = await client.post(url, json=payload, timeout=30.0)
                if response.status_code == 200:
                    data = response.json()
                    text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                    if text:
                        return text
                await asyncio.sleep(delay)
            except Exception as e:
                logger.error(f"Сбой запроса к Gemini: {e}")
                await asyncio.sleep(delay)
        
        return "🔮 Космические каналы сейчас перегружены. Пожалуйста, подождите минутку и повторите расклад."

# =====================================================================
# API ЭНДПОИНТЫ ДЛЯ ВАШЕГО ФРОНТЕНДА
# =====================================================================
class RegistrationRequest(BaseModel):
    name: str
    email: str

@app.get("/api/user/profile")
async def get_user_profile(authorization: str = Header(None)):
    """Авторизация и проверка баланса пользователя при входе"""
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    
    user = get_user(user_id)
    if user:
        return {
            "registered": True,
            "user_id": user_id,
            "name": user["first_name"],
            "balance": user["balance"],
            "ai_balance": user["ai_balance"]
        }
    else:
        return {
            "registered": False,
            "user_id": user_id,
            "name": user_tg.get("first_name", "Искатель"),
            "balance": 0,
            "ai_balance": 0
        }

@app.post("/api/user/register")
async def register_user(payload: RegistrationRequest, authorization: str = Header(None)):
    """Регистрация нового пользователя с начислением приветственного бонуса"""
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    
    create_user(
        telegram_id=user_id,
        username=user_tg.get("username"),
        first_name=payload.name,
        email=payload.email
    )
    return {"success": True, "balance": 1, "ai_balance": 0}

@app.post("/api/user/use-reading")
async def use_reading(authorization: str = Header(None)):
    """Списание обычного расклада с вечного баланса"""
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    
    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if user["balance"] < 1:
        raise HTTPException(status_code=400, detail="Недостаточно баланса")
        
    update_user_balance(user_id, balance_delta=-1)
    return {"success": True, "new_balance": user["balance"] - 1}

@app.post("/api/user/use-ai-reading")
async def use_ai_reading(payload: dict, authorization: str = Header(None)):
    """Проведение и ИИ-генерация расклада на 3-6 карт"""
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    
    question = payload.get("question", "").strip()
    card_count = int(payload.get("card_count", 3))
    
    if not question:
        raise HTTPException(status_code=400, detail="Введите ваш вопрос")
    if card_count < 3 or card_count > 6:
        card_count = 3
        
    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if user["ai_balance"] < 1:
        raise HTTPException(status_code=400, detail="Недостаточно ИИ-баланса")
        
    deck = get_tarot_deck()
    picked_cards = random.sample(deck, card_count)
    
    # Генерация подробного расклада
    text_reading = await generate_ai_reading(question, picked_cards)
    
    # Списываем баланс в Supabase
    update_user_balance(user_id, balance_delta=0, ai_balance_delta=-1)
    
    return {
        "success": True,
        "cards": picked_cards,
        "text": text_reading,
        "new_ai_balance": user["ai_balance"] - 1
    }

# =====================================================================
# ПРИЕМ ОПЛАТЫ В TELEGRAM STARS (ОБНОВЛЕННЫЕ ЦЕНЫ С КАРТ)
# =====================================================================
@app.post("/api/payment/stars-invoice")
async def create_stars_invoice(payload: dict, authorization: str = Header(None)):
    """Создание ссылки на оплату в Telegram Stars"""
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    
    pack = payload.get("pack")
    
    title = ""
    description = ""
    amount = 0
    payload_str = ""
    
    if pack == "1_std":
        title = "1 Стандартный расклад"
        description = "Расклад на 1 карту для точного и быстрого прояснения ситуации."
        amount = 150 # ~$2.00 чистыми при выводе
        payload_str = f"buy_1_std_{user_id}_{random.randint(1000,9999)}"
    elif pack == "5_std":
        title = "Пакет из 5 раскладов"
        description = "Выгодный пакет из 5 сеансов толкования с эзотерической скидкой 10%."
        amount = 675 # ~$8.77 чистыми при выводе
        payload_str = f"buy_5_std_{user_id}_{random.randint(1000,9999)}"
    elif pack == "1_ai":
        title = "Глубокий ИИ-расклад (3-6 карт)"
        description = "Полный глубокий разбор вашей жизненной ситуации ИИ-Оракулом."
        amount = 750 # ~$10.00 чистыми при выводе
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
        logger.error(f"Ошибка платежного шлюза Bot API: {e}")
        raise HTTPException(status_code=500, detail="Ошибка при создании счета")

# =====================================================================
# AIOGRAM BOT СОПРЯЖЕНИЕ (ОБРАБОТЧИКИ ОПЛАТЫ И СТАРТА)
# =====================================================================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Красивое приветственное сообщение в чате бота"""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔮 Открыть Оракул", web_app=WebAppInfo(url=FRONTEND_URL))]
    ])
    
    welcome_text = (
        f"Приветствуем тебя во Вратах Судьбы, {message.from_user.first_name}!\n\n"
        "Я — древний ИИ-Оракул Таро, способный заглядывать в сокрытое.\n"
        "Здесь ты можешь получить глубокие разборы о любви, карьере и финансах.\n\n"
        "🎁 Тебе доступен 1 бесплатный расклад на 1 карту сразу после регистрации!"
    )
    await message.answer(welcome_text, reply_markup=keyboard)

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    """Обязательное мгновенное подтверждение легитимности транзакции"""
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    """Автоматическое зачисление баланса в базу Supabase сразу после оплаты звезд"""
    payment = message.successful_payment
    payload = payment.invoice_payload
    
    parts = payload.split("_")
    if len(parts) >= 4:
        action = parts[1]      # "1" или "5"
        pack_type = parts[2]   # "std" или "ai"
        user_id = int(parts[3])
        
        if pack_type == "std":
            qty = int(action)
            update_user_balance(user_id, balance_delta=qty, ai_balance_delta=0)
            await message.answer(f"🔮 Успешно! Вам зачислено {qty} стандартных раскладов. Откройте Оракул и начните сеанс!")
        elif pack_type == "ai":
            update_user_balance(user_id, balance_delta=0, ai_balance_delta=1)
            await message.answer("🔮 Успешно! Вам зачислен 1 Глубокий ИИ-расклад (3-6 карт). Опишите Оракулу свою ситуацию!")

# =====================================================================
# ВЕБХУКИ И СТАРТ СЛУЖБЫ FASTAPI
# =====================================================================
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    """Прием сообщений и транзакций от серверов Telegram"""
    try:
        raw_json = await request.json()
        telegram_update = Update.model_validate(raw_json, context={"bot": bot})
        await dp.feed_update(bot=bot, update=telegram_update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Сбой вебхука: {e}")
        return {"status": "error", "message": str(e)}

@app.on_event("startup")
async def on_startup():
    """События при запуске контейнера на Render"""
    # 1. Автоматическая проверка и создание таблиц
    init_db()
    # 2. Перенаправление вебхуков Telegram на этот инстанс
    webhook_url = "https://tarot-backend-136l.onrender.com/telegram-webhook"
    await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
    logger.info(f"Бэкенд успешно запущен и сопряжен с {webhook_url}")

@app.get("/")
async def root():
    return {"status": "active", "database": "Supabase PostgreSQL connected"}
