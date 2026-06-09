$pythonPath = 'C:\Users\1\AppData\Local\Python\pythoncore-3.14-64\python.exe'
$scriptPath = 'C:\Users\1\material-tracking\sync_db.py'

$action = New-ScheduledTaskAction -Execute $pythonPath -Argument $scriptPath
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName 'WlgzSyncDB' -Action $action -Trigger $trigger -Settings $settings -Description 'WLGZ DB Sync' -Force
Write-Host "OK - Task installed"
