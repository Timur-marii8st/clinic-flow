"""PostgreSQL tests use isolated temporary schemas in a designated test database."""
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def postgres_url():
    raw = os.environ.get('TEST_POSTGRES_URL')
    if not raw:
        pytest.skip('Set TEST_POSTGRES_URL to a disposable PostgreSQL test DB')
    url = make_url(raw)
    if url.drivername != 'postgresql+psycopg':
        pytest.fail('TEST_POSTGRES_URL must use postgresql+psycopg')
    engine = create_engine(url)
    schema = 'cf_test_' + uuid4().hex
    try:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA {schema}'))
        isolated = url.update_query_dict({'options': f'-csearch_path={schema}'})
        cfg = Config(str(ROOT / 'alembic.ini'))
        cfg.attributes['database_url'] = isolated
        command.upgrade(cfg, 'head')
        yield isolated
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS {schema} CASCADE'))
        engine.dispose()
