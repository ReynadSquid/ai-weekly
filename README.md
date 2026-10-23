# AI Weekly — автоматический дайджест новостей ИИ для Telegram

Claude Code раз в неделю (и по желанию в середине недели) сам собирает новости ИИ, проверяет факты,
пишет выпуск на русском и публикует его в Telegram-канал. Человек правит только конфиги и читает отчёты.

```
init → collect (4 сборщика параллельно + RSS/API-скрипты) → merge/dedup → rank (curator)
     → fact-check (fact-checker) → gate (пороги) → write (editor) → QA → publish (скрипт) → finalize → report
```

| Где | Что |
|-----|-----|
| `.claude/commands/run-digest.md` | **Оркестратор** — слэш-команда `/run-digest`, главная сессия Claude Code |
| `.claude/agents/` | Субагенты: `collector-companies`, `collector-discovery`, `collector-research`, `collector-telegram`, `curator`, `fact-checker`, `editor`, `qa`, `publisher` |
| `scripts/` | Детерминированная часть на Python: `fetch_feeds.py`, `check_links.py`, `split_messages.py`, `tg_publish.py`, `pipeline.py` |
| `config/` | Всё, что меняется без правки кода |
| `state/` | `history.json` (новости за 8 недель), `published.json` (выпуски и message_id) |
| `runs/<дата>_<тип>/` | Промежуточные файлы шагов и `run.log` |
| `archive/` | Готовые выпуски в Markdown |

Оркестратор лежит в `commands/`, а не в `agents/`: субагенты Claude Code не умеют запускать других субагентов,
поэтому оркестратором может быть только главная сессия.

---

## 1. Что нужно один раз настроить

### 1.1 Telegram
1. В [@BotFather](https://t.me/BotFather) выполните `/newbot` и сохраните токен (`TELEGRAM_BOT_TOKEN`).
2. Создайте **закрытый тестовый канал** и **основной канал**. В каждом: «Администраторы» → добавить бота → право «Публикация сообщений».
3. Узнайте ID:
   - публичный канал можно указать как `@username`;
   - для закрытого канала перешлите любой его пост боту [@userinfobot](https://t.me/userinfobot) (или аналогичному) — ID вида `-100…`;
   - свой личный ID (для отчётов): напишите `/start` своему боту, затем откройте в браузере
     `https://api.telegram.org/bot<ТОКЕН>/getUpdates` — в ответе `"chat":{"id": …}`.
4. Проверка (локально, с заполненным `.env`): `python scripts/tg_publish.py --check`.

### 1.2 Секреты
| Переменная | Значение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен бота |
| `TELEGRAM_CHANNEL_ID` | основной канал |
| `TELEGRAM_TEST_CHAT_ID` | тестовый канал (для dry-run) |
| `TELEGRAM_ADMIN_CHAT_ID` | ваш личный ID — сюда приходят отчёты |

Локально — файл `.env` (скопируйте `.env.example`; `.env` в `.gitignore`). В облаке — переменные окружения
платформы. Токен маскируется во всех логах и отчётах (`***`), в репозиторий не пишется.

---

## 2. Запуск по расписанию

Код не зависит от платформы: выпуск = одна команда `/run-digest` (в облаке — промпт задачи, см. ниже).
Проверено по документации Claude Code (октябрь 2026): облачные задачи по расписанию доступны на Pro, Max, Team и Enterprise.

### Вариант A (основной): облачная задача Claude Code
ПК может быть выключен. Нужен репозиторий на GitHub.

1. **GitHub.** Создайте приватный репозиторий `ai-weekly` и залейте этот код (`git push`).
2. **Доступ.** На [claude.ai/code](https://claude.ai/code) подключите GitHub и дайте доступ к репозиторию.
3. **Облачное окружение.** В claude.ai/code → выбор окружения → «Добавить окружение» (или изменить своё):
   - **Network access:** `Full` (сборщики читают сотни разных сайтов; `Trusted` пускает только к пакетным реестрам,
     и `api.telegram.org`, `t.me` будут закрыты). Если хотите строже — `Custom` со списком доменов, но тогда часть
     источников будет недоступна.
   - **Environment variables** (формат `.env`): четыре переменные `TELEGRAM_*` из таблицы выше.
     Network secrets для Telegram не подходят: токен Telegram передаётся в адресе запроса, а не в заголовке.
   - **Setup script:**
     ```bash
     pip install pyyaml tzdata pytest
     ```
4. **Задача.** [claude.ai/code/routines](https://claude.ai/code/routines) → New task → Set up manually:
   - **Name:** `AI Weekly`
   - **Prompt:** текст из [docs/cloud-task-prompt.md](docs/cloud-task-prompt.md)
   - **Model:** Opus или Sonnet последней версии
   - **Trigger → Schedule → Weekly:** воскресенье **10:05** (по вашему времени; если время в браузере не
     красноярское, возьмите `cron_utc` из `python scripts/pipeline.py cron`). Плюс ещё два триггера-повтора:
     воскресенье 16:05 и 22:05 — они продолжат выпуск, если основной запуск сорвался (например, кончились лимиты),
     и ничего не сделают, если выпуск уже вышел.
   - **Advanced → Repository:** ваш `ai-weekly`; **Cloud environment:** созданное выше; **Connectors:** уберите все.
5. **Run now** — первый тестовый прогон (режим `dry-run` включён в `config/schedule.yaml` по умолчанию).

Время запуска «ровно в 10:00» может сдвигаться на несколько минут, поэтому в расписании стоит :05.

### Вариант B (резерв): GitHub Actions
`.github/workflows/digest.yml`. Добавьте секреты репозитория: `CLAUDE_CODE_OAUTH_TOKEN` (команда
`claude setup-token` на вашем ПК) и четыре `TELEGRAM_*`, раскомментируйте блок `schedule:`.
Ручной запуск: Actions → AI Weekly digest → Run workflow.

### Вариант C (резерв): Планировщик Windows
Нужны Python 3.12, Git, Claude Code CLI и `.env`. ПК должен быть включён в момент запуска.
```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\register_task.ps1
```

---

## 3. Ручной запуск и отладка

В Claude Code в папке репозитория:
```
/run-digest --dry-run --kind weekly     # выпуск в тестовый канал прямо сейчас
/run-digest --resume                    # продолжить прерванный выпуск этого слота
/run-digest --kind weekly --fresh --force   # собрать слот заново с нуля
```
Без `--kind` команда сама определяет слот по `schedule.yaml` и, если сейчас не время выпуска, отвечает `not_due`.

Полезные команды:
```bash
python -m pytest -q                                   # тесты скриптов
python scripts/pipeline.py status 2026-10-25_weekly   # какие шаги выполнены
python scripts/fetch_feeds.py --run 2026-10-25_weekly --group all
python scripts/check_links.py https://example.com/a
python scripts/split_messages.py --run 2026-10-25_weekly
python scripts/tg_publish.py --check
python scripts/pipeline.py cron                       # cron-строки из schedule.yaml
```
Лог запуска: `runs/<run_id>/run.log` — время шага, вход/выход, ошибки. Отчёт админу — `runs/<run_id>/report.txt`
(и в Telegram, если задан `TELEGRAM_ADMIN_CHAT_ID`).

---

## 4. Как менять без правки кода

| Хочу поменять | Файл |
|---|---|
| Список компаний и продуктов | `config/watchlist.yaml` — добавьте блок `- name: … products: […] section: A hashtag: "#…"` |
| RSS и API | `config/sources.yaml` |
| Telegram-каналы для поиска новостей | `config/tg_sources.yaml` |
| Формат выпуска (разделы A–G, эмодзи, стиль) | `config/format.md` — ключи разделов должны совпадать с `section` в watchlist |
| День и время | `config/schedule.yaml` → затем `python scripts/pipeline.py cron` и обновить триггер задачи |
| Включить средний выпуск | `config/schedule.yaml` → `issues.midweek.enabled: true` + триггер в среду 18:05 |
| Перейти на основной канал | `config/schedule.yaml` → `publish_mode: live` (сработает только после 2 успешных dry-run) |
| Пороги качества | `config/schedule.yaml` → `min_items`, `max_rejected_ratio` |

---

## 5. Как обеспечены требования ТЗ

| Требование | Реализация |
|---|---|
| Нет выдуманных фактов | Фактчекер — отдельный субагент, не видит черновик; редактор получает только `checked_claims` и не имеет доступа к интернету; QA сверяет каждый факт текста с `factcheck.json` |
| Ссылка на первоисточник | Telegram и агрегаторы только для обнаружения; `check_links.py` проверяет HTTP 200 и редирект на главную |
| Своими словами | Правило в промптах сборщиков и редактора, стиль-чек QA |
| Секреты | Только env/.env; маскировка в `common.mask`; запрет `env/printenv/cat .env` в `.claude/settings.json`; тесты на отсутствие токена в логах и состоянии |
| Нет дублей при повторном запуске | `state/published.json`: ключ `<run_id>:<mode>`, запись после каждого сообщения; повторный слот → `already_published` |
| ≤ 4096 символов, не рвать новость | `split_messages.py` режет по разделам, затем между новостями; длина по UTF-16 с запасом 4000 |
| 429 / 400 / 403 | `tg_publish.py`: ждёт `retry_after`; на 400/403 стоп и отчёт, отправленные части записаны |
| Источник недоступен | 3 попытки с паузой → пропуск, пометка в отчёте |
| --resume | Каждый шаг пишет файл в `runs/<id>/` и `progress.json`; `checkpoint` коммитит прогресс в git |
| Лимиты подписки | Слоты-повторы +6 ч и +12 ч продолжают выпуск, больше 2 повторов нет |
| Первые 2 запуска — dry-run | `publish_mode: dry-run` + блокировка live, пока нет 2 успешных dry-run |
| Окно сбора | От прошлой live-публикации, не больше 8 дней (`pipeline.py init`) |
