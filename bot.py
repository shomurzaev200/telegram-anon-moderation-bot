# Telegram Anon Moderation Bot
# Анонимные посты через канал модерации и текстовый чат через бота.
import hashlib
import hmac
import json
import logging
import signal
import threading
import time
import requests
from db import DATA, connect, decide, enqueue, event, get, init_db, set_value

log=logging.getLogger('anon')
STOP=threading.Event()
JOIN='💬 Вступить в анонимный чат'
LEAVE='🚪 Выйти из анонимного чата'
POST='📝 Написать пост'

def menu(active=False):
    return {'keyboard':[[{'text':POST}],[{'text':LEAVE if active else JOIN}]],'resize_keyboard':True}

class APIError(Exception):
    def __init__(self,code,description,retry_after=0):
        super().__init__(f'Telegram {code}: {description}')
        self.code=code; self.retry_after=retry_after

class UncertainError(Exception): pass

class Telegram:
    def __init__(self,token):
        self.token=token
        self.http=requests.Session()
    def call(self,method,**payload):
        try:
            response=self.http.post(f'https://api.telegram.org/bot{self.token}/{method}',
                json=payload,timeout=(5,40 if method=='getUpdates' else 20))
            data=response.json()
        except (requests.RequestException,ValueError):
            # Never include requests' URL (contains token) in logs.
            raise UncertainError('Сетевой сбой; результат запроса неизвестен') from None
        if not data.get('ok'):
            code=data.get('error_code',response.status_code)
            description=str(data.get('description','Ошибка API')).replace(self.token,'[TOKEN]')[:300]
            if code>=500: raise UncertainError(f'Telegram {code}: результат запроса неизвестен')
            raise APIError(code,description,data.get('parameters',{}).get('retry_after',0))
        return data['result']

def post_content(message):
    if 'text' in message:
        return {'method':'sendMessage','data':{'text':message['text'],'entities':message.get('entities',[])}}
    methods={'photo':'sendPhoto','video':'sendVideo','animation':'sendAnimation','document':'sendDocument',
             'voice':'sendVoice','audio':'sendAudio','sticker':'sendSticker','video_note':'sendVideoNote'}
    for kind,method in methods.items():
        if kind in message:
            file=message[kind][-1] if kind=='photo' else message[kind]
            data={kind:file['file_id']}
            if kind not in ('sticker','video_note'):
                data.update(caption=message.get('caption',''),caption_entities=message.get('caption_entities',[]))
            return {'method':method,'data':data}
    raise ValueError('Unsupported post content')

def send_post(api,post,destination,buttons=None):
    if post['content']:
        content=json.loads(post['content'])
        payload=dict(content['data'],chat_id=destination)
        if buttons: payload['reply_markup']=buttons
        return api.call(content['method'],**payload)
    # Compatibility for existing v2 submissions, whose original message still exists.
    payload={'chat_id':destination,'from_chat_id':post['source_chat'],'message_id':post['source_message']}
    if buttons: payload['reply_markup']=buttons
    return api.call('copyMessage',**payload)

def reply(db,uid,text,menu=False):
    payload={'chat_id':uid,'text':text}
    if menu:
        member=db.execute('SELECT chat_active FROM users WHERE id=?',(uid,)).fetchone()
        payload['reply_markup']=globals()['menu'](bool(member and member[0]))
    enqueue(db,'send',payload)

def process_update(update):
    update_id=update['update_id']
    callback=update.get('callback_query')
    if callback:
        admins={int(x) for x in get('ADMIN_IDS').split(',') if x.strip().isdigit()}
        text='Доступ запрещён'
        if callback['from']['id'] in admins:
            parts=callback.get('data','').split(':')
            if len(parts)==2 and parts[0] in ('publish','reject') and parts[1].isdigit():
                try:
                    ok=decide(int(parts[1]),parts[0],f"telegram:{callback['from']['id']}",'Решение модератора')
                    text='Принято' if ok else 'Уже обработано'
                except ValueError as e: text=str(e)
        with connect() as db:
            if not db.execute('INSERT OR IGNORE INTO processed VALUES(?)',(update_id,)).rowcount: return
            enqueue(db,'answer',{'callback_query_id':callback['id'],'text':text[:180]})
        return
    m=update.get('message')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('INSERT OR IGNORE INTO processed VALUES(?)',(update_id,)).rowcount: return
        if not m or m['chat']['type']!='private' or m.get('from',{}).get('is_bot'): return
        uid=m['from']['id']
        db.execute('INSERT OR IGNORE INTO chat_messages(chat_id,message_id,created) VALUES(?,?,?)',(uid,m['message_id'],time.time()))
        db.execute("INSERT OR IGNORE INTO users(id,created,mode) VALUES(?,?,'idle')",(uid,time.time()))
        u=db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
        if u['blocked']:
            # Silent block avoids creating spam reply jobs.
            return
        settings=dict(db.execute('SELECT key,value FROM settings').fetchall())
        text=m.get('text','')
        command=text.split()[0].split('@')[0] if text.startswith('/') else text
        if command in ('/start','/help'):
            db.execute("UPDATE users SET mode='idle' WHERE id=?",(uid,))
            reply(db,uid,'Добро пожаловать! Здесь можно общаться анонимно или предложить пост в канал. '
                  'Только посты проходят модерацию. В чате участники видят псевдонимы; '
                  'администрация сервера имеет доступ к Telegram ID. '
                  f"⚠️ Каждые {settings['CLEANUP_HOURS']} ч запускается очистка всей переписки с ботом: ваших сообщений, постов и ответов бота. "
                  'Заявки на модерации и опубликованные посты в канале сохраняются. '
                  'Удаление зависит от ограничений Telegram; скриншоты и сохранённые копии удалить невозможно.',True)
            return
        if command in ('/id','🆔 Мой ID'):
            reply(db,uid,f'Ваш Telegram ID: {uid}'); return
        if command in ('/rules','📜 Правила'):
            reply(db,uid,settings['RULES']); return
        if command in ('/chat',JOIN,'💬 Анонимный чат'):
            if settings['CHAT_ENABLED']!='1':
                reply(db,uid,'Анонимный чат сейчас выключен.',True); return
            db.execute("UPDATE users SET chat_session=chat_session+CASE WHEN chat_active=0 THEN 1 ELSE 0 END,chat_active=1,mode='idle' WHERE id=?",(uid,))
            reply(db,uid,'Вы в анонимном чате. Ваши сообщения увидят участники, которые вступили в чат. '
                  'Новая переписка приходит сюда, без модерации. Старая история не загружается. '
                  f"⚠️ Каждые {settings['CLEANUP_HOURS']} ч запускается очистка всей переписки с ботом, включая ваши сообщения и посты. "
                  'Telegram может не разрешить удалить отдельные сообщения; сохранённые копии и скриншоты удалить невозможно. '
                  'Чтобы прекратить общение, нажмите «Выйти из анонимного чата».',True)
            return
        if command in ('/leave',LEAVE):
            db.execute("UPDATE users SET chat_active=0,chat_session=chat_session+1,mode='idle' WHERE id=?",(uid,))
            reply(db,uid,'Вы вышли из анонимного чата. Новые сообщения больше не доставляются.',True)
            return
        if command in ('/post',POST,'📝 Отправить пост'):
            db.execute("UPDATE users SET mode='post' WHERE id=?",(uid,))
            reply(db,uid,'Следующее сообщение будет ПОСТОМ: оно попадёт на модерацию, затем — в канал после одобрения. '
                  'Отправьте один текст или медиа. /cancel — отменить.',True)
            return
        if command=='/cancel':
            db.execute("UPDATE users SET mode='idle' WHERE id=?",(uid,))
            reply(db,uid,'Написание поста отменено.' + (' Вы снова пишете в чат.' if u['chat_active'] else ''),True)
            return
        if text.startswith('/'):
            reply(db,uid,'Используйте кнопки меню или /chat, /leave, /post, /cancel.',True); return
        if u['mode']!='post' and not u['chat_active']:
            reply(db,uid,'Сначала нажмите «Вступить в анонимный чат» или «Написать пост».',True); return
        now=time.time()
        if now-u['last_sent']<int(settings['COOLDOWN_SECONDS']):
            reply(db,uid,'Слишком быстро. Подождите несколько секунд и отправьте сообщение снова.'); return
        if u['mode']!='post':
            if settings['CHAT_ENABLED']!='1':
                reply(db,uid,'Анонимный чат временно выключен.',True); return
            media=next((k for k in ('photo','video','animation','document','voice','audio') if k in m),None)
            if m.get('media_group_id') or (not text and not media):
                reply(db,uid,'Отправьте текст, отдельное фото, видео, анимацию, документ, аудио или голосовое. Альбомы, контакты и локации не поддерживаются.'); return
            if len(text)>3500 or len(m.get('caption',''))>900:
                reply(db,uid,'Максимум 3500 символов текста или 900 символов подписи.'); return
            content={'type':'text','text':text}
            if media:
                file=m[media][-1] if media=='photo' else m[media]
                content={'type':media,'file_id':file['file_id'],'text':m.get('caption','')}
            # Snapshot recipients and their membership generation at submission, not dispatch.
            members=db.execute('SELECT id,chat_session FROM users WHERE chat_active=1 AND blocked=0 AND id!=?',(uid,)).fetchall()
            db.execute('UPDATE users SET last_sent=? WHERE id=?',(now,uid))
            db.execute('INSERT OR IGNORE INTO chat_messages(chat_id,message_id,created) VALUES(?,?,?)',(uid,m['message_id'],now))
            for member in members:
                enqueue(db,'chat_delivery',{'uid':uid,'recipient':member['id'],'session':member['chat_session'],
                    'epoch':settings['NICK_EPOCH'],'content':content})
            if not members: reply(db,uid,'Сейчас других участников нет. Новые участники это сообщение не получат.')
            return
        if not settings['MODERATION_CHANNEL_ID'] or not settings['PUBLIC_CHANNEL_ID']:
            reply(db,uid,'Публикации ещё не настроены администратором.'); return
        if m.get('media_group_id'):
            reply(db,uid,'Альбом не принят. Отправьте нужное фото или видео отдельно.'); return
        kind=next((k for k in ('text','photo','video','animation','document','audio','voice','video_note','sticker') if k in m),None)
        if not kind:
            reply(db,uid,'Этот тип сообщения не поддерживается. Отправьте текст или медиа.'); return
        preview=(m.get('text') or m.get('caption') or f'[{kind}]')[:4096]
        cursor=db.execute('INSERT OR IGNORE INTO posts(uid,source_chat,source_message,preview,kind,created,content) VALUES(?,?,?,?,?,?,?)',
                          (uid,m['chat']['id'],m['message_id'],preview,kind,now,json.dumps(post_content(m),ensure_ascii=False)))
        if not cursor.rowcount: return
        db.execute("UPDATE users SET mode='idle',last_sent=? WHERE id=?",(now,uid))
        pid=cursor.lastrowid
        enqueue(db,'moderate',{'post_id':pid,'chat_id':int(settings['MODERATION_CHANNEL_ID'])})
        reply(db,uid,f'Заявка #{pid} принята на модерацию.' + (' Теперь вы снова пишете в анонимный чат.' if u['chat_active'] else ' Для нового поста снова нажмите «Написать пост».'),True)

def poll(token):
    api=Telegram(token)
    while not STOP.is_set():
        try:
            updates=api.call('getUpdates',offset=int(get('UPDATE_OFFSET','0')),timeout=25,
                             allowed_updates=['message','callback_query'])
            for update in updates:
                if STOP.is_set(): break
                process_update(update)
                with connect() as db:
                    set_value(db,'UPDATE_OFFSET',update['update_id']+1)
                    db.execute('DELETE FROM processed WHERE update_id<?',(update['update_id']-10000,))
        except Exception as e:
            # Exceptions from storage have no user content; suppress full requests traces.
            with connect() as db: event(db,f'Получение обновлений: {type(e).__name__}: {str(e)[:300]}')
            STOP.wait(max(5, e.retry_after) if isinstance(e, APIError) else 5)

def claim():
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute("SELECT * FROM jobs WHERE status='queued' AND run_after<=? ORDER BY CASE kind WHEN 'answer' THEN 0 ELSE 1 END,id LIMIT 1",(time.time(),)).fetchone()
        if row: db.execute("UPDATE jobs SET status='running' WHERE id=?",(row['id'],))
        return dict(row) if row else None

def rotate():
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        epoch=int(db.execute("SELECT value FROM settings WHERE key='NICK_EPOCH'").fetchone()[0])+1
        set_value(db,'NICK_EPOCH',epoch)
    return 'Псевдонимы сменены.'

def execute_job(api,job,secret):
    p=json.loads(job['payload']); kind=job['kind']
    if kind in ('send','answer'):
        sent=api.call('sendMessage' if kind=='send' else 'answerCallbackQuery',**p)
        if kind=='send' and int(p['chat_id'])>0:
            with connect() as db:
                db.execute('INSERT OR IGNORE INTO chat_messages(chat_id,message_id,created) VALUES(?,?,?)',(p['chat_id'],sent['message_id'],time.time()))
        return 'Доставлено'
    if kind=='moderate':
        with connect() as db: post=db.execute('SELECT * FROM posts WHERE id=?',(p['post_id'],)).fetchone()
        if post['status']!='pending': return 'Заявка уже обработана'
        buttons={'inline_keyboard':[[{'text':f"✅ Опубликовать #{post['id']}",'callback_data':f"publish:{post['id']}"},
                                    {'text':'❌ Отклонить','callback_data':f"reject:{post['id']}"}]]}
        sent=send_post(api,post,p['chat_id'],buttons)
        if not post['content']:
            with connect() as db:
                db.execute('UPDATE posts SET source_chat=?,source_message=? WHERE id=?',(p['chat_id'],sent['message_id'],post['id']))
        return 'Отправлено модераторам'
    if kind=='publish':
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            post=db.execute('SELECT * FROM posts WHERE id=?',(p['post_id'],)).fetchone()
            if post['status']!='queued': return 'Заявка уже обработана'
            db.execute("UPDATE posts SET status='publishing' WHERE id=?",(post['id'],))
        sent=send_post(api,post,post['destination'])
        with connect() as db:
            db.execute("UPDATE posts SET status='published',published_message=? WHERE id=?",(sent['message_id'],post['id']))
            reply(db,post['uid'],f"Заявка #{post['id']} опубликована.")
            event(db,f"Опубликована заявка #{post['id']}")
        return f"Опубликовано: сообщение {sent['message_id']}"
    if kind=='chat':
        return 'Старая доставка в группу отменена'
    if kind=='chat_delivery':
        with connect() as db:
            recipient=db.execute('SELECT * FROM users WHERE id=?',(p['recipient'],)).fetchone()
            author=db.execute('SELECT blocked FROM users WHERE id=?',(p['uid'],)).fetchone()
        if (not recipient or not recipient['chat_active'] or recipient['blocked'] or
            recipient['chat_session']!=p['session'] or not author or author['blocked'] or get('CHAT_ENABLED')!='1'):
            return 'Отменено: участник вышел, заблокирован или чат выключен'
        nick=hmac.new(secret.encode(),f"{p['epoch']}:{p['uid']}".encode(),hashlib.sha256).hexdigest()[:8]
        content=p['content']; label=f"Аноним {nick}"
        payload={'chat_id':p['recipient'],'protect_content':True}
        if content['type']=='text':
            method='sendMessage'; payload.update(text=label+'\n\n'+content['text'],link_preview_options={'is_disabled':True})
        else:
            kind_name=content['type']
            method='send'+kind_name.capitalize()
            payload.update({kind_name:content['file_id'],'caption':label+'\n\n'+content['text']})
        sent=api.call(method,**payload)
        with connect() as db:
            db.execute('INSERT OR IGNORE INTO chat_messages(chat_id,message_id,created) VALUES(?,?,?)',
                       (p['recipient'],sent['message_id'],time.time()))
        return 'Доставлено участнику'
    if kind=='rotate': return rotate()
    if kind=='cleanup':
        deleted=failed=0
        cutoff=p.get('cutoff',time.time())
        with connect() as db:
            # Don't deliver stale queued chat content after the cleanup cycle.
            db.execute("UPDATE jobs SET status='cancelled',payload='{}',result='Цикл очистки переписки' WHERE kind='chat_delivery' AND status='queued' AND created<=?",(cutoff,))
            rows=db.execute("SELECT * FROM chat_messages WHERE state='active' AND chat_id>0 AND created<=? ORDER BY created LIMIT 500",(cutoff,)).fetchall()
        for row in rows:
            if STOP.is_set(): break
            # Preserve an old-format pending post if its private source has not been archived.
            with connect() as db:
                legacy=db.execute("SELECT 1 FROM posts WHERE source_chat=? AND source_message=? AND content='' AND status IN ('pending','queued','publishing')",(row['chat_id'],row['message_id'])).fetchone()
            if legacy:
                with connect() as db: db.execute("UPDATE chat_messages SET state='protected' WHERE chat_id=? AND message_id=?",(row['chat_id'],row['message_id']))
                failed+=1
                continue
            try:
                api.call('deleteMessage',chat_id=row['chat_id'],message_id=row['message_id'])
                state='deleted'; deleted+=1
            except APIError as e:
                if 'message to delete not found' in str(e).lower(): state='deleted'; deleted+=1
                elif e.code==429: raise
                else: state='failed'; failed+=1
            with connect() as db:
                db.execute('UPDATE chat_messages SET state=? WHERE chat_id=? AND message_id=?',(state,row['chat_id'],row['message_id']))
                set_value(db,'HEARTBEAT',time.time())
        with connect() as db:
            remaining=db.execute("SELECT count(*) FROM chat_messages WHERE state='active' AND chat_id>0 AND created<=?",(cutoff,)).fetchone()[0]
            if remaining and not STOP.is_set(): enqueue(db,'cleanup',{'cutoff':cutoff})
        if not remaining: rotate()
        return f'Удалено: {deleted}; не удалось: {failed}; осталось: {remaining}. Неудачные удаления автоматически не повторяются.'
    if kind=='check':
        me=api.call('getMe'); results=[f"Бот: @{me.get('username','')} ({me['id']})"]
        for key in ('MODERATION_CHANNEL_ID','PUBLIC_CHANNEL_ID'):
            cid=get(key)
            if not cid: results.append(f'{key}: не настроено'); continue
            try:
                chat=api.call('getChat',chat_id=int(cid))
                member=api.call('getChatMember',chat_id=int(cid),user_id=me['id'])
                status=member.get('status')
                can_post=status=='creator' or (status=='administrator' and (chat.get('type')!='channel' or member.get('can_post_messages',False)))
                can_delete=status=='creator' or member.get('can_delete_messages',False)
                results.append(f"{key}: {chat.get('title','')} / {status}; публикация: {can_post}; удаление: {can_delete}")
            except APIError as e: results.append(f'{key}: {e}')
        return '\n'.join(results)
    raise ValueError('Неизвестная задача')

def run_job(api,job,secret):
    status='done'; result=''; retry=0
    try: result=execute_job(api,job,secret)
    except APIError as e:
        if e.code==429:
            status='queued'; retry=time.time()+max(1,e.retry_after)
        else: status='failed'
        result=str(e)
    except UncertainError as e:
        status='uncertain'; result=str(e)
    except Exception as e:
        status='uncertain'; result=f'{type(e).__name__}: обработка прервана; проверьте результат вручную'
    with connect() as db:
        db.execute('UPDATE jobs SET status=?,result=?,run_after=? WHERE id=?',(status,result[:2000],retry,job['id']))
        if job['kind']=='publish' and status!='done':
            pid=json.loads(job['payload'])['post_id']
            db.execute('UPDATE posts SET status=? WHERE id=? AND status IN (?,?)',(status,pid,'publishing','queued'))
        if status in ('failed','uncertain'): event(db,f"Задача #{job['id']} ({job['kind']}): {result}")
        if job['kind']=='chat_delivery':
            if status=='failed' and result.startswith('Telegram 403:'):
                p=json.loads(job['payload'])
                db.execute('UPDATE users SET chat_active=0,chat_session=chat_session+1 WHERE id=? AND chat_session=?',
                           (p['recipient'],p['session']))
            if status in ('done','failed','uncertain'):
                db.execute("UPDATE jobs SET payload='{}' WHERE id=?",(job['id'],))


def recover():
    with connect() as db:
        db.execute("UPDATE jobs SET status='uncertain',result='Процесс остановился во время выполнения. Проверьте результат вручную.' WHERE status='running'")
        db.execute("UPDATE posts SET status='uncertain' WHERE status='publishing'")
        # A worker can die between claiming a publish job and changing post status.
        for row in db.execute("SELECT payload FROM jobs WHERE kind='publish' AND status='uncertain'").fetchall():
            db.execute("UPDATE posts SET status='uncertain' WHERE id=? AND status='queued'",(json.loads(row['payload'])['post_id'],))

def main():
    import fcntl
    init_db()
    lock=open(DATA/'bot.lock','w')
    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit('Бот уже запущен для этой базы.')
    path=DATA/'secrets.json'
    if not path.exists(): raise SystemExit('Сначала выполните python setup.py')
    config=json.loads(path.read_text()); api=Telegram(config['bot_token'])
    # Do not silently remove a webhook installed by another application.
    try:
        info=api.call('getWebhookInfo')
        if info.get('url'): raise SystemExit('У бота установлен webhook. Отключите старое приложение и webhook перед запуском polling.')
    except (APIError,UncertainError) as e: raise SystemExit(str(e)) from None
    recover()
    for sig in (signal.SIGTERM,signal.SIGINT): signal.signal(sig,lambda *_:STOP.set())
    thread=threading.Thread(target=poll,args=(config['bot_token'],),daemon=True)
    thread.start()
    next_tick=0
    while not STOP.is_set():
        if not thread.is_alive(): raise RuntimeError('Поток получения сообщений остановился')
        now=time.time()
        if now>=next_tick:
            with connect() as db:
                set_value(db,'HEARTBEAT',now)
                last=float(get('LAST_CLEANUP',str(now)))
                if not get('LAST_CLEANUP'): set_value(db,'LAST_CLEANUP',now)
                if now-last>=int(get('CLEANUP_HOURS'))*3600:
                    if not db.execute("SELECT 1 FROM jobs WHERE kind='cleanup' AND status IN ('queued','running')").fetchone(): enqueue(db,'cleanup',{})
                    set_value(db,'LAST_CLEANUP',now)
            next_tick=now+10
        job=claim()
        if job: run_job(api,job,config['secret_key'])
        else: STOP.wait(.25)
    thread.join(timeout=40)
    with connect() as db: set_value(db,'HEARTBEAT',0)

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    main()
