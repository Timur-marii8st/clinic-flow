"""CI-only: actual PG17 dump/restore into two new databases in the service container."""
from pathlib import Path
import os
import subprocess
import tempfile
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from app.queue import PostgresQueue
from app.routing.service import Routing
from scripts.demo_backup import pg_backup,pg_restore

ROOT=Path(__file__).resolve().parents[1]


def main():
    container=os.environ['CF_TEST_PG_CONTAINER']
    base=make_url(os.environ['TEST_POSTGRES_URL'])
    suffix=uuid4().hex[:12]
    src='cf_backup_src_'+suffix;dst='cf_backup_dst_'+suffix
    runner=('docker','exec','-i','-e','PGUSER='+base.username,container)
    def run(*args):subprocess.run([*runner,*args],check=True)
    try:
        for name in (src,dst):run('createdb','--template=template0',name)
        source_url=base.set(database=src);dest_url=base.set(database=dst)
        cfg=Config(str(ROOT/'alembic.ini'));cfg.attributes['database_url']=source_url
        command.upgrade(cfg,'head')
        source=PostgresQueue(source_url);source.initialize()
        try:
            t=source.walk_in('101')
            r=Routing(source)
            r.create_template('test','Test',{'steps':[{'id':'one','label':'Test','rooms':['101']}]},0)
            r.attach('101',t['id'],'test',1,'one',source.staff_view('101')[0]['version'])
            with tempfile.TemporaryDirectory() as tmp:
                dump=Path(tmp)/'demo.dump'
                pg_backup(src,dump,runner)
                source.call_next('101')
                pg_restore(dst,dump,runner)
                restored=PostgresQueue(dest_url)
                try:
                    restored.initialize()
                    view=restored.staff_view('101')[0]
                    assert view['id']==t['id'] and view['status']=='waiting'
                    assert view['route']['template_id']=='test'
                    assert restored.walk_in('101')['id']>t['id']
                    try:pg_restore(dst,dump,runner)
                    except ValueError:pass
                    else:raise AssertionError('Refused to reject nonempty restore target')
                finally:restored.close()
        finally:source.close()
        print('PASS: PostgreSQL 17 pg_dump and pg_restore, route history and sequence, nonempty target refused')
    finally:
        for name in (dst,src):
            subprocess.run([*runner,'dropdb','--if-exists',name],check=True)


if __name__=='__main__':main()
