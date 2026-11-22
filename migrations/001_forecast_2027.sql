-- «Мой 2027»: данные для прогнозов, покупки, сами прогнозы. Только добавление таблиц – старые данные не трогаем.

CREATE TABLE IF NOT EXISTS forecast_profiles (
    telegram_id BIGINT PRIMARY KEY,
    name VARCHAR(64),
    birth_date DATE,
    sphere VARCHAR(16),               -- love / money / growth / all
    birth_time VARCHAR(5),            -- ЧЧ:ММ или NULL («не знаю»), для астропрогноза
    birth_city VARCHAR(128),
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    tz VARCHAR(64),
    source VARCHAR(64),               -- откуда пришёл: start=2027_<источник>
    teaser_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS forecast_orders (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    product VARCHAR(32) NOT NULL,     -- numerology_2027 / tarot_2027 / astro_2027 / full_2027
    method VARCHAR(16) NOT NULL,      -- stars / card / energy
    amount INTEGER NOT NULL DEFAULT 0,
    currency VARCHAR(8) NOT NULL,     -- XTR / rub / energy
    status VARCHAR(16) NOT NULL DEFAULT 'paid',
    charge_id TEXT UNIQUE,            -- защита от повторной доставки оплаты
    gift_code VARCHAR(32),
    source VARCHAR(64),
    lang VARCHAR(8),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS forecast_orders_user_idx ON forecast_orders (telegram_id, created_at);

CREATE TABLE IF NOT EXISTS forecasts_2027 (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    product VARCHAR(32) NOT NULL,
    calc TEXT NOT NULL,               -- расчёты и вытянутые карты (JSON) – один раз, дальше только из базы
    texts TEXT,                       -- {язык: [{key, text}]}
    lang VARCHAR(8) NOT NULL,         -- язык на момент покупки
    status VARCHAR(16) NOT NULL DEFAULT 'pending',   -- pending / ready / failed
    delivered BOOLEAN DEFAULT FALSE,  -- прогноз и PDF отправлены в чат
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (telegram_id, product)
);
