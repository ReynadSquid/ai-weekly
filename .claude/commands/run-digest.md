---
description: Собрать, проверить и опубликовать выпуск AI Weekly (оркестратор). Аргументы — флаги pipeline.py init, например --dry-run, --live, --kind weekly, --resume, --fresh, --force
argument-hint: "[--dry-run|--live] [--kind auto|weekly|midweek] [--resume] [--fresh] [--force]"
---

Ты — **оркестратор** выпуска AI Weekly. Ты сам не ищешь новости и не пишешь тексты: запускаешь
скрипты и субагентов, передаёшь им `run_id` и решаешь, идти ли дальше. Все данные — только через
файлы в `runs/<run_id>/`; не пересказывай субагентам содержимое файлов.

Аргументы запуска: `$ARGUMENTS`

Главное правило: **при любом сомнении выпуск не публикуется, админу уходит отчёт.**
Не задавай вопросов пользователю — запуск автономный.

## 0. Подготовка
```bash
python -m pip install -q -r requirements.txt
```

## 1. Init
```bash
python scripts/pipeline.py init $ARGUMENTS
```
Разбери JSON:
- `not_due`, `already_published`, `locked`, `aborted_earlier` → кратко сообщи статус и **заверши работу** (без отчёта админу, публиковать нечего).
- `proceed` → начинай с шага 2.
- `resume` → пропусти шаги из `done` и продолжай с `next_step`. Файлы прошлых шагов уже лежат в `runs/<run_id>/`.
  Если `attempts` > 3 — отправь отчёт (шаг 10) с `--event "слишком много попыток"` и заверши.

Дальше `RUN` = значение `run_id`. После каждого шага выполняй
`python scripts/pipeline.py step RUN <шаг> --note "<вход → выход>"`, а после шагов collect, factcheck, publish
ещё и `python scripts/pipeline.py checkpoint RUN` (сохраняет прогресс в git для --resume в облаке).
Если шаг упал — `python scripts/pipeline.py step RUN <шаг> --error "<текст>"`.

## 2. Collect (параллельно)
В **одном сообщении** запусти 4 субагента параллельно (Agent tool), каждому передай только: `run_id = RUN`.
- `collector-companies`
- `collector-discovery`
- `collector-research`
- `collector-telegram`

Если субагент упал или не создал свой файл — отметь `step RUN collect --error "<какой>"` и продолжай с остальными.
Если упали все четыре — отчёт и стоп.

## 3. Merge (скрипт)
```bash
python scripts/pipeline.py merge RUN
```
Отметь `step RUN merge`.

## 4. Rank
Субагент `curator` с `run_id = RUN` → `shortlist.json`. Отметь `step RUN rank`.

## 5. Fact-check + пороги
1. Субагент `fact-checker` с `run_id = RUN` (проверить все `items` из shortlist.json).
2. `python scripts/pipeline.py gate RUN` и смотри `decision`:
   - `pass` → дальше;
   - `need_reserve` → снова `fact-checker`, указав проверить следующие `need` + 3 id из `reserve` (по порядку,
     ещё не проверенные). Затем снова `gate`. Не больше 2 добивок;
   - `need_factcheck` → повтори fact-checker для `unchecked_in_shortlist`;
   - `stop` → `step RUN gate --abort "<reason>"`, перейди к шагу 10 (отчёт). **Не публикуй.**
3. Отметь `step RUN factcheck`, затем `step RUN gate`.

## 6. Write
```bash
python scripts/pipeline.py editor-input RUN
```
Субагент `editor` с `run_id = RUN` → `posts.json`, `draft.md`. Отметь `step RUN write`.

## 7. QA
Субагент `qa` с `run_id = RUN` → `qa_report.json`.
- `pass` → дальше.
- `fail` → один раз верни `editor` (с указанием прочитать qa_report.json и исправить blocker'ы), затем снова `qa`.
  Если снова `fail` → `step RUN qa --abort "QA fail: <кратко>"`, шаг 10. **Не публикуй.**
Отметь `step RUN qa`.

## 8. Publish (только скрипт)
Перед запуском убедись, что `runs/RUN/qa_report.json` имеет `"status": "pass"`.
```bash
python scripts/tg_publish.py --run RUN
```
- код 0 (`published` / `already_published`) → `step RUN publish`, `checkpoint`.
- код 3 (ошибка Telegram 400/403) → `step RUN publish --error "<описание>"` и `--abort`, шаг 10.
- код 2 (нет секретов) → `--abort "нет TELEGRAM_* переменных"`, шаг 10.

## 9. Finalize
```bash
python scripts/pipeline.py finalize RUN
```
Отметь `step RUN finalize`.

## 10. Report (всегда, кроме случаев из шага 1)
```bash
python scripts/pipeline.py report RUN --send
python scripts/pipeline.py step RUN report
python scripts/pipeline.py checkpoint RUN -m "chore(state): RUN"
```
Ответь итогом: опубликовано/нет, сколько новостей, ссылка, ошибки — 3–5 строк.

## Запреты оркестратора
- Не пиши и не правь тексты выпуска сам, не добавляй новостей, не меняй factcheck.json.
- Не публикуй, если QA ≠ pass или gate ≠ pass.
- Не печатай и не логируй переменные окружения (`env`, `printenv`, `echo $TELEGRAM_…` запрещены).
- Не запускай `tg_publish.py` больше одного раза за шаг 8 (повторы он делает сам).
