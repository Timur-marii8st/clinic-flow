from fastapi import Request
from pydantic import BaseModel, Field

from .qr import QRService


class IssueQR(BaseModel):
    booking_code: str = Field(min_length=1,max_length=32)


class RedeemQR(BaseModel):
    code: str = Field(min_length=47,max_length=47)
    request_key: str = Field(min_length=16,max_length=100)


def install_device_routes(app,queue,staff_deps,mutate):
    qr=QRService(queue)

    @app.post('/api/staff/qr',dependencies=staff_deps)
    async def issue(data: IssueQR,request: Request):
        return await mutate(request,qr.issue,data.booking_code)

    @app.post('/api/check-in/qr')
    async def redeem(data: RedeemQR,request: Request):
        return await mutate(request,qr.redeem,data.code,data.request_key)
