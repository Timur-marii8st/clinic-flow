"""SQLAlchemy Core adapter for PostgreSQL, using psycopg 3.

The SQL subset in QueueService uses DB-API qmarks for compatibility with the
SQLite demo. Convert those *fixed internal statements* to bound parameters;
never interpolate user data. The queue service uses mapping/positional rows.
"""
from contextlib import contextmanager
import os

from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import make_url


def database_url_from_env():
    if url := os.environ.get('CLINIC_DATABASE_URL'):
        return validate_url(url)
    if host := os.environ.get('CLINIC_POSTGRES_HOST'):
        return URL.create('postgresql+psycopg', username=os.environ.get('CLINIC_POSTGRES_USER', 'clinic'),
                          password=os.environ['CLINIC_POSTGRES_PASSWORD'], host=host,
                          port=int(os.environ.get('CLINIC_POSTGRES_PORT', '5432')),
                          database=os.environ.get('CLINIC_POSTGRES_DB', 'clinic'))
    return None


def validate_url(url):
    url = make_url(url)
    if url.drivername != 'postgresql+psycopg':
        raise ValueError('Use a postgresql+psycopg URL')
    return url


class Row(dict):
    def __getitem__(self, key):
        return tuple(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class Result:
    def __init__(self, result):
        self.result = result

    def fetchone(self):
        row = self.result.fetchone()
        return Row(row._mapping) if row is not None else None

    def fetchall(self):
        return list(self)

    def __iter__(self):
        for row in self.result:
            yield Row(row._mapping)


class Transaction:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, args=()):
        parts = sql.split('?')
        if len(parts) != len(args) + 1:
            raise ValueError('SQL bind parameter mismatch')
        statement = parts[0]
        binds = {}
        for i, value in enumerate(args):
            name = f'p{i}'
            statement += ':' + name + parts[i + 1]
            binds[name] = value
        return Result(self.conn.execute(text(statement), binds))


class PostgresStore:
    def __init__(self, url):
        self.engine = create_engine(validate_url(url), pool_pre_ping=True, pool_size=5, max_overflow=5)

    @contextmanager
    def connection(self, write=False):
        with self.engine.connect() as conn:
            # READ COMMITTED is essential for writes: after waiting for the
            # advisory lock the next statement sees the preceding commit.
            conn = conn.execution_options(isolation_level='READ COMMITTED' if write else 'REPEATABLE READ')
            with conn.begin():
                if write:
                    # Serialize commands per schema for the single-clinic demo.
                    # All writers must use this adapter. Room-level locking is
                    # a later scalability improvement; do not bypass the lock.
                    conn.execute(text('SELECT pg_advisory_xact_lock(hashtext(current_schema()), 31303)'))
                yield Transaction(conn)

    def initialize(self):
        with self.engine.connect() as conn:
            from sqlalchemy import inspect
            if not inspect(conn).has_table('alembic_version'):
                raise RuntimeError('PostgreSQL schema missing: run alembic upgrade head')
            revision = conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one_or_none()
            if revision != '0001_core':
                raise RuntimeError('PostgreSQL schema mismatch: run the current migrations')

    def close(self):
        self.engine.dispose()
