import logging
import hmac
import hashlib
import json
import os
import urllib.parse
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from typing import Optional

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Получаем токен из переменных окружения
BOT_TOKEN_RAW = os.environ.get("TELEGRAM_BOT_TOKEN", "8838358841:AAFf3LnY3Rd2LV46d09FGu_PkOpRlQoIYRY")
# Очистка токена от возможных лишних пробелов или кавычек
BOT_TOKEN = BOT_TOKEN_RAW.strip().strip("'").strip('"')

def check_webapp_signature(token: str, init_data: dict):
    """
    Строгая проверка подписи Telegram Web App согласно документации.
    """
    try:
        if isinstance(init_data, str):
            init_data = dict(urllib.parse.parse_qsl(init_data))
        
        data = {k: str(v) for k, v in init_data.items()}
        
        if 'hash' not in data:
            logger.error("В данных отсутствует поле 'hash'")
            return False
            
        check_hash = data.pop('hash')
        
        # Сортировка ключей по алфавиту — критически важно для Telegram
        data_check_string = "\n".join([
            f"{k}={data[k]}" for k in sorted(data.keys())
        ])
        
        # Создание секретного ключа HMAC-SHA256
        secret_key = hmac.new(
            "WebAppData".encode(), 
            token.encode(), 
            hashlib.sha256
        ).digest()
        
        # Вычисление хеша
        calculated_hash = hmac.new(
            secret_key, 
            data_check_string.encode(), 
            hashlib.sha256
        ).hexdigest()
        
        is_valid = hmac.compare_digest(calculated_hash, check_hash)
        
        if not is_valid:
            logger.warning(f"Ошибка подписи! Ожидалось: {check_hash}, Получено: {calculated_hash}")
            
        return is_valid
        
    except Exception as e:
        logger.error(f"Критическая ошибка проверки подписи: {e}")
        return False

# Эндпоинт для проверки данных пользователя при входе
@app.post("/validate-user")
async def validate_user(request: Request):
    data = await request.json()
    init_data = data.get("initData")
    
    if not init_data:
        raise HTTPException(status_code=400, detail="Отсутствуют данные initData")
    
    if check_webapp_signature(BOT_TOKEN, init_data):
        return {"status": "ok", "message": "Авторизация успешна"}
    else:
        raise HTTPException(status_code=403, detail="Invalid signature")

# Эндпоинт для получения обновлений (webhook) от Telegram
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    # Здесь будет логика обработки платежей (пре-checkout query и т.д.)
    logger.info(f"Получено обновление от Telegram: {update}")
    return {"ok": True}

@app.get("/")
async def root():
    return {"message": "Tarot Backend is running"}
