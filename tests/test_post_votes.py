import json
from concurrent.futures import ThreadPoolExecutor
import bot
import db
import post_votes
from test_app import setup, FakeAPI
from test_discussions_queue import publish

def vote(update,uid=500,choice='up',chat=-1002,mid=77):
    return {'update_id':update,'callback_query':{'id':str(update),'from':{'id':uid},'data':f'vote:1:{choice}',
            'message':{'chat':{'id':chat,'type':'channel'},'message_id':mid}}}

def counts():
    with db.connect() as c: return dict(c.execute('SELECT vote,count(*) FROM post_votes GROUP BY vote').fetchall())

def test_new_publication_buttons(setup):
    api,_=publish(); rows=api.calls[0][1]['reply_markup']['inline_keyboard']
    assert [b['text'] for b in rows[0]]==['👍 0','👎 0']
    assert rows[1][0]['url'].endswith('?start=post_1')
    assert 'Опубликовать' not in str(rows)

def test_vote_switch_remove_and_no_signup(setup):
    publish()
    bot.process_update(vote(200)); assert counts()=={1:1}
    bot.process_update(vote(201,choice='down')); assert counts()=={-1:1}
    bot.process_update(vote(202,choice='down')); assert counts()=={}
    with db.connect() as c: assert c.execute('SELECT 1 FROM users WHERE id=500').fetchone() is None

def test_duplicate_callback_is_not_a_second_toggle(setup):
    publish(); update=vote(200)
    bot.process_update(update); bot.process_update(update)
    assert counts()=={1:1}

def test_forged_message_and_blocked_user_cannot_vote(setup):
    publish(); bot.process_update(vote(200,chat=12)); bot.process_update(vote(201,mid=99))
    with db.connect() as c: c.execute('UPDATE users SET blocked=1 WHERE id=12')
    bot.process_update(vote(202,uid=12))
    assert counts()=={}

def test_parallel_votes_count_once_and_coalesce(setup):
    publish()
    with ThreadPoolExecutor(max_workers=30) as pool:
        list(pool.map(lambda i:bot.process_update(vote(200+i,uid=1000+i)),range(60)))
    assert counts()=={1:60}
    with db.connect() as c:
        jobs=[dict(r) for r in c.execute("SELECT * FROM jobs WHERE kind='post_vote_refresh'")]
    assert len(jobs)==1
    api=FakeAPI(); post_votes.execute(api,jobs[0])
    method,p=api.calls[0]; assert method=='editMessageReplyMarkup'
    assert p['chat_id']==-1002 and p['message_id']==77
    assert p['reply_markup']['inline_keyboard'][0][0]['text']=='👍 60'
    assert p['reply_markup']['inline_keyboard'][1][0]['url'].endswith('post_1')

def test_votes_survive_initialization_and_private_cleanup(setup):
    publish(); bot.process_update(vote(200)); db.init_db()
    bot.execute_job(FakeAPI(),{'kind':'cleanup','payload':'{}'},'secret')
    assert counts()=={1:1}

def test_not_modified_is_success(setup):
    publish()
    result=post_votes.execute(FakeAPI(bot.APIError(400,'Bad Request: message is not modified')),
        {'payload':json.dumps({'post_id':1})})
    assert 'уже' in result
