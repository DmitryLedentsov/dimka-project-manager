# Dimka Project Manager

Dimka Project Manager (DPM) — лёгкая панель для одного Linux-сервера. Она клонирует Git-репозитории, раз в минуту проверяет новые коммиты, собирает проекты и управляет их процессами. Один репозиторий может объявить несколько сервисов.

В DPM два интерфейса над одним демоном:

- адаптивная веб-панель по адресу `/admin`;
- CLI `dpm` для терминала.

Frontend написан на HTML, CSS и jQuery. Backend — Python, Flask, SQLite и обычные Linux-процессы без Docker, Kubernetes и тяжёлых UI-фреймворков.

## Возможности v0.1

- вход в панель, первоначально `admin / admin`;
- добавление проекта по Git URL;
- публичные и приватные репозитории через SSH;
- несколько сервисов в одном `dpm.yaml`;
- автоматическая проверка Git с настраиваемым интервалом;
- build-команды перед перезапуском;
- start, stop, restart и delete для процессов;
- restart policy: `always`, `on-failure`, `never`;
- HTTP- и command-healthchecks;
- stdout/stderr в файлы и live-логи в панели;
- статусы, PID, uptime, CPU и память;
- история последнего деплоя и видимые ошибки;
- никакого скрытого rollback: упавший build или process остаётся явно виден;
- CLI использует тот же HTTP API, что и веб-клиент.

## Установка

Поддерживается Linux с systemd. На Debian/Ubuntu недостающие Python, Git и OpenSSH установятся автоматически.

```bash
git clone https://github.com/DmitryLedentsov/dimka-project-manager.git
cd dimka-project-manager
sudo ./install.sh
```

Установщик запросит:

- админский логин;
- пароль, по умолчанию `admin`;
- публичный URL панели;
- listen host и port;
- базовый путь, по умолчанию `/admin`;
- интервал проверки Git, по умолчанию 60 секунд.

По умолчанию панель слушает порт `8787`:

```text
http://SERVER_IP:8787/admin
```

Можно указать порт `80`, тогда итоговый адрес будет вида:

```text
http://89.169.1.117/admin
```

После установки скрипт напечатает публичный SSH-ключ пользователя `dpm`. Добавьте его в GitHub для доступа к приватным репозиториям.

```bash
sudo cat /var/lib/dpm/.ssh/id_ed25519.pub
```

## Конфигурация проекта

В корне управляемого репозитория должен лежать `dpm.yaml`:

```yaml
version: 1

project:
  name: multiplayer-game

build:
  commands:
    - ./mvnw clean package -DskipTests
    - npm --prefix frontend ci
    - npm --prefix frontend run build

services:
  - name: main-service
    command:
      - java
      - -jar
      - main-service/target/main-service.jar
    working_directory: .
    environment_file: .env
    restart: always
    healthcheck:
      type: http
      url: http://127.0.0.1:8080/actuator/health
      timeout_seconds: 45

  - name: frontend
    command: "npm run preview -- --host 0.0.0.0 --port 3000"
    working_directory: frontend
    restart: on-failure
    depends_on:
      - main-service
```

`command` допускает массив аргументов либо shell-строку. Build-команды исполняются через Bash из корня репозитория.

Если build завершается ошибкой, работающие процессы не перезапускаются, а деплой получает статус `failed`. Если build прошёл, но новый сервис упал, он остаётся в состоянии `failed` с сохранёнными логами. Автоматического rollback в первой версии нет.

## CLI

```bash
dpm status

dpm project list
dpm project add git@github.com:DmitryLedentsov/my-project.git --branch master
dpm project check 1
dpm project deploy 1
dpm project remove 1

dpm service list
dpm service start 1
dpm service stop 1
dpm service restart 1
dpm service delete 1

dpm logs 1 --lines 300
dpm logs 1 --follow
```

CLI обращается к локальному API DPM и использует токен из `/etc/dpm/config.env`. Он не открывает SQLite и не управляет процессами параллельно с демоном.

## Управление самим DPM

```bash
sudo ./config.sh       # изменить логин, пароль, URL и polling
sudo ./update.sh       # обновить установленный manager из текущего checkout
sudo ./uninstall.sh    # удалить программу, сохранить данные
sudo ./uninstall.sh --purge
```

Полезные системные команды:

```bash
systemctl status dimka-project-manager
journalctl -u dimka-project-manager -f
```

Данные находятся в `/var/lib/dpm`, логи сервисов — в `/var/log/dpm`, конфигурация — в `/etc/dpm/config.env`.

## Модель обновления

DPM сравнивает SHA удалённой ветки с последней попыткой деплоя:

```text
git ls-remote
→ новый SHA
→ fetch + checkout
→ dpm.yaml
→ build
→ stop old processes
→ start new processes
→ optional healthchecks
```

Неуспешный commit не пересобирается каждую минуту бесконечно. Его можно повторить кнопкой **Deploy now** или новым коммитом.

## Разработка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v

DPM_DATA_DIR=/tmp/dpm-data \
DPM_LOG_DIR=/tmp/dpm-logs \
DPM_CONFIG_FILE=/tmp/dpm.env \
DPM_PORT=8787 \
python -m dpm.app
```

Откройте `http://127.0.0.1:8787/admin`, логин `admin`, пароль `admin`.
