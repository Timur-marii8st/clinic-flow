DDL = [
    "CREATE TABLE IF NOT EXISTS route_templates (id TEXT NOT NULL, version INTEGER NOT NULL, label TEXT NOT NULL, plan TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(id,version))",
    "CREATE TABLE IF NOT EXISTS visit_routes (ticket_id BIGINT PRIMARY KEY REFERENCES tickets(id), template_id TEXT NOT NULL, template_version INTEGER NOT NULL, active_step TEXT NOT NULL, completed TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','complete','cancelled')), attached_at TEXT NOT NULL, ended_at TEXT, FOREIGN KEY(template_id,template_version) REFERENCES route_templates(id,version))",
    "CREATE INDEX IF NOT EXISTS route_state_end ON visit_routes(state,ended_at)"
]
