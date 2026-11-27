"""One-time, read-only source import into an empty PostgreSQL demo schema.

Stop the old application and make a backup before using this utility. Never
use it for clinical data. Run migrations first, but don't start the new app
(it seeds demo rooms). Source v0.2/v0.3 only.
"""
import argparse
from contextlib import closing
from pathlib import Path
import sqlite3

from sqlalchemy import create_engine, text

from app.repositories.postgres import database_url_from_env, validate_url


TABLES = {
    'rooms': ('id','label'),
    'tickets': ('id','room_id','source','booking_code','status','created_at','checked_in_at',
                'updated_at','scheduled_at','request_key','booking_room_id','booking_scheduled_at'),
    'events': ('id','ticket_id','action','old_status','new_status','created_at'),
    'queue_legs': ('id','ticket_id','room_id','entered_at','called_at','started_at','ended_at','outcome'),
    'command_keys': ('request_key','fingerprint','response','created_at'),
}


def import_demo(source, target_url):
    path = Path(source).resolve(strict=True)
    engine = create_engine(validate_url(target_url), pool_pre_ping=True)
    try:
        # Keep a consistent source snapshot; do not write to the legacy DB.
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as src:
            src.row_factory = sqlite3.Row
            src.execute('BEGIN')
            version = src.execute('PRAGMA user_version').fetchone()[0]
            if version not in (2,3):
                raise ValueError('Expected SQLite demo schema version 2 or 3')
            if src.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or src.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('Source database integrity check failed')
            counts = {}
            with engine.begin() as dst:
                dst.execute(text('SELECT pg_advisory_xact_lock(hashtext(current_schema()), 31303)'))
                if dst.execute(text('SELECT version_num FROM alembic_version')).scalar_one_or_none() != '0001_core':
                    raise ValueError('Target schema must be at 0001_core')
                # Never merge into or truncate a populated target.
                for table in TABLES:
                    if dst.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar_one():
                        raise ValueError('Target must be empty; initialize a fresh demo database')
                for table, fields in TABLES.items():
                    if table == 'command_keys' and version == 2:
                        counts[table] = 0
                        continue
                    cols = ','.join(fields)
                    stmt = text(f'INSERT INTO {table} ({cols}) VALUES (' + ','.join(':'+f for f in fields) + ')')
                    cursor = src.execute(f'SELECT {cols} FROM {table}')
                    counts[table] = 0
                    while batch := cursor.fetchmany(500):
                        dst.execute(stmt, [dict(row) for row in batch])
                        counts[table] += len(batch)
                    if dst.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar_one() != counts[table]:
                        raise RuntimeError(f'Row count mismatch: {table}')
                # Explicit identity values do not advance PostgreSQL sequences.
                for table in ('tickets','events','queue_legs'):
                    dst.execute(text(f"""SELECT setval(CAST(pg_get_serial_sequence(:table_name,'id') AS regclass),
                        COALESCE(MAX(id),1), COUNT(*)>0) FROM {table}"""), {'table_name':table})
            return counts
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', help='Path to a backed-up SQLite demo database')
    parser.add_argument('--confirm-demo', action='store_true', required=True,
                        help='Confirm only synthetic demo data and an empty destination')
    args = parser.parse_args()
    url = database_url_from_env()
    if not url:
        parser.error('Set CLINIC_DATABASE_URL or CLINIC_POSTGRES_*')
    counts = import_demo(args.source, url)
    print('Import committed:', counts)


if __name__ == '__main__':
    main()
