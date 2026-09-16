# Registers or removes the Task Scheduler entry that starts the backend when the installing user logs on. Called by the installer (-Exe) and the uninstaller (-Remove). Needs no elevation because the trigger is for this user only.
param([string]$Exe, [switch]$Remove)
$ErrorActionPreference = "Stop"
$name = "LiveCaptionTranslate"
if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    exit 0
}
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ($env:USERDOMAIN + "\" + $env:USERNAME)
$action = New-ScheduledTaskAction -Execute $Exe -WorkingDirectory (Split-Path $Exe -Parent)
# ExecutionTimeLimit zero means no limit; the default of three days would kill a background process
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Register-ScheduledTask -TaskName $name -Trigger $trigger -Action $action -Settings $settings -Description "Starts the Live Caption Translate backend at logon" -Force | Out-Null
