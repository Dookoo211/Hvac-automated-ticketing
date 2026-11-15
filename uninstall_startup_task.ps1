param(
  [string]$TaskName = "HVACEmailAgent"
)

# Remove the startup task created by install_startup_task.ps1.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $existing) {
  Write-Host "Task not found: $TaskName"
  exit 0
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Startup task removed: $TaskName"
