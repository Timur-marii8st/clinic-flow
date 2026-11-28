"""Bounded local HTTP load smoke on disposable SQLite or PostgreSQL.

Starts its own loopback server and synthetic database. Never targets a real
clinic. Reports client-observed HTTP latency; not a production SLA.
"""
import argparse
import asyncio
from collections import defaultdict
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from app.routing.eta import percentile

ROOT=Path(__file__).resolve().parents[1]
ROOMS=('101','102','103')


@contextmanager
def isolated_env(postgres):
    env={k:v for k,v in os.environ.items() if not k.startswith('CLINIC_')}
    env.update(CLINIC_MODE='demo',CLINIC_STAFF_PASSWORD='')
    if not postgres:
        with tempfile.TemporaryDirectory() as tmp:
            env['CLINIC_DB_PATH']=str(Path(tmp)/'load.db')
            yield env
        return
    raw=os.environ.get('TEST_POSTGRES_URL')
    if not raw:raise RuntimeError('Set TEST_POSTGRES_URL to a disposable test DB')
    url=make_url(raw)
    if url.drivername!='postgresql+psycopg':raise RuntimeError('Wrong test DB driver')
    schema='cf_load_'+uuid4().hex
    engine=create_engine(url)
    try:
        with engine.begin() as conn:conn.execute(text(f'CREATE SCHEMA {schema}'))
        isolated=url.update_query_dict({'options':f'-csearch_path={schema}'})
        cfg=Config(str(ROOT/'alembic.ini'));cfg.attributes['database_url']=isolated
        command.upgrade(cfg,'head')
        env['CLINIC_DATABASE_URL']=isolated.render_as_string(hide_password=False)
        yield env
    finally:
        with engine.begin() as conn:conn.execute(text(f'DROP SCHEMA IF EXISTS {schema} CASCADE'))
        engine.dispose()


async def run(base,screens,staff,kiosks,duration):
    samples=defaultdict(list);errors=[];counters=defaultdict(int)
    ready=asyncio.Event();stop=asyncio.Event()
    limits=httpx.Limits(max_connections=screens+staff+kiosks+25,max_keepalive_connections=staff+kiosks+20)
    async with httpx.AsyncClient(base_url=base,limits=limits,timeout=httpx.Timeout(15)) as client:
        async def req(method,path,group,body=None):
            start=time.perf_counter()
            try:
                options={'json':body} if body is not None else {}
                if method=='POST':options['headers']={'Idempotency-Key':uuid4().hex}
                r=await client.request(method,path,**options)
                r.raise_for_status()
                samples[group].append((time.perf_counter()-start)*1000)
                return r.json()
            except Exception as e:
                errors.append(group+':'+type(e).__name__)
                raise

        async def screen():
            first=True
            async with client.stream('GET','/api/events',timeout=httpx.Timeout(15,read=None)) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line not in ('event: refresh','event: heartbeat'):continue
                    await req('GET','/api/board','board_snapshot')
                    if first:
                        first=False;counters['screen_connections']+=1
                        if counters['screen_connections']==screens:ready.set()
            if not stop.is_set():raise RuntimeError('Unexpected SSE disconnect')

        async def reader(i):
            room=ROOMS[i%3]
            while not stop.is_set():
                await req('GET',f'/api/staff/{room}','staff_read')
                await asyncio.sleep(.8+(i%4)*.1)

        async def kiosk(i):
            room=ROOMS[i%3]
            while not stop.is_set():
                await req('POST','/api/walk-in','registration',{'room_id':room,'request_key':uuid4().hex})
                counters['issued']+=1
                await asyncio.sleep(1.5)

        async def operator(room):
            while not stop.is_set():
                waiting=await req('GET',f'/api/staff/{room}','operator_read')
                if any(t['eligible'] for t in waiting) and not any(t['status'] in ('called','serving') for t in waiting):
                    ticket=await req('POST',f'/api/staff/{room}/call-next','call',{})
                    counters['calls']+=1
                    path=f"/api/staff/{room}/tickets/{ticket['id']}/action"
                    await req('POST',path,'action',{'action':'start'})
                    await asyncio.sleep(1.0)
                    await req('POST',path,'action',{'action':'finish'})
                    counters['completed']+=1
                await asyncio.sleep(.4)

        display_tasks=[asyncio.create_task(screen()) for _ in range(screens)]
        writers=[]
        try:
            await asyncio.wait_for(ready.wait(),timeout=30)
            started=time.perf_counter()
            writers=[asyncio.create_task(reader(i)) for i in range(staff)]
            writers += [asyncio.create_task(kiosk(i)) for i in range(kiosks)]
            writers += [asyncio.create_task(operator(room)) for room in ROOMS]
            await asyncio.sleep(duration)
            stop.set()
            results=await asyncio.wait_for(asyncio.gather(*writers,return_exceptions=True),timeout=30)
            errors.extend(type(e).__name__ for e in results if isinstance(e,Exception))
            elapsed=time.perf_counter()-started
            for t in display_tasks:
                if t.done() and not t.cancelled() and t.exception():errors.append(type(t.exception()).__name__)
        finally:
            stop.set()
            for t in display_tasks+writers:
                if not t.done():t.cancel()
            await asyncio.gather(*display_tasks,*writers,return_exceptions=True)
    stats={group:{'n':len(v),'p50_ms':percentile(v,.5),'p95_ms':percentile(v,.95),'p99_ms':percentile(v,.99)}
           for group,v in sorted(samples.items())}
    result={'screens':screens,'staff_readers':staff,'kiosks':kiosks,'room_operators':3,
            'duration_seconds':round(elapsed,2),'counters':dict(counters),'http':stats,'errors':errors}
    print('LOAD_RESULT '+json.dumps(result,sort_keys=True),flush=True)
    if errors or counters['screen_connections']!=screens or not counters['issued'] or not counters['completed']:
        raise RuntimeError('Load smoke failed')
    print('PASS: bounded local synthetic load, no request errors')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--screens',type=int,default=50)
    parser.add_argument('--staff',type=int,default=20)
    parser.add_argument('--kiosks',type=int,default=5)
    parser.add_argument('--seconds',type=int,default=8)
    a=parser.parse_args()
    if not (1<=a.screens<=100 and 1<=a.staff<=50 and 1<=a.kiosks<=10 and 5<=a.seconds<=60):
        parser.error('Scenario outside demo bounds')
    with isolated_env(a.postgres) as env:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        with subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(port),'--no-access-log'],
                              cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL) as server:
            try:
                for _ in range(150):
                    try:
                        if httpx.get(base+'/health',timeout=.4).status_code==200:break
                    except httpx.HTTPError:pass
                    if server.poll() is not None:raise RuntimeError('Server exited')
                    time.sleep(.1)
                else:raise RuntimeError('Server did not start')
                asyncio.run(run(base,a.screens,a.staff,a.kiosks,a.seconds))
            finally:
                server.terminate()
                try:server.wait(timeout=5)
                except subprocess.TimeoutExpired:server.kill();server.wait()


if __name__=='__main__':main()
