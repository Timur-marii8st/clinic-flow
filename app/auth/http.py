"""HTTP authorization policy for the single-clinic secure profile.

The API is deny-by-default. All POSTs in secure mode must have the configured
same Origin, including pairing and logout. Demo mode keeps v0.3 behavior.
"""
import hmac
import re

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from starlette.concurrency import run_in_threadpool

from .store import SESSION_TTL, DEVICE_TTL, LOGIN_TTL, Principal

SESSION='__Host-cf_session'
DEVICE='__Host-cf_device'
STATE='__Host-cf_state'

STAFF={'admin','registrar','doctor','analyst'}
QUEUE={'admin','registrar','doctor'}
REGISTRATION={'admin','registrar','kiosk'}
BOARD=QUEUE|{'board'}
RETURN_TO={'/','/staff','/analytics','/admin','/board','/kiosk'}


def set_cookie(response,name,value,ttl):
    response.set_cookie(name,value,max_age=ttl,path='/',secure=True,httponly=True,samesite='lax')


def clear_cookie(response,name):
    response.delete_cookie(name,path='/',secure=True,httponly=True,samesite='lax')


class UserConfig(BaseModel):
    subject: str = Field(min_length=1,max_length=255)
    label: str = Field(min_length=1,max_length=100)
    role: Literal['admin','registrar','doctor','analyst']
    rooms: list[str] = Field(default_factory=list,max_length=100)
    enabled: bool = True


class DeviceConfig(BaseModel):
    label: str = Field(min_length=1,max_length=100)
    kind: Literal['board','kiosk']
    room_id: str | None = Field(default=None,max_length=16)


class DeviceHeartbeat(BaseModel):
    model_config=ConfigDict(extra='forbid')
    page: Literal['board','kiosk','unknown']='unknown'
    state: Literal['ready','stale','starting','unknown']='unknown'
    age_seconds: int | None=Field(default=None,ge=0,le=86400,strict=True)
    audio_ready: bool=False
    client_version: str=Field(default='legacy',max_length=24,pattern=r'^[A-Za-z0-9_.-]+$')


class PairRequest(BaseModel):
    pairing_code: str = Field(min_length=32,max_length=128)


def require_role(principal, roles):
    if principal is None:
        raise HTTPException(401,'Требуется авторизация')
    if principal.role not in roles:
        raise HTTPException(403,'Недостаточно прав')


def require_room(principal,room):
    if principal is None or not principal.permits_room(room):
        raise HTTPException(403,'Нет доступа к кабинету')


def policy(method,path,principal):
    """Return (status, message) on denial, otherwise None."""
    def allowed(roles,room=None):
        if principal is None:
            return (401,'Требуется авторизация')
        if principal.role not in roles or (room is not None and not principal.permits_room(room)):
            return (403,'Недостаточно прав')
        return None

    if method=='GET' and path=='/api/me':
        return allowed(STAFF|{'board','kiosk'})
    if method=='GET' and path=='/api/rooms':
        return allowed(STAFF|{'board','kiosk'})
    if method=='GET' and path=='/api/board':
        return allowed(BOARD)
    if method=='GET' and path=='/api/events':
        return allowed(STAFF|{'board'})
    if ((method=='GET' and path in ('/api/integration/status','/api/integration/conflicts')) or
        (method=='POST' and path in ('/api/integration/resolve','/api/integration/retry'))):
        return allowed({'admin'})
    if method=='GET' and path in ('/api/analytics','/api/pilot/analytics'):
        return allowed({'admin','analyst'})
    if method=='POST' and path in ('/api/check-in','/api/walk-in','/api/check-in/qr'):
        return allowed(REGISTRATION)
    if method=='POST' and path in ('/api/staff/demo-appointments','/api/staff/qr'):
        return allowed({'admin','registrar'})
    if method=='GET' and path=='/api/routes/templates':
        return allowed({'admin','registrar'})
    if method=='POST' and path=='/api/routes/templates':
        return allowed({'admin'})
    if method=='POST' and re.fullmatch(r'/api/staff/[A-Za-z0-9_-]{1,16}/tickets/[0-9]+/route/attach',path):
        return allowed({'admin','registrar'},path.split('/')[3])
    if method=='GET' and re.fullmatch(r'/api/staff/[A-Za-z0-9_-]{1,16}/tickets/[0-9]+/route',path):
        return allowed(QUEUE,path.split('/')[3])
    if method=='POST' and re.fullmatch(r'/api/staff/[A-Za-z0-9_-]{1,16}/tickets/[0-9]+/route/advance',path):
        return allowed(QUEUE,path.split('/')[3])
    if method=='POST' and path=='/api/staff/rooms':
        return allowed({'admin'})
    if method=='POST' and path=='/api/devices/heartbeat':
        return allowed({'board','kiosk'})
    if method=='GET' and re.fullmatch(r'/api/staff/tickets/[0-9]+/history',path):
        return allowed({'admin','registrar'})
    if method=='GET' and re.fullmatch(r'/api/staff/[A-Za-z0-9_-]{1,16}(?:/no-shows)?',path):
        return allowed(QUEUE,path.split('/')[3])
    if method=='POST' and re.fullmatch(r'/api/staff/[A-Za-z0-9_-]{1,16}/(?:call-next|tickets/[0-9]+/(?:action|transfer))',path):
        return allowed(QUEUE,path.split('/')[3])
    if ((method=='GET' and path in ('/api/admin/users','/api/admin/devices','/api/admin/audit')) or
        (method=='POST' and (path in ('/api/admin/users','/api/admin/devices') or
         re.fullmatch(r'/api/admin/devices/[a-f0-9-]{36}/revoke',path)))):
        return allowed({'admin'})
    return (403,'Маршрут не разрешён политикой доступа')


async def resolve(request,store):
    # Explicit staff session has precedence; never combine roles or scopes.
    value=request.cookies.get(SESSION)
    if value:
        staff=await run_in_threadpool(store.staff,value)
        if staff:
            return staff
    value=request.cookies.get(DEVICE)
    return await run_in_threadpool(store.device,value) if value else None


def install_routes(app,settings,store,oidc):
    def secure_only():
        if settings is None:
            raise HTTPException(404,'Доступно только в secure-профиле')

    @app.get('/auth/login',include_in_schema=False)
    async def login(next: str='/staff'):
        secure_only()
        target=next if next in RETURN_TO else '/staff'
        state,verifier,nonce=await run_in_threadpool(store.begin_login,target)
        try:
            url=await oidc.authorize(state,verifier,nonce)
        except Exception:
            raise HTTPException(503,'Провайдер входа недоступен')
        response=RedirectResponse(url,status_code=303)
        set_cookie(response,STATE,state,LOGIN_TTL)
        return response

    @app.get('/auth/callback',include_in_schema=False)
    async def callback(request: Request, state: str='', code: str='', error: str=''):
        secure_only()
        saved=request.cookies.get(STATE,'')
        if error or not state or not code or len(state)>128 or len(code)>4096 or not hmac.compare_digest(saved.encode(),state.encode()):
            return callback_failure()
        attempt=await run_in_threadpool(store.consume_login,state)
        if not attempt:
            return callback_failure()
        try:
            subject=await oidc.redeem(code,attempt['verifier'],attempt['nonce'])
            value=await run_in_threadpool(store.login,subject)
        except Exception:
            return callback_failure()
        # Rotate any old local session, do not retain IdP tokens.
        await run_in_threadpool(store.logout,request.cookies.get(SESSION))
        response=RedirectResponse(attempt['return_to'],status_code=303)
        clear_cookie(response,STATE)
        clear_cookie(response,DEVICE)
        set_cookie(response,SESSION,value,SESSION_TTL)
        return response

    def callback_failure():
        response=JSONResponse({'detail':'Вход не завершён или аккаунт не разрешён'},status_code=401)
        clear_cookie(response,STATE)
        return response

    @app.post('/auth/logout')
    async def logout(request: Request):
        secure_only()
        await run_in_threadpool(store.logout,request.cookies.get(SESSION))
        response=JSONResponse({'status':'ok'})
        clear_cookie(response,SESSION)
        clear_cookie(response,STATE)
        return response

    @app.post('/auth/pair')
    async def pair(data: PairRequest,request: Request):
        secure_only()
        # A staff browser must explicitly log out before pairing as a device.
        if await run_in_threadpool(store.staff,request.cookies.get(SESSION)):
            raise HTTPException(409,'Сначала выйдите из учётной записи сотрудника')
        value,kind=await run_in_threadpool(store.pair_device,data.pairing_code)
        response=JSONResponse({'path':'/board' if kind=='board' else '/kiosk'})
        clear_cookie(response,SESSION)
        set_cookie(response,DEVICE,value,DEVICE_TTL)
        return response

    @app.get('/api/me')
    def me(request: Request):
        if settings is None:
            return {'mode':'demo','role':'demo','kind':'demo','rooms':[]}
        return {'mode':'secure',**request.state.principal.public()}

    @app.get('/api/admin/users')
    def users():
        secure_only()
        return store.users()

    @app.post('/api/admin/users')
    def user_upsert(data: UserConfig,request: Request):
        secure_only()
        return store.user_upsert(request.state.principal,data.subject,data.label,data.role,data.rooms,data.enabled)

    @app.get('/api/admin/devices')
    def devices():
        secure_only()
        return store.devices()

    @app.post('/api/admin/devices')
    def create_device(data: DeviceConfig,request: Request):
        secure_only()
        return store.create_device(request.state.principal,data.label,data.kind,data.room_id)

    @app.post('/api/admin/devices/{device_id}/revoke')
    def revoke_device(device_id: str,request: Request):
        secure_only()
        return store.revoke_device(request.state.principal,device_id)

    @app.post('/api/devices/heartbeat')
    def heartbeat(data: DeviceHeartbeat,request: Request):
        secure_only()
        return store.heartbeat(request.state.principal,data.model_dump())

    @app.get('/api/admin/audit')
    def recent_audit():
        secure_only()
        return store.recent_audit()
