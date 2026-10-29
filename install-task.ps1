<#
.SYNOPSIS
    Registers the WallpaperRotator scheduled task.

.DESCRIPTION
    Creates a hidden scheduled task that runs pythonw.exe "<script>" --next at
    logon and then every ROTATION_INTERVAL_MINUTES (read from rotator.py).

    Registers via Register-ScheduledTask and falls back to schtasks.exe when the
    CIM provider denies access. See README.md for the full task configuration
    and the reasoning behind it.

.EXAMPLE
    .\install-task.ps1
    .\install-task.ps1 -RunNow
    .\install-task.ps1 -IntervalMinutes 30 -Force
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'WallpaperRotator',
    [string]$PythonwPath,
    [int]$IntervalMinutes = 0,
    [switch]$RunNow,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$ScriptPath = Join-Path $PSScriptRoot 'rotator.py'
if (-not (Test-Path -LiteralPath $ScriptPath)) {
    throw "rotator.py not found next to this installer: $ScriptPath"
}
$ScriptPath = (Resolve-Path -LiteralPath $ScriptPath).Path

function Resolve-Pythonw {
    param([string]$Explicit)

    $candidates = New-Object System.Collections.Generic.List[string]
    if ($Explicit) { $candidates.Add($Explicit) }

    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $candidates.Add($cmd.Source) }

    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $exe = & $py.Source -3 -c "import sys; print(sys.executable)" 2>$null
            if ($exe) { $candidates.Add(($exe.Trim() -replace 'python\.exe$', 'pythonw.exe')) }
        } catch { }
    }

    foreach ($root in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles", "${env:ProgramFiles(x86)}")) {
        if (-not $root -or -not (Test-Path -LiteralPath $root)) { continue }
        Get-ChildItem -Path $root -Filter pythonw.exe -Recurse -Depth 2 -ErrorAction SilentlyContinue |
            ForEach-Object { $candidates.Add($_.FullName) }
    }

    foreach ($candidate in $candidates) {
        if (-not $candidate) { continue }
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        # Skip the 0-byte Microsoft Store alias stub.
        if ((Get-Item -LiteralPath $candidate).Length -lt 10000) { continue }
        return (Resolve-Path -LiteralPath $candidate).Path
    }
    return $null
}

$Pythonw = Resolve-Pythonw -Explicit $PythonwPath
if (-not $Pythonw) {
    throw @"
pythonw.exe was not found.

Python does not appear to be installed on this machine (the python.exe in
%LOCALAPPDATA%\Microsoft\WindowsApps is a 0-byte Microsoft Store alias stub).

Install it first, then re-run this script:
    winget install -e --id Python.Python.3.13

Or pass the path explicitly:
    .\install-task.ps1 -PythonwPath 'C:\path\to\pythonw.exe'
"@
}

$ScriptText = Get-Content -LiteralPath $ScriptPath -Raw
$match = [regex]::Match($ScriptText, '(?m)^\s*ROTATION_INTERVAL_MINUTES\s*=\s*(\d+)')
if ($match.Success) { $ConfiguredInterval = [int]$match.Groups[1].Value } else { $ConfiguredInterval = 60 }

if ($IntervalMinutes -le 0) { $IntervalMinutes = $ConfiguredInterval }
if ($IntervalMinutes -lt 1) { throw "IntervalMinutes must be >= 1." }

# ISO-8601 duration for the repetition interval.
$IntervalIso = if ($IntervalMinutes % 60 -eq 0) { "PT$([int]($IntervalMinutes / 60))H" } else { "PT$IntervalMinutes" + "M" }

Write-Host "Script           : $ScriptPath"
Write-Host "pythonw.exe      : $Pythonw"
Write-Host "Interval         : $IntervalMinutes min ($IntervalIso)"

$user = "$env:USERDOMAIN\$env:USERNAME"
$workDir = Split-Path -Parent $ScriptPath
$arguments = '"{0}" --next' -f $ScriptPath

# Repetition is anchored daily; the interval repeats indefinitely from there.
$anchor = (Get-Date -Hour 0 -Minute 5 -Second 0).ToString("yyyy-MM-ddTHH:mm:ss")

$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Rotates the desktop wallpaper through C:\Wallpapers, one image per run, same image on all monitors.</Description>
    <URI>\$TaskName</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$user</UserId>
    </LogonTrigger>
    <CalendarTrigger>
      <StartBoundary>$anchor</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>$IntervalIso</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$user</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$Pythonw</Command>
      <Arguments>$arguments</Arguments>
      <WorkingDirectory>$workDir</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

# Overwrite only when asked, so a typo cannot silently replace a good task.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and -not $Force) {
    throw "Task '$TaskName' already exists. Re-run with -Force to replace it."
}

$registeredVia = $null
try {
    # -Xml wants a bare document; drop the encoding declaration for this path.
    $bareXml = $taskXml -replace '^\s*<\?xml[^>]*\?>\s*', ''
    Register-ScheduledTask -TaskName $TaskName -Xml $bareXml -Force | Out-Null
    $registeredVia = 'Register-ScheduledTask'
} catch {
    $reason = $_.Exception.Message
    Write-Host "Register-ScheduledTask failed ($reason); falling back to schtasks.exe..." -ForegroundColor Yellow

    $tempXml = Join-Path $env:TEMP ("WallpaperRotator_{0}.xml" -f [guid]::NewGuid())
    try {
        # schtasks requires the UTF-16 declaration that matches the file format.
        [IO.File]::WriteAllText($tempXml, $taskXml, [Text.Encoding]::Unicode)
        $output = & schtasks.exe /create /tn $TaskName /xml $tempXml /f 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "schtasks.exe /create failed (exit $LASTEXITCODE): $output"
        }
        $registeredVia = 'schtasks.exe'
    } finally {
        Remove-Item -LiteralPath $tempXml -ErrorAction SilentlyContinue
    }
}

$task = Get-ScheduledTask -TaskName $TaskName
Write-Host ""
Write-Host "Registered '$TaskName' via $registeredVia." -ForegroundColor Green
Write-Host ("  State             : {0}" -f $task.State)
Write-Host ("  Hidden            : {0}" -f $task.Settings.Hidden)
Write-Host ("  MultipleInstances : {0}" -f $task.Settings.MultipleInstances)
Write-Host ("  ExecutionTimeLimit: {0}" -f $task.Settings.ExecutionTimeLimit)

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started the task once." -ForegroundColor Green
}

Write-Host ""
Write-Host "Log: $env:LOCALAPPDATA\WallpaperRotator\rotator.log"
