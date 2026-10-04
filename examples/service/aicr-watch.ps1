# Windows: register a logon task that runs `aicr watch` hidden and restarts it if it exits.
# Run once in PowerShell 7: pwsh -File aicr-watch.ps1
$source = Join-Path $HOME 'ai-config'
$cmd = "while (`$true) { aicr watch --source '$source' --remote-every 20 --exec 'sh commit-and-push.sh'; Start-Sleep 10 }"
$action = New-ScheduledTaskAction -Execute 'pwsh.exe' -Argument "-NoProfile -WindowStyle Hidden -Command $cmd"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'aicr watch' -Action $action -Trigger $trigger -Settings $settings -Force
