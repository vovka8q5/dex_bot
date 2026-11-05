# Деплой Dexbot на свой Linux-сервер

Инструкция для Ubuntu или Debian. Стол рассчитан на Bybit Demo Trading: это учебный счёт на `https://api-demo.bybit.com`, не боевые деньги и не testnet. Боевой mainnet по-прежнему выключен, пока в конфиге одновременно не стоят `mode: mainnet` и `allow_mainnet: true`.

Финляндия для Bybit подходит: демо-API оттуда открывается.

## Что понадобится

- Сервер с Ubuntu 22.04/24.04 или Debian 12.
- Ключ Bybit Demo Trading: Демо-торговля → API → Транзакция API, Чтение-Запись, **без вывода**.
- Ключ DeepSeek (`DEEPSEEK_API_KEY`) или xAI (`XAI_API_KEY`). Если xAI пустой или отклонён, стол сам перейдёт на DeepSeek, а если нет и его — на правила без модели.

## 1. Установить Docker

На сервере под пользователем с sudo:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

На Debian замените `ubuntu` на `debian` в адресе репозитория. Дальше (для Ubuntu):

```bash
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Выйдите из SSH и зайдите снова, чтобы группа `docker` применилась. Проверка: `docker compose version`.

## 2. Скачать код

```bash
sudo mkdir -p /opt/dexbot
sudo chown "$USER":"$USER" /opt/dexbot
git clone https://github.com/vovka8q5/dex_bot.git /opt/dexbot
cd /opt/dexbot
```

Адрес репозитория замените на свой, если форк другой.

## 3. Файл .env

```bash
cp .env.example .env
chmod 600 .env
```

`chmod 600` оставляет файл только вам. Его **нельзя** коммитить: `.env` уже в `.gitignore`. Откройте редактором и впишите ключи. Для демо-счёта достаточно:

```bash
DEXBOT_MODE=demo
DEEPSEEK_API_KEY=ваш_ключ
BYBIT_DEMO_API_KEY=ваш_ключ
BYBIT_DEMO_API_SECRET=ваш_секрет
```

`DEXBOT_MODE=demo` перекрывает `mode: paper` в yaml. Боевые ключи `DEXBOT_MAINNET_*` оставьте пустыми.

## 4. Проверить ключ, не покупая

```bash
cd /opt/dexbot
docker compose run --rm bot python -m dexbot check
```

В выводе должны быть `keys_set=true`, `withdraw_permission=false` и строки `balance`. Если вывод средств включён у ключа, команда завершится с кодом 2 и торговля не стартует. Секрет в лог не печатается.

## 5. Запустить круглосуточно

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f bot
```

Сервис называется `bot`. Политика `restart: unless-stopped` поднимает его после перезагрузки сервера и после сбоя. Журнал SQLite и файл лога лежат в томе `dexbot-data`, внутри контейнера это `/app/data` (`journal.sqlite` и `logs/dexbot.log`). Проверка живости контейнера — процесс Python на PID 1 (`healthcheck` раз в минуту).

Остановить, не удаляя данные: `docker compose stop`. Снова включить: `docker compose start`.

## 6. Отчёт

```bash
docker compose run --rm bot python -m dexbot report
```

Эта команда не открывает второй торговый цикл: у неё свой аргумент `report`. Не запускайте `docker compose run bot` без команды — поднимется второй такой же цикл.

## 7. Kill switch

Закрыть все позиции бота и не открывать новые, пока файл на месте:

```bash
docker compose exec bot touch /app/data/KILL
```

Срабатывает на ближайшем проходе (сразу, если цикл как раз идёт, иначе в начале следующего часа). Снять запрет:

```bash
docker compose exec bot rm -f /app/data/KILL
```

Закрыть немедленно одним проходом, не дожидаясь часа:

```bash
docker compose run --rm bot python -m dexbot run --once --kill-switch --config /app/config/default.yaml
```

Пока основной контейнер тоже запущен, не делайте второй `run` одновременно: два процесса будут смотреть один журнал. Сначала `docker compose stop`, затем одноразовый `--kill-switch`, затем `docker compose start`.

## 8. Обновление

```bash
cd /opt/dexbot
git pull
docker compose up -d --build
```

Том с журналом при пересборке не стирается.

## 9. Копия журнала

```bash
mkdir -p "$HOME/dexbot-backups"
docker compose exec bot python -c "import shutil; shutil.copy('/app/data/journal.sqlite', '/app/data/journal-backup.sqlite')"
docker compose cp bot:/app/data/journal.sqlite "$HOME/dexbot-backups/journal-$(date +%F).sqlite"
```

Храните копии не в git.

## 10. Утренний отчёт по cron

```bash
crontab -e
```

Строка (каждый день в 08:00 по времени сервера):

```cron
0 8 * * * cd /opt/dexbot && docker compose run --rm bot python -m dexbot report >> "$HOME/dexbot-report.log" 2>&1
```

## Стопы на бирже

После рыночной покупки на Bybit стол отдельно ставит две условные спот-заявки `tpslOrder`: продажа при падении до стопа и продажа при росте до тейка. ccxt не даёт приклеить stop-loss к самой рыночной заявке, поэтому это отдельные ордера. Если биржа одну из них отвергнет, покупка всё равно остаётся, а стоп проверяет сам цикл. Пока процесс выключен, защищает только та заявка, которую биржа приняла.

В начале каждого прохода, в том числе сразу после старта, стол читает открытые позиции из журнала и баланс счёта. Если монеты на бирже уже нет (стоп исполнился, пока бот лежал), строка в журнале закрывается без повторной продажи. Если монеты ещё есть и цена уже за стопом или тейком, бот продаёт сам.

## Без Docker: systemd

```bash
cd /opt/dexbot
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
chmod 600 .env
# вписать ключи в .env
sudo useradd --system --create-home --home-dir /opt/dexbot dexbot || true
sudo chown -R dexbot:dexbot /opt/dexbot
sudo cp deploy/dexbot.service /etc/systemd/system/dexbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now dexbot
sudo journalctl -u dexbot -f
```

Юнит перезапускается всегда (`Restart=always`). Kill switch: `sudo -u dexbot touch /opt/dexbot/data/KILL`. Отчёт из cron без Docker:

```cron
0 8 * * * cd /opt/dexbot && .venv/bin/python -m dexbot report >> "$HOME/dexbot-report.log" 2>&1
```
