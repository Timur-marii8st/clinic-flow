"""Durable inbox, outbox, cursors and external appointment mapping."""
from alembic import op

revision = '0003_integration'
down_revision = '0002_security'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE TABLE IF NOT EXISTS mis_appointments (source TEXT NOT NULL, external_id TEXT NOT NULL, version BIGINT NOT NULL, snapshot TEXT NOT NULL, ticket_id BIGINT UNIQUE REFERENCES tickets(id), conflict TEXT, PRIMARY KEY(source,external_id))")
    op.execute("CREATE TABLE IF NOT EXISTS mis_inbox (source TEXT NOT NULL, event_id TEXT NOT NULL, fingerprint TEXT NOT NULL, outcome TEXT NOT NULL, received_at TEXT NOT NULL, PRIMARY KEY(source,event_id))")
    op.execute("CREATE TABLE IF NOT EXISTS mis_cursors (source TEXT PRIMARY KEY, position BIGINT NOT NULL)")
    op.execute("CREATE TABLE IF NOT EXISTS mis_outbox (event_id TEXT PRIMARY KEY, sequence BIGINT NOT NULL UNIQUE REFERENCES events(id), source TEXT NOT NULL, external_id TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','inflight','delivered','dead')), attempts INTEGER NOT NULL DEFAULT 0, available_at BIGINT NOT NULL, lease_until BIGINT, lease_token TEXT, last_error TEXT, delivered_at TEXT)")
    op.execute("CREATE INDEX IF NOT EXISTS mis_outbox_due ON mis_outbox(source,state,available_at,sequence)")
    op.execute("CREATE INDEX IF NOT EXISTS mis_outbox_order ON mis_outbox(source,external_id,sequence,state)")


def downgrade():
    raise RuntimeError('Restore a pre-upgrade backup instead of deleting integration history')
