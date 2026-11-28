"""Internal contract, NOT an API specification of Vitacore or another vendor.

Every incoming event carries the full latest appointment snapshot; versions
are monotonic per external appointment, event IDs immutable per source.
Only synthetic data is accepted by the mock adapter.
"""
from datetime import datetime
from typing import Literal, Protocol
import json

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.queue_policy import utc_iso


class AppointmentSnapshot(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    external_id: str = Field(min_length=1,max_length=128,pattern=r'^[A-Za-z0-9_.:-]+$')
    version: int = Field(ge=1,le=2**53-1,strict=True)
    booking_code: str = Field(min_length=8,max_length=32,pattern=r'^MIS-[A-Z0-9-]+$')
    room_id: str = Field(min_length=1,max_length=16,pattern=r'^[A-Za-z0-9_-]+$')
    scheduled_at: datetime
    state: Literal['booked','cancelled']

    @field_validator('scheduled_at')
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('A timezone-aware appointment is required')
        return value

    def canonical(self):
        data=self.model_dump(mode='json')
        data['scheduled_at']=utc_iso(self.scheduled_at)
        return data


class AppointmentEvent(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    event_id: str = Field(min_length=1,max_length=128,pattern=r'^[A-Za-z0-9_.:-]+$')
    appointment: AppointmentSnapshot

    def canonical(self):
        return {'event_id':self.event_id,'appointment':self.appointment.canonical()}


class FeedItem(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    position: int = Field(ge=1,le=2**53-1,strict=True)
    event: AppointmentEvent


class Adapter(Protocol):
    source: str
    def pull(self, after: int, limit: int) -> list[FeedItem]: ...
    def deliver(self, event: dict) -> None: ...


def canonical_json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
