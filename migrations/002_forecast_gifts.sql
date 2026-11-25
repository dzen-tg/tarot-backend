-- «Мой 2027», этап 2: подарки. Покупатель платит, получает одноразовую ссылку start=gift_<код>,
-- получатель открывает её, вводит свои данные и получает прогноз. Только добавление таблицы.
CREATE TABLE IF NOT EXISTS forecast_gifts (
    code VARCHAR(24) PRIMARY KEY,
    buyer_id BIGINT NOT NULL,
    product VARCHAR(32) NOT NULL,
    order_charge_id TEXT,             -- оплата, которой куплен подарок
    redeemed_by BIGINT,
    redeemed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS forecast_gifts_buyer_idx ON forecast_gifts (buyer_id, created_at);
