"""Admin-only conflict and outbox controls; no public provider injection API."""
from typing import Literal
from uuid import UUID

from fastapi import Request
from pydantic import BaseModel, Field

from .service import Integration
from .outbox import Outbox


class ResolveConflict(BaseModel):
    source: str = Field(min_length=1,max_length=32)
    external_id: str = Field(min_length=1,max_length=128)
    version: int = Field(ge=1,le=2**53-1,strict=True)
    expected_conflict: str = Field(min_length=1,max_length=100)
    decision: Literal['apply','keep_local']
    reason: str = Field(min_length=1,max_length=500)


class RetryEvent(BaseModel):
    event_id: UUID


def install_integration_routes(app,queue,staff_deps,changes):
    from starlette.concurrency import run_in_threadpool
    integration=Integration(queue)
    outbox=Outbox(queue)

    def actor(request):
        principal=getattr(request.state,'principal',None)
        return principal.actor if principal else 'demo'

    @app.get('/api/integration/status',dependencies=staff_deps)
    def status():
        return integration.status()

    @app.get('/api/integration/conflicts',dependencies=staff_deps)
    def conflicts():
        return integration.conflicts()

    @app.post('/api/integration/resolve',dependencies=staff_deps)
    async def resolve(data: ResolveConflict,request: Request):
        result=await run_in_threadpool(integration.resolve,data.source,data.external_id,data.version,
                                      data.expected_conflict,data.decision,data.reason,actor(request))
        await changes.publish()
        return result

    @app.post('/api/integration/retry',dependencies=staff_deps)
    def retry(data: RetryEvent,request: Request):
        return outbox.retry_dead(str(data.event_id),actor(request))
