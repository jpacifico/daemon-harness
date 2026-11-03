[CmdletBinding()]
param([switch]$Plain, [switch]$NoAnimation, [switch]$Logo)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$entry = Join-Path $PSScriptRoot "DaemonHarnessCompanion.ps1"
if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
    throw "The DAEMONS package is incomplete. Keep its files together."
}
& $entry -Terminal -Plain:$Plain -NoAnimation:$NoAnimation -Logo:$Logo
exit $LASTEXITCODE
