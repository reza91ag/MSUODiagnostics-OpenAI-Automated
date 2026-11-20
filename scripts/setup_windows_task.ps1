# Run ONCE from an elevated Windows PowerShell on the IONOS server.
# The scheduled task starts the dashboard at boot without opening a browser.
[CmdletBinding()]
param(
    [string]$Destination = 'C:\MSUO\Observatory',
    [string]$TaskName = 'MSUO-Observatory',
    [string]$PythonExe = ''
)
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run Windows PowerShell as Administrator.'
}
if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $PythonExe = (& py -3 -c "import sys; print(sys.executable)").Trim()
}
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python executable not found: $PythonExe. Install Python 3.10+ for all users or pass -PythonExe."
}
New-Item -ItemType Directory -Force -Path $Destination | Out-Null
$script = Join-Path $Destination 'server.py'
$action = New-ScheduledTaskAction -Execute $PythonExe -Argument ('"{0}" --no-browser' -f $script) -WorkingDirectory $Destination
$trigger = New-ScheduledTaskTrigger -AtStartup
$taskPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $taskPrincipal -Settings $settings -Force | Out-Null
Write-Host "Installed startup task '$TaskName' pointing to $script"
Write-Host 'The task will start successfully after the first GitHub deployment.'
Write-Host 'The GitHub Actions runner service account must be allowed to stop/start this task and write to the destination.'
