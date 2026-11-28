"""Short-lived, process-local snapshot coalescing for public board projections."""
import asyncio
import time
from starlette.concurrency import run_in_threadpool


class BoardSnapshots:
    def __init__(self,queue,changes,ttl=.5,clock=time.monotonic):
        self.queue=queue
        self.changes=changes
        self.ttl=ttl
        self.clock=clock
        self.lock=asyncio.Lock()
        self.snapshot=None
        self.generation=-1
        self.expires=0.0

    async def read(self):
        async with self.lock:
            for _ in range(3):
                generation=self.changes.generation
                if self.snapshot is not None and generation==self.generation and self.clock()<self.expires:
                    return self.snapshot
                # Expired snapshots must not be used if the DB is unavailable.
                self.snapshot=None
                value=await run_in_threadpool(self.queue.board)
                if self.changes.generation==generation:
                    self.snapshot=value
                    self.generation=generation
                    self.expires=self.clock()+self.ttl
                    return value
            # Continuous writes: return a fresh read without caching it.
            return await run_in_threadpool(self.queue.board)
