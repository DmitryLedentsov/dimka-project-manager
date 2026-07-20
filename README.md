# Dimka Project Manager

Dimka Project Manager (DPM) — лёгкая панель управления приложениями на одном Linux-сервере.

DPM использует двухуровневую модель:

```text
Project
├── process component
├── static component
└── future component types
```

**Project** — логическая единица деплоя и управления Git-репозиторием. Его можно деплоить, редеплоить, запускать и останавливать целиком.

**Component** — конкретная часть проекта на сервере. У каждого типа свой lifecycle, страница, действия, настройки и логи. Добавление нового component handler не требует менять логику проекта.

Сейчас поддерживаются:

- `process` — долгоживущий Linux-процесс;
- `static` — публикация собранных файлов в целевую директорию.

В будущем registry рассчитан на `cron`, `job`, `container`, `systemd`, `external` и другие типы.

DPM, Git, build-команды и компоненты работают от `root`. Отдельный системный пользователь не создаётся.

## Интерфейсы

- адаптивная web-панель: `/admin`;
- CLI `dpm`;
- единый Python daemon и единый HTTP API;
- SQLite для состояния;
- HTML, CSS и jQuery без тяжёлого frontend-фреймворка.

## Установка

```bash
git clone https://github.com/DmitryLedentsov/dimka-project-manager.git
cd dimka-project-manager
sudo ./install.sh
```

Обновление:

```bash
git pull origin master
sudo ./update.sh
```

Конфигурация:

```bash
sudo ./config.sh
```

Данные:

```text
/var/lib/dpm
/var/log/dpm
/etc/dpm/config.env
```

SSH-ключ для приватных Git-репозиториев:

```bash
sudo cat /var/lib/dpm/.ssh/id_ed25519.pub
```

## `dpm.yaml`

В корне проекта должен находиться простой manifest:

```yaml
version: 1

project:
  name: example-app

build:
  - ./mvnw clean package -DskipTests
  - npm --prefix frontend ci
  - npm --prefix frontend run build

components:
  api:
    type: process
    command:
      - java
      - -jar
      - backend/target/app.jar
    cwd: .
    env_file: /etc/example-app/runtime.env
    env:
      SERVER_PORT: "8080"
    healthcheck:
      tcp: 127.0.0.1:8080
      timeout: 45s

  web:
    type: static
    depends_on: [api]
    source: frontend/dist
    target: /var/www/example-app
    url: https://example.com
    healthcheck:
      http: https://example.com
      timeout: 15s
```

`components` — map, где ключ одновременно является читаемым именем и стабильным ID внутри проекта.

Общие поля:

```yaml
components:
  component-name:
    type: process
    enabled: true
    depends_on: []
```

### Process

```yaml
worker:
  type: process
  command: python worker.py
  cwd: backend
  env_file: /etc/app/runtime.env
  env:
    WORKERS: "4"
  healthcheck:
    command: python healthcheck.py
    timeout: 30s
```

Healthcheck поддерживает одну из форм:

```yaml
healthcheck:
  tcp: 127.0.0.1:8080
  timeout: 30s
```

```yaml
healthcheck:
  http: http://127.0.0.1:8080/health
  timeout: 30s
```

```yaml
healthcheck:
  command: ./healthcheck.sh
  timeout: 30s
```

Process actions:

```text
Start
Stop
Restart
Logs
```

После аварийного выхода process остаётся `FAILED`. Автоматического рестарта нет.

### Static

```yaml
web:
  type: static
  source: frontend/dist
  target: /var/www/app
  url: https://example.com
  index: index.html
  healthcheck:
    http: https://example.com
    timeout: 15s
```

Static actions:

```text
Publish
Unpublish
Republish
Open
Publication logs
```

DPM копирует release во временную директорию, атомарно переключает target и восстанавливает предыдущую версию, если HTTP-check новой публикации не прошёл.

## Project lifecycle

```text
fetch repository
→ read dpm.yaml
→ register missing components
→ build
→ stop old components in reverse dependency order
→ apply component definitions
→ start components in dependency order
→ healthchecks
```

Если build падает, старые работающие компоненты не трогаются.

У проекта есть:

```text
desired_state = running | stopped
actual_state  = running | stopped | deploying | degraded | failed
```

`Stop project` останавливает все дочерние компоненты. Пока project остановлен, отдельный component нельзя запустить — сначала нужно запустить project.

Deploy остановленного project обновляет checkout и артефакты, но оставляет компоненты остановленными.

Логи разделены:

- project deployment log: Git, build, apply, component ordering;
- process component log: stdout/stderr;
- static component log: publication и HTTP-check.

## CLI

```bash
dpm status

dpm project list
dpm project add git@github.com:user/project.git --branch master
dpm project check 1
dpm project deploy 1
dpm project redeploy 1
dpm project start 1
dpm project stop 1
dpm project remove 1

dpm component list
dpm component start 1
dpm component stop 1
dpm component restart 1
dpm component delete 1

dpm logs 1 --lines 300
dpm logs 1 --follow
```

`dpm service ...` сохранён как compatibility alias для `dpm component ...`.

## Совместимость

Manifest первого поколения с `services:` продолжает читаться как набор `process`-компонентов. После следующего успешного деплоя записи автоматически получают новый component schema.

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
