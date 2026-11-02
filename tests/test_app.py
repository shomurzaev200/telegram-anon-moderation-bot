import json
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from werkzeug.security import generate_password_hash
import db
import admin
import bot

@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB_PATH',tmp_path/'test.sqlite3')
    db.init_db()
    with db.connect() as c:
        for k,v in {'ADMIN_PASSWORD_HASH':generate_password_hash('very-long-password'),
                    'ADMIN_IDS':'123','MODERATION_CHANNEL_ID':'-1001',
                    'PUBLIC_CHANNEL_ID':'-1002','ANON_GROUP_ID':'-1003','CHAT_ENABLED':'1'}.items(): db.set_value(c,k,v)
    app=admin.create_app({'TESTING':True,'SECRET_KEY':'test-key'})
    return app

def csrf(client):
    client.get('/login')
    with client.session_transaction() as s: return s['csrf']

def login(app):
    c=app.test_client(); token=csrf(c)
    assert c.post('/login',data={'csrf':token,'password':'very-long-password'}).status_code==302
    with c.session_transaction() as s: token=s['csrf']
    return c,token

def message(update_id=1,mid=1,text='История',uid=12):
    return {'update_id':update_id,'message':{'message_id':mid,'chat':{'id':uid,'type':'private'},'from':{'id':uid},'text':text}}

def test_fresh_schema_and_independent_initialization(setup):
    db.init_db(); db.init_db()
    assert db.get('PUBLIC_CHANNEL_ID')=='-1002'
    assert setup.test_client().get('/login').status_code==200

def test_login_logout_csrf_and_pages(setup):
    c=setup.test_client()
    assert c.get('/').status_code==302
    assert c.post('/login',data={'password':'very-long-password'}).status_code==400
    c,token=login(setup)
    for page in ('/','/posts','/users','/settings','/jobs','/logs','/password'):
        assert c.get(page).status_code==200
    assert c.post('/logout',data={'csrf':token}).status_code==302
    assert c.get('/').status_code==302

def test_login_throttle(setup):
    c=setup.test_client(); token=csrf(c)
    for _ in range(5): c.post('/login',data={'csrf':token,'password':'bad'})
    assert c.post('/login',data={'csrf':token,'password':'very-long-password'}).status_code==429

def test_password_revokes_other_sessions(setup):
    a,token=login(setup); b,_=login(setup)
    a.post('/password',data={'csrf':token,'old':'very-long-password','new':'replacement-secret','confirm':'replacement-secret'})
    assert a.get('/').status_code==302
    assert b.get('/').status_code==302

def test_dedup_and_atomic_publish(setup):
    bot.process_update(message()); bot.process_update(message())
    with db.connect() as c: assert c.execute('SELECT count(*) FROM posts').fetchone()[0]==1
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:db.decide(1,'publish','test'),range(4)))
    assert sum(results)==1
    with db.connect() as c: assert c.execute("SELECT count(*) FROM jobs WHERE kind='publish'").fetchone()[0]==1

def test_unauthorized_callback(setup):
    bot.process_update(message())
    bot.process_update({'update_id':2,'callback_query':{'id':'abc','from':{'id':999},'data':'publish:1'}})
    with db.connect() as c: assert c.execute('SELECT status FROM posts').fetchone()[0]=='pending'

def test_authorized_callback(setup):
    bot.process_update(message())
    bot.process_update({'update_id':2,'callback_query':{'id':'abc','from':{'id':123},'data':'publish:1'}})
    with db.connect() as c: assert c.execute('SELECT status FROM posts').fetchone()[0]=='queued'

class FakeAPI:
    def __init__(self,error=None): self.calls=[]; self.error=error
    def call(self,method,**kw):
        self.calls.append((method,kw))
        if self.error: raise self.error
        return {'message_id':77}

def publish_job():
    with db.connect() as c:
        c.execute("UPDATE jobs SET status='done'")
    db.decide(1,'publish','test')
    return bot.claim()

def test_success_and_no_double_send(setup):
    bot.process_update(message()); job=publish_job(); api=FakeAPI()
    bot.run_job(api,job,'secret'); bot.run_job(api,job,'secret')
    assert len(api.calls)==1
    with db.connect() as c:
        post=c.execute('SELECT * FROM posts').fetchone()
        assert post['status']=='published' and post['published_message']==77

def test_timeout_does_not_retry(setup):
    bot.process_update(message()); job=publish_job()
    bot.run_job(FakeAPI(bot.UncertainError('timeout')),job,'secret')
    with db.connect() as c:
        assert c.execute('SELECT status FROM posts').fetchone()[0]=='uncertain'
        assert c.execute('SELECT status FROM jobs WHERE id=?',(job['id'],)).fetchone()[0]=='uncertain'

def test_429_defers_and_resets_post(setup):
    bot.process_update(message()); job=publish_job()
    bot.run_job(FakeAPI(bot.APIError(429,'wait',30)),job,'secret')
    with db.connect() as c:
        j=c.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone()
        assert j['status']=='queued' and j['run_after']>time.time()+20
        assert c.execute('SELECT status FROM posts').fetchone()[0]=='queued'

def test_recover_running_job(setup):
    bot.process_update(message()); job=publish_job()
    bot.recover()
    with db.connect() as c: assert c.execute('SELECT status FROM posts').fetchone()[0]=='uncertain'

def test_settings_validation_and_save(setup):
    c,token=login(setup)
    vals={k:db.get(k) for k in admin.EDITABLE}; vals['csrf']=token
    vals['COOLDOWN_SECONDS']='0'
    assert c.post('/settings',data=vals).status_code==400
    vals['COOLDOWN_SECONDS']='2'
    assert c.post('/settings',data=vals).status_code==302
    assert db.get('COOLDOWN_SECONDS')=='2'

def test_user_block_and_chat(setup):
    bot.process_update(message(text='/chat'))
    bot.process_update(message(2,2,'Привет'))
    with db.connect() as c:
        row=c.execute("SELECT * FROM jobs WHERE kind='chat'").fetchone()
    api=FakeAPI(); bot.run_job(api,dict(row),'secret')
    text=api.calls[0][1]['text']; assert text.startswith('Аноним ') and 'Привет' in text
    c,token=login(setup); c.post('/users/12/toggle',data={'csrf':token})
    with db.connect() as cx: before=cx.execute('SELECT count(*) FROM jobs').fetchone()[0]
    bot.process_update(message(3,3,'заблокировано'))
    with db.connect() as cx: assert cx.execute('SELECT count(*) FROM jobs').fetchone()[0]==before

def test_cleanup_only_tracked_messages(setup):
    with db.connect() as c:
        c.execute('INSERT INTO chat_messages(chat_id,message_id,created) VALUES(?,?,?)',(-1003,55,time.time()))
    job={'id':999,'kind':'cleanup','payload':'{}'}; api=FakeAPI()
    result=bot.execute_job(api,job,'secret')
    assert 'Удалено: 1' in result
    assert api.calls==[('deleteMessage',{'chat_id':-1003,'message_id':55})]
    assert db.get('NICK_EPOCH')=='2'

def test_content_escaped(setup):
    bot.process_update(message(text='<script>alert(1)</script>'))
    c,_=login(setup)
    html=c.get('/posts').get_data(as_text=True)
    assert '<script>alert' not in html and '&lt;script&gt;' in html

def test_task_double_click_deduplicated(setup):
    c,token=login(setup)
    for _ in range(2): c.post('/tasks/check',data={'csrf':token})
    with db.connect() as cx: assert cx.execute("SELECT count(*) FROM jobs WHERE kind='check'").fetchone()[0]==1
