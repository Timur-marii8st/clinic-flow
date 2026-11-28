import asyncio
import pytest
from app.board_cache import BoardSnapshots
from app.main import Changes


def test_snapshot_coalescing_invalidation_and_failure():
    class Queue:
        calls=0
        fail=False
        def board(self):
            self.calls+=1
            if self.fail:raise RuntimeError('test outage')
            return [{'revision':self.calls}]
    async def run():
        queue=Queue();changes=Changes();clock=[0.0]
        cache=BoardSnapshots(queue,changes,clock=lambda:clock[0])
        snapshots=await asyncio.gather(*(cache.read() for _ in range(50)))
        assert queue.calls==1 and all(s==[{'revision':1}] for s in snapshots)
        await changes.publish()
        assert await cache.read()==[{'revision':2}]
        clock[0]+=1
        assert await cache.read()==[{'revision':3}]
        clock[0]+=1;queue.fail=True
        with pytest.raises(RuntimeError):await cache.read()
        queue.fail=False
        assert await cache.read()==[{'revision':5}]
    asyncio.run(run())
