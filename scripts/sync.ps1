# AI Daily Digest - local sync
# Pulls the latest digest from GitHub and shows a Windows notification.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\sync.ps1
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads BOM-less
#       scripts as ANSI, which would corrupt any non-ASCII literal.

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "[sync] root: $Root"

# 1) pull latest digest
Write-Host "[sync] git pull ..."
$pull = git pull --rebase --autostash 2>&1
Write-Host $pull
if ($LASTEXITCODE -ne 0) {
    Write-Host "[sync] git pull failed (network?). GitHub is usually reachable directly."
}

# 2) find newest digest
$latest = Get-ChildItem -Path (Join-Path $Root "digest") -Filter "*.md" -ErrorAction SilentlyContinue |
          Sort-Object Name -Descending | Select-Object -First 1

if (-not $latest) {
    Write-Host "[sync] no digest file found."
    exit 1
}

# 3) keep a stable "latest.md" pointer for quick opening
Copy-Item -LiteralPath $latest.FullName -Destination (Join-Path $Root "latest.md") -Force
Write-Host "[sync] latest digest: $($latest.Name)"

# 4) build the notification text
$lines = Get-Content -LiteralPath $latest.FullName -Encoding UTF8
$head = ($lines | Where-Object { $_ -match "^> .*(CST)" } | Select-Object -First 1)
$picks = ($lines | Where-Object { $_ -match "^### \d+\." } | Measure-Object).Count
$msg = "$($latest.BaseName) updated, $picks picks.`n$head"

# 5) toast notification (best effort, never blocks the main flow)
$ok = $false
try {
    Import-Module BurntToast -ErrorAction Stop
    New-BurntToastNotification -Text "AI Daily Digest" -Body $msg
    $ok = $true
} catch { }

if (-not $ok) {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $ni = New-Object System.Windows.Forms.NotifyIcon
        $ni.Icon = [System.Drawing.SystemIcons]::Information
        $ni.Visible = $true
        $ni.ShowBalloonTip(10000, "AI Daily Digest", $msg, [System.Windows.Forms.ToolTipIcon]::Info)
        Start-Sleep -Seconds 6
        $ni.Dispose()
        $ok = $true
    } catch { }
}

if (-not $ok) {
    Write-Host "[sync] $msg"
}

# 6) uncomment to open automatically
# Start-Process $latest.FullName
