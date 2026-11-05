# Деплой Dexbot на Windows

Это основная инструкция, если сервер — Windows Server или обычный Windows 10/11, в том числе виртуальная машина в Финляндии. Стол ходит на Bybit Demo Trading (`https://api-demo.bybit.com`): учебный счёт, не боевые деньги и не testnet. Боевой mainnet не включится, пока одновременно не заданы `mode: mainnet` и `allow_mainnet: true`.

Дальше два пути. Если на сервере можно включить виртуализацию, удобнее Docker. Если провайдер не даёт вложенную виртуализацию (WSL2 тогда не стартует), ставьте Python и службу Windows — путь Б. Он не требует Docker.

Команды ниже — PowerShell. Папка по умолчанию: `C:\dexbot`.

## Что вписать в .env

Ключи только латиницей. Блокнот подходит: файл останется в кодировке, которую Python читает (UTF-8, в том числе с меткой BOM, которую ставит «Сохранить как → UTF-8»).

1. Откройте PowerShell.
2. `notepad C:\dexbot\.env`
3. Впишите строки (без кавычек и пробелов вокруг `=`):

```text
DEXBOT_MODE=demo
DEEPSEEK_API_KEY=ваш_ключ
BYBIT_DEMO_API_KEY=ваш_ключ
BYBIT_DEMO_API_SECRET=ваш_секрет
```

`XAI_API_KEY` можно оставить пустым: при `llm.provider: auto` стол сначала пробует xAI, затем DeepSeek. Файл не коммитьте. Он уже в `.gitignore`.

Закрыть доступ посторонним (PowerShell от имени администратора):

```powershell
icacls C:\dexbot\.env /inheritance:r /grant:r "SYSTEM:(R)" "$env:USERNAME:(R,W)"
```

`SYSTEM` нужен, если службу запускает системная учётная запись.

## Путь А. Docker Desktop и WSL2

Так проще обновлять образ. Ограничение: Docker Desktop рассчитан на Windows 10/11 и работает, пока в систему кто-то вошёл. На Windows Server его часто нет. Тогда либо Docker Engine внутри Ubuntu в WSL2 (ниже), либо путь Б.

### Включить WSL2

Виртуализация должна быть включена у хостера (для виртуальной машины это nested virtualization). PowerShell от имени администратора:

```powershell
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
Restart-Computer
```

После перезагрузки снова от администратора:

```powershell
wsl --install -d Ubuntu
wsl --set-default-version 2
```

Если `wsl --install` пишет, что команда не найдена, поставьте обновление WSL с сайта Microsoft («WSL2 Linux kernel update») и повторите. В Ubuntu при первом запуске задайте имя и пароль пользователя Linux.

### Docker Desktop (Windows 10/11)

1. Скачайте Docker Desktop с сайта Docker и установите. В мастере оставьте бэкенд WSL2.
2. Запустите Docker Desktop один раз и дождитесь статуса Engine running.
3. Settings → General → включите **Start Docker Desktop when you sign in**.
4. Эта галочка поднимает Docker при входе пользователя, а не как службу до логина. Если на сервер никто не входит, контейнер не стартует. Для машины без рабочего стола используйте Docker Engine в WSL2 или путь Б.

Проверка:

```powershell
docker compose version
```

### Если Docker Desktop на Windows Server не ставится

Поставьте Docker Engine внутри Ubuntu в WSL2. В терминале Ubuntu:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
```

Закройте Ubuntu и откройте снова. Автозапуск после перезагрузки Windows — задача планировщика от администратора (один раз):

```powershell
$action = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d Ubuntu -u root -- service docker start"
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName "WslDocker" -Action $action -Trigger $trigger -Settings $settings -User "SYSTEM" -RunLevel Highest -Force
```

Репозиторий для этого варианта держите в файловой системе Linux (`\\wsl$\Ubuntu\home\<имя>\dexbot`), не на диске `C:`, иначе Docker в WSL будет медленным. Дальнейшие команды `docker compose` те же, их запускают из этой папки в Ubuntu. Путь Б проще, если WSL2 на сервере не включается.

### Клонировать и запустить (Docker Desktop)

PowerShell:

```powershell
New-Item -ItemType Directory -Force -Path C:\dexbot | Out-Null
git clone https://github.com/vovka8q5/dex_bot.git C:\dexbot
Set-Location C:\dexbot
Copy-Item .env.example .env
notepad .env
docker compose run --rm bot python -m dexbot check
docker compose up -d --build
docker compose ps
docker compose logs -f bot
```

Адрес `git clone` замените, если репозиторий другой. `check` ничего не покупает. В ответе нужны `keys_set=true` и `withdraw_permission=false`. Если у ключа есть право вывода, стол откажется работать.

Журнал SQLite и лог лежат в томе Docker `dexbot-data`. Внутри контейнера это `C:\` не используется: путь `/app/data/journal.sqlite` и `/app/data/logs/dexbot.log`. Контейнер сам перезапускается (`restart: unless-stopped`), пока запущен Docker.

Отчёт:

```powershell
Set-Location C:\dexbot
docker compose run --rm bot python -m dexbot report
```

Не запускайте `docker compose run bot` без команды: поднимется второй торговый цикл рядом с уже работающим.

Остановить торговлю и закрыть позиции на ближайшем проходе:

```powershell
docker compose exec bot sh -c "touch /app/data/KILL"
```

Снова разрешить входы:

```powershell
docker compose exec bot sh -c "rm -f /app/data/KILL"
```

Обновление:

```powershell
Set-Location C:\dexbot
git pull
docker compose up -d --build
```

Копия журнала на диск Windows:

```powershell
New-Item -ItemType Directory -Force -Path C:\dexbot-backups | Out-Null
docker compose cp bot:/app/data/journal.sqlite "C:\dexbot-backups\journal-$(Get-Date -Format yyyy-MM-dd).sqlite"
```

Утренний отчёт (планировщик, каждый день в 08:00):

```powershell
$action = New-ScheduledTaskAction -Execute "docker.exe" -Argument "compose run --rm bot python -m dexbot report" -WorkingDirectory "C:\dexbot"
$trigger = New-ScheduledTaskTrigger -Daily -At 8:00am
Register-ScheduledTask -TaskName "DexbotReport" -Action $action -Trigger $trigger -User $env:USERNAME -Force
```

Нужен запущенный Docker. Вывод задачи смотрите в «Планировщик заданий».

## Путь Б. Без Docker: Python и служба

Подходит для Windows Server, где WSL2 недоступен.

### Установить Python и Git

- Git for Windows: https://git-scm.com/download/win
- Python 3.12 или новее: https://www.python.org/downloads/windows/  
  В установщике включите **Add python.exe to PATH**.

Проверка в новом окне PowerShell:

```powershell
git --version
py -3.12 --version
```

Если `py` не находится, смотрите `python --version`. Нужна ветка 3.12 или выше.

### Поставить стол

```powershell
New-Item -ItemType Directory -Force -Path C:\dexbot | Out-Null
git clone https://github.com/vovka8q5/dex_bot.git C:\dexbot
Set-Location C:\dexbot
powershell -ExecutionPolicy Bypass -File C:\dexbot\deploy\windows\install.ps1
notepad C:\dexbot\.env
powershell -ExecutionPolicy Bypass -File C:\dexbot\deploy\windows\run-check.ps1
```

`install.ps1` создаёт `C:\dexbot\.venv`, ставит пакет и, если `.env` ещё нет, копирует пример. Скрипт проверки должен показать ключи и балансы и не купить монету.

### Служба: NSSM или планировщик

NSSM удобнее: отдельная служба, автозапуск до входа пользователя, перезапуск при падении. Скачайте ZIP с https://nssm.cc/download и положите `win64\nssm.exe` в `C:\nssm\win64\nssm.exe`.

PowerShell от имени администратора:

```powershell
powershell -ExecutionPolicy Bypass -File C:\dexbot\deploy\windows\register-service.ps1
Get-Service Dexbot
```

Если NSSM не найден, тот же скрипт создаёт задачу планировщика `Dexbot`: запуск при старте Windows и повтор при сбое. Статус:

```powershell
Get-ScheduledTask -TaskName Dexbot
```

Логи процесса: `C:\dexbot\data\logs\dexbot.log`. У службы NSSM дополнительно `service-stdout.log` и `service-stderr.log` в той же папке. Открыть в блокноте:

```powershell
notepad C:\dexbot\data\logs\dexbot.log
```

Остановить и запустить службу NSSM:

```powershell
Stop-Service Dexbot
Start-Service Dexbot
```

Для задачи планировщика: `Stop-ScheduledTask -TaskName Dexbot` и `Start-ScheduledTask -TaskName Dexbot`.

### Отчёт, kill switch, обновление, копия журнала

```powershell
powershell -ExecutionPolicy Bypass -File C:\dexbot\deploy\windows\report.ps1
powershell -ExecutionPolicy Bypass -File C:\dexbot\deploy\windows\kill-switch.ps1
powershell -ExecutionPolicy Bypass -File C:\dexbot\deploy\windows\resume.ps1
```

`kill-switch.ps1` создаёт `C:\dexbot\data\KILL`. Служба на следующем часовом проходе закрывает позиции и не открывает новые. `resume.ps1` удаляет файл.

Немедленное закрытие, чтобы не ждать час: остановите службу, выполните один проход и запустите службу снова.

```powershell
Stop-Service Dexbot
Set-Location C:\dexbot
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\python.exe -m dexbot run --once --kill-switch --config C:\dexbot\config\default.yaml
Start-Service Dexbot
```

Обновление кода:

```powershell
Set-Location C:\dexbot
git pull
powershell -ExecutionPolicy Bypass -File C:\dexbot\deploy\windows\install.ps1
Restart-Service Dexbot
```

Если вместо службы задача планировщика:

```powershell
Stop-ScheduledTask -TaskName Dexbot
Start-ScheduledTask -TaskName Dexbot
```

Копия базы:

```powershell
New-Item -ItemType Directory -Force -Path C:\dexbot-backups | Out-Null
Copy-Item C:\dexbot\data\journal.sqlite "C:\dexbot-backups\journal-$(Get-Date -Format yyyy-MM-dd).sqlite"
```

Файл может быть занят службой. Если копирование ругается, остановите службу, скопируйте и запустите снова. Копии не кладите в git.

Ежедневный отчёт в 08:00:

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\dexbot\deploy\windows\report.ps1" -WorkingDirectory "C:\dexbot"
$trigger = New-ScheduledTaskTrigger -Daily -At 8:00am
Register-ScheduledTask -TaskName "DexbotReport" -Action $action -Trigger $trigger -User "SYSTEM" -RunLevel Highest -Force
```

## Стопы

После покупки на Bybit стол ставит отдельные условные заявки стопа и тейка. Они остаются на бирже, если программа выключена. В начале каждого прохода, сразу после старта службы тоже, журнал сверяется с балансом: монеты, которых на счёте уже нет, не продаются повторно; по оставшимся позициям стоп проверяется сразу.
