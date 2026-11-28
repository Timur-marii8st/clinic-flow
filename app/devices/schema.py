DDL = [
    "CREATE TABLE IF NOT EXISTS qr_credentials (ticket_id BIGINT PRIMARY KEY REFERENCES tickets(id), token_hash TEXT NOT NULL UNIQUE, expires_at BIGINT NOT NULL, consumed_at BIGINT, request_hash TEXT, response TEXT)",
    "CREATE TABLE IF NOT EXISTS device_telemetry (device_id TEXT PRIMARY KEY REFERENCES devices(id), page TEXT NOT NULL, state TEXT NOT NULL, age_seconds INTEGER, audio_ready INTEGER NOT NULL, client_version TEXT NOT NULL, observed_at BIGINT NOT NULL)"
]
