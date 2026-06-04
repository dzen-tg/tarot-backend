# -*- coding: utf-8 -*-
import hmac
import hashlib
import json
import logging
import os
import re
import urllib.parse
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types
from aiogram.types import LabeledPrice, PreCheckoutQuery, SuccessfulPayment

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Получаем токен
BOT_TOKEN_RAW = os.environ.get("TELEGRAM_BOT_TOKEN", "8838358841:AAFf3LnY3Rd2LV46d09FGu_PkOpRlQoIYRY")

# Очищаем токен только от случайных пробелов и кавычек (оставляем дефисы и подчеркивания)
BOT_TOKEN = BOT_TOKEN_RAW.strip().strip("'").strip('"')
BOT_TOKEN = re.sub(r'\s+', '', BOT_TOKEN)

app = FastAPI(title="Tarot Backend")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# База данных пользователей в памяти
USERS_DB = {}

class UserState:
    @staticmethod
    def get_or_create(user_id: int, username: Optional[str] = None):
        if user_id not in USERS_DB:
            USERS_DB[user_id] = {
                "user_id": user_id,
                "username": username,
                "name": "Искатель",
                "email": None,
                "consent_given": False,
                "balance": 1,
                "history": []
            }
        return USERS_DB[user_id]

def verify_telegram_init_data(telegram_init_data: str) -> dict:
    """Функция проверки подписи от Telegram с умным выводом ошибок"""
    if not telegram_init_data:
        raise HTTPException(status_code=400, detail="Сессия пуста. Пожалуйста, откройте бота внутри Telegram.")
        
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN" or not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="Ошибка сервера: Токен бота не задан на Render.com!")

    # Очищаем заголовок от префиксов вроде 'tga ' или 'Bearer '
    if " " in telegram_init_data:
        parts = telegram_init_data.split(" ", 1)
        if "=" not in parts[0]:
            telegram_init_data = parts[1]

    try:
        parsed_data = dict(urllib.parse.parse_qsl(telegram_init_data, keep_blank_values=True))
        if "hash" not in parsed_data:
            raise HTTPException(status_code=400, detail="В данных отсутствует цифровой хэш.")
        
        received_hash = parsed_data.pop("hash")
        
        # Сортировка по алфавиту
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        
        # Генерация ключа и хэша
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode('utf-8'), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode('utf-8'), hashlib.sha256).hexdigest()
        
        if calculated_hash != received_hash:
            # ХИТРЫЙ ОТЛАДЧИК: Выводим первые 5 символов токена, который использует Render
            token_preview = f"{BOT_TOKEN[:5]}...{BOT_TOKEN[-4:]}" if len(BOT_TOKEN) > 10 else "ПУСТО/НЕВЕРНО"
            raise HTTPException(
                status_code=403, 
                detail=f"Сбой подписи! Сервер Render видит токен: [{token_preview}]. Совпадает ли он с BotFather?"
            )
        
        user_data = json.loads(parsed_data.get("user", "{}"))
        return user_data
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected validation error: {e}")
        raise HTTPException(status_code=401, detail=f"Непредвиденная ошибка валидации: {str(e)}")

# --- Модели данных ---
class RegisterRequest(BaseModel):
    name: str
    email: str
    consent: bool

class InvoiceRequest(BaseModel):
    package_id: int

# --- API Эндпоинты ---
@app.post("/api/user/register")
async def register_user(payload: RegisterRequest, authorization: str = Header(None)):
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")

    if not user_id:
        raise HTTPException(status_code=400, detail="Не удалось определить ID пользователя")

    if not payload.consent:
        raise HTTPException(status_code=400, detail="Необходимо согласие с политикой")

    user_profile = UserState.get_or_create(user_id, user_tg.get("username"))
    user_profile["name"] = payload.name
    user_profile["email"] = payload.email
    user_profile["consent_given"] = True
    
    return {
        "status": "success",
        "balance": user_profile["balance"],
        "name": user_profile["name"]
    }

@app.get("/api/user/profile")
async def get_user_profile(authorization: str = Header(None)):
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    
    if not user_id:
        raise HTTPException(status_code=400, detail="Не удалось определить ID пользователя")
        
    user_profile = UserState.get_or_create(user_id, user_tg.get("username"))
    return {
        "user_id": user_id,
        "balance": user_profile["balance"],
        "name": user_profile["name"],
        "registered": user_profile["consent_given"]
    }

@app.post("/api/payment/create-invoice")
async def create_invoice(payload: InvoiceRequest, authorization: str = Header(None)):
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")

    packages = {
        1: {"stars": 75, "title": "1 Расклад Таро", "desc": "Один подробный расклад"},
        5: {"stars": 340, "title": "5 Раскладов Таро", "desc": "Пакет 'Поток Мудрости' со скидкой"},
        15: {"stars": 900, "title": "15 Раскладов Таро", "desc": "Пакет 'Абсолютное Знание' со скидкой"}
    }

    pkg = packages.get(payload.package_id)
    if not pkg:
        raise HTTPException(status_code=400, detail="Неверный ID пакета")

    prices = [LabeledPrice(label=pkg["title"], amount=pkg["stars"])]

    try:
        invoice_link = await bot.create_invoice_link(
            title="Энергия Оракула",
            description=pkg["desc"],
            payload=json.dumps({"user_id": user_id, "questions_count": payload.package_id}),
            provider_token="",  # Пусто для Telegram Stars
            currency="XTR",     # Валюта Telegram Stars
            prices=prices,
            is_flexible=False
        )
        return {"invoice_link": invoice_link}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка генерации счета: {str(e)}")

# --- Обработка платежей (Webhook) ---
@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    try:
        payload = json.loads(pre_checkout_query.invoice_payload)
        user_id = payload.get("user_id")
        
        if user_id in USERS_DB and USERS_DB[user_id]["consent_given"]:
            await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
        else:
            await bot.answer_pre_checkout_query(
                pre_checkout_query.id, 
                ok=False, 
                error_message="Пожалуйста, сначала пройдите регистрацию в приложении."
            )
    except Exception as e:
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="Ошибка на сервере")

@dp.message()
async def process_successful_payment(message: types.Message):
    if not message.successful_payment:
        return

    payment = message.successful_payment
    try:
        payload = json.loads(payment.invoice_payload)
        user_id = int(payload["user_id"])
        questions_count = int(payload["questions_count"])

        user_profile = UserState.get_or_create(user_id)
        user_profile["balance"] += questions_count
        
        await bot.send_message(
            chat_id=user_id,
            text=f"🔮 *Баланс Оракула пополнен!*\n\n{user_profile['name']}, вам успешно зачислено *{questions_count}* раскладов. Откройте тайны будущего!"
        )
    except Exception as e:
        logger.error(f"Ошибка при начислении баланса: {e}")

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    update_data = await request.json()
    telegram_update = types.Update(**update_data)
    await dp.feed_update(bot=bot, update=telegram_update)
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"message": "Tarot Backend Active"}
