CREATE TABLE IF NOT EXISTS users(user_id BIGSERIAL PRIMARY KEY,telegram_id BIGINT UNIQUE,paypal_email VARCHAR(255),total_points DECIMAL(10,2) DEFAULT 0 CHECK(total_points>=0),created_at TIMESTAMP DEFAULT NOW());
CREATE TABLE IF NOT EXISTS transactions(tx_id BIGSERIAL PRIMARY KEY,user_id BIGINT REFERENCES users(user_id),crypto_tx_hash VARCHAR(255) UNIQUE,amount_usd DECIMAL(10,2),points_earned DECIMAL(10,2),status VARCHAR(20) DEFAULT 'pending',created_at TIMESTAMP DEFAULT NOW());
CREATE TABLE IF NOT EXISTS redemptions(redemption_id BIGSERIAL PRIMARY KEY,user_id BIGINT REFERENCES users(user_id),points_spent DECIMAL(10,2),paypal_email VARCHAR(255),status VARCHAR(20) DEFAULT 'processing',browser_session_id VARCHAR(255),error_message TEXT,created_at TIMESTAMP DEFAULT NOW());
CREATE INDEX IF NOT EXISTS idx_users_tg ON users(telegram_id);CREATE INDEX IF NOT EXISTS idx_tx_hash ON transactions(crypto_tx_hash);

ALTER TABLE users ADD COLUMN IF NOT EXISTS banned BOOLEAN DEFAULT FALSE;
ALTER TABLE redemptions ADD COLUMN IF NOT EXISTS gift_url TEXT;
