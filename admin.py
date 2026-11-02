"""Russian administration UI. All Telegram work runs in bot.py's durable queue."""
import json
import secrets
import time
from datetime import timedelta
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from db import DATA, DEFAULTS, connect, decide, enqueue, event, get, init_db, set_value

EDITABLE = tuple(k for k in DEFAULTS if k not in ('AUTH_VERSION','NICK_EPOCH','ANON_GROUP_ID'))

def create_app(test_config=None):
    init_db()
    path=DATA/'secrets.json'
    config=json.loads(path.read_text()) if path.exists() else {}
    app=Flask(__name__)
    app.config.update(SECRET_KEY=config.get('secret_key') or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Strict',
        SESSION_COOKIE_SECURE=config.get('secure_cookie',False),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),MAX_CONTENT_LENGTH=65536)
    if test_config: app.config.update(test_config)

    @app.template_filter('date')
    def date(v):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(v,timezone(timedelta(hours=5))).strftime('%d.%m %H:%M:%S')

    @app.context_processor
    def common():
        if 'csrf' not in session: session['csrf']=secrets.token_hex(24)
        return {'csrf':session['csrf']}

    @app.before_request
    def protect():
        if request.endpoint=='static': return
        if request.method=='POST':
            expected=session.get('csrf','')
            if not expected or not secrets.compare_digest(expected,request.form.get('csrf','')):
                abort(400,'Форма устарела. Обновите страницу и повторите.')
        if request.endpoint not in ('login',):
            if not session.get('auth') or session.get('version')!=get('AUTH_VERSION'):
                session.clear()
                return redirect(url_for('login'))

    @app.after_request
    def headers(response):
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
        return response

    @app.route('/login',methods=['GET','POST'])
    def login():
        error=''
        configured=bool(get('ADMIN_PASSWORD_HASH'))
        if request.method=='POST':
            if not configured: return render_template('login.html',error='Сначала выполните python setup.py'),503
            ip=request.remote_addr or 'unknown'
            now=time.time()
            with connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT * FROM login_attempts WHERE ip=?',(ip,)).fetchone()
                if row and row['until']>now and row['n']>=5:
                    return render_template('login.html',error='Слишком много попыток. Подождите до 15 минут.'),429
                n=row['n'] if row and row['until']>now else 0
                db.execute('INSERT OR REPLACE INTO login_attempts VALUES(?,?,?)',(ip,n+1,row['until'] if n else now+900))
                db.execute('DELETE FROM login_attempts WHERE until<?',(now-86400,))
            if check_password_hash(get('ADMIN_PASSWORD_HASH'),request.form.get('password','')):
                with connect() as db: db.execute('DELETE FROM login_attempts WHERE ip=?',(ip,))
                session.clear()
                session.update(auth=True,version=get('AUTH_VERSION'),csrf=secrets.token_hex(24))
                session.permanent=True
                return redirect(url_for('index'))
            error='Неверный пароль'
        if not configured: error='Первый запуск: выполните python setup.py на сервере.'
        return render_template('login.html',error=error)

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

    @app.get('/')
    def index():
        with connect() as db:
            stats=dict(db.execute('SELECT status,count(*) FROM posts GROUP BY status').fetchall())
            users=db.execute('SELECT count(*) FROM users').fetchone()[0]
            jobs=dict(db.execute('SELECT status,count(*) FROM jobs GROUP BY status').fetchall())
        heartbeat=float(get('HEARTBEAT','0'))
        return render_template('index.html',stats=stats,users=users,jobs=jobs,
            alive=time.time()-heartbeat<60,heartbeat=heartbeat)

    @app.get('/posts')
    def posts():
        status=request.args.get('status','pending')
        allowed=['pending','queued','publishing','published','rejected','failed','uncertain','all']
        if status not in allowed: abort(400)
        page=max(1,request.args.get('page',1,type=int))
        where='' if status=='all' else 'WHERE status=?'
        args=[] if status=='all' else [status]
        with connect() as db:
            rows=db.execute(f'SELECT * FROM posts {where} ORDER BY id DESC LIMIT 31 OFFSET ?',args+[(page-1)*30]).fetchall()
        return render_template('posts.html',rows=rows[:30],more=len(rows)>30,page=page,status=status,allowed=allowed)

    @app.post('/posts/<int:post_id>/<action>')
    def post_action(post_id,action):
        try:
            ok=decide(post_id,action,'web-admin',request.form.get('reason','').strip())
            flash('Действие принято.' if ok else 'Заявка уже обработана.')
        except ValueError as e: flash(str(e))
        return redirect(url_for('posts'))

    @app.route('/settings',methods=['GET','POST'])
    def settings():
        if request.method=='POST':
            values={k:request.form.get(k,'').strip() for k in EDITABLE}
            try:
                ids=values['ADMIN_IDS'].replace(' ',',').split(',')
                if not any(ids) or any(not v.isdigit() or int(v)<=0 for v in ids if v):
                    raise ValueError('Укажите положительные Telegram ID администраторов через запятую.')
                values['ADMIN_IDS']=','.join(str(int(v)) for v in ids if v)
                for key in ('MODERATION_CHANNEL_ID','PUBLIC_CHANNEL_ID'):
                    if not values[key] or not values[key].lstrip('-').isdigit() or int(values[key])>=0:
                        raise ValueError(f'{key}: нужен отрицательный числовой ID, обычно -100…')
                if len({values[k] for k in ('MODERATION_CHANNEL_ID','PUBLIC_CHANNEL_ID')})!=2:
                    raise ValueError('Каналы модерации и публикации должны иметь разные ID.')
                if values['CHAT_ENABLED'] not in ('0','1'): raise ValueError('CHAT_ENABLED: только 0 или 1')
                for key,low,high in [('CLEANUP_HOURS',1,24),('COOLDOWN_SECONDS',1,3600)]:
                    if not values[key].isdigit() or not low<=int(values[key])<=high: raise ValueError(f'{key}: от {low} до {high}')
                if not 1<=len(values['RULES'])<=3000: raise ValueError('Правила: от 1 до 3000 символов')
                with connect() as db:
                    for k,v in values.items(): set_value(db,k,v)
                    event(db,'web-admin: настройки сохранены')
                flash('Сохранено. Бот применит настройки без перезапуска.')
                return redirect(url_for('settings'))
            except ValueError as e:
                flash(str(e))
                return render_template('settings.html',values=values),400
        return render_template('settings.html',values={k:get(k) for k in EDITABLE})

    @app.get('/users')
    def users():
        page=max(1,request.args.get('page',1,type=int))
        q=request.args.get('q','').strip()
        with connect() as db:
            if q:
                rows=db.execute('SELECT * FROM users WHERE id=?',(q,)).fetchall()
            else: rows=db.execute('SELECT * FROM users ORDER BY id DESC LIMIT 31 OFFSET ?',((page-1)*30,)).fetchall()
        return render_template('users.html',rows=rows[:30],more=len(rows)>30,page=page,q=q)

    @app.post('/users/<int:uid>/toggle')
    def block(uid):
        with connect() as db:
            db.execute('UPDATE users SET blocked=1-blocked,chat_active=0,chat_session=chat_session+1 WHERE id=?',(uid,))
            event(db,f'web-admin: изменение блокировки {uid}')
        return redirect(url_for('users'))

    @app.get('/jobs')
    def jobs():
        page=max(1,request.args.get('page',1,type=int))
        with connect() as db: rows=db.execute('SELECT id,kind,status,result,created FROM jobs ORDER BY id DESC LIMIT 31 OFFSET ?',((page-1)*30,)).fetchall()
        return render_template('jobs.html',rows=rows[:30],more=len(rows)>30,page=page)

    @app.post('/tasks/<kind>')
    def task(kind):
        if kind not in ('check','cleanup','rotate'): abort(404)
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            exists=db.execute("SELECT 1 FROM jobs WHERE kind=? AND status IN ('queued','running')",(kind,)).fetchone()
            if not exists: enqueue(db,kind,{})
        flash('Задача в очереди. Результат появится в разделе «Задачи».')
        return redirect(url_for('jobs'))

    @app.get('/logs')
    def logs():
        page=max(1,request.args.get('page',1,type=int))
        with connect() as db: rows=db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 51 OFFSET ?',((page-1)*50,)).fetchall()
        return render_template('logs.html',rows=rows[:50],more=len(rows)>50,page=page)

    @app.route('/password',methods=['GET','POST'])
    def password():
        if request.method=='POST':
            old=request.form.get('old',''); new=request.form.get('new','')
            if not check_password_hash(get('ADMIN_PASSWORD_HASH'),old): flash('Текущий пароль неверен.')
            elif len(new)<12 or new!=request.form.get('confirm'): flash('Нужно не менее 12 символов и совпадение двух вводов.')
            else:
                hashed=generate_password_hash(new)
                with connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    set_value(db,'ADMIN_PASSWORD_HASH',hashed)
                    ver=db.execute("SELECT value FROM settings WHERE key='AUTH_VERSION'").fetchone()[0]
                    set_value(db,'AUTH_VERSION',int(ver)+1)
                    event(db,'web-admin: пароль изменён; сессии завершены')
                session.clear()
                return redirect(url_for('login'))
        return render_template('password.html')
    return app

app=create_app()
if __name__=='__main__':
    # Production entry point without Flask development server.
    import os, sys
    os.execv(sys.executable,[sys.executable,'-m','gunicorn','--bind','0.0.0.0:8080',
                            '--workers','2','--threads','2','--timeout','30','admin:app'])
