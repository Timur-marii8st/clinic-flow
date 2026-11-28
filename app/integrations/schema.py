"""Additive MIS synchronization schema shared by both stores."""
DDL = [
    "CREATE TABLE IF NOT EXISTS mis_appointments (source TEXT NOT NULL, external_id TEXT NOT NULL, version BIGINT NOT NULL, snapshot TEXT NOT NULL, ticket_id BIGINT UNIQUE REFERENCES tickets(id), conflict TEXT, PRIMARY KEY(source,external_id))",
    "CREATE TABLE IF NOT EXISTS mis_inbox (source TEXT NOT NULL, event_id TEXT NOT NULL, fingerprint TEXT NOT NULL, outcome TEXT NOT NULL, received_at TEXT NOT NULL, PRIMARY KEY(source,event_id))",
    "CREATE TABLE IF NOT EXISTS mis_cursors (source TEXT PRIMARY KEY, position BIGINT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS mis_outbox (event_id TEXT PRIMARY KEY, sequence BIGINT NOT NULL UNIQUE REFERENCES events(id), source TEXT NOT NULL, external_id TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','inflight','delivered','dead')), attempts INTEGER NOT NULL DEFAULT 0, available_at BIGINT NOT NULL, lease_until BIGINT, lease_token TEXT, last_error TEXT, delivered_at TEXT)",
    "CREATE INDEX IF NOT EXISTS mis_outbox_due ON mis_outbox(source,state,available_at,sequence)",
    "CREATE INDEX IF NOT EXISTS mis_outbox_order ON mis_outbox(source,external_id,sequence,state)"
]
