$ErrorActionPreference = 'Stop'
$root = 'C:\EntryFlow\Frontend'
$file = Join-Path $root 'main.dart.js'
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
    if (!(Test-Path -LiteralPath $file -PathType Leaf)) { throw 'The EntryFlow frontend path is absent on this runner.' }
    if ((Hash $file) -eq $patched) { Check-Origin $patched; return }
    if ((Hash $file) -ne $original) { throw 'The on-disk build differs from the original used to prepare this patch.' }
    Check-Origin $original
    Expand-Archive -LiteralPath (Join-Path $PSScriptRoot 'patch.zip') -DestinationPath $work
    $bundle = Join-Path $work 'main.dart.js'
    if ((Hash $bundle) -ne $patched) { throw 'Patch checksum mismatch.' }

    $backup = 'C:\EntryFlow\Frontend-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8)
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
