# -*- coding: utf-8 -*-
import hmac
import hashlib
import json
import logging
import os
import re
import sqlite3
from typing import Optional
import urllib.parse

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
BOT_TOKEN = BOT_TOKEN_RAW.strip().strip("'").strip('"')
BOT_TOKEN = re.sub(r'\s+', '', BOT_TOKEN)

app = FastAPI(title="Tarot Backend with DB")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === НАСТРОЙКА БАЗЫ ДАННЫХ SQLITE ===
DB_FILE = "users.db"

def init_db():
    """Создает таблицу пользователей, если её еще нет"""
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

init_db()

def get_user(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def create_or_update_user(user_id: int, username: Optional[str], name: str = "Искатель", email: Optional[str] = None, consent: int = 0, balance_change: int = 0):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    user = get_user(user_id)
    
    if not user:
        cursor.execute(
            "INSERT INTO users (user_id, username, name, email, consent_given, balance) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, name, email, consent, 1 + balance_change)
        )
    else:
        # Обновляем только те поля, которые переданы
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
    return get_user(user_id)


def verify_telegram_init_data(telegram_init_data: str) -> dict:
    if not telegram_init_data:
        raise HTTPException(status_code=400, detail="Сессия пуста.")
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN" or not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="Токен бота не задан на Render.com!")

    if " " in telegram_init_data:
        parts = telegram_init_data.split(" ", 1)
        if "=" not in parts[0]:
            telegram_init_data = parts[1]

    try:
        parsed_data = dict(urllib.parse.parse_qsl(telegram_init_data, keep_blank_values=True))
        if "hash" not in parsed_data:
            raise HTTPException(status_code=400, detail="Отсутствует цифровой хэш.")
        
        received_hash = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode('utf-8'), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode('utf-8'), hashlib.sha256).hexdigest()
        
        if calculated_hash != received_hash:
            raise HTTPException(status_code=403, detail="Сбой верификации сессии Telegram.")
        
        return json.loads(parsed_data.get("user", "{}"))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Ошибка валидации: {str(e)}")

# --- Модели данных ---
class RegisterRequest(BaseModel):
    name: str
    email: str
    consent: bool

class InvoiceRequest(BaseModel):
    package_id: int

# --- API Эндпоинты ---

@app.get("/api/user/profile")
async def get_user_profile(authorization: str = Header(None)):
    """Проверяет, есть ли пользователь в БД. Если есть, сразу авторизует без ввода email"""
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")
    
    user_profile = get_user(user_id)
    if user_profile and user_profile["consent_given"] == 1:
        return {
            "user_id": user_id,
            "balance": user_profile["balance"],
            "name": user_profile["name"],
            "registered": True
        }
    else:
        return {"registered": false, "balance": 1, "name": "Искатель"}

@app.post("/api/user/register")
async def register_user(payload: RegisterRequest, authorization: str = Header(None)):
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")

    if not payload.consent:
        raise HTTPException(status_code=400, detail="Необходимо согласие с политикой")

    # Сохраняем в реальную БД SQLite
    user_profile = create_or_update_user(
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
    user_tg = verify_telegram_init_data(authorization)
    user_id = user_tg.get("id")

    packages = {
        1: {"stars": 75, "title": "1 Расклад Таро", "desc": "Один подробный расклад"},
        5: {"stars": 340, "title": "5 Раскладов Таро", "desc": "Пакет со скидкой"},
        15: {"stars": 900, "title": "15 Раскладов Таро", "desc": "Максимальный пакет"}
    }
    pkg = packages.get(payload.package_id)
    if not pkg: raise HTTPException(status_code=400, detail="Неверный пакет")

    prices = [LabeledPrice(label=pkg["title"], amount=pkg["stars"])]
    try:
        invoice_link = await bot.create_invoice_link(
            title="Энергия Оракула", description=pkg["desc"],
            payload=json.dumps({"user_id": user_id, "questions_count": payload.package_id}),
            provider_token="", currency="XTR", prices=prices, is_flexible=False
        )
        return {"invoice_link": invoice_link}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- ЭКСПОРТ БАЗЫ ДАННЫХ ДЛЯ РАССЫЛОК (CSV формат) ---
@app.get("/api/admin/export-users")
async def export_users():
    """Эндпоинт для выгрузки email-базы. Просто открой в браузере: твой-бэкенд.onrender.com/api/admin/export-users"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, name, email FROM users WHERE consent_given = 1")
    rows = cursor.fetchall()
    conn.close()
    
    csv_content = "Telegram_ID,Username,Name,Email\n"
    for row in rows:
        csv_content += f"{row[0]},{row[1]},{row[2]},{row[3]}\n"
        
    from fastapi.responses import Response
    return Response(content=csv_content, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=tarot_emails.csv"})

# --- Webhook Платежей ---
@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    payload = json.loads(pre_checkout_query.invoice_payload)
    user_id = payload.get("user_id")
    user = get_user(user_id)
    if user and user["consent_given"] == 1:
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    else:
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="Пройдите регистрацию.")

@dp.message()
async def process_successful_payment(message: types.Message):
    if not message.successful_payment: return
    payment = message.successful_payment
    try:
        payload = json.loads(payment.invoice_payload)
        user_id = int(payload["user_id"])
        questions_count = int(payload["questions_count"])

        # Обновляем баланс в SQLite
        user_profile = create_or_update_user(user_id=user_id, username=message.from_user.username, balance_change=questions_count)
        
        await bot.send_message(
            chat_id=user_id,
            text=f"🔮 *Баланс Оракула пополнен!*\n\n{user_profile['name']}, вам зачислено *{questions_count}* раскладов."
        )
    except Exception as e:
        logger.error(f"Ошибка БД: {e}")

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    update_data = await request.json()
    await dp.feed_update(bot=bot, update=types.Update(**update_data))
    return {"status": "ok"}

@app.get("/")
async def root(): return {"message": "DB Backend Active"}
```eof

---

## 📱 Шаг 2. Обновление Фронтенда (`index.html`)

Теперь добавим логику автоматического входа. При запуске приложение сразу спросит у бэкенда: *«Знаешь ли ты этого пользователя?»*. Если бэкенд ответит «Да» (пользователь уже есть в базе SQLite), экран регистрации мгновенно скроется, и юзер сразу попадет на экран выбора вопросов.

В твоем файле `index.html` найди тег `<script>` и замени функцию инициализации и загрузки приложения (начиная от инициализации `let tg = ...` до начала базы вопросов) следующим блоком логики автоматического входа:

```javascript
// Инициализация Telegram WebApp
let tg = window.Telegram ? window.Telegram.WebApp : null;
if (tg) { tg.ready(); tg.expand(); }

let state = {
    registered: false, user_name: '', user_email: '', balance: 1, 
    selectedQuestionId: null, currentScreen: 'register'
};

const BACKEND_URL = "https://tarot-backend-136l.onrender.com"; // Твой бэкенд

// АВТОМАТИЧЕСКАЯ ПРОВЕРКА ПОЛЬЗОВАТЕЛЯ ПРИ ЗАПУСКЕ
async function checkAuth() {
    const initData = tg ? tg.initData : "dummy_data_for_browser";
    
    try {
        const res = await fetch(`${BACKEND_URL}/api/user/profile`, {
            method: 'GET',
            headers: { 'Authorization': initData }
        });
        
        if (res.ok) {
            const data = await res.json();
            if (data.registered) {
                // Если пользователь уже регистрировался ранее — пускаем его без ввода данных
                state.balance = data.balance;
                state.registered = true;
                document.getElementById('user-name-display').innerText = data.name;
                document.getElementById('user-balance').innerText = `${state.balance} раскладов`;
                
                document.getElementById('screen-register').classList.add('hidden');
                document.getElementById('app-header').classList.remove('hidden');
                document.getElementById('app-navigation').classList.remove('hidden');
                switchTab('questions');
            }
        }
    } catch (e) {
        console.log("Пользователь заходит впервые, оставляем форму регистрации.");
    }
}

// Запускаем проверку при загрузке страницы
window.addEventListener('DOMContentLoaded', checkAuth);
```eof

---

### 📥 Как забирать базу email-адресов для рассылки?

Всё автоматизировано. Никаких сторонних программ открывать не нужно. Раз в неделю или когда планируешь рассылку, просто открой в браузере (на ПК или телефоне) эту ссылку:

`[https://tarot-backend-136l.onrender.com/api/admin/export-users](https://tarot-backend-136l.onrender.com/api/admin/export-users)`

Бэкенд мгновенно выгрузит из файла SQLite актуальный список и скачает на твой компьютер готовый файл **`tarot_emails.csv`**, в котором будут колонки:
* `Telegram ID`
* `Юзернейм`
* `Имя`
* `Email`

Этот файл напрямую открывается в Excel, и его можно сразу загружать в любой сервис рассылок (Mailgun, Unisender, SendPulse и т.д.).

### Что делать сейчас:
1. Замени код в `main.py` на GitHub ➡️ Render автоматически пересоберет его.
2. Замени скрипт автоматического входа в `index.html` на GitHub ➡️ Vercel обновит фронтенд.
3. Перезапусти бота. В первый раз он попросит регистрацию, но если ты закроешь приложение и зайдешь снова — форма ввода исчезнет навсегда!
