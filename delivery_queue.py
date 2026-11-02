"""Durable per-destination FIFO with parallel network workers and free API pacing."""
import threading
import time
from db import connect

class Deferred(Exception):
    def __init__(self,delay):
        self.delay=max(.05,float(delay))
        super().__init__('Отложено до свободного слота доставки')

class RateLimiter:
    def __init__(self,clock=time.monotonic):
        self.clock=clock; self.lock=threading.Lock()
        self.next_global=0.; self.chats={}; self.pause_until=0.
    def acquire(self,method,payload):
        if method.startswith('get'): return
        now=self.clock(); chat=payload.get('chat_id')
        with self.lock:
            ready=max(self.next_global,self.pause_until,self.chats.get(chat,0.) if chat is not None else 0.)
            if ready>now: raise Deferred(ready-now)
            self.next_global=now+.04  # at most 25 API writes/s, no paid broadcasts
            if chat is not None:
                self.chats[chat]=now+(1.05 if int(chat)>0 else 3.05)
            if len(self.chats)>10000: self.chats={k:v for k,v in self.chats.items() if v>now}
    def backoff(self,seconds):
        with self.lock: self.pause_until=max(self.pause_until,self.clock()+max(1,seconds))

def claim():
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute("""SELECT j.* FROM jobs j WHERE j.status='queued' AND j.run_after<=?
          AND NOT EXISTS (SELECT 1 FROM jobs prior WHERE prior.lane=j.lane
              AND prior.status IN ('queued','running') AND prior.id<j.id)
          ORDER BY j.priority DESC,j.id LIMIT 1""",(time.time(),)).fetchone()
        if row: db.execute("UPDATE jobs SET status='running' WHERE id=?",(row['id'],))
        return dict(row) if row else None
