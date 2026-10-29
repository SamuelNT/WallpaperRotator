<#
.SYNOPSIS
    Removes the WallpaperRotator scheduled task.

.DESCRIPTION
    Stops the task if it is running and unregisters it. State and log files in
    %LOCALAPPDATA%\WallpaperRotator are kept by default, because the queue is
    what lets a reinstall continue where it left off; add -RemoveData to wipe
    them as well.

.EXAMPLE
    .\uninstall-task.ps1
    .\uninstall-task.ps1 -RemoveData
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'WallpaperRotator',
    [switch]$RemoveData
)

$ErrorActionPreference = 'Stop'

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "Task '$TaskName' is not registered." -ForegroundColor Yellow
} else {
    if ($existing.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $TaskName
        Write-Host "Stopped running instance."
    }
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Unregistered '$TaskName'." -ForegroundColor Green
    } catch {
        Write-Host "Unregister-ScheduledTask failed ($($_.Exception.Message)); trying schtasks.exe..." -ForegroundColor Yellow
        $output = & schtasks.exe /delete /tn $TaskName /f 2>&1
        if ($LASTEXITCODE -ne 0) { throw "schtasks.exe /delete failed: $output" }
        Write-Host "Unregistered '$TaskName' via schtasks.exe." -ForegroundColor Green
    }
}

$dataDir = Join-Path $env:LOCALAPPDATA 'WallpaperRotator'
if ($RemoveData) {
    if (Test-Path -LiteralPath $dataDir) {
        Remove-Item -LiteralPath $dataDir -Recurse -Force
        Write-Host "Removed data folder: $dataDir"
    } else {
        Write-Host "No data folder at $dataDir"
    }
} else {
    Write-Host "Kept state/log in: $dataDir  (use -RemoveData to delete)"
}
