"""Anonymous per-user votes on the actual published channel message."""
import json
from db import connect, enqueue, get

def markup(db,pid,username):
    counts=dict(db.execute('SELECT vote,count(*) FROM post_votes WHERE post_id=? GROUP BY vote',(pid,)).fetchall())
    rows=[[{'text':f"👍 {counts.get(1,0)}",'callback_data':f'vote:{pid}:up'},
           {'text':f"👎 {counts.get(-1,0)}",'callback_data':f'vote:{pid}:down'}]]
    if username:
        rows.append([{'text':'💬 Анонимные комментарии','url':f'https://t.me/{username}?start=post_{pid}'}])
    return {'inline_keyboard':rows}

def callback(update):
    q=update['callback_query']; data=q.get('data','')
    if not data.startswith('vote:'): return False
    parts=data.split(':'); uid=q['from']['id']; message=q.get('message',{})
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('INSERT OR IGNORE INTO processed VALUES(?)',(update['update_id'],)).rowcount: return True
        text='Публикация недоступна.'
        if len(parts)==3 and parts[1].isdigit() and parts[2] in ('up','down'):
            pid=int(parts[1])
            post=db.execute("SELECT * FROM posts WHERE id=? AND status='published'",(pid,)).fetchone()
            blocked=db.execute('SELECT blocked FROM users WHERE id=?',(uid,)).fetchone()
            if blocked and blocked[0]: text='Действие недоступно.'
            elif (post and post['destination']==message.get('chat',{}).get('id') and
                  post['published_message']==message.get('message_id')):
                vote=1 if parts[2]=='up' else -1
                old=db.execute('SELECT vote FROM post_votes WHERE post_id=? AND uid=?',(pid,uid)).fetchone()
                if old and old[0]==vote:
                    db.execute('DELETE FROM post_votes WHERE post_id=? AND uid=?',(pid,uid)); text='Голос убран.'
                else:
                    db.execute('INSERT INTO post_votes VALUES(?,?,?) ON CONFLICT(post_id,uid) DO UPDATE SET vote=excluded.vote',(pid,uid,vote))
                    text='Лайк учтён.' if vote==1 else 'Дизлайк учтён.'
                # Coalesce waiting refreshes; a running edit needs a subsequent refresh.
                if not db.execute("SELECT 1 FROM jobs WHERE kind='post_vote_refresh' AND status='queued' AND json_extract(payload,'$.post_id')=?",(pid,)).fetchone():
                    enqueue(db,'post_vote_refresh',{'post_id':pid,'chat_id':post['destination']})
        enqueue(db,'answer',{'callback_query_id':q['id'],'text':text})
    return True

def execute(api,job):
    pid=json.loads(job['payload'])['post_id']
    with connect() as db:
        post=db.execute("SELECT * FROM posts WHERE id=? AND status='published'",(pid,)).fetchone()
        if not post: return 'Пост недоступен'
        keyboard=markup(db,pid,get('BOT_USERNAME'))
    try:
        api.call('editMessageReplyMarkup',chat_id=post['destination'],message_id=post['published_message'],reply_markup=keyboard)
    except Exception as e:
        if getattr(e,'code',None)==400 and 'message is not modified' in str(e).lower(): return 'Счётчики уже обновлены'
        raise
    return 'Счётчики голосов обновлены'
