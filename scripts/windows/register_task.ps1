# Вариант C: регистрирует задачи Планировщика Windows по config/schedule.yaml.
# Время ПК должно быть в часовом поясе из schedule.yaml (или поправьте вручную).
# Запуск: powershell -ExecutionPolicy Bypass -File scripts\windows\register_task.ps1
$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$runner = Join-Path $PSScriptRoot "run_digest.ps1"
$rows = python (Join-Path $repo "scripts\pipeline.py") cron | ConvertFrom-Json
$days = @("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")

foreach ($r in $rows | Where-Object { $_.enabled }) {
    $parts = $r.cron_local.Split(" ")
    $at = "{0:D2}:{1:D2}" -f [int]$parts[1], [int]$parts[0]
    $day = $days[[int]$parts[4]]
    $name = "AIWeekly-$($r.kind)-$($at.Replace(':', ''))"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $day -At $at
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3)
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Host "Зарегистрирована задача $($name): $day $at ($($r.purpose))"
}
