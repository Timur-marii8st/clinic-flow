"""Bounded pull + outbox worker. All provider I/O occurs outside queue transactions."""
from .service import Integration
from .outbox import Outbox


class Worker:
    def __init__(self,queue,adapter,outbox=None):
        self.integration=Integration(queue)
        self.adapter=adapter
        self.outbox=outbox or Outbox(queue)

    def cycle(self,limit=50):
        if not 1<=limit<=500:
            raise ValueError('Invalid batch size')
        result={'pulled':0,'delivered':0,'failed':0,'pull_error':False}
        try:
            cursor=self.integration.cursor(self.adapter.source)
            for item in self.adapter.pull(cursor,limit):
                if item.position!=cursor+1:
                    raise ValueError('Invalid provider feed order')
                self.integration.apply(self.adapter.source,item.event,item.position)
                cursor=item.position
                result['pulled']+=1
        except Exception:
            # No raw provider exceptions or appointment payloads in logs.
            # Cursor only moves after an atomic accepted inbox transaction.
            result['pull_error']=True
        for _ in range(limit):
            claim=self.outbox.claim(self.adapter.source)
            if not claim:
                break
            try:
                self.adapter.deliver(claim['payload'])
            except Exception:
                self.outbox.settle(claim,False)
                result['failed']+=1
            else:
                if self.outbox.settle(claim,True):
                    result['delivered']+=1
        return result
