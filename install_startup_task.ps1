param(
  [string]$TaskName = "HVACEmailAgent"
)

# Install a Windows Task Scheduler entry that starts the agent on user logon.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcherPath = Join-Path $scriptDir "start_agent.bat"

if (-not (Test-Path $launcherPath)) {
  throw "Launcher not found: $launcherPath"
}

$userId = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$launcherPath`"" -WorkingDirectory $scriptDir
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType InteractiveToken -RunLevel Limited
# Ignore overlapping launches to prevent duplicate responders.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings

Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null

Write-Host "Startup task installed: $TaskName"
Write-Host "It will launch on user logon and run start_agent.bat from:"
Write-Host "  $scriptDir"
