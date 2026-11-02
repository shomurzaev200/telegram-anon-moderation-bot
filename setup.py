"""Interactive local bootstrap; never sends credentials anywhere."""
import getpass
import json
import os
import re
import secrets
from db import DATA, init_db, connect, set_value, get
from werkzeug.security import generate_password_hash

def main():
    init_db()
    path=DATA/'secrets.json'
    config=json.loads(path.read_text()) if path.exists() else {}
    token=getpass.getpass('BOT_TOKEN (скрытый ввод; Enter — оставить прежний): ').strip()
    if token:
        if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}',token): raise SystemExit('Неверный формат токена')
        config['bot_token']=token
    if not config.get('bot_token'): raise SystemExit('Нужен BOT_TOKEN от BotFather')
    password=getpass.getpass('Новый пароль админки (минимум 12 символов): ')
    if len(password)<12: raise SystemExit('Пароль слишком короткий')
    if password!=getpass.getpass('Повторите пароль: '): raise SystemExit('Пароли не совпадают')
    config.setdefault('secret_key',secrets.token_hex(32))
    config.setdefault('secure_cookie',False)
    tmp=DATA/'secrets.tmp'
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f: json.dump(config,f)
    tmp.replace(path)
    os.chmod(path,0o600)
    with connect() as db:
        set_value(db,'ADMIN_PASSWORD_HASH',generate_password_hash(password))
        set_value(db,'AUTH_VERSION',int(get('AUTH_VERSION','1'))+1)
        db.execute('DELETE FROM login_attempts')
    print('Готово. Настройте ID каналов и администраторов в веб-панели.')

if __name__=='__main__': main()
