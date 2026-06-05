# -*- coding: utf-8 -*-
"""
Финальный рабочий бэкенд для Telegram Mini App "Таро Оракул".
Содержит базу данных SQLite, команду /start, автоматическую кнопку запуска,
проверку подписи Telegram, регистрацию и выгрузку базы email в CSV.
"""

import hmac
import hashlib
import json
import logging
import os
import re
import sqlite3
import urllib.parse
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import LabeledPrice, PreCheckoutQuery, SuccessfulPayment, WebAppInfo, MenuButtonWebApp

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация токена бота
BOT_TOKEN_RAW = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
BOT_TOKEN = BOT_TOKEN_RAW.strip().strip("'").strip('"')
BOT_TOKEN = re.sub(r'\s+', '', BOT_TOKEN)  # Удаляем любые пробелы и переносы

app = FastAPI(title="Tarot Complete Backend")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Настройка CORS для работы с Vercel фронтендом
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === РАБОТА С БАЗОЙ ДАННЫХ SQLITE ===
DB_FILE = "users.db"

def init_db():
    """Инициализирует таблицу пользователей"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            name TEXT,
            email TEXT,
            consent_given INTEGER DEFAULT 0,
            balance INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()
    logger.info("База данных SQLite успешно инициализирована.")

init_db()

def get_user_from_db(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def save_or_update_user(user_id: int, username: Optional[str], name: str = "Искатель", email: Optional[str] = None, consent: int = 0, balance_change: int = 0):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    user = get_user_from_db(user_id)
    
    if not user:
        cursor.execute(
            "INSERT INTO users (user_id, username, name, email, consent_given, balance) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, name, email, consent, 1 + balance_change)
        )
    else:
        new_name = name if name != "Искатель" else user["name"]
        new_email = email if email else user["email"]
        new_consent = consent if consent == 1 else user["consent_given"]
        new_balance = user["balance"] + balance_change
        
        cursor.execute(
            "UPDATE users SET username = ?, name = ?, email = ?, consent_given = ?, balance = ? WHERE user_id = ?",
            (username, new_name, new_email, new_consent, new_balance, user_id)
        )
    
    conn.commit()
    conn.close()
    return get_user_from_db(user_id)

# === ВЕРИФИКАЦИЯ ТЕЛЕГРАМ СЕССИИ ===
def verify_telegram_init_data(telegram_init_data: str) -> dict:
    """Усиленная проверка цифровой подписи Telegram WebApp"""
    if not telegram_init_data:
        raise HTTPException(status_code=400, detail="Сессия не найдена. Пожалуйста, откройте приложение в Telegram.")
        
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN" or not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="Ошибка конфигурации сервера: отсутствует токен бота.")

    # Очистка заголовка от префиксов tga/Bearer
    telegram_init_data = telegram_init_data.strip()
    if " " in telegram_init_data:
        parts = telegram_init_data.split(" ", 1)
        if "=" not in parts[0]:
            telegram_init_data = parts[1]

    try:
        parsed_data = dict(urllib.parse.parse_qsl(telegram_init_data, keep_blank_values=True))
        if "hash" not in parsed_data:
            raise HTTPException(status_code=400, detail="Отсутствует криптографический хэш сессии.")
        
        received_hash = parsed_data.pop("hash")
        
        # Сортировка по алфавиту — жесткое требование Telegram
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        
        # Генерация подписи
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode('utf-8'), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode('utf-8'), hashlib.sha256).hexdigest()
        
        if calculated_hash != received_hash:
            raise HTTPException(
                status_code=403, 
                detail="Сбой авторизации: токен бота на сервере не совпадает с вашим ботом в Telegram."
            )
        
        return json.loads(parsed_data.get("user", "{}"))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error validating initData: {e}")
        raise HTTPException(status_code=401, detail=f"Ошибка валидации сессии: {str(e)}")

# === ВАЛИДАЦИЯ КОМАНДЫ /START И КНОПКИ МЕНЮ ===
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Приветствие при команде /start с кнопкой запуска приложения"""
    web_app_url = "https://tarot-frontend.vercel.app"  # Твоя ссылка на Vercel
    
    # 1. Текст приветствия
    welcome_text = (
        f"🌌 *Добро пожаловать во Врата Судьбы, {message.from_user.first_name}!*\n\n"
        "Я — ваш верный проводник в мире Таро. Здесь вы можете получить "
        "индивидуальный расклад и детальные ответы Оракула на любые вопросы о любви, карьере и будущем.\n\n"
        "🔮 *Первый расклад — абсолютно бесплатно!*\n\n"
        "Нажмите кнопку ниже, чтобы открыть Оракул и вытянуть свою карту."
    )
    
    # 2. Кнопка под сообщением
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔮 Открыть Оракул", web_app=WebAppInfo(url=web_app_url))]
    ])
    
    # 3. Настройка постоянной кнопки «Оракул» слева от поля ввода сообщения
    try:
        await bot.set_chat_menu_button(
            chat_id=message.chat.id,
            menu_button=MenuButtonWebApp(text="Оракул", web_app=WebAppInfo(url=web_app_url))
        )
    except Exception as e:
        logger.error(f"Ошибка настройки Menu Button: {e}")

    await message.answer(welcome_text, reply_markup=keyboard, parse_mode="Markdown")

# === МОДЕЛИ ДАННЫХ ===
class RegisterRequest(BaseModel):
    name: str
    email: str
    consent: bool

class InvoiceRequest(BaseModel):
    package_id: int

# --- API ДЛЯ МИНИ-АПП ---

@app.get("/api/user/profile")
async def get_user_profile(authorization: str = Header(None)):
    """Автоматический вход: проверяет, зарегистрирован ли уже пользователь"""
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    
    user_profile = get_user_from_db(user_id)
    if user_profile and user_profile["consent_given"] == 1:
        return {
            "user_id": user_id,
            "balance": user_profile["balance"],
            "name": user_profile["name"],
            "registered": True
        }
    return {
        "registered": False,
        "balance": 1,
        "name": "Искатель"
    }

@app.post("/api/user/register")
async def register_user(payload: RegisterRequest, authorization: str = Header(None)):
    """Регистрация пользователя в БД SQLite при первом входе"""
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")

    if not payload.consent:
        raise HTTPException(status_code=400, detail="Необходимо дать согласие на обработку данных.")

    user_profile = save_or_update_user(
        user_id=user_id,
        username=user_tg.get("username"),
        name=payload.name,
        email=payload.email,
        consent=1
    )
    
    return {
        "status": "success",
        "balance": user_profile["balance"],
        "name": user_profile["name"]
    }

@app.post("/api/payment/create-invoice")
async def create_invoice(payload: InvoiceRequest, authorization: str = Header(None)):
    """Генерация ссылки на оплату в Telegram Stars"""
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")

    packages = {
        1: {"stars": 75, "title": "1 Расклад Таро", "desc": "Задайте 1 волнующий вопрос Оракулу"},
        5: {"stars": 340, "title": "5 Раскладов Таро", "desc": "Пакет 'Поток Мудрости' со скидкой 10%"},
        15: {"stars": 900, "title": "15 Раскладов Таро", "desc": "Пакет 'Абсолютное Знание' со скидкой 20%"}
    }

    pkg = packages.get(payload.package_id)
    if not pkg:
        raise HTTPException(status_code=400, detail="Некорректный ID пакета.")

    prices = [LabeledPrice(label=pkg["title"], amount=pkg["stars"])]

    try:
        invoice_link = await bot.create_invoice_link(
            title="Энергия Вопросов",
            description=pkg["desc"],
            payload=json.dumps({"user_id": user_id, "questions_count": payload.package_id}),
            provider_token="",  # Пусто для Telegram Stars
            currency="XTR",     # Код валюты Telegram Stars
            prices=prices,
            is_flexible=False
        )
        return {"invoice_link": invoice_link}
    except Exception as e:
        logger.error(f"Error creating invoice: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка генерации счета: {str(e)}")

# === АДМИН-ПАНЕЛЬ ДЛЯ EMAIL-РАССЫЛОК (ВЫГРУЗКА CSV) ===
@app.get("/api/admin/export-users")
async def export_users():
    """Открой в браузере: твой-бэкенд.onrender.com/api/admin/export-users для скачивания базы"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, name, email FROM users WHERE consent_given = 1")
    rows = cursor.fetchall()
    conn.close()
    
    # Сборка CSV файла
    csv_content = "\ufeffTelegram_ID;Username;Name;Email\n"  # \ufeff для корректного открытия в Excel на русском
    for row in rows:
        username = f"@{row[1]}" if row[1] else "нет"
        csv_content += f"{row[0]};{username};{row[2]};{row[3]}\n"
        
    return Response(
        content=csv_content, 
        media_type="text/csv", 
        headers={"Content-Disposition": "attachment; filename=tarot_email_database.csv"}
    )

# === ОБРАБОТКА ТРАНЗАКЦИЙ (AIOGRAM WEBHOOK) ===

@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    """Шаг проверки перед оплатой"""
    try:
        payload = json.loads(pre_checkout_query.invoice_payload)
        user_id = payload.get("user_id")
        user = get_user_from_db(user_id)
        
        if user and user["consent_given"] == 1:
            await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
        else:
            await bot.answer_pre_checkout_query(
                pre_checkout_query.id, 
                ok=False, 
                error_message="Пожалуйста, сначала зарегистрируйтесь внутри приложения."
            )
    except Exception as e:
        logger.error(f"Error in pre-checkout: {e}")
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="Ошибка на сервере оплат.")

@dp.message()
async def process_successful_payment(message: types.Message):
    """Моментальное начисление раскладов после успешного списания Stars"""
    if not message.successful_payment:
        return

    payment: SuccessfulPayment = message.successful_payment
    try:
        payload = json.loads(payment.invoice_payload)
        user_id = int(payload["user_id"])
        questions_count = int(payload["questions_count"])

        # Обновляем баланс в SQLite
        user_profile = save_or_update_user(
            user_id=user_id, 
            username=message.from_user.username, 
            balance_change=questions_count
        )
        
        # Отправляем сообщение-подтверждение в чат бота
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"🔮 *Баланс Оракула успешно пополнен!*\n\n"
                f"{user_profile['name']}, вам зачислено *{questions_count}* раскладов.\n"
                "Откройте приложение через меню и задайте свой вопрос картам!"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error processing successful payment: {e}")

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    """ webhook эндпоинт для связи с серверами Telegram """
    update_data = await request.json()
    telegram_update = types.Update(**update_data)
    await dp.feed_update(bot=bot, update=telegram_update)
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "online", "database": "SQLite (Active)"}
