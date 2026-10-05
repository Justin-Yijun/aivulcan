# Register a Windows scheduled task: sync AI Daily Digest every day at 16:45.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1
# NOTE: keep this file ASCII-only.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Sync = Join-Path $Root "scripts\sync.ps1"
$TaskName = "AI-Daily-Digest-Sync"

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Sync`""

# GitHub Actions runs at 16:00 CST and may be delayed 5-30 min, so sync at 16:45.
$trigger = New-ScheduledTaskTrigger -Daily -At "16:45"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Pull the daily AI digest from GitHub and show a notification" `
    -Force | Out-Null

Write-Host "Registered task: $TaskName (daily 16:45)"
Write-Host "Run once now : Start-ScheduledTask -TaskName $TaskName"
Write-Host "Remove task  : Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
