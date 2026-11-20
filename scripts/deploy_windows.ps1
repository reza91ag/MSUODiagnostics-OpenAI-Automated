# Deploy only application files. Never overwrite production log configuration.
# Requires the MSUO-Observatory scheduled task, created once by setup_windows_task.ps1.
[CmdletBinding()]
param(
    [string]$Destination = 'C:\MSUO\Observatory',
    [string]$TaskName = 'MSUO-Observatory',
    [int]$Port = 8765
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$sourcePy = Join-Path $repo 'server.py'
$sourceWeb = Join-Path $repo 'web\index.html'
if (-not (Test-Path -LiteralPath $sourcePy) -or -not (Test-Path -LiteralPath $sourceWeb)) {
    throw 'Application source is missing. Add server.py and web/index.html to GitHub before deploying.'
}
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    throw "Scheduled task '$TaskName' is not installed. Run scripts/setup_windows_task.ps1 as Administrator on the server first."
}
New-Item -ItemType Directory -Force -Path $Destination | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Destination 'web') | Out-Null

# Back up the two application files; the live msuo_live_config.json is untouched.
$backup = Join-Path $env:TEMP ("msuo-observatory-backup-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path (Join-Path $backup 'web') | Out-Null
$destPy = Join-Path $Destination 'server.py'
$destWeb = Join-Path $Destination 'web\index.html'
if (Test-Path -LiteralPath $destPy) { Copy-Item -LiteralPath $destPy -Destination (Join-Path $backup 'server.py') }
if (Test-Path -LiteralPath $destWeb) { Copy-Item -LiteralPath $destWeb -Destination (Join-Path $backup 'web\index.html') }

function Wait-ForDashboard([int]$TimeoutSeconds) {
    $uri = "http://127.0.0.1:$Port/"
    for ($i = 0; $i -lt $TimeoutSeconds; $i++) {
        Start-Sleep -Seconds 1
        try {
            $result = Invoke-WebRequest -Uri $uri -UseBasicParsing -TimeoutSec 2
            if ($result.StatusCode -eq 200 -and $result.Content -match 'MSUO') {
                return $true
            }
        } catch { }
    }
    return $false
}

try {
    if ((Get-ScheduledTask -TaskName $TaskName).State -eq 'Running') {
        Stop-ScheduledTask -TaskName $TaskName
        Start-Sleep -Seconds 2
    }
    Copy-Item -LiteralPath $sourcePy -Destination $destPy -Force
    Copy-Item -LiteralPath $sourceWeb -Destination $destWeb -Force
    Start-ScheduledTask -TaskName $TaskName
    if (-not (Wait-ForDashboard -TimeoutSeconds 20)) {
        throw "Dashboard did not become healthy on 127.0.0.1:$Port"
    }
    Write-Host "Deployment successful: http://127.0.0.1:$Port"
} catch {
    Write-Warning "Deployment failed: $($_.Exception.Message). Attempting rollback."
    try {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        $oldPy = Join-Path $backup 'server.py'
        $oldWeb = Join-Path $backup 'web\index.html'
        if (Test-Path $oldPy) { Copy-Item $oldPy $destPy -Force }
        if (Test-Path $oldWeb) { Copy-Item $oldWeb $destWeb -Force }
        if ((Test-Path $oldPy) -and (Test-Path $oldWeb)) {
            Start-ScheduledTask -TaskName $TaskName
        }
    } catch {
        Write-Warning "Rollback also failed: $($_.Exception.Message)"
    }
    throw
} finally {
    Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
}
