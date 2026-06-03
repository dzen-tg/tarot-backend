# -*- coding: utf-8 -*-
"""
Python Бэкенд для Telegram Mini App "Таро Оракул • 78 Карт".
Полностью оптимизирован под бизнес-модель "$1 за расклад" (оплата в Telegram Stars).
Обеспечивает легальный сбор Name/Email по законам РК и начисление баланса.
"""

import hmac
import hashlib
import json
import logging
import os
import re
from urllib.parse import parse_qsl
from typing import Optional, Dict, Any
from pydantic import BaseModel, EmailStr

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher, types
from aiogram.types import LabeledPrice, PreCheckoutQuery, SuccessfulPayment

# Настройки логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Токен твоего бота от @BotFather
BOT_TOKEN_RAW = os.environ.get("TELEGRAM_BOT_TOKEN", "8838358841:AAFf3LnY3Rd2LV46d09FGu_PkOpRlQoIYRY")

# Жесткая очистка токена от любых невидимых символов, пробелов, кавычек и спецсимволов
BOT_TOKEN = BOT_TOKEN_RAW.strip().strip("'").strip('"')
BOT_TOKEN = re.sub(r'[^a-zA-Z0-9:]', '', BOT_TOKEN)

logger.info(f"BOT_TOKEN raw length: {len(BOT_TOKEN_RAW)}, sanitized length: {len(BOT_TOKEN)}")
if len(BOT_TOKEN) > 8:
    logger.info(f"BOT_TOKEN preview: {BOT_TOKEN[:4]}...{BOT_TOKEN[-4:]}")

app = FastAPI(title="Tarot 78 Cards Core Backend", version="1.0.0")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Хранение балансов пользователей на сервере (в рабочей версии рекомендуется использовать БД)
USERS_DB: Dict[int, Dict[str, Any]] = {}


class UserState:
    @staticmethod
    def get_or_create(user_id: int, username: Optional[str] = None) -> Dict[str, Any]:
        if user_id not in USERS_DB:
            USERS_DB[user_id] = {
                "user_id": user_id,
                "username": username,
                "name": "Искатель",
                "email": None,
                "consent_given": False,
                "balance": 1,  # 1 бесплатный стартовый вопрос для лидогенерации
                "history": []
            }
        return USERS_DB[user_id]


def verify_telegram_init_data(telegram_init_data: str) -> dict:
    """
    Проверяет валидность данных инициализации Telegram WebApp.
    Возвращает точные технические ошибки для быстрой настройки.
    """
    if not telegram_init_data:
        logger.error("Telegram validation error: Auth header is empty")
        raise HTTPException(
            status_code=400, 
            detail="Вход не выполнен: сессия пуста. Вы запустили приложение внутри Telegram?"
        )
        
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN" or not BOT_TOKEN:
        logger.error("CRITICAL CONFIG ERROR: TELEGRAM_BOT_TOKEN environment variable is not configured on Render!")
        raise HTTPException(
            status_code=500, 
            detail="Ошибка сервера: Переменная TELEGRAM_BOT_TOKEN не настроена на Render.com!"
        )

    # Очищаем заголовок от возможных префиксов авторизации (например, "tga <data>" или "Bearer <data>")
    telegram_init_data = telegram_init_data.strip()
    if " " in telegram_init_data:
        parts = telegram_init_data.split(" ", 1)
        if "=" not in parts[0]:
            telegram_init_data = parts[1]

    try:
        parsed_data = dict(parse_qsl(telegram_init_data, keep_blank_values=True))
        if "hash" not in parsed_data:
            logger.error(f"Telegram validation error: Hash missing in initData. Received keys: {list(parsed_data.keys())}")
            raise HTTPException(
                status_code=400, 
                detail="Ошибка сессии: отсутствует цифровой хэш Telegram."
            )
        
        received_hash = parsed_data.pop("hash")
        # Сортировка параметров в алфавитном порядке
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        
        # Генерация секретного ключа на основе токена бота
        secret_key = hmac.new(b"WebApps", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        if calculated_hash != received_hash:
            logger.error("Telegram validation error: Hash mismatch!")
            logger.error(f"Calculated hash: {calculated_hash}")
            logger.error(f"Received hash: {received_hash}")
            logger.error(f"Data check string: {data_check_string}")
            raise HTTPException(
                status_code=403, 
                detail="Ошибка подписи: Токен бота в Render.com не совпадает с реальным токеном вашего бота. Проверьте вкладку Environment!"
            )
        
        if "user" not in parsed_data:
            raise HTTPException(
                status_code=400, 
                detail="Ошибка данных: Telegram не передал информацию о пользователе."
            )
            
        user_data = json.loads(parsed_data["user"])
        return user_data
        
    except HTTPException as http_err:
        # Пробрасываем наши понятные HTTPException без изменений
        raise http_err
    except Exception as e:
        logger.error(f"Telegram validation unexpected crash: {e}")
        raise HTTPException(
            status_code=401, 
            detail=f"Непредвиденная ошибка валидации сессии: {str(e)}"
        )


class RegisterRequest(BaseModel):
    name: str
    email: EmailStr
    consent: bool


class InvoiceRequest(BaseModel):
    package_id: int  # 1, 5 или 15 раскладов


# --- API ЭНДПОИНТЫ ДЛЯ МИНИ-АПП ---

@app.get("/")
async def health_check():
    return {"status": "ok", "app": "Tarot 78 Cards Complete Server"}


@app.post("/api/user/register")
async def register_user(payload: RegisterRequest, authorization: str = Header(None)):
    """
    Эндпоинт для легальной регистрации пользователя по законам РК.
    """
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg["id"]

    if not payload.consent:
        raise HTTPException(status_code=400, detail="Необходимо ваше согласие с политикой")

    user_profile = UserState.get_or_create(user_id, user_tg.get("username"))
    user_profile["name"] = payload.name
    user_profile["email"] = payload.email
    user_profile["consent_given"] = True
    
    logger.info(f"User {user_id} registered successfully: {payload.name} ({payload.email})")
    
    return {
        "status": "success",
        "balance": user_profile["balance"],
        "name": user_profile["name"]
    }


@app.get("/api/user/profile")
async def get_user_profile(authorization: str = Header(None)):
    """
    Загружает профиль пользователя при старте приложения.
    """
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg["id"]
    
    user_profile = UserState.get_or_create(user_id, user_tg.get("username"))
    return {
        "user_id": user_id,
        "balance": user_profile["balance"],
        "name": user_profile["name"],
        "registered": user_profile["consent_given"]
    }


@app.post("/api/payment/create-invoice")
async def create_invoice(payload: InvoiceRequest, authorization: str = Header(None)):
    """
    Генерирует платежную ссылку на оплату в Telegram Stars:
    - 1 Вопрос: 75 звезд (~$1)
    - 5 Вопросов: 340 звезд (~$4.5)
    - 15 Вопросов: 900 звезд (~$12)
    """
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg["id"]

    packages = {
        1: {"stars": 75, "title": "1 Расклад Таро", "desc": "Один подробный расклад на любой из 10 вопросов"},
        5: {"stars": 340, "title": "5 Раскладов Таро", "desc": "Пакет 'Поток Мудрости' со скидкой 10%"},
        15: {"stars": 900, "title": "15 Раскладов Таро", "desc": "Пакет 'Абсолютное Знание' со скидкой 20%"}
    }

    pkg = packages.get(payload.package_id)
    if not pkg:
        raise HTTPException(status_code=400, detail="Неверный ID пакета")

    prices = [LabeledPrice(label=pkg["title"], amount=pkg["stars"])]

    try:
        invoice_link = await bot.create_invoice_link(
            title="Энергия Вопросов",
            description=pkg["desc"],
            payload=json.dumps({"user_id": user_id, "questions_count": payload.package_id}),
            provider_token="",  # Оставляем пустым для оплаты через Telegram Stars
            currency="XTR",     # Код валюты Telegram Stars
            prices=prices,
            is_flexible=False
        )
        return {"invoice_link": invoice_link}
    except Exception as e:
        logger.error(f"Error producing invoice link: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка генерации счета: {str(e)}")


# --- ОБРАБОТКА ВЕБХУКОВ И ПЛАТЕЖЕЙ STARS (AIOGRAM) ---

@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    """
    Подтверждает платеж, проверяя, зарегистрирован ли пользователь.
    """
    try:
        payload = json.loads(pre_checkout_query.invoice_payload)
        user_id = payload.get("user_id")
        
        if user_id in USERS_DB and USERS_DB[user_id]["consent_given"]:
            await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
            logger.info(f"Payment precheck ok for user {user_id}")
        else:
            await bot.answer_pre_checkout_query(
                pre_checkout_query.id, 
                ok=False, 
                error_message="Пожалуйста, сначала пройдите регистрацию внутри приложения."
            )
    except Exception as e:
        logger.error(f"PreCheckout query processing error: {e}")
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="Ошибка сервера")


@dp.message()
async def process_successful_payment(message: types.Message):
    """
    Начисляет баланс на игровой счет пользователя после успешной оплаты в Telegram Stars.
    """
    if not message.successful_payment:
        return

    payment: SuccessfulPayment = message.successful_payment
    try:
        payload = json.loads(payment.invoice_payload)
        user_id = int(payload["user_id"])
        questions_count = int(payload["questions_count"])

        user_profile = UserState.get_or_create(user_id)
        user_profile["balance"] += questions_count
        
        logger.info(f"Credited {questions_count} questions to user {user_id}")

        await bot.send_message(
            chat_id=user_id,
            text=f"🔮 *Баланс Оракула пополнен!*\n\n{user_profile['name']}, вам успешно зачислено *{questions_count}* раскладов. Возвращайтесь в приложение и откройте тайны будущего!"
        )
    except Exception as e:
        logger.error(f"Successful payment balance adjustment failed: {e}")


@app.post("/telegram-webhook")
async def telegram_webhook(update: dict):
    """
    Вебхук для приема входящих событий от Telegram (команды боту и успешные транзакции).
    """
    telegram_update = types.Update(**update)
    await dp.feed_update(bot=bot, update=telegram_update)
    return {"status": "ok"}
