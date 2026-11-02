"""Anonymous post discussions in bot DMs, separate from the live relay chat."""
import json
import time
from db import connect, enqueue
from chat_features import nickname

PAGE_SIZE=3

def buttons(pid):
    return {'inline_keyboard':[[{'text':'💬 Читать комментарии','callback_data':f'discussion:open:{pid}:0'},
                              {'text':'✍️ Комментировать','callback_data':f'discussion:write:{pid}'}]]}

def published(db,post):
    """Called in the publication commit: exactly one notification task per subscriber."""
    for row in db.execute('SELECT id FROM users WHERE blocked=0 AND notifications=1').fetchall():
        enqueue(db,'post_notice',{'uid':row[0],'post_id':post['id']})

def panel(db,uid):
    value=db.execute('SELECT notifications FROM users WHERE id=?',(uid,)).fetchone()[0]
    enqueue(db,'send',{'chat_id':uid,'text':'Уведомления о новых анонах: '+('включены' if value else 'выключены'),
        'reply_markup':{'inline_keyboard':[[{'text':'Выключить' if value else 'Включить',
        'callback_data':'discussion:notify:'+('off' if value else 'on')}]]}})

def open_post(db,uid,pid,page=0):
    if not db.execute("SELECT 1 FROM posts WHERE id=? AND status='published'",(pid,)).fetchone():
        enqueue(db,'send',{'chat_id':uid,'text':'Пост ещё не опубликован или недоступен.'}); return
    enqueue(db,'discussion_view',{'uid':uid,'post_id':pid,'page':max(0,page)})

def callback(update):
    q=update['callback_query']; data=q.get('data','')
    if not data.startswith('discussion:'): return False
    uid=q['from']['id']; parts=data.split(':')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('INSERT OR IGNORE INTO processed VALUES(?)',(update['update_id'],)).rowcount: return True
        u=db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
        text='Сначала отправьте /start боту.'
        if u and not u['blocked']:
            text='Неверная кнопка.'
            if len(parts)==3 and parts[1]=='notify' and parts[2] in ('on','off'):
                db.execute('UPDATE users SET notifications=? WHERE id=?',(int(parts[2]=='on'),uid)); panel(db,uid); text='Сохранено.'
            elif len(parts) in (3,4) and parts[2].isdigit():
                pid=int(parts[2]); post=db.execute("SELECT 1 FROM posts WHERE id=? AND status='published'",(pid,)).fetchone()
                if not post: text='Пост недоступен.'
                elif parts[1]=='write':
                    db.execute("UPDATE users SET mode='comment',comment_post=? WHERE id=?",(pid,uid))
                    enqueue(db,'send',{'chat_id':uid,'text':f'Напишите комментарий к анону #{pid} (текст до 800 символов). Он появится в обсуждении этого поста, а не в общем чате. /cancel — отменить. Комментарии остаются в обсуждении после очистки лички.'})
                    text='Жду комментарий.'
                elif parts[1]=='open' and len(parts)==4 and parts[3].isdigit():
                    open_post(db,uid,pid,int(parts[3])); text='Открываю обсуждение.'
        enqueue(db,'answer',{'callback_query_id':q['id'],'text':text})
    return True

def receive(db,user,message):
    if user['mode']!='comment': return False
    pid=user['comment_post']; text=message.get('text','').strip()
    if not text or len(text.encode('utf-16-le'))//2>800:
        enqueue(db,'send',{'chat_id':user['id'],'text':'Отправьте текст до 800 символов или /cancel.'}); return True
    if not db.execute("SELECT 1 FROM posts WHERE id=? AND status='published'",(pid,)).fetchone():
        db.execute("UPDATE users SET mode='idle',comment_post=NULL WHERE id=?",(user['id'],))
        enqueue(db,'send',{'chat_id':user['id'],'text':'Пост недоступен. Комментарий не отправлен.'}); return True
    cid=db.execute('INSERT INTO post_comments(post_id,uid,body,created) VALUES(?,?,?,?)',(pid,user['id'],text,time.time())).lastrowid
    db.execute("UPDATE users SET mode='idle',comment_post=NULL WHERE id=?",(user['id'],))
    count=db.execute('SELECT count(*) FROM post_comments WHERE post_id=? AND hidden=0',(pid,)).fetchone()[0]
    enqueue(db,'send',{'chat_id':user['id'],'text':f'Комментарий #{cid} добавлен к анону #{pid}.'+(' Теперь вы снова пишете в общий чат.' if user['chat_active'] else ''),'reply_markup':buttons(pid)})
    open_post(db,user['id'],pid,(count-1)//PAGE_SIZE)
    return True

def execute(api,job,secret):
    p=json.loads(job['payload']); uid=p['uid']; pid=p['post_id']
    with connect() as db:
        user=db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
        post=db.execute("SELECT * FROM posts WHERE id=? AND status='published'",(pid,)).fetchone()
        if not user or user['blocked'] or not post: return 'Получатель или пост недоступен'
        if job['kind']=='post_notice' and not user['notifications']: return 'Уведомления выключены'
        markup=buttons(pid)
        header=f"📣 Новый анон #{pid}" if job['kind']=='post_notice' else f"💬 Обсуждение анона #{pid}"
        text=header+'\n\n'+post['preview'][:150]+'\n\nАнон.'
        if job['kind']=='discussion_view':
            count=db.execute('SELECT count(*) FROM post_comments WHERE post_id=? AND hidden=0',(pid,)).fetchone()[0]
            page=min(p.get('page',0),max(0,(count-1)//PAGE_SIZE))
            rows=db.execute('SELECT * FROM post_comments WHERE post_id=? AND hidden=0 ORDER BY id LIMIT ? OFFSET ?',
                (pid,PAGE_SIZE,page*PAGE_SIZE)).fetchall()
            text+=f'\n\nКомментарии: {count} · страница {page+1}\n'
            for row in rows:
                text+='\n'+nickname(row['uid'],f'post:{pid}',secret)+f" · #{row['id']}\n"+row['body']+'\n'
            if not rows: text+='\nПока пусто. Напиши первый комментарий!'
            nav=[]
            if page: nav.append({'text':'← Раньше','callback_data':f'discussion:open:{pid}:{page-1}'})
            nav.append({'text':'Обновить','callback_data':f'discussion:open:{pid}:{page}'})
            if (page+1)*PAGE_SIZE<count: nav.append({'text':'Далее →','callback_data':f'discussion:open:{pid}:{page+1}'})
            markup['inline_keyboard'].append(nav)
        else:
            text+='\n\nМожно прочитать и оставить анонимный комментарий. /notifications — настройки уведомлений.'
        dest=post['destination']
        if dest and dest < -1000000000000 and post['published_message']:
            markup['inline_keyboard'].append([{'text':'Открыть пост в канале','url':f"https://t.me/c/{-dest-1000000000000}/{post['published_message']}"}])
    sent=api.call('sendMessage',chat_id=uid,text=text,reply_markup=markup,link_preview_options={'is_disabled':True})
    with connect() as db:
        db.execute('INSERT OR IGNORE INTO chat_messages(chat_id,message_id,created) VALUES(?,?,?)',(uid,sent['message_id'],time.time()))
    return 'Доставлено'
