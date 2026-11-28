"""Bounded DAG. A step is required exactly once; rooms are approved alternatives."""
from pydantic import BaseModel, ConfigDict, Field, model_validator

class Step(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    id: str=Field(min_length=1,max_length=32,pattern=r'^[A-Za-z0-9_-]+$')
    label: str=Field(min_length=1,max_length=100)
    rooms: list[str]=Field(min_length=1,max_length=16)
    requires: list[str]=Field(default_factory=list,max_length=20)

    @model_validator(mode='after')
    def validate_lists(self):
        import re
        if not self.label.strip() or len(set(self.rooms))!=len(self.rooms) or len(set(self.requires))!=len(self.requires):
            raise ValueError('Blank label or duplicate rooms/dependencies')
        if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,16}',r) for r in self.rooms):
            raise ValueError('Invalid room identifier')
        return self

class Plan(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    steps: list[Step]=Field(min_length=1,max_length=20)

    @model_validator(mode='after')
    def validate_dag(self):
        ids={s.id for s in self.steps}
        if len(ids)!=len(self.steps):
            raise ValueError('Duplicate step IDs')
        if any(not set(s.requires)<=ids for s in self.steps):
            raise ValueError('Unknown dependency')
        done=set()
        while len(done)<len(ids):
            ready={s.id for s in self.steps if s.id not in done and set(s.requires)<=done}
            if not ready:
                raise ValueError('Cyclic route')
            done.update(ready)
        return self

    def ready(self,completed):
        done=set(completed)
        return [s for s in self.steps if s.id not in done and set(s.requires)<=done]

    def step(self,step_id):
        return next((s for s in self.steps if s.id==step_id),None)
