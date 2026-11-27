"""Local HTTP smoke: POST commits, connected SSE subscribers receive notification."""
from pathlib import Path
import os
import socket
import subprocess
import sys
import tempfile
import time
import httpx

ROOT = Path(__file__).resolve().parents[1]


def next_event(lines):
    event = None
    for line in lines:
        if line.startswith('event: '):
            event = line[7:]
        if not line and event:
            return event
    raise AssertionError('SSE stream closed')


with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
base = f'http://127.0.0.1:{port}'
with tempfile.TemporaryDirectory() as tmp:
    env = {**os.environ, 'CLINIC_DB_PATH': str(Path(tmp) / 'test.db'), 'CLINIC_STAFF_PASSWORD': ''}
    with subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as server:
        try:
            for _ in range(100):
                try:
                    if httpx.get(base + '/health', timeout=0.4).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if server.poll() is not None:
                    raise RuntimeError('Server exited')
                time.sleep(0.1)
            else:
                raise RuntimeError('Server did not start')
            with httpx.Client(timeout=5) as first, httpx.Client(timeout=5) as second, httpx.Client(timeout=5) as writer:
                with first.stream('GET', base + '/api/events') as s1, second.stream('GET', base + '/api/events') as s2:
                    assert s1.status_code == s2.status_code == 200
                    assert s1.headers['content-type'].startswith('text/event-stream')
                    a,b = s1.iter_lines(), s2.iter_lines()
                    assert next_event(a) == next_event(b) == 'refresh'
                    ticket = writer.post(base + '/api/walk-in', json={'room_id':'101','request_key':'test-network-key-123'}).json()
                    assert next_event(a) == next_event(b) == 'refresh'
                    t0 = time.perf_counter()
                    r = writer.post(base + '/api/staff/101/call-next', json={})
                    assert r.status_code == 200 and r.json()['id'] == ticket['id']
                    assert next_event(a) == next_event(b) == 'refresh'
                    board = writer.get(base + '/api/board').json()
                    assert board[0]['code'] == ticket['code']
                    print(f'POST + notification to 2 subscribers + board GET: {(time.perf_counter()-t0)*1000:.1f} ms (one local run)')
                with first.stream('GET', base + '/api/events') as reconnect:
                    assert next_event(reconnect.iter_lines()) == 'refresh'
                assert writer.get(base + '/api/board').json()[0]['code'] == ticket['code']
            print('PASS: actual HTTP streaming, 2 subscribers, commit notification, snapshot and reconnect')
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill(); server.wait()
