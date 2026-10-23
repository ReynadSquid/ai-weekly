# Вариант C: запуск выпуска на ПК пользователя (Windows 10).
# Требует: Python 3.12, Git, Claude Code CLI (`claude` в PATH, вход через подписку), файл .env в корне репозитория.
# Пример: powershell -ExecutionPolicy Bypass -File scripts\windows\run_digest.ps1 -DigestArgs "--dry-run --kind weekly"
param([string]$DigestArgs = "")

$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $repo
$env:PYTHONIOENCODING = "utf-8"

git pull --rebase --quiet
python -m pip install -q -r requirements.txt
$log = Join-Path $repo "runs\scheduler.log"
"=== $(Get-Date -Format s) /run-digest $DigestArgs" | Out-File -Append -Encoding utf8 $log
claude -p "/run-digest $DigestArgs" --permission-mode acceptEdits 2>&1 | Out-File -Append -Encoding utf8 $log
