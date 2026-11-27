"""HTTP API and single-process notification stream. Local demonstration only."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import hmac
import os
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .queue import Queue, QueueError

STATIC = Path(__file__).parent / 'static'


class CheckIn(BaseModel):
    booking_code: str = Field(min_length=1, max_length=32)


class WalkIn(BaseModel):
    room_id: str = Field(min_length=1, max_length=16)
    request_key: str | None = Field(default=None, min_length=16, max_length=100)


class Action(BaseModel):
    action: Literal['start', 'finish', 'no_show', 'cancel', 'repeat', 'requeue']
    expected_version: str | None = Field(default=None, max_length=64)


class Transfer(BaseModel):
    target_room: str = Field(min_length=1, max_length=16)
    expected_version: str | None = Field(default=None, max_length=64)


class DemoAppointment(BaseModel):
    booking_code: str = Field(min_length=6, max_length=32)
    room_id: str = Field(min_length=1, max_length=16)
    scheduled_at: datetime


class Changes:
    """Wake connected displays after a commit; no queue payload in the stream."""
    def __init__(self):
        self.condition = asyncio.Condition()
        self.generation = 0

    async def publish(self):
        async with self.condition:
            self.generation += 1
            self.condition.notify_all()

    async def wait(self, previous, timeout=10):
        async with self.condition:
            try:
                await asyncio.wait_for(self.condition.wait_for(lambda: self.generation != previous), timeout)
            except TimeoutError:
                pass
            return self.generation


def create_app(db_path: str | Path | None = None, staff_password: str | None = None) -> FastAPI:
    queue = Queue(db_path or os.environ.get('CLINIC_DB_PATH', 'data/clinic.sqlite3'))
    password = os.environ.get('CLINIC_STAFF_PASSWORD', '') if staff_password is None else staff_password
    basic = HTTPBasic(auto_error=False)
    changes = Changes()

    @asynccontextmanager
    async def lifespan(_app):
        await run_in_threadpool(queue.initialize)
        yield

    app = FastAPI(title='Clinic Flow Demo', version='0.2.0', lifespan=lifespan,
                  description='Локальный прототип. Только синтетические данные. Опциональный общий пароль персонала не заменяет промышленную IAM.')
    app.mount('/static', StaticFiles(directory=STATIC), name='static')

    def staff_only(credentials: HTTPBasicCredentials | None = Depends(basic)):
        if not password:
            return
        if not credentials or not (hmac.compare_digest(credentials.username.encode(), b'staff') and
                                    hmac.compare_digest(credentials.password.encode(), password.encode())):
            raise HTTPException(401, 'Требуется доступ персонала', headers={'WWW-Authenticate': 'Basic realm="Clinic Flow Demo"'})

    staff_deps = [Depends(staff_only)]

    async def mutate(method, *args):
        result = await run_in_threadpool(method, *args)
        await changes.publish()
        return result

    @app.exception_handler(QueueError)
    async def queue_error(_request: Request, exc: QueueError):
        return JSONResponse(status_code=exc.status, content={'detail': str(exc)})

    @app.middleware('http')
    async def headers(request: Request, call_next):
        # No cross-origin browser mutations. This is not a substitute for TLS/RBAC.
        origin = request.headers.get('origin')
        if request.method == 'POST' and (request.headers.get('sec-fetch-site') == 'cross-site' or
                (origin and origin != f'{request.url.scheme}://{request.url.netloc}') or
                request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json'):
            response = JSONResponse(status_code=403, content={'detail': 'Разрешены только same-origin JSON запросы'})
        else:
            response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @app.get('/', include_in_schema=False)
    @app.get('/kiosk', include_in_schema=False)
    @app.get('/board', include_in_schema=False)
    def ui():
        return FileResponse(STATIC / 'index.html')

    @app.get('/staff', include_in_schema=False, dependencies=staff_deps)
    @app.get('/analytics', include_in_schema=False, dependencies=staff_deps)
    def staff_ui():
        return FileResponse(STATIC / 'index.html')

    @app.get('/health')
    def health():
        queue.rooms()
        return {'status': 'ok', 'mode': 'demo', 'version': '0.2.0'}

    @app.get('/api/rooms')
    def rooms():
        return queue.rooms()

    @app.post('/api/check-in')
    async def check_in(data: CheckIn):
        return await mutate(queue.check_in, data.booking_code.strip().upper())

    @app.post('/api/walk-in', status_code=201)
    async def walk_in(data: WalkIn):
        return await mutate(queue.walk_in, data.room_id, data.request_key)

    @app.post('/api/staff/demo-appointments', dependencies=staff_deps, status_code=201)
    async def book_demo(data: DemoAppointment):
        return await mutate(queue.book_demo, data.booking_code, data.room_id, data.scheduled_at)

    @app.get('/api/staff/{room_id}', dependencies=staff_deps)
    def staff_view(room_id: str):
        return queue.staff_view(room_id)

    @app.get('/api/staff/{room_id}/no-shows', dependencies=staff_deps)
    def no_shows(room_id: str):
        return queue.recent_no_shows(room_id)

    @app.get('/api/staff/tickets/{ticket_id}/history', dependencies=staff_deps)
    def history(ticket_id: int):
        return queue.history(ticket_id)

    @app.get('/api/analytics', dependencies=staff_deps)
    def analytics():
        return queue.analytics()

    @app.post('/api/staff/{room_id}/call-next', dependencies=staff_deps)
    async def call_next(room_id: str):
        return await mutate(queue.call_next, room_id)

    @app.post('/api/staff/{room_id}/tickets/{ticket_id}/action', dependencies=staff_deps)
    async def ticket_action(room_id: str, ticket_id: int, data: Action):
        return await mutate(queue.act, room_id, ticket_id, data.action, data.expected_version)

    @app.post('/api/staff/{room_id}/tickets/{ticket_id}/transfer', dependencies=staff_deps)
    async def transfer(room_id: str, ticket_id: int, data: Transfer):
        return await mutate(queue.transfer, room_id, ticket_id, data.target_room, data.expected_version)

    @app.get('/api/board')
    def board():
        return queue.board()

    @app.get('/api/events', include_in_schema=False)
    async def events(request: Request):
        async def stream():
            version = changes.generation
            yield 'event: refresh\ndata: {}\n\n'
            while not await request.is_disconnected():
                new_version = await changes.wait(version)
                if new_version != version:
                    version = new_version
                    yield 'event: refresh\ndata: {}\n\n'
                else:
                    # Also refresh after a time slot becomes due or a different
                    # process commits. Use one worker for immediate updates.
                    yield 'event: heartbeat\ndata: {}\n\n'
        return StreamingResponse(stream(), media_type='text/event-stream',
                                 headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-store'})

    return app


app = create_app()
