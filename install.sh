#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ $EUID -eq 0 ]]; then
  echo 'Запустите bash install.sh от обычного пользователя ubuntu, без sudo.'; exit 1
fi
project_dir="$(pwd -P)"
service_user="$(id -un)"
if [[ "$project_dir" =~ [[:space:]%] ]]; then echo 'Путь проекта не должен содержать пробелы или %'; exit 1; fi
if [[ -f /etc/systemd/system/anon-v2-bot.service ]]; then
  echo 'Сервисы v2 уже установлены. Для обновления используйте README.md; установщик ничего не меняет.'; exit 1
fi
if command -v ss >/dev/null && ss -H -ltn 'sport = :8080' | grep -q .; then
  echo 'Порт 8080 занят. Остановите старую админку (Ctrl+C в её терминале или её systemd-сервис), затем повторите.'; exit 1
fi
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
venv/bin/python setup.py
sudo tee /etc/systemd/system/anon-v2-bot.service >/dev/null <<EOF
[Unit]
Description=Telegram anonymous moderation bot v2
After=network-online.target
Wants=network-online.target
[Service]
User=$service_user
WorkingDirectory=$project_dir
ExecStart=$project_dir/venv/bin/python $project_dir/bot.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=50
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
[Install]
WantedBy=multi-user.target
EOF
sudo tee /etc/systemd/system/anon-v2-admin.service >/dev/null <<EOF
[Unit]
Description=Anonymous bot admin on port 8080
After=network.target
[Service]
User=$service_user
WorkingDirectory=$project_dir
ExecStart=$project_dir/venv/bin/gunicorn --bind 0.0.0.0:8080 --workers 2 --threads 2 --timeout 30 admin:app
Restart=on-failure
RestartSec=5
TimeoutStopSec=35
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now anon-v2-admin anon-v2-bot
sudo systemctl --no-pager --full status anon-v2-admin anon-v2-bot || true
printf '\nАдминка: http://IP_СЕРВЕРА:8080\nПароль — тот, который вы только что задали.\n'
