"""Add QR credentials and device telemetry without rewriting the queue."""
from alembic import op
revision = '0004_devices'
down_revision = '0003_integration'
branch_labels = None
depends_on = None

def upgrade():
    op.execute("CREATE TABLE IF NOT EXISTS qr_credentials (ticket_id BIGINT PRIMARY KEY REFERENCES tickets(id), token_hash TEXT NOT NULL UNIQUE, expires_at BIGINT NOT NULL, consumed_at BIGINT, request_hash TEXT, response TEXT)")
    op.execute("CREATE TABLE IF NOT EXISTS device_telemetry (device_id TEXT PRIMARY KEY REFERENCES devices(id), page TEXT NOT NULL, state TEXT NOT NULL, age_seconds INTEGER, audio_ready INTEGER NOT NULL, client_version TEXT NOT NULL, observed_at BIGINT NOT NULL)")

def downgrade():
    raise RuntimeError('Restore pre-upgrade backup rather than delete credential history')
