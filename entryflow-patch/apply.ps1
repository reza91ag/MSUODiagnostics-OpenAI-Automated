$ErrorActionPreference = 'Stop'
$original = '35f6a7317aab9d74e3165955f9d92de4fb61b89e14bf116d5760582c79764d09'
$patched = '676d1081f563e8bc5c0b83c2e07e03a5d51cdce536cb53d0430c0a89af52bdd3'
function Hash($path) { (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() }
$work = Join-Path $env:TEMP ('entryflow-verify-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
function Check-Origin($hash) {
    $response = Join-Path $work 'origin.js'
    $uri = 'http://127.0.0.1:61310/main.dart.js?verify=' + [guid]::NewGuid().ToString('N')
    Invoke-WebRequest -Uri $uri -Headers @{Host='entryflow.msuo-runtime.io'; 'Cache-Control'='no-cache'} -UseBasicParsing -TimeoutSec 12 -OutFile $response
    if ((Hash $response) -ne $hash) { throw 'EntryFlow origin did not return the expected build.' }
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
    if ((Hash $file) -eq $patched) { Check-Origin $patched; return }
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
