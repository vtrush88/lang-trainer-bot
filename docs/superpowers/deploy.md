# Spanish Bot — Deploy Run-book (DigitalOcean)

Пошаговый провижининг с нуля. Значения в угловых скобках (`<DROPLET_IP>`,
`<MAC_REPO>`) подставляются по ходу. Дизайн: `specs/2026-06-15-spanish-bot-deploy-design.md`.

## 0. Предусловия (на маке)
- Аккаунт DigitalOcean и SSH-ключ для входа на дроплет (`~/.ssh/id_ed25519`).
- Локальный репо: `<MAC_REPO>` = `Projects/Spanish Bot/app`.
- Рабочий `.env` и `spanish_bot.db` в `<MAC_REPO>`.

## 1. Приватный GitHub-репозиторий
```bash
cd "<MAC_REPO>"
# через gh (если установлен) — создаёт приватный репо и добавляет remote:
gh repo create spanish-bot --private --source=. --remote=origin --push
# либо вручную: создать приватный репо на github.com, затем:
#   git remote add origin git@github.com:<USER>/lang-trainer-bot.git && git push -u origin main
```
Проверка: `git remote -v` показывает `origin`, на GitHub виден `main`.

## 2. Дроплет
- DigitalOcean → Create Droplet: Ubuntu 24.04 LTS, Basic, самый дешёвый
  (512 MiB $4/mo или 1 GiB $6/mo), регион Frankfurt/Amsterdam, аутентификация — твой SSH-ключ.
- Запиши `<DROPLET_IP>`.
```bash
ssh root@<DROPLET_IP>          # первый вход
```

## 3. Пользователь, пакеты, firewall (на сервере, под root)
```bash
adduser --disabled-password --gecos "" spanishbot
apt update && apt install -y python3.12 python3.12-venv git sqlite3
ufw allow OpenSSH && ufw --force enable
```

Дать `spanishbot` SSH-доступ (иначе `scp spanishbot@...` и вход не сработают —
у нового пользователя нет `authorized_keys`). Переиспользуем ключ Victoria,
который DigitalOcean уже положил для `root`:
```bash
mkdir -p /home/spanishbot/.ssh
cp ~/.ssh/authorized_keys /home/spanishbot/.ssh/authorized_keys
chown -R spanishbot:spanishbot /home/spanishbot/.ssh
chmod 700 /home/spanishbot/.ssh
chmod 600 /home/spanishbot/.ssh/authorized_keys
```

Ограниченный sudoers — чтобы рутинные `restart`/`status` работали из-под
`spanishbot` без полного root (у `--disabled-password` пароля и sudo нет):
```bash
cat > /etc/sudoers.d/spanishbot-service <<'EOF'
spanishbot ALL=(root) NOPASSWD: /usr/bin/systemctl restart spanish-bot, /usr/bin/systemctl status spanish-bot
EOF
chmod 440 /etc/sudoers.d/spanishbot-service
visudo -c        # проверка синтаксиса sudoers

# чтение журнала сервиса без sudo — членством в группе (надёжнее wildcard в sudoers):
usermod -aG systemd-journal spanishbot   # вступит в силу при следующем входе spanishbot
```

## 4. Deploy key — доступ сервера к приватному репо (read-only)
```bash
su - spanishbot
ssh-keygen -t ed25519 -f ~/.ssh/spanish_bot_deploy -N ""
cat ~/.ssh/spanish_bot_deploy.pub        # скопировать вывод
```
На GitHub: репо → Settings → Deploy keys → Add deploy key → вставить ключ,
**не** включать «Allow write access». Затем на сервере направить git на этот ключ:
```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
    IdentityFile ~/.ssh/spanish_bot_deploy
    IdentitiesOnly yes
EOF
ssh -T git@github.com    # ожидается "Hi <USER>/spanish-bot! You've successfully authenticated"
```

## 5. Клонирование и окружение (под spanishbot)
```bash
cd ~
git clone git@github.com:<USER>/lang-trainer-bot.git
cd spanish-bot
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 6. Секреты и стартовая БД (с мака, в новом терминале)
Остановить локального бота, чтобы БД не писалась при копировании:
```bash
pkill -f "MacOS/Python bot.py"   # на маке
```
Скопировать `.env` и БД на сервер:
```bash
cd "<MAC_REPO>"
scp .env spanishbot@<DROPLET_IP>:/home/spanishbot/spanish-bot/.env
scp spanish_bot.db spanishbot@<DROPLET_IP>:/home/spanishbot/spanish-bot/spanish_bot.db
```
Файлы прилетели в домашнюю папку `spanishbot`, поэтому уже принадлежат ему —
`chown` не нужен (и под не-root упал бы). На сервере **под `spanishbot`** только
ужесточить права `.env` и прописать абсолютный путь к БД ровно одной строкой:
```bash
chmod 600 ~/spanish-bot/.env

# DB_PATH: заменить существующую строку или добавить — без дублей
# (.env.example уже содержит DB_PATH, локальный .env мог его принести)
cd ~/spanish-bot
if grep -q '^DB_PATH=' .env; then
    sed -i 's#^DB_PATH=.*#DB_PATH=/home/spanishbot/spanish-bot/spanish_bot.db#' .env
else
    echo "DB_PATH=/home/spanishbot/spanish-bot/spanish_bot.db" >> .env
fi
grep -c '^DB_PATH=' .env    # должно быть РОВНО 1
```
(Если после `scp` файлы вдруг оказались root-owned — выполнить под root
`chown spanishbot:spanishbot ~spanishbot/spanish-bot/.env ~spanishbot/spanish-bot/spanish_bot.db`.)

## 7. СВЕРКА МИГРАЦИИ (до запуска сервиса!)
```bash
# на сервере:
sqlite3 ~/spanish-bot/spanish_bot.db "SELECT COUNT(*) FROM cards;"
# на маке:
sqlite3 "<MAC_REPO>/spanish_bot.db" "SELECT COUNT(*) FROM cards;"
```
Числа ДОЛЖНЫ совпасть. Если на сервере 0 — БД не доехала/не читается; НЕ запускать
сервис (иначе init_db создаст пустую базу), разобраться с путём/правами и повторить.

## 8. systemd-сервис (на сервере, под root)
```bash
cp /home/spanishbot/spanish-bot/spanish-bot.service /etc/systemd/system/
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/spanish-bot.service   # синтаксис/пути
systemctl enable --now spanish-bot
systemctl status spanish-bot          # active (running)
journalctl -u spanish-bot -n 20       # "Start polling", без TelegramConflictError
```
⚠️ Локальный бот на маке с этого момента запускать НЕЛЬЗЯ (один поллер на токен).

## 9. Ночной бэкап (cron, под spanishbot)
```bash
crontab -e
# добавить строку (бэкап в 03:30 каждый день):
30 3 * * * /home/spanishbot/spanish-bot/scripts/backup-db.sh /home/spanishbot/spanish-bot/spanish_bot.db /home/spanishbot/backups >> /home/spanishbot/backup.log 2>&1
```
Проверить разово:
```bash
~/spanish-bot/scripts/backup-db.sh ~/spanish-bot/spanish_bot.db ~/backups
ls -1 ~/backups        # появился spanish_bot-<сегодня>.db
```

## 10. Проверка устойчивости
Ребут — под root (sudoers у `spanishbot` разрешает только restart/status сервиса,
не `reboot`):
```bash
ssh root@<DROPLET_IP> reboot
# через минуту:
ssh spanishbot@<DROPLET_IP> 'systemctl is-active spanish-bot'   # active
```
Живой тест в Telegram: `/start`, «Мой словарь» (старые слова на месте), добавить
слово, тренировка, аудио.

## Рутина обновлений
```bash
# на маке
git push
# на сервере
cd ~/spanish-bot && git pull
.venv/bin/pip install -r requirements.txt   # только если менялся requirements.txt
sudo systemctl restart spanish-bot
journalctl -u spanish-bot -n 20
```

⚠️ **Разовое — миграция колонок (2026-08):** перед `git pull`+restart сделать бэкап
(миграция колонок необратима в одну сторону):
```bash
~/spanish-bot/scripts/backup-db.sh ~/spanish-bot/spanish_bot.db ~/backups
```

Pre-flight (до рестарта) — RENAME COLUMN нужен sqlite ≥3.25:
```bash
~/spanish-bot/.venv/bin/python -c "import sqlite3; print(sqlite3.sqlite_version)"
```
Версия < 3.25 → НЕ рестартовать, разбираться с апгрейдом sqlite отдельно.

После рестарта — проверить, что миграция и старт прошли:
```bash
sqlite3 ~/spanish-bot/spanish_bot.db "PRAGMA table_info(cards);"
# должны быть: word / translation / example / example_translation
journalctl -u spanish-bot -n 20   # "Start polling"
```

Откат после миграции — это НЕ `git revert`: старый код на уже смигрированной БД
стартует чисто, но падает на любой операции с карточкой
(`no such column: spanish`). Правильный откат = restore бэкапа:
`systemctl stop spanish-bot` → скопировать бэкап поверх `spanish_bot.db` →
`git checkout` на старый коммит → `systemctl start spanish-bot`.

**Новый секрет в `.env` на сервере (не показывая значение в чате/логах):** Victoria кладёт
строку в локальный `.env` (он git-ignored), дальше перенос одной командой с мака —
```bash
{ printf '\n'; grep '^GEMINI_API_KEY=' .env; } | ssh spanishbot@<DROPLET_IP> 'cat >> ~/spanish-bot/.env'
```
и проверка без раскрытия: `ssh … 'grep -c "^GEMINI_API_KEY=" ~/spanish-bot/.env'` (ожидается `1`).
Так делали при миграции на Gemini 2026-07-30.

## Второй бот на том же сервере (английский, 2026-08)

Тот же код, другой язык: отдельный клон, `.env` с `BOT_LANG=en`, свой юнит.
Секреты: новый токен от BotFather + ключ Gemini из ОТДЕЛЬНОГО Google-проекта
(лимиты бесплатного тира — по проекту; мамин бот и английский не делятся).

```bash
# на сервере (под spanishbot)
cd ~ && git clone git@github.com:<USER>/lang-trainer-bot.git english-bot
cd english-bot
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt

# .env: секреты нового бота (TELEGRAM_TOKEN от BotFather, GEMINI_API_KEY из
# ОТДЕЛЬНОГО Google-проекта) Victoria кладёт в локальный .env.english (git-ignored,
# НЕ в общий .env — иначе python-dotenv last-wins и это перезапишет токен мамы).
```

С мака — перенос секретов одной командой, без значения в чате/логах:
```bash
{ printf '\n'; grep '^TELEGRAM_TOKEN=' .env.english; grep '^GEMINI_API_KEY=' .env.english; } | ssh spanishbot@<DROPLET_IP> 'cat >> ~/english-bot/.env'
```

На сервере (под spanishbot) — дописать не-секретные строки + ALLOWED_USER_IDS —
это пользователи АНГЛИЙСКОГО бота (Victoria и друзья), НЕ мамин id:
```bash
printf 'BOT_LANG=en\nDB_PATH=/home/spanishbot/english-bot/english_bot.db\nALLOWED_USER_IDS=<id Victoria и друзей>\n' >> ~/english-bot/.env
grep -c '^BOT_LANG=' ~/english-bot/.env; grep -c '^DB_PATH=' ~/english-bot/.env   # оба должны быть 1
chmod 600 ~/english-bot/.env

# юнит (под root)
cp /home/spanishbot/english-bot/english-bot.service /etc/systemd/system/
systemd-analyze verify /etc/systemd/system/english-bot.service
systemctl daemon-reload && systemctl enable --now english-bot
journalctl -u english-bot -n 20     # Start polling, без TelegramConflictError

# sudoers (под root): расширить /etc/sudoers.d/spanishbot-service
#   ... /usr/bin/systemctl restart spanish-bot, /usr/bin/systemctl status spanish-bot,
#   /usr/bin/systemctl restart english-bot, /usr/bin/systemctl status english-bot
# и проверить: visudo -c

# бэкап (под spanishbot): вторая строка crontab
35 3 * * * /home/spanishbot/english-bot/scripts/backup-db.sh /home/spanishbot/english-bot/english_bot.db /home/spanishbot/backups >> /home/spanishbot/backup.log 2>&1
```

Мамин бот при этом обновляется обычной «Рутиной обновлений» — там же (выше)
разовые pre-flight/бэкап/откат для миграции колонок (2026-08).

### Ежедневная практика (en-бот)

Фича реализована на ветке `daily-practice` (2026-10-03); выкатка — отдельным решением
Victoria, после слияния ветки. Включается **только в `.env` английского юнита**
(`BOT_LANG=en`); мамин юнит не трогать — без `DAILY_AT` цикл не стартует.

```bash
# на сервере (под spanishbot), после обычной «Рутины обновлений» для english-bot
printf 'DAILY_AT=09:30\nDAILY_TZ=Europe/Madrid\n' >> ~/english-bot/.env
grep -c '^DAILY_AT=' ~/english-bot/.env; grep -c '^DAILY_TZ=' ~/english-bot/.env   # оба должны быть 1
sudo systemctl restart english-bot
journalctl -u english-bot -n 30     # Start polling; при DAILY_AT вне 05:00–13:59 — строка WARNING
```

Ручная проверка (спека, «Тесты»): поставить `DAILY_AT` на 2 минуты вперёд и
рестартнуть → пришло задание; ответить своим предложением → разбор; в другой день
«не помню» → пример; `/next` → новое слово в рамках нормы или ответ про норму («Новое слово будет через N дней» / «Сохранённое сегодня придёт завтра» / «Новых слов нет»); переслать боту сообщение с
английским текстом → превью с контекстом → «Беру» → на следующее утро фраза
приходит как compose_hinted (подсказка + своё предложение). После проверки вернуть
`DAILY_AT=09:30`.

**Расписание повторов и новых слов (2026-10-08).** Выкат — обычная «Рутина обновлений» для
`english-bot`: `git pull` + `sudo systemctl restart english-bot`; `.env` не менять (`DAILY_TZ` уже
есть и теперь задаёт «сегодня» для всего бота). Миграции (`daily_tasks.is_new`, `daily_tasks.requested`)
и backfill `is_new` по `compose_hinted` выполняются автоматически на старте — вручную ничего. Мамин
юнит не трогать. Накопившиеся повторы разбираются не больше 5 в день **в темпе ответов**: утро даёт
первое задание, остальные приходят по одному после каждого ответа; что не влезло — следующим утром.
Новые слова — не больше двух в неделю, `/next` выдаёт только новое слово (повторы не смотрит, даже в день долга). Проверка после рестарта:
`journalctl -u english-bot -n 30` без traceback, затем `/next` → новое слово в рамках нормы или ответ про норму
(«Новое слово будет через N дней» / «Сохранённое сегодня придёт завтра» / «Новых слов нет»).

**Меню команд «/» вместо клавиатуры (2026-10-03).** После рестарта `english-bot` сам выставляет
список команд (`set_my_commands`: /next, /add, /vocab, /cards, /check, /listen). Клиент Telegram
может подтянуть его не сразу — перезапустить клиент или заново открыть чат. Старая клавиатура с
пятью кнопками исчезнет после `/start` (или конца тренировки). Мамин `spanish-bot` список команд
не трогает — у него всё по-прежнему.
