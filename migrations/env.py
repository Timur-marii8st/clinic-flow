"""Explicit PostgreSQL migrations; never run against SQLite."""
from alembic import context
from sqlalchemy import create_engine, pool
from app.repositories.postgres import database_url_from_env, validate_url

config = context.config


def run_migrations(connection):
    context.configure(connection=connection, target_metadata=None, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()


def online():
    # Allow tests and the migration utility to supply an isolated connection.
    supplied = config.attributes.get('connection')
    if supplied is not None:
        run_migrations(supplied)
        return
    url = config.attributes.get('database_url') or database_url_from_env()
    if not url:
        raise RuntimeError('Set CLINIC_DATABASE_URL or CLINIC_POSTGRES_*')
    engine = create_engine(validate_url(url), poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            run_migrations(conn)
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError('Offline migration generation is not supported in this demo')
else:
    online()
