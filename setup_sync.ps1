$action = New-ScheduledTaskAction -Execute 'C:\Users\1\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe' -Argument 'D:\板卡物料追溯系统\sync_db.py'
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -WakeToRun -Hidden
Register-ScheduledTask -TaskName 'WlgzSyncDB' -Action $action -Trigger $trigger -Settings $settings -Description 'WLGZ DB Sync' -Force
Write-Host "OK - Hidden mode"
