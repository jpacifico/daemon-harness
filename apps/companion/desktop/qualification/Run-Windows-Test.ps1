# Disposable native attachment qualification. No administrator rights needed.
$ErrorActionPreference = 'Stop'
$exe = Join-Path $PSScriptRoot 'app\Daemons Companion.exe'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'Extract the entire test ZIP before running this script.' }
$reportDirectory = Join-Path $PSScriptRoot ('results-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $reportDirectory | Out-Null
$stdout = Join-Path $reportDirectory 'launcher.stdout.log'
$stderr = Join-Path $reportDirectory 'launcher.stderr.log'
Write-Host 'Starting an isolated test. Private Python and OpenClaw downloads can take several minutes.'
Write-Host 'No account, API key or Telegram bot is required. Keep this window and the test app open.'
Write-Host "Reports: $reportDirectory"
$process = Start-Process -FilePath $exe -ArgumentList @('--smoke-test','--attachment-native-smoke') -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
while (-not $process.WaitForExit(15000)) { Write-Host 'Test is still running. Downloading, starting services or verifying memory...' }
$process.Refresh()
$fixtureRoot = $null
foreach ($line in Get-Content -LiteralPath $stdout) {
    try { $entry = $line | ConvertFrom-Json; if ($entry.phase -and $entry.root) { $fixtureRoot = [string]$entry.root } } catch { }
}
$passed = $false
if ($fixtureRoot -and (Split-Path $fixtureRoot -Leaf) -like 'daemons-companion-ui-test-*') {
    foreach ($name in @('packaged-result.json','fixture-result.json','fixture.log','worker.log')) {
        $source = Join-Path $fixtureRoot $name
        if (Test-Path -LiteralPath $source -PathType Leaf) { Copy-Item -LiteralPath $source -Destination $reportDirectory }
    }
    $result = Join-Path $reportDirectory 'packaged-result.json'
    if (Test-Path -LiteralPath $result) {
        $data = Get-Content -LiteralPath $result -Raw | ConvertFrom-Json
        $passed = ($process.ExitCode -eq 0 -and $data.result -eq 'passed' -and $data.cleanup.target_jobs_removed -and $data.cleanup.ports_closed -and $data.cleanup.worker_stopped -and $data.cleanup.rollback_verified)
    }
}
[ordered]@{schema='daemon.windows-attachment-launcher.v1';passed=$passed;exit_code=$process.ExitCode;fixture_root=$fixtureRoot;report_directory=$reportDirectory} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $reportDirectory 'summary.json') -Encoding UTF8
if ($passed) { Write-Host 'PASS: activation, shared memory read, restart and rollback succeeded.' -ForegroundColor Green }
else { Write-Host 'NOT VALIDATED. Keep the test directory and reports for diagnosis. Do not remove it if cleanup is incomplete.' -ForegroundColor Yellow }
Write-Host 'Share summary.json, packaged-result.json and fixture-result.json first. These contain only this synthetic test.'
Invoke-Item -LiteralPath $reportDirectory
if (-not $passed) { exit 1 }
