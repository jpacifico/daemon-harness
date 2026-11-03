[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Uv,
    [Parameter(Mandatory = $true)][ValidatePattern("^[0-9a-f]{64}$")][string]$UvSha256,
    [Parameter(Mandatory = $true)][string]$Output,
    [string]$BuildPython = $env:DAEMON_HARNESS_BUILD_PYTHON
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
if ([string]::IsNullOrWhiteSpace($BuildPython)) { $BuildPython = Join-Path $repo ".venv\Scripts\python.exe" }
if (-not (Test-Path -LiteralPath $Uv -PathType Leaf)) { throw "A uv executable is required." }
$uvItem = Get-Item -LiteralPath $Uv -Force
if ($uvItem.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "A direct uv executable is required." }
if (-not (Test-Path -LiteralPath $BuildPython -PathType Leaf)) { throw "The build Python is unavailable." }
if (Test-Path -LiteralPath $Output) { throw "Output already exists: $Output" }
if ((Get-FileHash -LiteralPath $Uv -Algorithm SHA256).Hash.ToLowerInvariant() -ne $UvSha256) { throw "uv SHA-256 mismatch." }
if (-not ((& $Uv --version) -like "uv 0.12.16 *")) { throw "Expected uv 0.12.16." }
if (-not [string]::IsNullOrWhiteSpace((& git -C $repo status --porcelain))) { throw "A clean committed candidate is required." }

$stage = Join-Path ([IO.Path]::GetTempPath()) ("daemon-companion-windows-" + [guid]::NewGuid().ToString("N"))
try {
    $payload = Join-Path $stage "payload"
    $bin = Join-Path $stage "bin"
    New-Item -ItemType Directory -Path $payload, $bin | Out-Null
    $archive = Join-Path $stage "payload.zip"
    & git -C $repo archive --format=zip -o $archive HEAD
    if ($LASTEXITCODE -ne 0) { throw "Could not archive the tracked payload." }
    Expand-Archive -LiteralPath $archive -DestinationPath $payload
    Remove-Item -LiteralPath $archive
    Copy-Item -LiteralPath $Uv -Destination (Join-Path $bin "uv.exe")
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "DaemonHarnessCompanion.ps1") -Destination (Join-Path $stage "DaemonHarnessCompanion.ps1")
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "Start-Daemons.ps1") -Destination (Join-Path $stage "Start-Daemons.ps1")
    $env:PYTHONPATH = Join-Path $repo "src"
    & $BuildPython -m daemons_companion.desktop.package create $stage | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not create the package manifest." }
    $outputParent = Split-Path -Parent $Output
    if (-not [string]::IsNullOrWhiteSpace($outputParent)) { New-Item -ItemType Directory -Path $outputParent -Force | Out-Null }
    Move-Item -LiteralPath $stage -Destination $Output
    $stage = $null
    Write-Output $Output
} finally {
    if ($null -ne $stage -and (Test-Path -LiteralPath $stage)) { Remove-Item -LiteralPath $stage -Recurse -Force }
}
