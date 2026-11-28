"""Synthetic backups only. Never overwrite an existing backup or restore in place."""
import argparse
from contextlib import closing,contextmanager
from pathlib import Path
import os
import re
import sqlite3
import subprocess
import tempfile


@contextmanager
def new_file(destination):
    destination=Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    fd,path=tempfile.mkstemp(prefix='.clinic-backup-',dir=destination.parent)
    os.close(fd)
    temp=Path(path)
    try:
        yield temp
        with temp.open('rb') as handle:os.fsync(handle.fileno())
        # Atomic publish, refusing to replace an existing target, even on a race.
        os.link(temp,destination)
    finally:
        temp.unlink(missing_ok=True)


def sqlite_copy(source,destination):
    source=Path(source).resolve(strict=True)
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src:
        with new_file(destination) as temp:
            with closing(sqlite3.connect(temp)) as dst:
                src.backup(dst)
                if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                    raise RuntimeError('SQLite integrity check failed')
                if dst.execute('PRAGMA foreign_key_check').fetchone():
                    raise RuntimeError('SQLite foreign keys invalid')
                if not dst.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tickets'").fetchone():
                    raise RuntimeError('Not a Clinic Flow database')
                dst.commit()


def dbname(value):
    if not re.fullmatch(r'[A-Za-z0-9_]{1,63}',value):
        raise ValueError('Use a plain database name, not a URI or connection string')
    return value


EMPTY_SQL="""SELECT
 (SELECT count(*) FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname NOT IN ('information_schema','public'))+
 (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public')+
 (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public')+
 (SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='public')+
 (SELECT count(*) FROM pg_extension WHERE extname<>'plpgsql')"""


def pg_backup(database,destination,runner=()):
    database=dbname(database)
    with new_file(destination) as temp:
        with temp.open('wb') as out:
            subprocess.run([*runner,'pg_dump','--no-password','--format=custom','--no-owner','--no-acl','--dbname='+database],
                           stdout=out,check=True)
        if temp.stat().st_size==0:raise RuntimeError('Empty dump')


def pg_restore(database,source,runner=()):
    database=dbname(database)
    source=Path(source).resolve(strict=True)
    result=subprocess.check_output([*runner,'psql','-X','--no-password','-v','ON_ERROR_STOP=1','-At','--dbname='+database,'-c',EMPTY_SQL],text=True)
    if result.strip()!='0':
        raise ValueError('Restore target is not an empty database')
    # Target must be exclusively reserved by the operator. No --clean or --create.
    with source.open('rb') as handle:
        subprocess.run([*runner,'pg_restore','--list'],stdin=handle,stdout=subprocess.DEVNULL,check=True)
        handle.seek(0)
        subprocess.run([*runner,'pg_restore','--no-password','--single-transaction','--exit-on-error',
                        '--no-owner','--no-acl','--dbname='+database],stdin=handle,check=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm-demo',action='store_true',required=True)
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('sqlite-backup','sqlite-restore'):
        p=sub.add_parser(name);p.add_argument('source');p.add_argument('destination')
    p=sub.add_parser('pg-backup');p.add_argument('database');p.add_argument('destination')
    p=sub.add_parser('pg-restore');p.add_argument('source');p.add_argument('database')
    args=parser.parse_args()
    if args.command.startswith('sqlite'):
        sqlite_copy(args.source,args.destination)
    elif args.command=='pg-backup':
        pg_backup(args.database,args.destination)
    else:
        pg_restore(args.database,args.source)
    print('Demo backup/restore completed; verify in an isolated application before use')


if __name__=='__main__':main()
