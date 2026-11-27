"""Shared additive security schema for SQLite and PostgreSQL."""
DDL = [
    "CREATE TABLE IF NOT EXISTS staff_users (subject TEXT PRIMARY KEY, label TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','registrar','doctor','analyst')), room_ids TEXT NOT NULL, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)))",
    "CREATE TABLE IF NOT EXISTS staff_sessions (token_hash TEXT PRIMARY KEY, subject TEXT NOT NULL REFERENCES staff_users(subject), expires_at BIGINT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS staff_sessions_subject ON staff_sessions(subject)",
    "CREATE TABLE IF NOT EXISTS oidc_attempts (state_hash TEXT PRIMARY KEY, verifier TEXT NOT NULL, nonce TEXT NOT NULL, return_to TEXT NOT NULL, expires_at BIGINT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, label TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('board','kiosk')), room_id TEXT REFERENCES rooms(id), pairing_hash TEXT UNIQUE, pairing_expires BIGINT, token_hash TEXT UNIQUE, token_expires BIGINT, last_seen BIGINT, revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)))",
    "CREATE TABLE IF NOT EXISTS audit_events (id TEXT PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, object_type TEXT NOT NULL, object_id TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS audit_events_time ON audit_events(created_at,id)"
]
