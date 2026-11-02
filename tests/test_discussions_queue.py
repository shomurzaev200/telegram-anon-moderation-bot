import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
import bot
import db
import discussions
from delivery_queue import Deferred, RateLimiter, claim
from test_app import setup, message, submit_post, publish_job, FakeAPI, login

def publish():
    submit_post()
    with db.connect() as c: db.set_value(c,'BOT_USERNAME','example_bot')
    api=FakeAPI(); job=publish_job(); bot.run_job(api,job,'secret')
    return api,job

def click(uid,update,data):
    bot.process_update({'update_id':update,'callback_query':{'id':str(update),'from':{'id':uid},'data':data}})

def test_publication_signature_button_and_notifications(setup):
    bot.process_update(message(50,50,'/start',uid=20))
    api,job=publish()
    payload=api.calls[0][1]
    assert payload['text'].endswith('\n\nАнон.')
    assert payload['reply_markup']['inline_keyboard'][1][0]['url']=='https://t.me/example_bot?start=post_1'
    bot.run_job(api,job,'secret')
    with db.connect() as c:
        notices=[json.loads(r[0])['uid'] for r in c.execute("SELECT payload FROM jobs WHERE kind='post_notice'")]
    assert sorted(notices)==[12,20]

def test_comment_mode_separate_and_persistent_after_cleanup(setup):
    publish()
    bot.process_update(message(200,200,'/start post_1',uid=20))
    click(20,201,'discussion:write:1')
    bot.process_update(message(202,202,'Мой комментарий',uid=20))
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM posts').fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM jobs WHERE kind='chat_delivery'").fetchone()[0]==0
        assert c.execute('SELECT body FROM post_comments').fetchone()[0]=='Мой комментарий'
        job=dict(c.execute("SELECT * FROM jobs WHERE kind='discussion_view' ORDER BY id DESC").fetchone())
    api=FakeAPI(); bot.run_job(api,job,'secret')
    assert 'Мой комментарий' in api.calls[0][1]['text'] and 'Аноним ' in api.calls[0][1]['text']
    bot.execute_job(FakeAPI(),{'kind':'cleanup','payload':'{}'},'secret')
    with db.connect() as c: assert c.execute('SELECT count(*) FROM post_comments').fetchone()[0]==1

def test_unpublished_post_cannot_be_commented(setup):
    submit_post(); click(12,200,'discussion:write:1')
    with db.connect() as c: assert c.execute('SELECT mode FROM users WHERE id=12').fetchone()[0]!='comment'

def test_notification_unsubscribe_skips_existing_queue(setup):
    publish(); click(12,200,'discussion:notify:off')
    with db.connect() as c: job=dict(c.execute("SELECT * FROM jobs WHERE kind='post_notice'").fetchone())
    api=FakeAPI(); bot.run_job(api,job,'secret'); assert not api.calls

def test_comment_pagination_and_admin_hide(setup):
    publish()
    with db.connect() as c:
        for i in range(5): c.execute('INSERT INTO post_comments(post_id,uid,body,created) VALUES(1,12,?,?)',(f'Comment-{i}',time.time()))
    api=FakeAPI()
    discussions.execute(api,{'kind':'discussion_view','payload':json.dumps({'uid':12,'post_id':1,'page':1})},'secret')
    assert 'Comment-3' in api.calls[0][1]['text'] and 'Comment-0' not in api.calls[0][1]['text']
    client,token=login(setup)
    assert client.get('/comments').status_code==200
    assert client.post('/comments/4/hide',data={'csrf':token}).status_code==302
    api=FakeAPI(); discussions.execute(api,{'kind':'discussion_view','payload':json.dumps({'uid':12,'post_id':1,'page':1})},'secret')
    assert 'Comment-3' not in api.calls[0][1]['text']

def test_fast_input_not_rejected(setup):
    for uid in (10,20): bot.process_update(message(uid,uid,'/chat',uid=uid))
    for i in range(30): bot.process_update(message(100+i,100+i,f'Message {i}',uid=10))
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM relay_messages').fetchone()[0]==30
        assert c.execute("SELECT count(*) FROM jobs WHERE kind='chat_delivery'").fetchone()[0]==30
        assert not c.execute("SELECT 1 FROM jobs WHERE payload LIKE '%Слишком быстро%'").fetchone()

def test_fifo_and_parallel_lanes(setup):
    with db.connect() as c:
        a=db.enqueue(c,'send',{'chat_id':10,'text':'first'})
        b=db.enqueue(c,'send',{'chat_id':10,'text':'second'})
        other=db.enqueue(c,'send',{'chat_id':20,'text':'other'})
    assert claim()['id']==a
    assert claim()['id']==other
    assert claim() is None
    with db.connect() as c: c.execute("UPDATE jobs SET status='done' WHERE id=?",(a,))
    assert claim()['id']==b

def test_limiter_defers_without_sleep_or_drop(setup):
    clock=[10.]; limiter=RateLimiter(lambda:clock[0])
    limiter.acquire('sendMessage',{'chat_id':1})
    with pytest.raises(Deferred): limiter.acquire('sendMessage',{'chat_id':2})
    clock[0]+=.05; limiter.acquire('sendMessage',{'chat_id':2})
    with pytest.raises(Deferred): limiter.acquire('sendMessage',{'chat_id':1})
    limiter.backoff(4); clock[0]+=1
    with pytest.raises(Deferred): limiter.acquire('answerCallbackQuery',{})

def test_deferred_publish_stays_queued_not_uncertain(setup):
    submit_post(); job=publish_job()
    bot.run_job(FakeAPI(Deferred(.5)),job,'secret')
    with db.connect() as c:
        assert c.execute('SELECT status FROM posts').fetchone()[0]=='queued'
        assert c.execute('SELECT status FROM jobs WHERE id=?',(job['id'],)).fetchone()[0]=='queued'

def test_60_concurrent_senders_and_3540_deliveries(setup):
    for uid in range(1,61): bot.process_update(message(uid,uid,'/chat',uid=uid))
    with db.connect() as c: c.execute("UPDATE jobs SET status='done'")
    start=time.perf_counter()
    with ThreadPoolExecutor(max_workers=60) as pool:
        list(pool.map(lambda uid:bot.process_update(message(1000+uid,1000+uid,f'Load {uid}',uid=uid)),range(1,61)))
    ingestion=time.perf_counter()-start
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM relay_messages').fetchone()[0]==60
        assert c.execute("SELECT count(*) FROM jobs WHERE kind='chat_delivery' AND status='queued'").fetchone()[0]==3540
    class LoadAPI:
        def __init__(self): self.lock=threading.Lock(); self.active=set(); self.seen=set(); self.count=0
        def call(self,method,**p):
            key=(p['chat_id'],p['text'])
            with self.lock:
                assert p['chat_id'] not in self.active
                assert key not in self.seen
                self.active.add(p['chat_id']); self.seen.add(key); self.count+=1; mid=self.count+10000
            time.sleep(.0001)
            with self.lock: self.active.remove(p['chat_id'])
            return {'message_id':mid}
    api=LoadAPI()
    def drain(_):
        while True:
            job=claim()
            if job: bot.run_job(api,job,'secret')
            else:
                with db.connect() as c: pending=c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
                if not pending: return
                time.sleep(.002)
    with ThreadPoolExecutor(max_workers=8) as pool: list(pool.map(drain,range(8)))
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM jobs WHERE kind='chat_delivery' AND status='done'").fetchone()[0]==3540
        assert c.execute("SELECT count(*) FROM jobs WHERE status IN ('failed','uncertain')").fetchone()[0]==0
    assert len(api.seen)==3540
    print(f'LOAD: 60 simultaneous inputs in {ingestion:.3f}s; 3540 mock deliveries in {time.perf_counter()-start:.3f}s, no duplicates or failures (API rate limits not simulated).')

def test_cleanup_cutoff_survives_deferred_retry(setup):
    with db.connect() as c:
        c.execute('INSERT INTO chat_messages(chat_id,message_id,created) VALUES(12,1,1)')
        jid=db.enqueue(c,'cleanup',{})
    job=claim(); bot.run_job(FakeAPI(Deferred(.1)),job,'secret')
    with db.connect() as c:
        saved=dict(c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
        cutoff=json.loads(saved['payload'])['cutoff']
        c.execute('INSERT INTO chat_messages(chat_id,message_id,created) VALUES(12,2,?)',(cutoff+10,))
    api=FakeAPI(); bot.run_job(api,saved,'secret')
    assert api.calls==[('deleteMessage',{'chat_id':12,'message_id':1})]

def test_long_post_rejected_before_moderation_without_truncation(setup):
    bot.process_update(message(100,100,'/post'))
    bot.process_update(message(text='a'*4096))
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM posts').fetchone()[0]==0
        assert c.execute('SELECT mode FROM users WHERE id=12').fetchone()[0]=='post'
