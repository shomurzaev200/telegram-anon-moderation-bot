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

def submit_post():
    bot.process_update(message(update_id=100,mid=100,text='/post'))
    bot.process_update(message())

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
    submit_post(); bot.process_update(message())
    with db.connect() as c: assert c.execute('SELECT count(*) FROM posts').fetchone()[0]==1
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:db.decide(1,'publish','test'),range(4)))
    assert sum(results)==1
    with db.connect() as c: assert c.execute("SELECT count(*) FROM jobs WHERE kind='publish'").fetchone()[0]==1

def test_unauthorized_callback(setup):
    submit_post()
    bot.process_update({'update_id':2,'callback_query':{'id':'abc','from':{'id':999},'data':'publish:1'}})
    with db.connect() as c: assert c.execute('SELECT status FROM posts').fetchone()[0]=='pending'

def test_authorized_callback(setup):
    submit_post()
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
    submit_post(); job=publish_job(); api=FakeAPI()
    bot.run_job(api,job,'secret'); bot.run_job(api,job,'secret')
    assert len(api.calls)==1
    with db.connect() as c:
        post=c.execute('SELECT * FROM posts').fetchone()
        assert post['status']=='published' and post['published_message']==77

def test_timeout_does_not_retry(setup):
    submit_post(); job=publish_job()
    bot.run_job(FakeAPI(bot.UncertainError('timeout')),job,'secret')
    with db.connect() as c:
        assert c.execute('SELECT status FROM posts').fetchone()[0]=='uncertain'
        assert c.execute('SELECT status FROM jobs WHERE id=?',(job['id'],)).fetchone()[0]=='uncertain'

def test_429_defers_and_resets_post(setup):
    submit_post(); job=publish_job()
    bot.run_job(FakeAPI(bot.APIError(429,'wait',30)),job,'secret')
    with db.connect() as c:
        j=c.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone()
        assert j['status']=='queued' and j['run_after']>time.time()+20
        assert c.execute('SELECT status FROM posts').fetchone()[0]=='queued'

def test_recover_running_job(setup):
    submit_post(); job=publish_job()
    bot.recover()
    with db.connect() as c: assert c.execute('SELECT status FROM posts').fetchone()[0]=='uncertain'

def test_settings_validation_and_save(setup):
    c,token=login(setup)
    vals={k:db.get(k) for k in admin.EDITABLE}; vals['csrf']=token
    vals['CLEANUP_HOURS']='0'
    assert c.post('/settings',data=vals).status_code==400
    vals['CLEANUP_HOURS']='2'
    assert c.post('/settings',data=vals).status_code==302
    assert db.get('CLEANUP_HOURS')=='2'

def test_user_block_and_chat(setup):
    bot.process_update(message(99,99,'/chat',uid=13))
    bot.process_update(message(text='/chat'))
    bot.process_update(message(2,2,'Привет'))
    with db.connect() as c:
        row=c.execute("SELECT * FROM jobs WHERE kind='chat_delivery'").fetchone()
    api=FakeAPI(); bot.run_job(api,dict(row),'secret')
    text=api.calls[0][1]['text']; assert text.startswith('Аноним ') and 'Привет' in text
    c,token=login(setup); c.post('/users/12/toggle',data={'csrf':token})
    with db.connect() as cx: before=cx.execute('SELECT count(*) FROM jobs').fetchone()[0]
    bot.process_update(message(3,3,'заблокировано'))
    with db.connect() as cx: assert cx.execute('SELECT count(*) FROM jobs').fetchone()[0]==before

def test_cleanup_only_tracked_messages(setup):
    with db.connect() as c:
        c.execute('INSERT INTO chat_messages(chat_id,message_id,created) VALUES(?,?,?)',(123,55,time.time()))
    job={'id':999,'kind':'cleanup','payload':'{}'}; api=FakeAPI()
    result=bot.execute_job(api,job,'secret')
    assert 'Удалено: 1' in result
    assert api.calls==[('deleteMessage',{'chat_id':123,'message_id':55})]
    assert db.get('NICK_EPOCH')=='2'

def test_content_escaped(setup):
    bot.process_update(message(100,100,'/post'))
    bot.process_update(message(text='<script>alert(1)</script>'))
    c,_=login(setup)
    html=c.get('/posts').get_data(as_text=True)
    assert '<script>alert' not in html and '&lt;script&gt;' in html

def test_task_double_click_deduplicated(setup):
    c,token=login(setup)
    for _ in range(2): c.post('/tasks/check',data={'csrf':token})
    with db.connect() as cx: assert cx.execute("SELECT count(*) FROM jobs WHERE kind='check'").fetchone()[0]==1

def test_idle_message_never_becomes_post(setup):
    bot.process_update(message())
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM posts').fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM jobs WHERE kind IN ('moderate','chat_delivery')").fetchone()[0]==0

def test_membership_snapshot_no_history_and_leave_rejoin(setup):
    bot.process_update(message(1,1,'/chat',uid=10))
    bot.process_update(message(2,2,'/chat',uid=20))
    bot.process_update(message(3,3,'Первое',uid=10))
    bot.process_update(message(4,4,'/chat',uid=30))
    with db.connect() as c:
        jobs=[dict(r) for r in c.execute("SELECT * FROM jobs WHERE kind='chat_delivery'")]
        assert len(jobs)==1 and json.loads(jobs[0]['payload'])['recipient']==20
        assert c.execute('SELECT count(*) FROM posts').fetchone()[0]==0
    bot.process_update(message(5,5,'/leave',uid=20))
    bot.process_update(message(6,6,'/chat',uid=20))
    api=FakeAPI()
    bot.run_job(api,jobs[0],'secret')
    assert api.calls==[]

def test_one_post_then_back_to_chat(setup):
    bot.process_update(message(1,1,'/chat',uid=10))
    bot.process_update(message(2,2,'/chat',uid=20))
    bot.process_update(message(3,3,'/post',uid=10))
    bot.process_update(message(4,4,'Только это пост',uid=10))
    with db.connect() as c: c.execute('UPDATE users SET last_sent=0 WHERE id=10')
    bot.process_update(message(5,5,'Это чат',uid=10))
    with db.connect() as c:
        posts=c.execute('SELECT * FROM posts').fetchall()
        assert len(posts)==1 and posts[0]['preview']=='Только это пост'
        assert c.execute("SELECT count(*) FROM jobs WHERE kind='chat_delivery'").fetchone()[0]==1

def test_chat_without_channels_or_group(setup):
    with db.connect() as c:
        for key in ('MODERATION_CHANNEL_ID','PUBLIC_CHANNEL_ID','ANON_GROUP_ID'): db.set_value(c,key,'')
    bot.process_update(message(1,1,'/chat',uid=10))
    bot.process_update(message(2,2,'/chat',uid=20))
    bot.process_update(message(3,3,'Привет',uid=10))
    with db.connect() as c:
        job=dict(c.execute("SELECT * FROM jobs WHERE kind='chat_delivery'").fetchone())
    api=FakeAPI(); bot.run_job(api,job,'secret')
    assert api.calls[0][1]['chat_id']==20
    assert 'uid' not in api.calls[0][1]

def test_photo_relay_uses_file_id_not_forward(setup):
    bot.process_update(message(1,1,'/chat',uid=10))
    bot.process_update(message(2,2,'/chat',uid=20))
    update=message(3,3,'',uid=10)
    update['message'].pop('text')
    update['message'].update(photo=[{'file_id':'photo-file'}],caption='Картинка')
    bot.process_update(update)
    with db.connect() as c: job=dict(c.execute("SELECT * FROM jobs WHERE kind='chat_delivery'").fetchone())
    api=FakeAPI(); bot.run_job(api,job,'secret')
    method,payload=api.calls[0]
    assert method=='sendPhoto' and payload['photo']=='photo-file' and payload['caption'].startswith('Аноним ')
    assert 'from_chat_id' not in payload

def test_failed_recipient_does_not_stop_others(setup):
    for uid in (10,20,30): bot.process_update(message(uid,uid,'/chat',uid=uid))
    bot.process_update(message(40,40,'Привет',uid=10))
    with db.connect() as c: jobs=[dict(r) for r in c.execute("SELECT * FROM jobs WHERE kind='chat_delivery' ORDER BY id")]
    bot.run_job(FakeAPI(bot.APIError(403,'Forbidden')),jobs[0],'secret')
    api=FakeAPI(); bot.run_job(api,jobs[1],'secret')
    assert len(api.calls)==1 and api.calls[0][1]['chat_id']==30
    with db.connect() as c: assert c.execute('SELECT chat_active FROM users WHERE id=20').fetchone()[0]==0

def test_migration_preserves_settings_and_posts(setup):
    submit_post()
    with db.connect() as c:
        db.set_value(c,'ADMIN_IDS','777')
        db.set_value(c,'ANON_GROUP_ID','-12345')
        c.execute("DELETE FROM settings WHERE key='RELAY_VERSION'")
        c.execute("UPDATE users SET mode='post'")
        db.enqueue(c,'chat',{'text':'legacy queued'})
    db.init_db(); db.init_db()
    assert db.get('ADMIN_IDS')=='777' and db.get('ANON_GROUP_ID')=='-12345'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM posts').fetchone()[0]==1
        assert c.execute('SELECT mode FROM users').fetchone()[0]=='idle'
        assert c.execute("SELECT status FROM jobs WHERE kind='chat'").fetchone()[0]=='cancelled'

def test_active_menu_has_exit_and_post_only(setup):
    bot.process_update(message(text='/chat'))
    with db.connect() as c: payload=json.loads(c.execute("SELECT payload FROM jobs WHERE kind='send' AND json_type(payload,'$.reply_markup.keyboard') IS NOT NULL ORDER BY id DESC").fetchone()[0])
    buttons=[x['text'] for row in payload['reply_markup']['keyboard'] for x in row]
    assert buttons==[bot.POST,bot.LEAVE]
    assert 'очистка' in payload['text']

def test_all_incoming_and_bot_replies_are_tracked(setup):
    bot.process_update(message(text='/start'))
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM chat_messages WHERE chat_id=12').fetchone()[0]==1
        job=dict(c.execute("SELECT * FROM jobs WHERE kind='send'").fetchone())
    bot.run_job(FakeAPI(),job,'secret')
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM chat_messages WHERE chat_id=12').fetchone()[0]==2
    api=FakeAPI()
    bot.execute_job(api,{'kind':'cleanup','payload':'{}'},'secret')
    assert {p['message_id'] for method,p in api.calls}=={1,77}

def test_private_cleanup_preserves_post_for_later_publication(setup):
    submit_post()
    api=FakeAPI()
    bot.execute_job(api,{'kind':'cleanup','payload':'{}'},'secret')
    job=publish_job(); api=FakeAPI()
    bot.run_job(api,job,'secret')
    assert api.calls[0][0]=='sendMessage'
    assert api.calls[0][1]['chat_id']==-1002
    assert api.calls[0][1]['text']=='История\n\nАнон.'
    with db.connect() as c: assert c.execute('SELECT status FROM posts').fetchone()[0]=='published'

def test_cleanup_does_not_delete_groups_or_newer_messages(setup):
    with db.connect() as c:
        c.executemany('INSERT INTO chat_messages(chat_id,message_id,created) VALUES(?,?,?)',[(12,1,50),(12,2,150),(-1003,3,50)])
    api=FakeAPI()
    bot.execute_job(api,{'kind':'cleanup','payload':json.dumps({'cutoff':100})},'secret')
    assert api.calls==[('deleteMessage',{'chat_id':12,'message_id':1})]

def test_welcome_buttons_and_cleanup_notice(setup):
    bot.process_update(message(text='/start'))
    with db.connect() as c: p=json.loads(c.execute("SELECT payload FROM jobs WHERE kind='send'").fetchone()[0])
    assert [button['text'] for row in p['reply_markup']['keyboard'] for button in row]==[bot.POST,bot.JOIN]
    assert '12' in p['text'] and 'всей переписки' in p['text'] and 'Добро пожаловать' in p['text']

def test_upgrade_original_users_schema(tmp_path,monkeypatch):
    import sqlite3
    path=tmp_path/'old.sqlite3'; monkeypatch.setattr(db,'DB_PATH',path)
    c=sqlite3.connect(path)
    c.executescript("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE users(id INTEGER PRIMARY KEY,mode TEXT DEFAULT 'post',blocked INTEGER DEFAULT 0,last_sent REAL DEFAULT 0,created REAL NOT NULL);")
    c.execute("INSERT INTO settings VALUES('ADMIN_IDS','555')")
    c.execute("INSERT INTO settings VALUES('ADMIN_PASSWORD_HASH','retained-hash')")
    c.execute("INSERT INTO users(id,created) VALUES(42,1)")
    c.commit(); c.close()
    with ThreadPoolExecutor(max_workers=3) as pool: list(pool.map(lambda _:db.init_db(),range(3)))
    assert db.get('ADMIN_IDS')=='555' and db.get('ADMIN_PASSWORD_HASH')=='retained-hash'
    with db.connect() as c:
        u=c.execute('SELECT * FROM users').fetchone()
        assert u['mode']=='idle' and u['chat_active']==0 and u['chat_session']==0
