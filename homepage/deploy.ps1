[CmdletBinding()]
param(
    [string]$Destination = 'C:\MSUO\Homepage',
    [int]$Port = 8766
)

$ErrorActionPreference = 'Stop'
$taskName = 'MSUO-Homepage'
$python = (& py -3 -c "import sys; print(sys.executable)").Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $python)) {
    throw 'Python 3 is required on the Windows runner.'
}
$files = @('serve.py', 'index.html', 'methodology.html', 'figure-1.webp', 'figure-2.webp', 'figure-3.webp', 'favicon.png.b64')
foreach ($file in $files) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $file) -PathType Leaf)) {
        throw "Missing homepage source: $file"
    }
}

New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$server = Join-Path $Destination 'serve.py'
$action = New-ScheduledTaskAction -Execute $python -Argument ('"{0}" --port {1}' -f $server, $Port) -WorkingDirectory $Destination
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\LOCAL SERVICE' -LogonType ServiceAccount
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$backup = Join-Path $env:TEMP ('msuo-homepage-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $backup | Out-Null

try {
    foreach ($file in $files) {
        $deployed = Join-Path $Destination $file
        if (Test-Path -LiteralPath $deployed) {
            Copy-Item -LiteralPath $deployed -Destination $backup
        }
    }
    if ($existingTask -and $existingTask.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $taskName
    }
    try {
        foreach ($file in $files) {
            Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination $Destination -Force
        }
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
        Start-ScheduledTask -TaskName $taskName
        $expected = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'index.html') -Algorithm SHA256).Hash
        $methodologyExpected = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'methodology.html') -Algorithm SHA256).Hash
        $download = Join-Path $backup 'health.html'
        $methodologyDownload = Join-Path $backup 'methodology-health.html'
        $faviconDownload = Join-Path $backup 'favicon-health.png'
        $faviconExpected = [Convert]::FromBase64String((Get-Content -LiteralPath (Join-Path $PSScriptRoot 'favicon.png.b64') -Raw).Trim())
        $faviconExpectedFile = Join-Path $backup 'favicon-expected.png'
        [System.IO.File]::WriteAllBytes($faviconExpectedFile, $faviconExpected)
        $faviconExpectedHash = (Get-FileHash -LiteralPath $faviconExpectedFile -Algorithm SHA256).Hash
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            try {
                Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 3 -OutFile $download
                Invoke-WebRequest -Uri "http://127.0.0.1:$Port/methodology/" -UseBasicParsing -TimeoutSec 3 -OutFile $methodologyDownload
                Invoke-WebRequest -Uri "http://127.0.0.1:$Port/favicon.png" -UseBasicParsing -TimeoutSec 3 -OutFile $faviconDownload
                $homepageOK = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash -eq $expected
                $methodologyOK = (Get-FileHash -LiteralPath $methodologyDownload -Algorithm SHA256).Hash -eq $methodologyExpected
                $faviconOK = (Get-FileHash -LiteralPath $faviconDownload -Algorithm SHA256).Hash -eq $faviconExpectedHash
                if ($homepageOK -and $methodologyOK -and $faviconOK) {
                    Write-Host "Homepage verified at http://127.0.0.1:$Port/ (SHA256 $expected)"
                    return
                }
            } catch {
                if ($attempt -eq 19) { Write-Warning $_.Exception.Message }
            }
            Start-Sleep -Seconds 1
        }
        throw 'The homepage or methodology health check did not return the deployed HTML.'
    } catch {
        $deploymentError = $_
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        foreach ($file in $files) {
            $previous = Join-Path $backup $file
            if (Test-Path -LiteralPath $previous) {
                Copy-Item -LiteralPath $previous -Destination $Destination -Force
            } else {
                Remove-Item -LiteralPath (Join-Path $Destination $file) -ErrorAction SilentlyContinue
            }
        }
        if ($existingTask) {
            Register-ScheduledTask -TaskName $taskName -InputObject $existingTask -Force | Out-Null
            if ($existingTask.State -eq 'Running') {
                Start-ScheduledTask -TaskName $taskName
            }
        } else {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        }
        throw $deploymentError
    }
} finally {
    Remove-Item -LiteralPath $backup -Recurse -Force
}
