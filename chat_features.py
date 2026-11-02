"""Logical chat messages: per-recipient replies, anonymous reactions and social tools."""
import hashlib
import hmac
import json
import secrets
import time
from db import connect, enqueue, get

EMOJIS=('👍','❤️','😂','🔥')
TOPICS=(
 'Если бы завтра можно было отправиться в любую страну — куда бы ты поехал?',
 'Какой фильм или сериал ты бы посоветовал всем в этом чате?',
 'Какую суперспособность ты бы выбрал на один день?',
 'Какая маленькая вещь всегда поднимает тебе настроение?',
 'Какую игру ты можешь перепроходить бесконечно?',
 'Какой навык ты хотел бы освоить за один вечер?',
 'Что лучше: спонтанное путешествие или идеальный план?',
 'Какую песню ты сейчас слушаешь чаще всего?',
)
TOOLS={'inline_keyboard':[[{'text':'👥 Кто в чате','callback_data':'social:members'},
                          {'text':'🎲 Тема для разговора','callback_data':'social:topic'}]]}

def nickname(uid,epoch,secret):
    return 'Аноним '+hmac.new(secret.encode(),f'{epoch}:{uid}'.encode(),hashlib.sha256).hexdigest()[:8]

def register(db,user,message,content,epoch):
    parent=None
    target=message.get('reply_to_message',{}).get('message_id')
    if target:
        row=db.execute('''SELECT r.id FROM relay_copies c JOIN relay_messages r ON r.id=c.relay_id
          WHERE c.chat_id=? AND c.message_id=? AND c.session=? AND r.active=1''',
          (user['id'],target,user['chat_session'])).fetchone()
        if row: parent=row[0]
    rid=db.execute('INSERT INTO relay_messages(uid,epoch,preview,parent_id,created) VALUES(?,?,?,?,?)',
        (user['id'],epoch,(content.get('text') or '['+content['type']+']')[:120],parent,time.time())).lastrowid
    db.execute('INSERT INTO relay_copies VALUES(?,?,?,?,0)',(user['id'],message['message_id'],rid,user['chat_session']))
    return rid

def reaction_markup(db,rid):
    counts=dict(db.execute('SELECT emoji,count(*) FROM chat_reactions WHERE relay_id=? GROUP BY emoji',(rid,)).fetchall())
    return {'inline_keyboard':[[{'text':f'{emoji} {counts.get(emoji,0)}','callback_data':f'react:{rid}:{i}'} for i,emoji in enumerate(EMOJIS)]]}

def decorate(db,p,secret,payload):
    """Return a readable anonymous reply label and the LOCAL Telegram reply target."""
    rid=p.get('relay_id')
    if not rid: return ''  # jobs submitted before this feature was installed
    message=db.execute('SELECT * FROM relay_messages WHERE id=? AND active=1',(rid,)).fetchone()
    if not message: return ''
    payload['reply_markup']=reaction_markup(db,rid)
    if not message['parent_id']: return ''
    parent=db.execute('SELECT * FROM relay_messages WHERE id=? AND active=1',(message['parent_id'],)).fetchone()
    local=db.execute('SELECT message_id FROM relay_copies WHERE relay_id=? AND chat_id=? AND session=?',
                     (message['parent_id'],p['recipient'],p['session'])).fetchone()
    if not parent or not local: return '↪ Ответ на более раннее сообщение\n'
    payload['reply_parameters']={'message_id':local[0],'allow_sending_without_reply':True}
    label='↪ Ответ '+nickname(parent['uid'],parent['epoch'],secret)
    if p['content']['type']=='text': label+=': «'+parent['preview']+'»'
    return label+'\n'

def save_copy(db,p,message_id):
    if p.get('relay_id'):
        db.execute('INSERT OR IGNORE INTO relay_copies VALUES(?,?,?,?,1)',
                   (p['recipient'],message_id,p['relay_id'],p['session']))

def callback(update):
    """No network under DB transaction; validate membership AND possession of this copy."""
    q=update['callback_query']; uid=q['from']['id']; data=q.get('data','')
    if not (data.startswith('react:') or data.startswith('social:')): return False
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('INSERT OR IGNORE INTO processed VALUES(?)',(update['update_id'],)).rowcount: return True
        u=db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
        result='Сначала вступите в чат.'
        enabled=db.execute("SELECT value FROM settings WHERE key='CHAT_ENABLED'").fetchone()[0]=='1'
        if u and u['chat_active'] and not u['blocked'] and enabled:
            limit=db.execute('SELECT last FROM social_limits WHERE uid=?',(uid,)).fetchone()
            if limit and time.time()-limit[0]<2:
                result='Подождите пару секунд.'
            else:
                db.execute('INSERT OR REPLACE INTO social_limits VALUES(?,?)',(uid,time.time()))
                parts=data.split(':')
                if data in ('social:members','social:topic'):
                    enqueue(db,'social',{'uid':uid,'session':u['chat_session'],'action':parts[1]})
                    result='Сейчас покажу.'
                elif len(parts)==3 and parts[1].isdigit() and parts[2] in ('0','1','2','3'):
                    rid=int(parts[1]); msg=q.get('message',{})
                    own=db.execute('''SELECT c.message_id FROM relay_copies c JOIN relay_messages r ON r.id=c.relay_id
                        WHERE c.relay_id=? AND c.chat_id=? AND c.message_id=? AND c.session=? AND c.is_bot=1 AND r.active=1''',
                        (rid,uid,msg.get('message_id',0),u['chat_session'])).fetchone()
                    if not own or msg.get('chat',{}).get('id')!=uid:
                        result='Сообщение уже очищено или недоступно.'
                    else:
                        emoji=EMOJIS[int(parts[2])]
                        previous=db.execute('SELECT emoji FROM chat_reactions WHERE relay_id=? AND uid=?',(rid,uid)).fetchone()
                        if previous and previous[0]==emoji:
                            db.execute('DELETE FROM chat_reactions WHERE relay_id=? AND uid=?',(rid,uid))
                            result='Реакция убрана.'
                        else:
                            db.execute('INSERT OR REPLACE INTO chat_reactions VALUES(?,?,?)',(rid,uid,emoji))
                            result='Реакция добавлена.'
                        # One tiny task per copy: errors/rate limits never block another recipient.
                        for c in db.execute('SELECT * FROM relay_copies WHERE relay_id=?',(rid,)).fetchall():
                            kind='reaction_edit' if c['is_bot'] else 'reaction_notice'
                            key=f"{rid}:{c['chat_id']}:{c['message_id']}"
                            exists=db.execute("SELECT 1 FROM jobs WHERE kind=? AND status='queued' AND json_extract(payload,'$.key')=?",(kind,key)).fetchone()
                            if not exists: enqueue(db,kind,{'key':key,'relay_id':rid,'uid':c['chat_id'],'message_id':c['message_id'],'session':c['session']})
        enqueue(db,'answer',{'callback_query_id':q['id'],'text':result})
    return True

def execute(api,job,secret):
    p=json.loads(job['payload']); kind=job['kind']
    with connect() as db:
        u=db.execute('SELECT * FROM users WHERE id=?',(p['uid'],)).fetchone()
        if not u or not u['chat_active'] or u['blocked'] or u['chat_session']!=p['session'] or get('CHAT_ENABLED')!='1':
            return 'Участник вышел или чат выключен'
        if kind=='social':
            if p['action']=='topic': text='🎲 Тема для разговора\n\n'+secrets.choice(TOPICS)+'\n\nНапиши свой ответ в чат — его увидят участники.'
            else:
                # Alias generation uses the same current epoch/secret as message delivery.
                total=db.execute('SELECT count(*) FROM users WHERE chat_active=1 AND blocked=0').fetchone()[0]
                rows=db.execute('SELECT id FROM users WHERE chat_active=1 AND blocked=0 ORDER BY id LIMIT 50').fetchall()
                text=f'👥 В чате: {total}\n\n'+'\n'.join(nickname(r[0],get('NICK_EPOCH'),secret)+(' (ты)' if r[0]==p['uid'] else '') for r in rows)
                if total>50: text+='\n…показаны первые 50'
                text+='\n\nЭто вступившие участники, а не статус онлайн в Telegram.'
            payload={'chat_id':p['uid'],'text':text,'reply_markup':TOOLS}
            method='sendMessage'
        else:
            r=db.execute('SELECT active FROM relay_messages WHERE id=?',(p['relay_id'],)).fetchone()
            if not r or not r[0]: return 'Сообщение очищено'
            markup=reaction_markup(db,p['relay_id'])
            if kind=='reaction_edit':
                method='editMessageReplyMarkup'
                payload={'chat_id':p['uid'],'message_id':p['message_id'],'reply_markup':markup}
            else:
                method='sendMessage'
                text='Реакции на твоё сообщение: '+' · '.join(b['text'] for b in markup['inline_keyboard'][0])
                payload={'chat_id':p['uid'],'text':text,'reply_parameters':{'message_id':p['message_id'],'allow_sending_without_reply':True}}
    try: sent=api.call(method,**payload)
    except Exception as e:
        if kind=='reaction_edit' and getattr(e,'code',None)==400 and ('message is not modified' in str(e).lower() or 'message to edit not found' in str(e).lower()): return 'Копия уже обновлена или удалена'
        raise
    if method=='sendMessage':
        with connect() as db: db.execute('INSERT OR IGNORE INTO chat_messages(chat_id,message_id,created) VALUES(?,?,?)',(p['uid'],sent['message_id'],time.time()))
    return 'Готово'

def expire(db,cutoff):
    db.execute("UPDATE relay_messages SET active=0,preview='' WHERE created<=?",(cutoff,))
    db.execute('DELETE FROM chat_reactions WHERE relay_id IN (SELECT id FROM relay_messages WHERE active=0)')
    db.execute('DELETE FROM relay_copies WHERE relay_id IN (SELECT id FROM relay_messages WHERE active=0)')
