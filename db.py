"""Shared SQLite storage. Deliberately separate from unknown legacy databases."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DATA.mkdir(mode=0o700, exist_ok=True)
DB_PATH = DATA / 'anon-v2.sqlite3'
DEFAULTS = {'ADMIN_IDS':'', 'MODERATION_CHANNEL_ID':'', 'PUBLIC_CHANNEL_ID':'',
 'ANON_GROUP_ID':'', 'CHAT_ENABLED':'0', 'CLEANUP_HOURS':'12', 'COOLDOWN_SECONDS':'10',
 'RULES':'Запрещены угрозы, спам, реклама и незаконный контент. Не отправляйте личные данные.',
 'AUTH_VERSION':'1', 'NICK_EPOCH':'1'}

@contextmanager
def connect():
    db = sqlite3.connect(DB_PATH, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def init_db():
    with connect() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,mode TEXT DEFAULT 'post',
          blocked INTEGER DEFAULT 0,last_sent REAL DEFAULT 0,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS posts(id INTEGER PRIMARY KEY,uid INTEGER NOT NULL,
          source_chat INTEGER NOT NULL,source_message INTEGER NOT NULL,preview TEXT NOT NULL,
          kind TEXT NOT NULL,status TEXT DEFAULT 'pending',reason TEXT DEFAULT '',created REAL NOT NULL,
          destination INTEGER,published_message INTEGER,UNIQUE(source_chat,source_message));
        CREATE INDEX IF NOT EXISTS posts_status ON posts(status,id);
        CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,
          status TEXT DEFAULT 'queued',result TEXT DEFAULT '',created REAL NOT NULL,run_after REAL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status,run_after,id);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,created REAL NOT NULL,detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS processed(update_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS chat_messages(chat_id INTEGER,message_id INTEGER,created REAL NOT NULL,
          state TEXT DEFAULT 'active',PRIMARY KEY(chat_id,message_id));
        CREATE TABLE IF NOT EXISTS login_attempts(ip TEXT PRIMARY KEY,n INTEGER NOT NULL,until REAL NOT NULL);
        ''')
        db.executemany('INSERT OR IGNORE INTO settings VALUES(?,?)', DEFAULTS.items())
    os.chmod(DB_PATH, 0o600)

def get(key, default=''):
    with connect() as db:
        row = db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    return row[0] if row else default

def set_value(db,key,value):
    db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key,str(value)))

def enqueue(db,kind,payload):
    return db.execute('INSERT INTO jobs(kind,payload,created) VALUES(?,?,?)',
                      (kind,json.dumps(payload,ensure_ascii=False),time.time())).lastrowid

def event(db,detail):
    db.execute('INSERT INTO events(created,detail) VALUES(?,?)',(time.time(),detail[:1500]))

def decide(post_id,action,actor,reason=''):
    if action not in ('publish','reject'): raise ValueError('Неизвестное действие')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM posts WHERE id=?',(post_id,)).fetchone()
        if not row or row['status']!='pending': return False
        dest=db.execute("SELECT value FROM settings WHERE key='PUBLIC_CHANNEL_ID'").fetchone()[0]
        if action=='publish' and not dest: raise ValueError('Сначала настройте PUBLIC_CHANNEL_ID')
        state='queued' if action=='publish' else 'rejected'
        db.execute('UPDATE posts SET status=?,reason=?,destination=? WHERE id=?',
                   (state,reason[:300],int(dest) if dest else None,post_id))
        if action=='publish': enqueue(db,'publish',{'post_id':post_id})
        else: enqueue(db,'send',{'chat_id':row['uid'],'text':f'Заявка #{post_id} отклонена. {reason[:300]}'})
        event(db,f'{actor}: {action} #{post_id}')
    return True
