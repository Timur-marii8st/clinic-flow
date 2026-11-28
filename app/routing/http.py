from fastapi import Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .plan import Plan
from .service import Routing
from .report import flow_report
from app.auth.http import require_room


class TemplateBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id: str=Field(min_length=1,max_length=32,pattern=r'^[A-Za-z0-9_-]+$')
    label: str=Field(min_length=1,max_length=100)
    expected_version: int=Field(ge=0,strict=True)
    plan: Plan


class AttachBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    template_id: str=Field(min_length=1,max_length=32)
    template_version: int=Field(ge=1,strict=True)
    step_id: str=Field(min_length=1,max_length=32)
    expected_version: str=Field(min_length=1,max_length=64)


class AdvanceBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    step_id: str=Field(min_length=1,max_length=32)
    target_room: str=Field(min_length=1,max_length=16)
    expected_version: str=Field(min_length=1,max_length=64)


def install_routing_routes(app,queue,staff_deps,mutate,settings):
    routing=Routing(queue)

    @app.get('/api/routes/templates',dependencies=staff_deps)
    def templates():
        return routing.templates()

    @app.post('/api/routes/templates',dependencies=staff_deps,status_code=201)
    async def create_template(data:TemplateBody,request:Request):
        return await mutate(request,routing.create_template,data.id,data.label,data.plan,data.expected_version)

    @app.post('/api/staff/{room_id}/tickets/{ticket_id}/route/attach',dependencies=staff_deps)
    async def attach(room_id:str,ticket_id:int,data:AttachBody,request:Request,
                     key:str|None=Header(default=None,alias='Idempotency-Key',min_length=16,max_length=100)):
        return await mutate(request,routing.attach,room_id,ticket_id,data.template_id,data.template_version,
                            data.step_id,data.expected_version,key)

    @app.get('/api/staff/{room_id}/tickets/{ticket_id}/route',dependencies=staff_deps)
    def options(room_id:str,ticket_id:int,request:Request):
        principal=request.state.principal
        allowed=set(principal.rooms) if settings and principal.role=='doctor' else None
        return routing.options(room_id,ticket_id,allowed)

    @app.post('/api/staff/{room_id}/tickets/{ticket_id}/route/advance',dependencies=staff_deps)
    async def advance(room_id:str,ticket_id:int,data:AdvanceBody,request:Request,
                      key:str|None=Header(default=None,alias='Idempotency-Key',min_length=16,max_length=100)):
        if settings:
            require_room(request.state.principal,data.target_room)
        return await mutate(request,routing.advance,room_id,ticket_id,data.step_id,data.target_room,data.expected_version,key)

    @app.get('/api/pilot/analytics',dependencies=staff_deps)
    def report(hours:int=Query(default=24,ge=1,le=168)):
        return flow_report(queue,hours)
