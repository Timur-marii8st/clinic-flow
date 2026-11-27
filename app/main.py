"""HTTP API and single-process notification stream. Local demonstration only."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import hmac
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .queue import Queue, PostgresQueue, QueueError
from .repositories.postgres import database_url_from_env
from .auth.oidc import SecuritySettings, OIDCClient
from .auth.store import AuthStore, acting_as
from .auth.http import install_routes, resolve, policy, require_room, STAFF, QUEUE, BOARD, REGISTRATION

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


class RoomConfig(BaseModel):
    room_id: str = Field(min_length=1, max_length=16, pattern=r'^[A-Za-z0-9_-]+$')
    label: str = Field(min_length=1, max_length=100)


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


def create_app(db_path: str | Path | None = None, staff_password: str | None = None,
               database_url=None, *, security_settings=None, oidc_client=None) -> FastAPI:
    if db_path is not None and database_url is not None:
        raise ValueError('Choose exactly one database')
    url = database_url if database_url is not None else (database_url_from_env() if db_path is None else None)
    queue = PostgresQueue(url) if url else Queue(db_path or os.environ.get('CLINIC_DB_PATH', 'data/clinic.sqlite3'))
    password = os.environ.get('CLINIC_STAFF_PASSWORD', '') if staff_password is None else staff_password
    basic = HTTPBasic(auto_error=False)
    changes = Changes()
    settings = security_settings if security_settings is not None else SecuritySettings.from_env()
    if settings and not url and db_path is None:
        raise RuntimeError('Secure profile requires PostgreSQL; SQLite is available only for explicit test instances')
    auth = AuthStore(queue, settings.bootstrap_subject if settings else None)
    oidc = oidc_client or (OIDCClient(settings) if settings else None)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            await run_in_threadpool(queue.initialize)
            await run_in_threadpool(auth.initialize)
            yield
        finally:
            await run_in_threadpool(queue.close)

    app = FastAPI(title='Clinic Flow', version='0.4.0', lifespan=lifespan,
                  docs_url=None if settings else '/docs', redoc_url=None if settings else '/redoc',
                  openapi_url=None if settings else '/openapi.json',
                  description='Только синтетические данные. Secure-профиль требует OIDC, HTTPS и зарегистрированные устройства.')
    app.mount('/static', StaticFiles(directory=STATIC), name='static')

    def staff_only(credentials: HTTPBasicCredentials | None = Depends(basic)):
        if settings or not password:
            return
        if not credentials or not (hmac.compare_digest(credentials.username.encode(), b'staff') and
                                    hmac.compare_digest(credentials.password.encode(), password.encode())):
            raise HTTPException(401, 'Требуется доступ персонала', headers={'WWW-Authenticate': 'Basic realm="Clinic Flow Demo"'})

    staff_deps = [Depends(staff_only)]

    async def mutate(request, method, *args):
        principal = getattr(request.state, 'principal', None)
        actor = principal.actor if principal else 'demo'
        def execute():
            with acting_as(actor):
                return method(*args)
        result = await run_in_threadpool(execute)
        await changes.publish()
        return result

    @app.exception_handler(QueueError)
    async def queue_error(_request: Request, exc: QueueError):
        return JSONResponse(status_code=exc.status, content={'detail': str(exc)})

    @app.middleware('http')
    async def headers(request: Request, call_next):
        # Secure mode uses a configured HTTPS origin, never an untrusted Host header.
        origin = request.headers.get('origin')
        expected_origin = settings.public_origin if settings else f'{request.url.scheme}://{request.url.netloc}'
        request.state.principal = None
        if settings and request.url.path != '/health' and request.headers.get('host','').lower() != urlsplit(settings.public_origin).netloc.lower():
            response = JSONResponse({'detail':'Некорректный Host'},status_code=400)
        elif request.method == 'POST' and (request.headers.get('sec-fetch-site') == 'cross-site' or
                (origin != expected_origin if settings else bool(origin and origin != expected_origin)) or
                request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json'):
            response = JSONResponse(status_code=403, content={'detail': 'Разрешены только same-origin JSON запросы'})
        elif settings and request.method not in ('GET','POST','HEAD'):
            response = JSONResponse(status_code=405, content={'detail':'Метод не разрешён'})
        elif settings:
            request.state.principal = await resolve(request,auth)
            if request.url.path.startswith('/api/'):
                denied = policy(request.method,request.url.path,request.state.principal)
                response = JSONResponse({'detail':denied[1]},status_code=denied[0]) if denied else await call_next(request)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        if settings:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
            response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        return response

    @app.get('/', include_in_schema=False)
    @app.get('/pair', include_in_schema=False)
    @app.get('/kiosk', include_in_schema=False)
    @app.get('/board', include_in_schema=False)
    def ui(request: Request):
        if settings and request.url.path in ('/board','/kiosk'):
            principal = request.state.principal
            roles = BOARD if request.url.path == '/board' else REGISTRATION
            if not principal:
                return RedirectResponse('/pair',status_code=303)
            if principal.role not in roles:
                raise HTTPException(403,'Недостаточно прав')
        return FileResponse(STATIC / 'index.html')

    @app.get('/staff', include_in_schema=False, dependencies=staff_deps)
    @app.get('/analytics', include_in_schema=False, dependencies=staff_deps)
    @app.get('/admin', include_in_schema=False, dependencies=staff_deps)
    def staff_ui(request: Request):
        if settings:
            principal = request.state.principal
            path=request.url.path
            if not principal:
                return RedirectResponse('/auth/login?next='+path,status_code=303)
            roles = QUEUE if path=='/staff' else {'admin','analyst'} if path=='/analytics' else {'admin'}
            if principal.role not in roles:
                raise HTTPException(403,'Недостаточно прав')
        elif request.url.path=='/admin':
            raise HTTPException(404,'Доступно только в secure-профиле')
        return FileResponse(STATIC / 'index.html')

    install_routes(app,settings,auth,oidc)

    @app.get('/health')
    def health():
        queue.rooms()
        return {'status': 'ok', 'mode': 'secure' if settings else 'demo', 'version': '0.4.0'}

    @app.get('/api/rooms')
    def rooms(request: Request):
        rooms = queue.rooms()
        principal = request.state.principal
        return [r for r in rooms if principal.permits_room(r['id'])] if settings and principal.role in ('doctor','board') else rooms

    @app.post('/api/check-in')
    async def check_in(data: CheckIn,request: Request):
        return await mutate(request,queue.check_in, data.booking_code.strip().upper())

    @app.post('/api/walk-in', status_code=201)
    async def walk_in(data: WalkIn,request: Request):
        return await mutate(request,queue.walk_in, data.room_id, data.request_key)

    @app.post('/api/staff/demo-appointments', dependencies=staff_deps, status_code=201)
    async def book_demo(data: DemoAppointment,request: Request):
        return await mutate(request,queue.book_demo, data.booking_code, data.room_id, data.scheduled_at)

    @app.post('/api/staff/rooms', dependencies=staff_deps)
    async def configure_room(data: RoomConfig,request: Request):
        return await mutate(request,queue.upsert_room, data.room_id, data.label)

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
    async def call_next(room_id: str, request: Request, request_key: str | None = Header(default=None, alias='Idempotency-Key', min_length=16, max_length=100)):
        return await mutate(request,queue.call_next, room_id, request_key)

    @app.post('/api/staff/{room_id}/tickets/{ticket_id}/action', dependencies=staff_deps)
    async def ticket_action(room_id: str, ticket_id: int, data: Action, request: Request,
                            request_key: str | None = Header(default=None, alias='Idempotency-Key', min_length=16, max_length=100)):
        return await mutate(request,queue.act, room_id, ticket_id, data.action, data.expected_version, request_key)

    @app.post('/api/staff/{room_id}/tickets/{ticket_id}/transfer', dependencies=staff_deps)
    async def transfer(room_id: str, ticket_id: int, data: Transfer, request: Request,
                       request_key: str | None = Header(default=None, alias='Idempotency-Key', min_length=16, max_length=100)):
        if settings:
            require_room(request.state.principal,data.target_room)
        return await mutate(request,queue.transfer, room_id, ticket_id, data.target_room, data.expected_version, request_key)

    @app.get('/api/board')
    def board(request: Request):
        rows = queue.board()
        return [r for r in rows if request.state.principal.permits_room(r['room_id'])] if settings else rows

    @app.get('/api/events', include_in_schema=False)
    async def events(request: Request):
        async def stream():
            version = changes.generation
            yield 'event: refresh\ndata: {}\n\n'
            while not await request.is_disconnected():
                new_version = await changes.wait(version)
                if settings and await resolve(request,auth) != request.state.principal:
                    return
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
