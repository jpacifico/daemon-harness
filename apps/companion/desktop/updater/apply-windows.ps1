# Runs hidden once Daemons Companion has quit: the verified release's own
# install script replaces the app, then the app reopens, new or restored.
param(
    [Parameter(Mandatory)][int]$ProcessId,
    [Parameter(Mandatory)][string]$Installer,
    [Parameter(Mandatory)][string]$Package,
    [Parameter(Mandatory)][string]$Destination,
    [Parameter(Mandatory)][string]$Log,
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
# Electron puts its children in a job that is killed when the app quits, and a
# detached PowerShell never runs. The app starts this script attached; it hands
# the work to a copy of itself, which leaves the job because it allows silent
# breakaway, and returns so the app can quit.
if (-not $Apply) {
    try {
        $arguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"",
            '-ProcessId', $ProcessId, '-Installer', "`"$Installer`"", '-Package', "`"$Package`"",
            '-Destination', "`"$Destination`"", '-Log', "`"$Log`"", '-Apply')
        $helper = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $arguments -WindowStyle Hidden -PassThru
        "helper: started $($helper.Id)" | Set-Content -LiteralPath $Log
        exit 0
    } catch {
        "helper: $($_.Exception.Message)" | Set-Content -LiteralPath $Log
        exit 1
    }
}
$relaunch = $false
$outcome = $null
try {
    $app = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    # The app did not quit: leave it untouched.
    if ($app -and -not $app.WaitForExit(60000)) { 'helper: the app did not quit' | Add-Content -LiteralPath $Log; exit 1 }
    $relaunch = $true
    # Electron's helper processes share the app executable; let them end too.
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline -and @(Get-Process -Name 'Daemons Companion' -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -and $_.Path.StartsWith($Destination + '\', [StringComparison]::OrdinalIgnoreCase) }).Count) {
        Start-Sleep -Milliseconds 250
    }
    # Start-Process writes the install output to files. Redirecting a native
    # command inside this script would turn its first stderr line (Windows
    # PowerShell writes progress there) into a terminating error.
    $arguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', "`"$Installer`"",
        '-Package', "`"$Package`"", '-Destination', "`"$Destination`"", '-NoLaunch')
    $install = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $arguments -WindowStyle Hidden `
        -Wait -PassThru -RedirectStandardOutput "$Log.out" -RedirectStandardError "$Log.err"
    $outcome = "exit=$($install.ExitCode)"
} catch {
    $outcome = "helper: $($_.Exception.Message)"
} finally {
    foreach ($output in "$Log.out", "$Log.err") {
        if (Test-Path -LiteralPath $output) {
            Get-Content -LiteralPath $output | Where-Object { $_ -notmatch '^#< CLIXML|<Objs ' } | Add-Content -LiteralPath $Log
            Remove-Item -LiteralPath $output -Force -ErrorAction SilentlyContinue
        }
    }
    if ($outcome) { $outcome | Add-Content -LiteralPath $Log }
    Remove-Item -LiteralPath $Package -Force -ErrorAction SilentlyContinue
    if ($relaunch) { Start-Process -FilePath (Join-Path $Destination 'Daemons Companion.exe') }
}
