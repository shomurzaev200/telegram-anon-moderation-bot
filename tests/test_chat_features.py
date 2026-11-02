import json
import time
import pytest
import bot
import db
import chat_features as features
from test_app import setup, message

class ChatAPI:
    def __init__(self): self.calls=[]; self.count=1000
    def call(self,method,**payload):
        self.calls.append((method,payload)); self.count+=1
        return {'message_id':self.count}

def join(uid): bot.process_update(message(uid,uid,'/chat',uid=uid))
def say(uid,update,text,target=None):
    msg=message(update,update,text,uid=uid)
    if target: msg['message']['reply_to_message']={'message_id':target}
    with db.connect() as c: c.execute('UPDATE users SET last_sent=0 WHERE id=?',(uid,))
    bot.process_update(msg)

def deliver(api):
    with db.connect() as c: jobs=[dict(r) for r in c.execute("SELECT * FROM jobs WHERE kind='chat_delivery' AND status='queued' ORDER BY id")]
    for job in jobs: bot.run_job(api,job,'secret')

def copy_id(uid,rid):
    with db.connect() as c: return c.execute('SELECT message_id FROM relay_copies WHERE chat_id=? AND relay_id=?',(uid,rid)).fetchone()[0]

def react(uid,mid,update,rid=1,index=0):
    with db.connect() as c: c.execute('DELETE FROM social_limits')
    bot.process_update({'update_id':update,'callback_query':{'id':str(update),'from':{'id':uid},'data':f'react:{rid}:{index}',
                        'message':{'message_id':mid,'chat':{'id':uid,'type':'private'}}}})

def test_reply_maps_to_every_local_copy_and_author_original(setup):
    for uid in (10,20,30): join(uid)
    api=ChatAPI(); say(10,100,'Исходный текст'); deliver(api)
    parent_b=copy_id(20,1); parent_c=copy_id(30,1)
    say(20,101,'Ответ',parent_b); api.calls.clear(); deliver(api)
    sent={p['chat_id']:p for method,p in api.calls}
    assert sent[10]['reply_parameters']['message_id']==100
    assert sent[30]['reply_parameters']['message_id']==parent_c
    assert 'Исходный текст' in sent[30]['text']
    assert features.nickname(10,'1','secret') in sent[30]['text']
    assert 'from_chat_id' not in sent[30]

def test_new_member_does_not_receive_old_quote(setup):
    join(10); join(20); api=ChatAPI(); say(10,100,'Секрет старой истории'); deliver(api)
    join(30); say(20,101,'Новый ответ',copy_id(20,1)); api.calls.clear(); deliver(api)
    p=next(p for _,p in api.calls if p['chat_id']==30)
    assert 'reply_parameters' not in p and 'Секрет старой истории' not in p['text']
    assert 'более раннее' in p['text']

def test_rejoin_cannot_reference_old_copy(setup):
    join(10); join(20); api=ChatAPI(); say(10,100,'Старое'); deliver(api)
    target=copy_id(20,1)
    bot.process_update(message(102,102,'/leave',uid=20))
    bot.process_update(message(103,103,'/chat',uid=20))
    say(20,104,'Не цитировать',target)
    with db.connect() as c: assert c.execute('SELECT parent_id FROM relay_messages WHERE id=2').fetchone()[0] is None

def test_reactions_toggle_switch_and_sync(setup):
    for uid in (10,20,30): join(uid)
    api=ChatAPI(); say(10,100,'Реакции'); deliver(api)
    mid=copy_id(20,1)
    react(20,mid,101)
    with db.connect() as c:
        assert c.execute('SELECT emoji FROM chat_reactions').fetchone()[0]=='👍'
        jobs=[dict(r) for r in c.execute("SELECT * FROM jobs WHERE kind IN ('reaction_edit','reaction_notice')")]
    api.calls.clear()
    for job in jobs: bot.run_job(api,job,'secret')
    edits=[p for method,p in api.calls if method=='editMessageReplyMarkup']
    assert {p['chat_id'] for p in edits}=={20,30}
    assert all(p['reply_markup']['inline_keyboard'][0][0]['text']=='👍 1' for p in edits)
    assert any(p.get('chat_id')==10 and p.get('reply_parameters',{}).get('message_id')==100 for _,p in api.calls)
    react(20,mid,102,index=1)
    with db.connect() as c: assert list(c.execute('SELECT emoji FROM chat_reactions'))[0][0]=='❤️'
    react(20,mid,103,index=1)
    with db.connect() as c: assert c.execute('SELECT count(*) FROM chat_reactions').fetchone()[0]==0

def test_forged_or_exited_reaction_denied(setup):
    join(10); join(20); api=ChatAPI(); say(10,100,'Hi'); deliver(api)
    join(30); react(30,copy_id(20,1),101)
    bot.process_update(message(102,102,'/leave',uid=20)); react(20,copy_id(20,1),103)
    with db.connect() as c: assert c.execute('SELECT count(*) FROM chat_reactions').fetchone()[0]==0

def test_cleanup_expires_replies_and_reactions(setup):
    join(10); join(20); api=ChatAPI(); say(10,100,'Стереть'); deliver(api)
    react(20,copy_id(20,1),101)
    bot.execute_job(api,{'kind':'cleanup','payload':'{}'},'secret')
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM relay_copies').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM chat_reactions').fetchone()[0]==0
        assert c.execute('SELECT preview FROM relay_messages').fetchone()[0]==''

def test_social_members_only_anonymous_names(setup):
    join(10); join(20); api=ChatAPI()
    with db.connect() as c: session=c.execute('SELECT chat_session FROM users WHERE id=10').fetchone()[0]
    features.execute(api,{'kind':'social','payload':json.dumps({'uid':10,'session':session,'action':'members'})},'secret')
    text=api.calls[0][1]['text']
    assert 'В чате: 2' in text and features.nickname(20,'1','secret') in text
    assert 'Telegram ID' not in text
    features.execute(api,{'kind':'social','payload':json.dumps({'uid':10,'session':session,'action':'topic'})},'secret')
    assert any(t in api.calls[-1][1]['text'] for t in features.TOPICS)

def test_reaction_markup_missing_message_is_harmless(setup):
    class DeletedAPI:
        def call(self,*a,**kw): raise bot.APIError(400,'Bad Request: message to edit not found')
    join(10); join(20); api=ChatAPI(); say(10,100,'Hi'); deliver(api)
    with db.connect() as c: session=c.execute('SELECT chat_session FROM users WHERE id=20').fetchone()[0]
    result=features.execute(DeletedAPI(),{'kind':'reaction_edit','payload':json.dumps({'uid':20,'session':session,'relay_id':1,'message_id':copy_id(20,1)})},'secret')
    assert 'удалена' in result
