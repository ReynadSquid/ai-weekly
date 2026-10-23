# Промпт облачной задачи AI Weekly

Скопируйте текст ниже (между линиями) в поле **Prompt** задачи на claude.ai/code/routines.

---

Ты запускаешься автоматически по расписанию в репозитории ai-weekly. Вопросов не задавай — рядом никого нет.

1. Прочитай CLAUDE.md.
2. Прочитай .claude/commands/run-digest.md и выполни его инструкции как оркестратор.
   Аргументы запуска пустые (слот и режим определяются по config/schedule.yaml).
3. Состояние сохраняет `python scripts/pipeline.py checkpoint`: он коммитит state/, archive/ и runs/
   и пушит **прямо в ветку main**. Не создавай веток claude/*, не открывай pull request и не меняй
   код и конфиги.
4. Если init вернул not_due, already_published, locked или aborted_earlier, коротко напиши статус и заверши работу.

---

Для ручного тестового прогона через **Run now** можно дописать в конец промпта:
`Аргументы запуска: --dry-run --kind weekly`.
