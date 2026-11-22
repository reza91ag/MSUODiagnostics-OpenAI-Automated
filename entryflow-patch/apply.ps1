$ErrorActionPreference = 'Stop'
$original = '3fd2f1dfab0f7b75204bebdb4273d77b9f831d970424f35cc183177d87388470'
$patched = 'e737baac33ae8b065e7c9f76a088a197150c33f6c60a44a589bf419217ade2af'
function Hash($path) { (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() }
$work = Join-Path $env:TEMP ('entryflow-verify-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
function Check-Origin($hash) {
    $response = Join-Path $work 'origin.js'
    $uri = 'http://127.0.0.1:61310/main.dart.js?verify=' + [guid]::NewGuid().ToString('N')
    Invoke-WebRequest -Uri $uri -Headers @{Host='entryflow.msuo-runtime.io'; 'Cache-Control'='no-cache'} -UseBasicParsing -TimeoutSec 12 -OutFile $response
    if ((Hash $response) -ne $hash) { throw 'EntryFlow origin did not return the expected build.' }
}
function Update-Entrypoint {
    $index = Join-Path $root 'index.html'
    $from = '"mainJsPath":"main.dart.js?entryflow=3fd2f1df"'
    $to = '"mainJsPath":"main.dart.js?entryflow=e737baac"'
    $html = [IO.File]::ReadAllText($index)
    if ($html.Contains($to)) { return }
    if ([regex]::Matches($html,[regex]::Escape($from)).Count -ne 1) { throw 'Unexpected Flutter boot configuration.' }
    $parent = Split-Path -Parent $root
    $stagedHtml = Join-Path $parent ('.entryflow-' + [guid]::NewGuid().ToString('N') + '.tmp')
    $backupHtml = $stagedHtml + '.original-index.html'
    try {
        [IO.File]::WriteAllText($stagedHtml,$html.Replace($from,$to))
        [IO.File]::Replace($stagedHtml,$index,$backupHtml)
        try {
            $served = Invoke-WebRequest -Uri ('http://127.0.0.1:61310/?verify=' + [guid]::NewGuid().ToString('N')) -Headers @{Host='entryflow.msuo-runtime.io'} -UseBasicParsing -TimeoutSec 12
            if (!$served.Content.Contains($to)) { throw 'Flutter boot configuration did not reach the origin.' }
            Check-Origin $patched
        } catch {
            [IO.File]::Replace($backupHtml,$index,$stagedHtml)
            throw
        }
    } finally {
        Remove-Item -LiteralPath $stagedHtml -ErrorAction SilentlyContinue
    }
    Write-Host 'Versioned Flutter boot entry verified.'
}
try {
    $dirs = @($env:ProgramFiles, $env:ProgramW6432, [Environment]::GetEnvironmentVariable('ProgramFiles(x86)')) | Where-Object { $_ } | Sort-Object -Unique
    $launchers = @($dirs | ForEach-Object { Join-Path (Join-Path $_ 'EntryFlow') 'start.bat' } | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    if (!$launchers.Count) { throw 'Could not find Program Files\\EntryFlow\\start.bat in either Program Files folder.' }
    $candidates = @(
        foreach ($launcher in $launchers) {
            $install = Split-Path -Parent $launcher
            foreach ($candidate in Get-ChildItem -LiteralPath $install -Filter 'main.dart.js' -File -Recurse -ErrorAction Stop) {
                if ($candidate.DirectoryName -match '(?i)backup|\\.dart_tool|node_modules') { continue }
                if (!(Test-Path -LiteralPath (Join-Path $candidate.DirectoryName 'index.html') -PathType Leaf)) { continue }
                if ($candidate.Length -gt 2000000 -and (Hash $candidate.FullName) -in @($original,$patched)) { $candidate.FullName }
            }
        }
    ) | Sort-Object -Unique
    $candidates = @($candidates | Where-Object { $_ })
    if ($candidates.Count -ne 1) { throw "Found $($candidates.Count) matching frontend bundles beside the EntryFlow launcher; expected one." }
    $file = $candidates[0]
    $root = Split-Path -Parent $file
    if ((Hash $file) -eq $patched) { Check-Origin $patched; Update-Entrypoint; return }
    if ((Hash $file) -ne $original) { throw 'The on-disk build differs from the original used to prepare this patch.' }
    Check-Origin $original
    Expand-Archive -LiteralPath (Join-Path $PSScriptRoot 'patch.zip') -DestinationPath $work
    $bundle = Join-Path $work 'main.dart.js'
    if ((Hash $bundle) -ne $patched) { throw 'Patch checksum mismatch.' }

    $backup = (Join-Path (Split-Path -Parent $root) 'EntryFlow-Frontend-backup-') + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8)
    Copy-Item -LiteralPath $root -Destination $backup -Recurse
    $saved = Join-Path $backup 'main.dart.js'
    if ((Hash $saved) -ne $original) { throw 'Backup checksum mismatch.' }
    Write-Host "Original frontend backed up to $backup"

    $staged = Join-Path $backup 'staged-bundle.tmp'
    try {
        Copy-Item -LiteralPath $bundle -Destination $staged
        [IO.File]::Replace($staged,$file,(Join-Path $backup 'replaced-original.js'))
        try {
            Check-Origin $patched
            Update-Entrypoint
        } catch {
            Copy-Item -LiteralPath $saved -Destination $staged -Force
            [IO.File]::Replace($staged,$file,(Join-Path $backup 'failed-patch.js'))
            Check-Origin $original
            throw 'Patch verification failed and the original bundle was restored.'
        }
    } finally {
        Remove-Item -LiteralPath $staged -ErrorAction SilentlyContinue
    }
    Write-Host 'Patched EntryFlow origin verified.'
} finally {
    Remove-Item -LiteralPath $work -Recurse -Force
}
